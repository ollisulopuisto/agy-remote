"""Tests for the agent-to-agent mailbox and its `agy-remote msg` CLI."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from agy_remote.cli import msg_command
from agy_remote.mailbox import MAX_MESSAGE_BYTES, MailboxError, mailbox_dir, send_message, validate_target


@pytest.fixture
def mailbox(tmp_path: Path, monkeypatch):
    """Point the mailbox at a throwaway directory and clear sender identity."""
    monkeypatch.setenv("AGY_REMOTE_MAILBOX_DIR", str(tmp_path / "mailbox"))
    monkeypatch.delenv("AGY_REMOTE_SESSION_ID", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return tmp_path / "mailbox"


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _run_cli(target: str, *args: str, env: dict[str, str] | None = None, input: str | None = None):
    """Invoke `agy-remote msg` the way an agent's bash call would."""
    return CliRunner().invoke(msg_command, [target, *args], env=env, input=input)


# ---------------------------------------------------------------------------
# Target validation: a mailbox target becomes a filename.
# ---------------------------------------------------------------------------


def test_a_valid_target_passes():
    assert validate_target("agy-remote-myrepo") == "agy-remote-myrepo"
    assert validate_target("3") == "3"


@pytest.mark.parametrize(
    "target",
    ["", "../etc/passwd", "../../etc/passwd", "a/b", "a b", "name.dot", "----", ".", "..", "x" * 129],
)
def test_traversal_and_nonsense_targets_are_rejected(target):
    with pytest.raises(MailboxError):
        validate_target(target)


# ---------------------------------------------------------------------------
# Message size and shape.
# ---------------------------------------------------------------------------


def test_oversized_message_is_rejected(mailbox):
    with pytest.raises(MailboxError, match="bytes"):
        send_message("agy-work", "x" * (MAX_MESSAGE_BYTES + 1))
    assert not (mailbox / "agy-work.jsonl").exists()


def test_empty_message_is_rejected(mailbox):
    with pytest.raises(MailboxError, match="empty"):
        send_message("agy-work", "   ")


def test_nul_bytes_are_rejected(mailbox):
    with pytest.raises(MailboxError, match="NUL"):
        send_message("agy-work", "a\x00b")


def test_the_cap_counts_bytes_not_characters(mailbox):
    # Three-byte characters: 1365 of them is 4095 bytes, the last valid size.
    send_message("agy-work", "€" * 1365)
    with pytest.raises(MailboxError):
        send_message("agy-work", "€" * 1366)


# ---------------------------------------------------------------------------
# The append.
# ---------------------------------------------------------------------------


def test_a_message_lands_as_one_valid_json_line(mailbox, monkeypatch):
    monkeypatch.setenv("AGY_REMOTE_SESSION_ID", "agy-remote-a")
    inbox = send_message("agy-remote-b", "Unit tests failed, see log")

    lines = _lines(inbox)
    assert len(lines) == 1
    msg = lines[0]
    assert msg["to"] == "agy-remote-b"
    assert msg["from"] == "agy-remote-a"
    assert msg["text"] == "Unit tests failed, see log"
    assert msg["id"]
    assert msg["ts"]
    # The inbox is private: it holds what other agents write about the session.
    assert inbox.stat().st_mode & 0o777 == 0o600


def test_the_mailbox_directory_is_private(mailbox):
    send_message("agy-work", "hi")
    assert mailbox.stat().st_mode & 0o777 == 0o700


def test_messages_accumulate_in_order(mailbox):
    send_message("agy-work", "first")
    send_message("agy-work", "second")
    assert [m["text"] for m in _lines(mailbox / "agy-work.jsonl")] == ["first", "second"]


def test_the_sender_falls_back_to_the_pane_then_unknown(mailbox, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%17")
    send_message("agy-work", "from the pane")
    assert _lines(mailbox / "agy-work.jsonl")[0]["from"] == "%17"

    monkeypatch.delenv("TMUX_PANE")
    (mailbox / "agy-work.jsonl").unlink()
    send_message("agy-work", "from nowhere")
    assert _lines(mailbox / "agy-work.jsonl")[0]["from"] == "unknown"


# ---------------------------------------------------------------------------
# The CLI: what the agent actually types.
# ---------------------------------------------------------------------------


def test_cli_appends_a_valid_message(mailbox):
    result = _run_cli("session-2", "Unit tests failed, see log", env={"AGY_REMOTE_SESSION_ID": "session-1"})
    assert result.exit_code == 0
    msg = _lines(mailbox / "session-2.jsonl")[0]
    assert msg["text"] == "Unit tests failed, see log"
    assert msg["from"] == "session-1"


def test_cli_reads_the_message_from_stdin_when_piped(mailbox):
    result = _run_cli("session-2", env={"AGY_REMOTE_SESSION_ID": "s1"}, input="piped message\n")
    assert result.exit_code == 0
    assert _lines(mailbox / "session-2.jsonl")[0]["text"] == "piped message"


def test_cli_rejects_oversized_messages_with_exit_code_one(mailbox):
    result = _run_cli("session-2", "x" * (MAX_MESSAGE_BYTES + 1))
    assert result.exit_code == 1
    assert "cap" in result.output
    assert not (mailbox / "session-2.jsonl").exists()


def test_cli_rejects_traversal_targets_with_exit_code_one(mailbox):
    result = _run_cli("../../etc/passwd", "hello")
    assert result.exit_code == 1
    assert "invalid target" in result.output


def test_cli_rejects_empty_messages_with_exit_code_one(mailbox):
    assert _run_cli("session-2").exit_code == 1
    assert _run_cli("session-2", input="   \n").exit_code == 1
    assert not (mailbox / "session-2.jsonl").exists()


def test_without_server_identity_the_pane_id_signs_the_message(mailbox: Path, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%14")
    assert _run_cli("session-2", "hi").exit_code == 0
    assert _lines(mailbox / "session-2.jsonl")[0]["from"] == "%14"


def test_without_any_identity_the_sender_is_unknown(mailbox: Path):
    assert _run_cli("session-2", "hi").exit_code == 0
    assert _lines(mailbox / "session-2.jsonl")[0]["from"] == "unknown"


def test_the_mailbox_directory_is_created_private(mailbox: Path):
    assert _run_cli("session-2", "hi").exit_code == 0
    assert mailbox.stat().st_mode & 0o777 == 0o700


def test_the_env_override_points_send_and_cli_at_the_same_place(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGY_REMOTE_MAILBOX_DIR", str(tmp_path / "elsewhere" / "deep"))
    inbox = send_message("agy-work", "hi")
    assert inbox == tmp_path / "elsewhere" / "deep" / "agy-work.jsonl"
    assert mailbox_dir() == tmp_path / "elsewhere" / "deep"
    assert _run_cli("agy-work", "again").exit_code == 0
    assert len(_lines(tmp_path / "elsewhere" / "deep" / "agy-work.jsonl")) == 2
