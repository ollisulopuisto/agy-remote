"""Mailbox delivery: the receive side of agent-to-agent messaging (W2, item 2.1).

An agent's `agy-msg` appends one JSONL line to the target's inbox; the server's
watch loop tails each session's inbox and types new lines into that session
through the normal prompt path, wrapped in an envelope that names the sender.
"""

import json
import uuid
from pathlib import Path

import pytest

from agy_remote.config import RemoteConfig
from agy_remote.mailbox import format_envelope, parse_message_line
from agy_remote.models import SessionRecord
from agy_remote.session_manager import SessionManager

TARGET = "agy-work"


class _FakeSupervisor:
    def __init__(self) -> None:
        self.injected: list[str] = []

    def inject_input(self, text: str) -> bool:
        self.injected.append(text)
        return True


class _FlakySupervisor(_FakeSupervisor):
    """A target that refuses injections while down, takes them when up."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True

    def inject_input(self, text: str) -> bool:
        if self.down:
            return False
        return super().inject_input(text)


class _MidBatchDyingSupervisor(_FlakySupervisor):
    """A target that dies on its second envelope, and only then."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False
        self.calls = 0

    def inject_input(self, text: str) -> bool:
        self.calls += 1
        if self.calls == 2:
            self.down = True  # dies mid-batch; stays down until the test says so
        return super().inject_input(text)


@pytest.fixture
def maildir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "mailbox"
    monkeypatch.setenv("AGY_REMOTE_MAILBOX_DIR", str(d))
    return d


def _line(text: str, sender: str = "agy-fe") -> str:
    return json.dumps(
        {
            "id": uuid.uuid4().hex,
            "from": sender,
            "to": TARGET,
            "text": text,
            "ts": "2026-08-30T00:00:00+00:00",
        }
    )


def _manager(maildir: Path, supervisor=_FakeSupervisor()) -> SessionManager:
    cfg = RemoteConfig(brain_dir=maildir.parent / "brain", auth_token="token")
    mgr = SessionManager(cfg)
    record = SessionRecord(id=TARGET, tmux_name=TARGET, workdir="/p/work")
    mgr.register_session(record, supervisor=supervisor)
    return mgr


def _inbox(maildir: Path) -> Path:
    path = maildir / f"{TARGET}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write(inbox: Path, *lines: str) -> None:
    with inbox.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _append(inbox: Path, *lines: str) -> None:
    with inbox.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


# -- parse_message_line ------------------------------------------------------


def test_a_complete_line_is_a_message():
    msg = parse_message_line(_line("tests are red, see log"))
    assert msg is not None
    assert msg["text"] == "tests are red, see log"
    assert msg["from"] == "agy-fe"


def test_a_torn_or_stray_line_is_no_message():
    for bad in (
        "",
        "   ",
        "{not json",
        "42",
        "[]",
        json.dumps({"from": "a"}),
        json.dumps({"text": "   "}),
        json.dumps({"text": 5}),
    ):
        assert parse_message_line(bad) is None, bad


# -- format_envelope ---------------------------------------------------------


def test_the_envelope_names_the_sender_around_the_text():
    assert format_envelope({"text": "tests are red, see log"}, "agy-fe") == (
        "[message from agy-fe: tests are red, see log]"
    )


# -- delivery ----------------------------------------------------------------


def test_a_new_message_reaches_the_target_enveloped(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("old, written before the server looked"))
    mgr.poll_inboxes()  # first sight: learn where the inbox already stands
    assert sup.injected == []

    _append(inbox, _line("still red, now green?"))
    mgr.poll_inboxes()
    assert sup.injected == ["[message from agy-fe: still red, now green?]"]


def test_messages_written_before_the_server_looks_are_not_replayed(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("old message"), _line("older message"))
    mgr.poll_inboxes()
    mgr.poll_inboxes()
    assert sup.injected == []


def test_a_batch_of_messages_is_delivered_in_order(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("first"))
    mgr.poll_inboxes()

    _append(inbox, _line("second"), _line("third"))
    mgr.poll_inboxes()
    assert sup.injected == [
        "[message from agy-fe: second]",
        "[message from agy-fe: third]",
    ]


