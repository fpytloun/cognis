"""Unit tests for cognis.core.harness_guards."""

from __future__ import annotations

import json

import pytest

from cognis.core.harness_guards import (
    LoopGuardState,
    argument_sanity_rejection_payload,
    check_argument_sanity,
    check_loop_guard,
    loop_guard_rejection_payload,
    record_tool_call,
)


class TestLoopGuard:
    def test_first_call_is_allowed(self) -> None:
        state = LoopGuardState()
        assert check_loop_guard(state, "foo", {"a": 1}) is None

    def test_second_identical_call_is_rejected(self) -> None:
        state = LoopGuardState()
        record_tool_call(state, "foo", {"a": 1})
        message = check_loop_guard(state, "foo", {"a": 1})
        assert message is not None
        assert "foo" in message
        assert "Do not retry the same call." in message

    def test_different_arguments_reset_streak(self) -> None:
        state = LoopGuardState()
        record_tool_call(state, "foo", {"a": 1})
        assert check_loop_guard(state, "foo", {"a": 2}) is None
        record_tool_call(state, "foo", {"a": 2})
        assert check_loop_guard(state, "foo", {"a": 1}) is None

    def test_different_tool_name_resets_streak(self) -> None:
        state = LoopGuardState()
        record_tool_call(state, "foo", {"a": 1})
        assert check_loop_guard(state, "bar", {"a": 1}) is None

    def test_argument_key_order_does_not_matter(self) -> None:
        state = LoopGuardState()
        record_tool_call(state, "foo", {"a": 1, "b": 2})
        assert check_loop_guard(state, "foo", {"b": 2, "a": 1}) is not None

    def test_exempt_tool_is_not_loop_checked(self) -> None:
        state = LoopGuardState()
        record_tool_call(state, "memory_search", {"query": "x"})
        # Even repeated identical calls to exempt tools pass.
        assert check_loop_guard(state, "memory_search", {"query": "x"}) is None

    def test_non_json_arguments_do_not_crash(self) -> None:
        state = LoopGuardState()

        class Unjsonable:
            pass

        record_tool_call(state, "foo", {"obj": Unjsonable()})
        # No exception — the guard falls back to a deterministic repr.
        assert check_loop_guard(state, "foo", {"obj": "not the same"}) is None

    def test_third_identical_call_after_rejection_still_trips(self) -> None:
        state = LoopGuardState()
        record_tool_call(state, "foo", {"a": 1})
        # Second call rejected.
        assert check_loop_guard(state, "foo", {"a": 1}) is not None
        record_tool_call(state, "foo", {"a": 1})
        # Third call still flagged.
        assert check_loop_guard(state, "foo", {"a": 1}) is not None

    def test_rejection_payload_shape(self) -> None:
        payload = loop_guard_rejection_payload("foo", {"a": 1}, "teach back")
        data = json.loads(payload)
        assert data["status"] == "rejected"
        assert data["reason"] == "loop_detected"
        assert data["tool"] == "foo"
        assert data["message"] == "teach back"


