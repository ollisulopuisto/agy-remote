"""Working-tree diff pane: the session's git state on the phone.

opencode's PWA ships a working-tree diff viewer (unstaged + staged, per-file
stats); agy-remote rendered only per-tool-call diffs from the transcript, so
there was no way to review what the agent's edits add up to mid-session.
"""

import subprocess
from pathlib import Path

import pytest

from agy_remote.config import RemoteConfig
from agy_remote.gitdiff import split_git_diff
from agy_remote.models import SessionRecord
from agy_remote.session_manager import SessionManager


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout


def _repo(tmp_path: Path) -> Path:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    _git(workdir, "init", "-q")
    _git(workdir, "config", "user.email", "test@example.com")
    _git(workdir, "config", "user.name", "Test")
    (workdir / "hello.txt").write_text("line one\nline two\n")
    _git(workdir, "add", ".")
    _git(workdir, "commit", "-q", "-m", "init")
    return workdir


def _mgr_with_session(tmp_path: Path, workdir: Path) -> SessionManager:
    cfg = RemoteConfig(brain_dir=tmp_path / "brain", auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    mgr.register_session(SessionRecord(id="s1", tmux_name="t", conversation_id="conv-1", workdir=str(workdir)))
    mgr.active_conversation_id = "conv-1"
    return mgr


# ---------------------------------------------------------------------------
# The pure splitter
# ---------------------------------------------------------------------------


def test_split_git_diff_parses_per_file_chunks():
    text = (
        "diff --git a/a.txt b/a.txt\n"
        "index 111..222 100644\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/added.txt b/added.txt\n"
        "new file mode 100644\n"
        "+++ b/added.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+alpha\n"
        "+beta\n"
    )
    files = split_git_diff(text)
    assert [f["path"] for f in files] == ["a.txt", "added.txt"]
    assert files[0]["additions"] == 1 and files[0]["deletions"] == 1
    assert files[1]["additions"] == 2 and files[1]["deletions"] == 0
    assert files[0]["diff"].startswith("diff --git a/a.txt")
    assert "-old\n+new" in files[0]["diff"]


def test_split_git_diff_of_empty_text_is_empty():
    assert split_git_diff("") == []


def test_split_git_diff_counts_context_and_headers_as_neither():
    text = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -1,3 +1,3 @@\n ctx\n-gone\n+here\n"
    (only,) = split_git_diff(text)
    assert only["additions"] == 1 and only["deletions"] == 1


# ---------------------------------------------------------------------------
# The manager surface
# ---------------------------------------------------------------------------


def test_a_clean_tree_reports_clean(tmp_path: Path):
    workdir = _repo(tmp_path)
    mgr = _mgr_with_session(tmp_path, workdir)

    result = mgr.working_tree_diff("conv-1")

    assert result["clean"] is True
    assert result["files"] == []
    assert result["workdir"] == str(workdir)


def test_modified_and_new_files_are_reported_with_stats(tmp_path: Path):
    workdir = _repo(tmp_path)
    (workdir / "hello.txt").write_text("line one\nline two changed\n")
    (workdir / "extra.txt").write_text("fresh\n")
    mgr = _mgr_with_session(tmp_path, workdir)

    result = mgr.working_tree_diff("conv-1")

    assert result["clean"] is False
    paths = {f["path"] for f in result["files"]}
    assert paths == {"hello.txt", "extra.txt"}
    hello = next(f for f in result["files"] if f["path"] == "hello.txt")
    assert hello["additions"] == 1 and hello["deletions"] == 1
    assert "+line two changed" in hello["diff"]


def test_staged_changes_are_included(tmp_path: Path):
    """opencode's viewer shows staged work too; `git diff HEAD` covers both."""
    workdir = _repo(tmp_path)
    (workdir / "hello.txt").write_text("staged work\n")
    _git(workdir, "add", ".")
    mgr = _mgr_with_session(tmp_path, workdir)

    result = mgr.working_tree_diff("conv-1")

    assert result["clean"] is False
    assert any(f["path"] == "hello.txt" for f in result["files"])


def test_a_session_without_a_workdir_is_a_404_shape(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path / "brain", auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    with pytest.raises(LookupError):
        mgr.working_tree_diff("conv-1")


def test_a_non_git_directory_is_refused(tmp_path: Path):
    workdir = tmp_path / "plain"
    workdir.mkdir()
    mgr = _mgr_with_session(tmp_path, workdir)

    with pytest.raises(ValueError, match="not a git repository"):
        mgr.working_tree_diff("conv-1")


# ---------------------------------------------------------------------------
# The REST surface
# ---------------------------------------------------------------------------


def test_the_git_diff_endpoint_serves_the_session_tree(tmp_path: Path):
    from fastapi.testclient import TestClient

    from agy_remote.server import create_app

    workdir = _repo(tmp_path)
    (workdir / "hello.txt").write_text("line one\nline two changed\n")

    cfg = RemoteConfig(brain_dir=tmp_path / "brain", auth_token="secret123", enable_auth=True)
    app = create_app(cfg)
    client = TestClient(app)
    app.state.session_manager.register_session(
        SessionRecord(id="s1", tmux_name="t", conversation_id="conv-1", workdir=str(workdir))
    )

    resp = client.get("/api/git-diff?conversation_id=conv-1&token=secret123")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["clean"] is False
    assert {f["path"] for f in body["files"]} == {"hello.txt"}

    # Auth is not optional, like every other data endpoint.
    assert client.get("/api/git-diff?conversation_id=conv-1").status_code == 401