def test_a_write_in_flight_is_held_until_the_line_completes(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("settled"))
    mgr.poll_inboxes()

    with inbox.open("a", encoding="utf-8") as f:
        f.write(_line("half"))  # the sender's newline has not landed yet
    mgr.poll_inboxes()
    assert sup.injected == []

    with inbox.open("a", encoding="utf-8") as f:
        f.write("\n")
    mgr.poll_inboxes()
    assert sup.injected == ["[message from agy-fe: half]"]

    mgr.poll_inboxes()  # a completed line is delivered exactly once
    assert len(sup.injected) == 1


def test_a_bad_line_does_not_block_the_lines_after_it(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("first"))
    mgr.poll_inboxes()

    _append(inbox, "garbage that is not json", _line("after the tear"))
    mgr.poll_inboxes()
    assert sup.injected == ["[message from agy-fe: after the tear]"]


def test_a_shrunken_inbox_resyncs_rather_than_reading_garbage(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("one"), _line("two"))
    mgr.poll_inboxes()

    _write(inbox, _line("three"))  # truncated and rewritten, now shorter
    mgr.poll_inboxes()
    assert sup.injected == []

    _append(inbox, _line("four"))
    mgr.poll_inboxes()
    assert sup.injected == ["[message from agy-fe: four]"]


def test_a_target_with_no_supervisor_holds_its_mail_until_it_is_live(maildir: Path):
    mgr = _manager(maildir, None)  # registered, but nothing to type into yet
    inbox = _inbox(maildir)
    _write(inbox, _line("before you were up"))
    mgr.poll_inboxes()

    _append(inbox, _line("while you were gone"))
    mgr.poll_inboxes()

    sup = _FakeSupervisor()
    mgr.register_session(
        SessionRecord(id=TARGET, tmux_name=TARGET, workdir="/p/work"),
        supervisor=sup,
    )
    mgr.poll_inboxes()
    assert sup.injected == ["[message from agy-fe: while you were gone]"]


def test_the_envelope_names_a_known_sender_by_its_session_name(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    mgr.register_session(
        SessionRecord(id="fe-1", tmux_name="agy-remote-fe", workdir="/p/fe"),
        supervisor=_FakeSupervisor(),
    )
    inbox = _inbox(maildir)
    _write(inbox, _line("earlier", sender="fe-1"))
    mgr.poll_inboxes()

    _append(inbox, _line("still there", sender="fe-1"))
    mgr.poll_inboxes()
    assert sup.injected == ["[message from agy-remote-fe: still there]"]


def test_an_unknown_sender_is_named_by_its_id(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("hi"))
    mgr.poll_inboxes()

    _append(inbox, _line("from the dark", sender="pane-%12"))
    mgr.poll_inboxes()
    assert sup.injected == ["[message from pane-%12: from the dark]"]


def test_a_batch_is_held_when_the_target_refuses_and_delivered_when_it_recovers(maildir: Path):
    sup = _FlakySupervisor()  # down at first, recovers later
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()

    _append(inbox, _line("one"), _line("two"))
    mgr.poll_inboxes()
    assert sup.injected == []  # refused: held, not lost, not half-delivered

    sup.down = False
    mgr.poll_inboxes()
    assert sup.injected == ["[message from agy-fe: one]", "[message from agy-fe: two]"]

    mgr.poll_inboxes()  # delivered exactly once
    assert len(sup.injected) == 2


def test_a_target_dying_mid_batch_holds_the_rest_and_recovers(maildir: Path):
    sup = _MidBatchDyingSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()

    _append(inbox, _line("one"), _line("two"))
    mgr.poll_inboxes()
    # "one" landed, "two" was refused: nothing is dropped
    assert sup.injected == ["[message from agy-fe: one]"]

    mgr.poll_inboxes()  # the target is dead: nothing is lost, nothing repeated
    assert sup.injected == ["[message from agy-fe: one]"]

    sup.down = False  # the target is back
    mgr.poll_inboxes()
    # "two" lands; "one" is NOT re-typed -- the offset advanced past what
    # already landed, so a flaky target cannot make its colleague's words
    # (or its own, echoed back) arrive twice.
    assert sup.injected == [
        "[message from agy-fe: one]",
        "[message from agy-fe: two]",
    ]

    mgr.poll_inboxes()
    assert len(sup.injected) == 2