class TestArgumentSanity:
    def test_list_tool_output_anchors_rejects_dummy(self) -> None:
        violation = check_argument_sanity("list_tool_output_anchors", {"call_id": "dummy"})
        assert violation is not None
        assert violation.reason == "invalid_call_id"

    def test_list_tool_output_anchors_rejects_placeholder(self) -> None:
        for placeholder in ("placeholder", "example", "...", "<call_id>"):
            violation = check_argument_sanity("list_tool_output_anchors", {"call_id": placeholder})
            assert violation is not None, f"failed for {placeholder!r}"

    def test_list_tool_output_anchors_rejects_empty_call_id(self) -> None:
        violation = check_argument_sanity("list_tool_output_anchors", {"call_id": ""})
        assert violation is not None
        assert violation.reason == "invalid_call_id"

    def test_list_tool_output_anchors_accepts_real_call_id(self) -> None:
        assert check_argument_sanity("list_tool_output_anchors", {"call_id": "call_abc123"}) is None

    def test_read_tool_output_anchor_same_rules(self) -> None:
        violation = check_argument_sanity(
            "read_tool_output_anchor", {"call_id": "dummy", "anchor": "result:1"}
        )
        assert violation is not None
        assert violation.reason == "invalid_call_id"

    def test_read_tool_output_also_rejects_placeholders(self) -> None:
        violation = check_argument_sanity("read_tool_output", {"call_id": "dummy"})
        assert violation is not None
        assert violation.reason == "invalid_call_id"

    def test_search_tool_output_also_rejects_placeholders(self) -> None:
        violation = check_argument_sanity("search_tool_output", {"call_id": "", "pattern": "error"})
        assert violation is not None
        assert violation.reason == "invalid_call_id"

    def test_all_tool_output_readers_accept_real_call_ids(self) -> None:
        for tool in (
            "list_tool_output_anchors",
            "read_tool_output_anchor",
            "read_tool_output",
            "search_tool_output",
        ):
            args = {"call_id": "call_abc123"}
            if tool == "read_tool_output_anchor":
                args["anchor"] = "result:1"
            if tool == "search_tool_output":
                args["pattern"] = "foo"
            assert check_argument_sanity(tool, args) is None, f"failed for {tool}"

    def test_multiedit_empty_edits_rejected(self) -> None:
        violation = check_argument_sanity("multiedit", {"file_path": "/tmp/foo", "edits": []})
        assert violation is not None
        assert violation.reason == "empty_edits"

    def test_multiedit_missing_edits_rejected(self) -> None:
        violation = check_argument_sanity("multiedit", {"file_path": "/tmp/foo"})
        assert violation is not None
        assert violation.reason == "empty_edits"

    def test_multiedit_with_edits_accepted(self) -> None:
        assert (
            check_argument_sanity(
                "multiedit",
                {
                    "file_path": "/tmp/foo",
                    "edits": [{"old_string": "a", "new_string": "b"}],
                },
            )
            is None
        )

    def test_dev_null_file_path_rejected_for_write_tools(self) -> None:
        for tool in ("write", "edit", "multiedit", "patch"):
            args = {"file_path": "/dev/null"}
            if tool == "multiedit":
                args["edits"] = [{"old_string": "a", "new_string": "b"}]
            if tool == "patch":
                args["patch_text"] = "*** Begin Patch\n*** Add File: /dev/null\n+x\n*** End Patch\n"
            violation = check_argument_sanity(tool, args)
            assert violation is not None, f"failed for {tool}"
            assert violation.reason == "invalid_file_path"

    def test_patch_dev_null_header_rejected(self) -> None:
        violation = check_argument_sanity(
            "patch",
            {"patch_text": "*** Begin Patch\n*** Add File: /dev/null\n+x\n*** End Patch\n"},
        )
        assert violation is not None
        assert violation.reason == "invalid_file_path"

    def test_patch_dev_null_like_hunk_content_is_allowed(self) -> None:
        violation = check_argument_sanity(
            "patch",
            {
                "patch_text": (
                    "--- a/tmp/file.txt\n+++ b/tmp/file.txt\n@@ -1 +1 @@\n"
                    " --- /dev/null\n+actual content\n"
                )
            },
        )

        assert violation is None

    def test_patch_envelope_header_like_hunk_content_is_allowed(self) -> None:
        violation = check_argument_sanity(
            "patch",
            {
                "patch_text": (
                    "*** Begin Patch\n*** Update File: tmp/file.txt\n@@\n"
                    " *** Add File: /dev/null\n+literal content\n*** End Patch\n"
                )
            },
        )

        assert violation is None

    def test_other_dev_devices_rejected(self) -> None:
        for special in ("/dev/zero", "/dev/random", "/dev/urandom"):
            violation = check_argument_sanity(
                "edit",
                {"file_path": special, "old_string": "a", "new_string": "b"},
            )
            assert violation is not None
            assert violation.reason == "invalid_file_path"

    def test_normal_file_path_accepted(self) -> None:
        assert (
            check_argument_sanity(
                "edit",
                {
                    "file_path": "/tmp/foo.txt",
                    "old_string": "a",
                    "new_string": "b",
                },
            )
            is None
        )

    def test_unknown_tool_passes(self) -> None:
        # Guards are narrow on purpose; unknown tools pass through.
        assert check_argument_sanity("some_mcp_tool", {"a": 1}) is None

    def test_rejection_payload_shape(self) -> None:
        violation = check_argument_sanity("list_tool_output_anchors", {"call_id": "dummy"})
        assert violation is not None
        payload = argument_sanity_rejection_payload(
            "list_tool_output_anchors", {"call_id": "dummy"}, violation
        )
        data = json.loads(payload)
        assert data["status"] == "rejected"
        assert data["reason"] == "invalid_call_id"
        assert data["tool"] == "list_tool_output_anchors"
        # received is a string (possibly truncated JSON)
        assert isinstance(data["received"], str)
        assert "dummy" in data["received"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
