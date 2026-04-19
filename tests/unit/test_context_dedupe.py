"""Unit tests for the current-turn user-message dedupe helper.

Regression: the controller records the current turn's user message into the
Intaris event store before context assembly so the intention barrier can
start updating in parallel. Context assembly then replays history and, if
the helper is broken, appends the message again — the LLM sees the prompt
twice. That exact defect was observed in the daily-brief trace and drove
pathological tool calling on gpt-5.4 low reasoning.
"""

from __future__ import annotations

from cognis.core.context import _current_user_message_already_in_history


def test_empty_history_returns_false() -> None:
    assert (
        _current_user_message_already_in_history(
            history_messages=[],
            user_message="hello",
            user_message_role="user",
            user_attachments=None,
        )
        is False
    )


def test_non_user_role_returns_false() -> None:
    # System-initiated turns don't record user_message events; they pass
    # role="system" and must never dedupe.
    assert (
        _current_user_message_already_in_history(
            history_messages=[{"role": "user", "content": "hello"}],
            user_message="hello",
            user_message_role="system",
            user_attachments=None,
        )
        is False
    )


def test_exact_match_returns_true() -> None:
    assert (
        _current_user_message_already_in_history(
            history_messages=[{"role": "user", "content": "hello"}],
            user_message="hello",
            user_message_role="user",
            user_attachments=None,
        )
        is True
    )


def test_whitespace_tolerant_match() -> None:
    # Both sides stripped before comparison.
    assert (
        _current_user_message_already_in_history(
            history_messages=[{"role": "user", "content": "  hello \n"}],
            user_message="hello",
            user_message_role="user",
            user_attachments=None,
        )
        is True
    )


def test_mismatched_content_returns_false() -> None:
    assert (
        _current_user_message_already_in_history(
            history_messages=[{"role": "user", "content": "hello"}],
            user_message="goodbye",
            user_message_role="user",
            user_attachments=None,
        )
        is False
    )


def test_assistant_follows_user_returns_false() -> None:
    # If the last user message already has an assistant reply after it,
    # we are not looking at the current turn's replay.
    assert (
        _current_user_message_already_in_history(
            history_messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            user_message="hello",
            user_message_role="user",
            user_attachments=None,
        )
        is False
    )


def test_tool_result_follows_user_returns_false() -> None:
    assert (
        _current_user_message_already_in_history(
            history_messages=[
                {"role": "user", "content": "hello"},
                {"role": "tool", "tool_call_id": "x", "content": "ok"},
            ],
            user_message="hello",
            user_message_role="user",
            user_attachments=None,
        )
        is False
    )


def test_canonical_attachment_note_suffix_matches() -> None:
    # Recording appends an attachment note in the canonical
    # ``Attachments: <name> (<kind>, artifact_id=<id>)`` form produced by
    # ``cognis.core.attachment_utils.attachment_note``. The dedupe helper
    # must tolerate this suffix so an attachment-bearing turn is still
    # correctly identified as already-recorded.
    history = [
        {
            "role": "user",
            "content": "hello\n\nAttachments: foo.pdf (application/pdf, artifact_id=art_1)",
        }
    ]
    assert (
        _current_user_message_already_in_history(
            history_messages=history,
            user_message="hello",
            user_message_role="user",
            user_attachments=[{"filename": "foo.pdf"}],
        )
        is True
    )


def test_legacy_attachment_note_suffix_matches() -> None:
    # Legacy/synthetic "Attached files" format from earlier recorders
    # should also be accepted, to avoid breaking old sessions.
    history = [
        {
            "role": "user",
            "content": "hello\n\nAttached files: foo.pdf",
        }
    ]
    assert (
        _current_user_message_already_in_history(
            history_messages=history,
            user_message="hello",
            user_message_role="user",
            user_attachments=[{"filename": "foo.pdf"}],
        )
        is True
    )


def test_unknown_trailing_text_returns_false() -> None:
    # Don't false-positive when there's meaningful extra content.
    history = [
        {
            "role": "user",
            "content": "hello\n\nHere is additional discussion.",
        }
    ]
    assert (
        _current_user_message_already_in_history(
            history_messages=history,
            user_message="hello",
            user_message_role="user",
            user_attachments=None,
        )
        is False
    )


def test_multipart_block_content_matches() -> None:
    # Some providers (or our attachment handling) put content as a list
    # of text+file blocks. Only text blocks factor into the comparison.
    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "image_url", "image_url": {"url": "..."}},
            ],
        }
    ]
    assert (
        _current_user_message_already_in_history(
            history_messages=history,
            user_message="hello",
            user_message_role="user",
            user_attachments=None,
        )
        is True
    )


def test_empty_user_message_returns_false() -> None:
    # Empty user_message should not match anything — the current turn has
    # nothing textual to compare.
    assert (
        _current_user_message_already_in_history(
            history_messages=[{"role": "user", "content": "hello"}],
            user_message="",
            user_message_role="user",
            user_attachments=None,
        )
        is False
    )
