"""Loop protection through the delivery path (W2, item 2.3).

The guard itself is tested in `test_loop_guard.py`; these tests pin the
contract between the guard and the watch loop: what `poll_inboxes` holds, what
it delivers, what it reports, and how a human prompt or a mute changes the
fate of the mail already in the inbox.
"""

import json
import uuid
from pathlib import Path

import pytest

from agy_remote.config import RemoteConfig
from agy_remote.models import SessionRecord
from agy_remote.session_manager import SessionManager

TARGET = "agy-work"
SENDER = "agy-fe"


class _FakeSupervisor:
    def __init__(self) -> None:
        self.injected: list[str] = []

    def inject_input(self, text: str) -> bool:
        self.injected.append(text)
        return True


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def maildir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "mailbox"
    monkeypatch.setenv("AGY_REMOTE_MAILBOX_DIR", str(d))
    return d


def _line(text: str, sender: str = SENDER) -> str:
    return json.dumps(
        {
            "id": uuid.uuid4().hex,
            "from": sender,
            "to": TARGET,
            "text": text,
            "ts": "2026-08-30T00:00:00+00:00",
        }
    )


def _manager(maildir: Path, supervisor=_FakeSupervisor(), **cfg_overrides) -> SessionManager:
    cfg = RemoteConfig(brain_dir=maildir.parent / "brain", auth_token="token", **cfg_overrides)
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


# -- the ping-pong latch, through delivery -------------------------------------


def test_a_pair_that_runs_the_limit_is_paused_and_reported(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup, mailbox_ping_pong_limit=3)
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()  # first sight: learn the offset

    for i in range(2):  # under the limit: delivered, no report
        _append(inbox, _line(f"exchange {i}"))
        looped, delivered = mgr.poll_inboxes()
        assert looped == []
        assert delivered == 1

    _append(inbox, _line("the reply that completes the pattern"))
    looped, delivered = mgr.poll_inboxes()
    assert delivered == 1  # the completing message still lands
    assert looped == [(SENDER, TARGET)]  # ...and the pair is reported

    _append(inbox, _line("another reply"))
    looped, delivered = mgr.poll_inboxes()
    assert delivered == 0  # paused: held, not dropped
    assert looped == []  # reported exactly once, not every poll


def test_the_held_mail_is_delivered_when_a_human_breaks_the_loop(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup, mailbox_ping_pong_limit=2)
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()

    _append(inbox, _line("one"), _line("two"))  # "two" trips the latch
    mgr.poll_inboxes()
    _append(inbox, _line("three"))  # held
    assert mgr.poll_inboxes() == ([], 0)
    assert sup.injected == [
        "[message from agy-fe: one]",
        "[message from agy-fe: two]",
    ]

    mgr.note_human_prompt(TARGET)  # the human types into the target
    looped, delivered = mgr.poll_inboxes()
    assert delivered == 1
    assert sup.injected == [
        "[message from agy-fe: one]",
        "[message from agy-fe: two]",
        "[message from agy-fe: three]",
    ]


def test_a_human_prompt_by_conversation_id_resets_the_pair(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup, mailbox_ping_pong_limit=2)
    record = mgr.get_session(TARGET)
    record.conversation_id = "conv-123"
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()
    _append(inbox, _line("one"), _line("two"))
    mgr.poll_inboxes()  # latched
    _append(inbox, _line("three"))
    assert mgr.poll_inboxes() == ([], 0)

    mgr.note_human_prompt("conv-123")  # prompts carry the conversation id
    assert mgr.poll_inboxes() == ([], 1)
    assert sup.injected[-1] == "[message from agy-fe: three]"


def test_a_loop_in_one_pair_does_not_pause_another_pair(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup, mailbox_ping_pong_limit=2)
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()
    _append(inbox, _line("one"), _line("two"))  # SENDER<->TARGET latched
    mgr.poll_inboxes()
    _append(inbox, _line("hello", sender="agy-be"))  # a different pair
    looped, delivered = mgr.poll_inboxes()
    assert delivered == 1
    assert looped == []
    assert sup.injected[-1] == "[message from agy-be: hello]"


# -- the rate limit, through delivery -------------------------------------------


def test_a_burst_beyond_the_rate_window_is_held_until_it_slides(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup, mailbox_rate_limit=2, mailbox_rate_window_seconds=100.0)
    clock = _Clock()
    mgr.loop_guard._now = clock  # the guard's clock, swapped for determinism
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()

    _append(inbox, _line("one"), _line("two"), _line("three"))
    looped, delivered = mgr.poll_inboxes()
    assert delivered == 2  # the window holds two
    assert looped == []
    assert sup.injected[-1] == "[message from agy-fe: two]"

    # held, and still held a moment later: the window has not slid
    clock.advance(10.0)
    assert mgr.poll_inboxes() == ([], 0)

    clock.advance(95.0)  # the first delivery falls out of the window
    looped, delivered = mgr.poll_inboxes()
    assert delivered == 1
    assert sup.injected[-1] == "[message from agy-fe: three]"


# -- mute, through delivery -----------------------------------------------------


def test_a_muted_pair_holds_its_mail_until_unmuted(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()

    mgr.mute_mailbox_pair(SENDER, TARGET)
    _append(inbox, _line("quiet now"))
    assert mgr.poll_inboxes() == ([], 0)
    assert sup.injected == []

    mgr.unmute_mailbox_pair(SENDER, TARGET)
    assert mgr.poll_inboxes() == ([], 1)
    assert sup.injected == ["[message from agy-fe: quiet now]"]


def test_muting_does_not_unmute_a_human_reset_and_vice_versa(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup, mailbox_ping_pong_limit=2)
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()
    _append(inbox, _line("one"), _line("two"))  # latched
    mgr.poll_inboxes()
    _append(inbox, _line("three"))

    mgr.mute_mailbox_pair(SENDER, TARGET)
    mgr.note_human_prompt(TARGET)  # breaks the latch...
    assert mgr.poll_inboxes() == ([], 0)  # ...but the pair stays muted

    mgr.unmute_mailbox_pair(SENDER, TARGET)
    assert mgr.poll_inboxes() == ([], 1)  # both releases were needed


# -- reporting -------------------------------------------------------------------


def test_agent_traffic_reports_each_pair(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup, mailbox_ping_pong_limit=2)
    mgr.loop_guard._now = _Clock()
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()
    _append(inbox, _line("one"), _line("two"))
    mgr.poll_inboxes()  # latched, one delivered pair
    assert mgr.agent_traffic() == [
        {"a": SENDER, "b": TARGET, "count": 2, "looping": True, "muted": False, "last_at": 1000.0}
    ]

    mgr.mute_mailbox_pair("agy-be", TARGET)  # a dark pair is reported too
    traffic = {tuple(sorted((p["a"], p["b"]))): p for p in mgr.agent_traffic()}
    assert traffic[("agy-be", TARGET)]["muted"] is True
    assert traffic[("agy-be", TARGET)]["count"] == 0


def test_the_init_snapshot_carries_agent_traffic(maildir: Path):
    sup = _FakeSupervisor()
    mgr = _manager(maildir, sup)
    inbox = _inbox(maildir)
    _write(inbox, _line("old"))
    mgr.poll_inboxes()
    _append(inbox, _line("one"))
    mgr.poll_inboxes()

    # a client that connects mid-traffic must see the counters, not start blind
    snapshot = mgr._init_data()["data"]
    assert "agent_traffic" in snapshot
    assert snapshot["agent_traffic"][0]["count"] == 1
