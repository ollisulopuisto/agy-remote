"""Question dock: structured ask_question gates on the phone.

agy's `ask_question` tool puts a real question in front of the operator --
options, sometimes multi-select -- but the hook hands the server the
questions as a JSON *string* under `args.questions`, and the phone was
drawing a permission banner that could not even read the question text
(it looked for `args.question`; the real key is `questions`). The server
now normalizes the questions once, the way it decodes every other
agy JSON-string argument, and the answer travels back through the hook's
existing `reason` channel -- the selected option text, exactly what agy's
own TUI would deliver.
"""

import asyncio
import json
from pathlib import Path

import pytest

from agy_remote.config import RemoteConfig
from agy_remote.models import ApprovalResponseRequest
from agy_remote.session_manager import SessionManager

REALISTIC_PAYLOAD = json.dumps(
    [
        {
            "question": "How would you like to proceed with publishing and benchmarks?",
            "options": [
                (
                    "(Recommended) Prepare release & publishing (validate CalVer/CHANGELOG, "
                    "run full workspace typecheck/lint/build, check GitHub Actions publish workflow)"
                ),
                "Run the 6-task canonical smoke benchmark suite (`opencode harness bench --smoke`)",
                "Run the complete 60-task benchmark matrix (`opencode harness bench`)",
                "Dry-run the CLI binary build (`./packages/opencode/script/build.ts --single`)",
            ],
            "is_multi_select": False,
        },
        {
            "question": "Which checks should the release run?",
            "options": ["full typecheck", "lint", "build"],
            "is_multi_select": True,
        },
    ]
)


def _mgr(tmp_path: Path) -> SessionManager:
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token")
    mgr = SessionManager(cfg)
    mgr.active_conversation_id = "conv-q"
    mgr._connected_clients.add(_FakeWebSocket())
    return mgr


class _FakeWebSocket:
    """A client the approval broadcast can be sent into, like the
    session-manager tests' stand-in."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000) -> None:
        pass


@pytest.mark.asyncio
async def test_ask_question_approval_carries_normalized_questions(tmp_path: Path):
    mgr = _mgr(tmp_path)

    await mgr.register_approval(
        approval_id="app-q",
        conversation_id="conv-q",
        tool_name="ask_question",
        args={"questions": REALISTIC_PAYLOAD, "toolAction": '"Asking user for action preference"'},
    )

    (pending,) = mgr.get_active_pending_approvals()
    questions = pending["questions"]
    assert [q["question"] for q in questions] == [
        "How would you like to proceed with publishing and benchmarks?",
        "Which checks should the release run?",
    ]
    assert questions[0]["options"][0].startswith("(Recommended)")
    assert questions[0]["multi_select"] is False
    assert questions[1]["multi_select"] is True
    assert questions[1]["options"] == ["full typecheck", "lint", "build"]


@pytest.mark.asyncio
async def test_a_single_question_object_without_the_array_wrapper(tmp_path: Path):
    """agy may hand one question object rather than a one-element array;
    the dock must still have something to draw."""
    single = json.dumps({"question": "Ship today?", "options": ["Yes", "No"]})
    mgr = _mgr(tmp_path)

    await mgr.register_approval("app-1", "conv-q", "ask_question", args={"questions": single})

    (pending,) = mgr.get_active_pending_approvals()
    (only,) = pending["questions"]
    assert only["question"] == "Ship today?"
    assert only["options"] == ["Yes", "No"]
    assert only["multi_select"] is False


@pytest.mark.asyncio
async def test_unparseable_questions_fall_back_to_a_plain_gate(tmp_path: Path):
    """A dock with nothing to draw is worse than the plain banner, so a
    payload that does not decode to question-shaped data carries no
    `questions` at all -- the client then falls back on its own."""
    mgr = _mgr(tmp_path)
    for bad in ("not json", "42", json.dumps({"nope": 1}), json.dumps([{"options": []}]), None):
        mgr._pending_approvals.clear()
        await mgr.register_approval("app-x", "conv-q", "ask_question", args={"questions": bad})
        (pending,) = mgr.get_active_pending_approvals()
        assert "questions" not in pending, bad


@pytest.mark.asyncio
async def test_a_non_question_tool_carries_no_questions(tmp_path: Path):
    mgr = _mgr(tmp_path)

    await mgr.register_approval("app-r", "conv-q", "run_command", args={"CommandLine": "ls"})

    (pending,) = mgr.get_active_pending_approvals()
    assert "questions" not in pending


@pytest.mark.asyncio
async def test_a_question_answer_travels_back_as_the_reason(tmp_path: Path):
    """The hook's answer is {decision, reason}; the selected option text is
    the answer agy receives, the way agy's own TUI would deliver it."""
    mgr = _mgr(tmp_path)
    task = asyncio.create_task(
        mgr.request_approval("app-q", "conv-q", "ask_question", args={"questions": REALISTIC_PAYLOAD})
    )
    while not mgr.get_active_pending_approvals():
        await asyncio.sleep(0.01)

    answer = (
        "(Recommended) Prepare release & publishing (validate CalVer/CHANGELOG, "
        "run full workspace typecheck/lint/build, check GitHub Actions publish workflow)"
    )
    await mgr.resolve_approval("app-q", ApprovalResponseRequest(decision="allow", reason=answer))

    result = await task
    assert result["decision"] == "allow"
    assert result["reason"] == answer
