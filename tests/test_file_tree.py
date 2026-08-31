"""File tree: the session's project directory, browsable from the phone.

opencode's PWA ships a file-tree side panel; agy-remote could only open a
file the transcript had named. This browses the registered session's workdir
-- the same sanctioned roots `/api/file` reads from -- so the operator can
see what the agent actually produced, not only what it mentioned.
"""

from pathlib import Path

from agy_remote.config import RemoteConfig
from agy_remote.models import SessionRecord
from agy_remote.session_manager import SessionManager


def _mgr_with_session(tmp_path: Path, workdir: Path) -> SessionManager:
    cfg = RemoteConfig(brain_dir=tmp_path / "brain", auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    mgr.register_session(SessionRecord(id="s1", tmux_name="t", conversation_id="conv-1", workdir=str(workdir)))
    mgr.active_conversation_id = "conv-1"
    return mgr


def _project(tmp_path: Path) -> Path:
    workdir = tmp_path / "proj"
    (workdir / "src").mkdir(parents=True)
    (workdir / "README.md").write_text("readme\n")
    (workdir / "src" / "main.py").write_text("print('hi')\n")
    (workdir / "src" / "util.py").write_text("x = 1\n")
    (workdir / "zeta.txt").write_text("z\n")
    return workdir


# ---------------------------------------------------------------------------
# The manager surface
# ---------------------------------------------------------------------------


def test_listing_defaults_to_the_session_workdir(tmp_path: Path):
    workdir = _project(tmp_path)
    mgr = _mgr_with_session(tmp_path, workdir)

    result = mgr.list_host_dir(None)

    assert result["path"] == str(workdir)
    assert result["name"] == "proj"
    names = [e["name"] for e in result["entries"]]
    assert set(names) == {"src", "README.md", "zeta.txt"}


def test_entries_are_typed_and_sorted_dirs_first(tmp_path: Path):
    workdir = _project(tmp_path)
    mgr = _mgr_with_session(tmp_path, workdir)

    entries = mgr.list_host_dir(None)["entries"]

    # Directories first; within each group, case-insensitive name order.
    assert [e["name"] for e in entries] == ["src", "README.md", "zeta.txt"]
    assert entries[0]["type"] == "dir"
    assert all(e["type"] == "file" for e in entries[1:])
    readme = entries[1]
    assert readme["size"] == len("readme\n")


def test_descending_into_a_subdirectory_resolves_under_the_root(tmp_path: Path):
    workdir = _project(tmp_path)
    mgr = _mgr_with_session(tmp_path, workdir)

    result = mgr.list_host_dir(str(workdir / "src"))

    assert result["name"] == "src"
    assert [e["name"] for e in result["entries"]] == ["main.py", "util.py"]
    main = result["entries"][0]
    assert main["path"] == str(workdir / "src" / "main.py")


def test_a_path_outside_the_roots_is_refused(tmp_path: Path):
    workdir = _project(tmp_path)
    mgr = _mgr_with_session(tmp_path, workdir)

    import pytest

    with pytest.raises(PermissionError):
        mgr.list_host_dir(str(tmp_path / "brain"))


def test_missing_and_non_directory_paths_are_refused(tmp_path: Path):
    workdir = _project(tmp_path)
    mgr = _mgr_with_session(tmp_path, workdir)

    import pytest

    with pytest.raises(FileNotFoundError):
        mgr.list_host_dir(str(workdir / "nope"))
    with pytest.raises(ValueError):
        mgr.list_host_dir(str(workdir / "README.md"))


def test_a_session_without_a_workdir_has_no_root(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path / "brain", auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)

    import pytest

    with pytest.raises(LookupError):
        mgr.list_host_dir(None)


# ---------------------------------------------------------------------------
# The REST surface
# ---------------------------------------------------------------------------


def test_the_files_endpoint_serves_and_refuses(tmp_path: Path):
    from fastapi.testclient import TestClient

    from agy_remote.server import create_app

    workdir = _project(tmp_path)

    cfg = RemoteConfig(brain_dir=tmp_path / "brain", auth_token="secret123", enable_auth=True)
    app = create_app(cfg)
    client = TestClient(app)
    app.state.session_manager.register_session(
        SessionRecord(id="s1", tmux_name="t", conversation_id="conv-1", workdir=str(workdir))
    )

    # Auth is not optional, like every other data endpoint.
    assert client.get("/api/files?conversation_id=conv-1").status_code == 401

    # No path: the session's workdir root.
    resp = client.get("/api/files?conversation_id=conv-1&token=secret123")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == str(workdir)
    assert {e["name"] for e in body["entries"]} == {"src", "README.md", "zeta.txt"}

    # Descending works and carries the absolute path of each entry.
    sub = client.get(f"/api/files?path={workdir / 'src'}&conversation_id=conv-1&token=secret123")
    assert sub.status_code == 200
    assert sub.json()["entries"][0]["path"] == str(workdir / "src" / "main.py")

    # Traversal and unknown sessions are refused, not served.
    assert client.get("/api/files?path=/etc&conversation_id=conv-1&token=secret123").status_code == 403
    assert client.get("/api/files?conversation_id=nope&token=secret123").status_code == 404
