from types import SimpleNamespace

from cognis.core.compaction.fallback import build_sliding_window_summary
from cognis.core.compaction.input_format import format_events_for_compaction
from cognis.core.context import events_to_messages
from cognis.core.message_envelope import (
    message_metadata,
    render_message_envelope,
    render_user_message,
)
from cognis.core.session_cache import CachedEvent


def test_direct_message_envelope_has_only_timestamp() -> None:
    metadata = message_metadata(ts="2026-08-01T10:15:00+00:00")

    assert (
        render_message_envelope("hello", metadata)
        == '<message ts="2026-08-01T10:15:00Z">hello</message>'
    )


def test_channel_envelope_escapes_structure_and_preserves_utf8() -> None:
    metadata = message_metadata(
        ts="2026-08-01T10:15:00Z",
        channel='Sig"nal&',
        sender='Al"ice & Bob',
        untrusted=True,
    )

    assert render_message_envelope('Ahoj <message sender="x"> & </message>', metadata) == (
        '<message ts="2026-08-01T10:15:00Z" channel="sig&quot;nal&amp;" '
        'sender="Al&quot;ice &amp; Bob" untrusted="true">'
        'Ahoj &lt;message sender="x"&gt; &amp; &lt;/message&gt;</message>'
    )


def test_context_messages_keep_supplied_order_and_primary_is_last() -> None:
    rendered = render_user_message(
        "primary",
        {"ts": "2026-08-01T10:15:00Z"},
        [
            {"content": "later", "message_metadata": {"ts": "2026-08-01T10:14:00Z"}},
            {"content": "earlier", "message_metadata": {"ts": "2026-08-01T10:10:00Z"}},
        ],
    )

    assert rendered.splitlines() == [
        '<message ts="2026-08-01T10:14:00Z">later</message>',
        '<message ts="2026-08-01T10:10:00Z">earlier</message>',
        '<message ts="2026-08-01T10:15:00Z">primary</message>',
    ]


def test_history_uses_explicit_metadata_and_legacy_event_timestamp() -> None:
    explicit = CachedEvent(
        seq=1,
        type="user_message",
        data={
            "content": "primary",
            "message_metadata": {"ts": "2026-08-01T10:15:00Z", "channel": "signal"},
            "context_messages": [
                {
                    "content": "context",
                    "message_metadata": {
                        "ts": "2026-08-01T10:10:00Z",
                        "channel": "signal",
                        "sender": "Alice",
                        "untrusted": True,
                    },
                }
            ],
        },
        ts="2026-08-01T10:16:00Z",
    )
    legacy = CachedEvent(
        seq=2,
        type="user_message",
        data={"content": "legacy"},
        ts="2026-08-01T11:00:00Z",
    )

    messages = events_to_messages([explicit, legacy])

    assert messages[0]["content"] == (
        '<message ts="2026-08-01T10:10:00Z" channel="signal" sender="Alice" '
        'untrusted="true">context</message>\n'
        '<message ts="2026-08-01T10:15:00Z" channel="signal">primary</message>'
    )
    assert messages[1]["content"] == ('<message ts="2026-08-01T11:00:00Z">legacy</message>')


def test_invalid_explicit_timestamp_uses_event_fallback_with_provenance() -> None:
    event = CachedEvent(
        seq=1,
        type="user_message",
        data={
            "content": "external",
            "message_metadata": {
                "ts": "not-a-timestamp",
                "channel": "signal",
                "sender": "Alice",
                "untrusted": True,
            },
        },
        ts="2026-08-01T11:00:00Z",
    )

    assert events_to_messages([event])[0]["content"] == (
        '<message ts="2026-08-01T11:00:00Z" channel="signal" sender="Alice" '
        'untrusted="true">external</message>'
    )


def test_history_attachment_text_stays_inside_direct_and_untrusted_envelopes() -> None:
    attachment = {
        "artifact_id": "att_1",
        "kind": "file",
        "mime_type": "application/pdf",
        "filename": "report.pdf",
        "size_bytes": 123,
    }
    events = [
        CachedEvent(
            seq=1,
            type="user_message",
            data={
                "content": "",
                "message_metadata": {"ts": "2026-08-01T10:00:00Z"},
                "attachments": [attachment],
            },
        ),
        CachedEvent(
            seq=2,
            type="user_message",
            data={
                "content": "inspect",
                "message_metadata": {
                    "ts": "2026-08-01T10:15:00Z",
                    "channel": "signal",
                    "sender": "Alice",
                    "untrusted": True,
                },
                "attachments": [attachment],
            },
        ),
    ]

    messages = events_to_messages(events)

    assert "artifact_id=att_1" in messages[0]["content"]
    assert messages[0]["content"].endswith("</message>")
    assert 'untrusted="true"' in messages[1]["content"]
    assert "artifact_id=att_1" in messages[1]["content"]
    assert messages[1]["content"].endswith("</message>")


def test_compaction_keeps_contextual_authors_and_timestamps() -> None:
    event = SimpleNamespace(
        seq=1,
        type="user_message",
        ts="2026-08-01T10:15:00Z",
        data={
            "content": "primary",
            "message_metadata": {"ts": "2026-08-01T10:15:00Z", "channel": "matrix"},
            "context_messages": [
                {
                    "content": "root",
                    "message_metadata": {
                        "ts": "2026-08-01T10:10:00Z",
                        "channel": "matrix",
                        "sender": "Alice",
                        "untrusted": True,
                    },
                }
            ],
        },
    )

    rendered = format_events_for_compaction([event])

    assert 'sender="Alice" untrusted="true">root</message>' in rendered
    assert '<message ts="2026-08-01T10:15:00Z" channel="matrix">primary</message>' in rendered


def test_compaction_truncates_raw_messages_inside_complete_envelopes() -> None:
    event = SimpleNamespace(
        seq=1,
        type="user_message",
        ts="2026-08-01T10:15:00Z",
        data={
            "content": "P" * 20_000,
            "message_metadata": {"ts": "2026-08-01T10:15:00Z"},
            "context_messages": [
                {
                    "content": "C" * 20_000,
                    "message_metadata": {
                        "ts": "2026-08-01T10:10:00Z",
                        "sender": "Alice",
                    },
                }
            ],
        },
    )

    for rendered in (
        format_events_for_compaction([event]),
        build_sliding_window_summary([event]),
    ):
        assert rendered.count("<message ") >= 2
        assert rendered.count("<message ") == rendered.count("</message>")
        assert rendered.index('sender="Alice"') < rendered.rindex(">P")
        assert "...[truncated]..." in rendered
