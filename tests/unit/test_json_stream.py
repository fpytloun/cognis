"""Tests for streamed JSON tool-call argument accumulation helpers."""

from __future__ import annotations

import json

from cognis.json_stream import merge_incremental_json_fragment, recover_trailing_json_object


def test_empty_incoming_is_a_noop() -> None:
    result = merge_incremental_json_fragment('{"a": 1}', "")
    assert result.merged == '{"a": 1}'
    assert result.emitted == ""
    assert result.replaced is False


def test_empty_existing_seeds_accumulator() -> None:
    result = merge_incremental_json_fragment("", '{"a": 1')
    assert result.merged == '{"a": 1'
    assert result.emitted == '{"a": 1'


def test_duplicate_replay_is_deduplicated() -> None:
    result = merge_incremental_json_fragment('{"a": 1, "b": 2}', '{"a": 1, "b": 2}')
    assert result.merged == '{"a": 1, "b": 2}'
    assert result.emitted == ""


def test_full_prefix_replay_emits_only_the_suffix() -> None:
    result = merge_incremental_json_fragment('{"a": 1', '{"a": 1, "b": 2}')
    assert result.merged == '{"a": 1, "b": 2}'
    assert result.emitted == ', "b": 2}'


def test_overlapping_suffix_prefix_merges_without_duplication() -> None:
    result = merge_incremental_json_fragment('{"a": 1, "b"', '"b": 2}')
    assert result.merged == '{"a": 1, "b": 2}'
    assert result.emitted == ": 2}"


def test_plain_delta_chunks_concatenate() -> None:
    # Typical OpenAI-style streaming: each delta is a small, non-self-contained
    # fragment of a much larger tool call. Neither fragment parses as JSON on
    # its own, and neither is a prefix/suffix of the other.
    existing = '{"action": "write_deliverable", "content": "Lorem ipsum '
    incoming = 'dolor sit amet longer body continues here'
    result = merge_incremental_json_fragment(existing, incoming)
    assert result.merged == existing + incoming
    assert result.replaced is False


def test_large_in_progress_accumulation_is_never_dropped_for_a_smaller_complete_fragment() -> (
    None
):
    """Regression test for the "write_deliverable large content" truncation bug.

    A large in-progress tool-call accumulation (e.g. a long `write_deliverable`
    payload) is normally still-unparseable simply because streaming hasn't
    finished yet -- not because it is corrupt. If a later, much smaller,
    unrelated fragment happens to parse as a complete JSON object on its own,
    it must never replace the larger accumulation; the chunk must instead be
    appended so streaming can continue toward the real complete payload.
    """

    existing = (
        '{"action": "write_deliverable", "content": "## Report\\n\\n'
        + ("Lorem ipsum dolor sit amet. " * 50)
        + '","format": "rich easi'  # deliberately unterminated/invalid tail
    )
    # A small, unrelated fragment that happens to parse as a complete object.
    incoming = '{"x": 1}'

    assert json.loads(incoming) is not None  # sanity: incoming parses standalone
    result = merge_incremental_json_fragment(existing, incoming)

    assert result.replaced is False
    assert result.merged == existing + incoming
    assert len(result.merged) > len(existing)


def test_genuine_full_corrected_replay_still_replaces_when_at_least_as_long() -> None:
    """A provider-initiated full replay after a corrupted partial should still
    win, as long as it is not shorter than what was accumulated so far."""

    existing = '{"foo": "bar", "baz": 1, "trunc'  # invalid, mid-string cut
    incoming = '{"different": "complete", "shape": true}'  # complete, longer
    assert len(incoming) >= len(existing)

    result = merge_incremental_json_fragment(existing, incoming)

    assert result.replaced is True
    assert result.merged == incoming


def test_equal_length_complete_replacement_still_replaces() -> None:
    existing = '{"a": "unterminat'
    incoming = '{"a": "b", "c": 1}'
    assert len(incoming) >= len(existing)

    result = merge_incremental_json_fragment(existing, incoming)
    assert result.replaced is True
    assert result.merged == incoming


def test_recover_trailing_json_object_prefers_last_complete_object() -> None:
    raw = '{"broken": tru} {"action": "write_deliverable", "content": "ok"}'
    recovered = recover_trailing_json_object(raw)
    assert recovered == {"action": "write_deliverable", "content": "ok"}


def test_two_complete_divergent_objects_do_not_concatenate() -> None:
    """A provider double-feed of the same logical tool call (two complete,
    divergent top-level objects on one stream index) must not be concatenated
    into ``{a}{b}``. Concatenation corrupts the arguments and downstream
    fabricates a second tool call. The later complete object wins as the
    corrected replay.
    """

    existing = '{"command": "ls"}'
    incoming = '{"command": "rm -rf /tmp/x"}'
    assert len(incoming) >= len(existing)

    result = merge_incremental_json_fragment(existing, incoming)

    assert result.replaced is True
    assert result.merged == incoming
    assert "}{" not in result.merged


def test_shorter_divergent_complete_object_replaces_without_concatenation() -> None:
    """A later shorter complete object is a corrected replay on one index.

    It must replace the prior complete object rather than concatenate into an
    invalid buffer that can fabricate a second call during finalization.
    """

    existing = '{"command": "echo hello world"}'
    incoming = '{"x": 1}'
    assert len(incoming) < len(existing)

    result = merge_incremental_json_fragment(existing, incoming)

    assert result.replaced is True
    assert result.merged == incoming
    assert "}{" not in result.merged
