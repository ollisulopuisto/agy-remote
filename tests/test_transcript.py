"""Unit tests for turning agy's raw transcript into something readable."""

from agy_remote.transcript import clean_user_content, normalize_tool_calls

# Exactly what agy writes for a prompt sent from the phone.
REAL_ENVELOPE = (
    "<USER_REQUEST>\n"
    "What's taking up space on this computer and what could we clean up?\n"
    "</USER_REQUEST>\n"
    "<ADDITIONAL_METADATA>\n"
    "The current local time is: 2026-08-21T17:16:16+03:00.\n"
    "</ADDITIONAL_METADATA>\n"
    "<USER_SETTINGS_CHANGE>\n"
    "The user changed setting `Model Selection` from None to Gemini 3.7 Flash (Medium). "
    "No need to comment on this change if the user doesn't ask about it.\n"
    "</USER_SETTINGS_CHANGE>"
)


def test_the_request_survives_and_the_plumbing_does_not():
    """The wrapper is agy's, not the user's, and it filled the whole phone screen."""
    assert clean_user_content(REAL_ENVELOPE) == "What's taking up space on this computer and what could we clean up?"


def test_metadata_without_a_request_block_is_still_stripped():
    content = "do the thing\n<ADDITIONAL_METADATA>\nThe current local time is: now.\n</ADDITIONAL_METADATA>"
    assert clean_user_content(content) == "do the thing"


def test_ordinary_prose_is_left_exactly_alone():
    assert clean_user_content("fix the bug in a < b") == "fix the bug in a < b"
    assert clean_user_content("use <div> tags here") == "use <div> tags here"
    assert clean_user_content("") == ""


def test_an_unclosed_wrapper_does_not_swallow_the_message():
    assert clean_user_content("<USER_REQUEST>\nhalf an envelope") == "half an envelope"


def test_tool_arguments_are_decoded_not_double_escaped():
    """agy stores each argument as a JSON string, so raw rendering shows \\"cmd\\"."""
    calls = [{"name": "run_command", "args": {"CommandLine": '"du -hd 1 /Users/dst"', "WaitMsBeforeAsync": "5000"}}]

    args = normalize_tool_calls(calls)[0]["args"]
    assert args["CommandLine"] == "du -hd 1 /Users/dst"
    assert args["WaitMsBeforeAsync"] == "5000"  # not turned into a number


def test_tool_arguments_that_are_not_encoded_are_untouched():
    calls = [{"name": "edit", "args": {"path": "src/app.py", "count": 3, "nested": {"a": 1}}}]

    args = normalize_tool_calls(calls)[0]["args"]
    assert args == {"path": "src/app.py", "count": 3, "nested": {"a": 1}}


def test_normalizing_never_drops_a_call_it_does_not_understand():
    calls = [{"function": {"name": "x", "arguments": "{}"}}, {"weird": True}]
    assert normalize_tool_calls(calls) == calls
