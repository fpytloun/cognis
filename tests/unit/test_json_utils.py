"""Tests for robust JSON extraction utilities."""

from __future__ import annotations

import pytest

from cognis.core.json_utils import (
    extract_json_object,
    extract_text_from_response,
    extract_visible_text_from_response,
    infer_evaluation_from_text,
)

# ---------------------------------------------------------------------------
# extract_text_from_response
# ---------------------------------------------------------------------------


class TestExtractTextFromResponse:
    def test_standard_response(self) -> None:
        response = {"choices": [{"message": {"content": "hello"}}]}
        assert extract_text_from_response(response) == "hello"

    def test_empty_choices(self) -> None:
        assert extract_text_from_response({"choices": []}) == ""

    def test_missing_choices(self) -> None:
        assert extract_text_from_response({}) == ""

    def test_choices_not_list(self) -> None:
        assert extract_text_from_response({"choices": "bad"}) == ""

    def test_message_not_dict(self) -> None:
        assert extract_text_from_response({"choices": [{"message": "bad"}]}) == ""

    def test_content_not_string(self) -> None:
        assert extract_text_from_response({"choices": [{"message": {"content": 42}}]}) == ""

    def test_missing_content(self) -> None:
        assert extract_text_from_response({"choices": [{"message": {}}]}) == ""

    def test_reasoning_content_fallback(self) -> None:
        """When content is empty, fall back to reasoning_content (litellm standardized)."""
        response = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": '{"decision": "revise", "reasoning": "tests missing"}',
                    }
                }
            ]
        }
        assert (
            extract_text_from_response(response)
            == '{"decision": "revise", "reasoning": "tests missing"}'
        )

    def test_reasoning_fallback(self) -> None:
        """When content is empty and no reasoning_content, fall back to reasoning."""
        response = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning": "The step is incomplete because tests are missing",
                    }
                }
            ]
        }
        assert (
            extract_text_from_response(response)
            == "The step is incomplete because tests are missing"
        )

    def test_content_preferred_over_reasoning(self) -> None:
        """When content is present, reasoning fields are ignored."""
        response = {
            "choices": [
                {
                    "message": {
                        "content": '{"decision": "approved"}',
                        "reasoning_content": "some chain of thought",
                    }
                }
            ]
        }
        assert extract_text_from_response(response) == '{"decision": "approved"}'

    def test_null_content_with_reasoning(self) -> None:
        """When content is None, fall back to reasoning_content."""
        response = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": '{"decision": "revise"}',
                    }
                }
            ]
        }
        assert extract_text_from_response(response) == '{"decision": "revise"}'

    def test_whitespace_content_with_reasoning(self) -> None:
        """When content is only whitespace, fall back to reasoning_content."""
        response = {
            "choices": [
                {
                    "message": {
                        "content": "   \n  ",
                        "reasoning_content": '{"decision": "approved"}',
                    }
                }
            ]
        }
        assert extract_text_from_response(response) == '{"decision": "approved"}'

    def test_list_content_blocks_are_concatenated(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "output_text", "text": '{"decision": '},
                            {"type": "output_text", "text": '"approved"}'},
                        ]
                    }
                }
            ]
        }
        assert extract_text_from_response(response) == '{"decision": "approved"}'

    def test_dict_reasoning_payload_is_serialized(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": {"decision": "revise", "reason": "tests missing"},
                    }
                }
            ]
        }
        extracted = extract_text_from_response(response)
        assert '"decision": "revise"' in extracted
        assert '"feedback": "add tests"' not in extracted

    def test_structured_reasoning_object_keeps_full_json(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": {
                            "decision": "revise",
                            "reasoning": "tests missing",
                            "feedback": "add tests",
                        },
                    }
                }
            ]
        }
        extracted = extract_text_from_response(response)
        assert '"decision": "revise"' in extracted
        assert '"feedback": "add tests"' in extracted


class TestExtractVisibleTextFromResponse:
    def test_standard_response(self) -> None:
        response = {"choices": [{"message": {"content": "hello"}}]}
        assert extract_visible_text_from_response(response) == "hello"

    def test_list_content_blocks_are_concatenated(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "output_text", "text": "visible "},
                            {"type": "output_text", "text": "text"},
                        ]
                    }
                }
            ]
        }
        assert extract_visible_text_from_response(response) == "visible text"

    def test_reasoning_content_is_not_promoted_to_visible_text(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": '{"tool_ids": ["builtin:bash"]}',
                    }
                }
            ]
        }
        assert extract_visible_text_from_response(response) == ""

    def test_reasoning_is_not_promoted_to_visible_text(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning": '{"tool_ids": ["builtin:bash"]}',
                    }
                }
            ]
        }
        assert extract_visible_text_from_response(response) == ""


# ---------------------------------------------------------------------------
# extract_json_object — Layer 1: direct parse
# ---------------------------------------------------------------------------


class TestDirectParse:
    def test_valid_json(self) -> None:
        result = extract_json_object('{"decision": "approved", "reasoning": "ok"}')
        assert result["decision"] == "approved"

    def test_json_with_whitespace(self) -> None:
        result = extract_json_object('  \n  {"decision": "revise"}  \n  ')
        assert result["decision"] == "revise"

    def test_json_in_code_fences(self) -> None:
        content = '```json\n{"decision": "revise", "reasoning": "tests missing"}\n```'
        result = extract_json_object(content)
        assert result["decision"] == "revise"
        assert result["reasoning"] == "tests missing"

    def test_json_in_code_fences_no_lang(self) -> None:
        content = '```\n{"decision": "approved"}\n```'
        result = extract_json_object(content)
        assert result["decision"] == "approved"

    def test_code_fences_with_backticks_in_value(self) -> None:
        """Regression test: old strip('`') would mangle backticks in values."""
        content = '```json\n{"key": "value with `backtick`"}\n```'
        result = extract_json_object(content)
        assert result["key"] == "value with `backtick`"

    def test_nested_json(self) -> None:
        content = '{"decision": "approved", "details": {"score": 0.9, "tags": ["a", "b"]}}'
        result = extract_json_object(content)
        assert result["decision"] == "approved"
        assert result["details"]["score"] == 0.9

    def test_json_with_null(self) -> None:
        content = '{"decision": "approved", "feedback": null}'
        result = extract_json_object(content)
        assert result["feedback"] is None


# ---------------------------------------------------------------------------
# extract_json_object — Layer 2: brace-matching extraction
# ---------------------------------------------------------------------------


class TestBraceMatching:
    def test_json_with_leading_prose(self) -> None:
        content = 'Here is my evaluation:\n{"decision": "revise", "reasoning": "incomplete"}'
        result = extract_json_object(content)
        assert result["decision"] == "revise"

    def test_json_with_trailing_prose(self) -> None:
        content = '{"decision": "failed", "reasoning": "broken"}\nHope this helps!'
        result = extract_json_object(content)
        assert result["decision"] == "failed"

    def test_json_surrounded_by_prose(self) -> None:
        content = (
            "After careful analysis, I believe the step is incomplete.\n"
            '{"decision": "revise", "reasoning": "tests missing", "feedback": "add tests"}\n'
            "Please review and try again."
        )
        result = extract_json_object(content)
        assert result["decision"] == "revise"
        assert result["feedback"] == "add tests"

    def test_json_with_nested_braces_in_prose(self) -> None:
        content = (
            "The output format should be {like this}.\n"
            '{"decision": "approved", "reasoning": "all good"}\n'
        )
        # The first {like this} is not valid JSON, so brace matching
        # should fail on it and then find the real JSON object.
        # Actually, brace matching finds the first { and tries to match.
        # {like this} has balanced braces but isn't valid JSON, so it
        # returns None. Then we need to handle this case.
        # Let's verify the actual behavior:
        result = extract_json_object(content)
        assert result["decision"] == "approved"

    def test_json_with_strings_containing_braces(self) -> None:
        content = '{"decision": "approved", "reasoning": "the {output} looks correct"}'
        result = extract_json_object(content)
        assert result["decision"] == "approved"
        assert "{output}" in result["reasoning"]


# ---------------------------------------------------------------------------
# extract_json_object — Layer 3: regex field extraction
# ---------------------------------------------------------------------------


class TestRegexFields:
    def test_json_style_fields_in_broken_json(self) -> None:
        # JSON-like but with trailing comma (invalid JSON)
        content = '"decision": "revise", "reasoning": "tests are missing",'
        result = extract_json_object(content)
        assert result["decision"] == "revise"
        assert result["reasoning"] == "tests are missing"

    def test_relaxed_key_value(self) -> None:
        content = "decision: revise\nreasoning: the tests are missing\nfeedback: add unit tests"
        result = extract_json_object(content)
        assert result["decision"] == "revise"
        assert "tests" in result["reasoning"]

    def test_mixed_json_and_relaxed(self) -> None:
        content = '"decision": "approved"\nreasoning: everything looks good'
        result = extract_json_object(content)
        assert result["decision"] == "approved"

    def test_workflow_id_extraction(self) -> None:
        content = '"workflow_id": "system:direct", "confidence": 0.9, "reason": "simple task"'
        result = extract_json_object(content)
        assert result["workflow_id"] == "system:direct"
        assert result["confidence"] == 0.9

    def test_numeric_confidence(self) -> None:
        content = '"confidence": 0.85'
        result = extract_json_object(content)
        assert result["confidence"] == 0.85


# ---------------------------------------------------------------------------
# extract_json_object — failure cases
# ---------------------------------------------------------------------------


class TestExtractionFailure:
    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="Empty content"):
            extract_json_object("")

    def test_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="Empty content"):
            extract_json_object("   \n  ")

    def test_completely_unrelated_text(self) -> None:
        with pytest.raises(ValueError, match="Could not extract"):
            extract_json_object("The weather is nice today and I like cats.")

    def test_no_recognizable_fields(self) -> None:
        with pytest.raises(ValueError, match="Could not extract"):
            extract_json_object("Some random text with no JSON structure at all.")


# ---------------------------------------------------------------------------
# infer_evaluation_from_text
# ---------------------------------------------------------------------------


class TestInferEvaluation:
    def test_revise_incomplete(self) -> None:
        result = infer_evaluation_from_text("The step is incomplete because tests are missing")
        assert result["decision"] == "revise"

    def test_revise_not_met(self) -> None:
        result = infer_evaluation_from_text("The objective is not met — no API endpoints created")
        assert result["decision"] == "revise"

    def test_revise_doesnt_mention(self) -> None:
        result = infer_evaluation_from_text("The claims doesn't mention tests, which were required")
        assert result["decision"] == "revise"

    def test_revise_does_not_mention(self) -> None:
        result = infer_evaluation_from_text(
            "The claims does not mention tests, which were required"
        )
        assert result["decision"] == "revise"

    def test_revise_didnt(self) -> None:
        result = infer_evaluation_from_text("The agent didn't implement the required tests")
        assert result["decision"] == "revise"

    def test_revise_missing(self) -> None:
        result = infer_evaluation_from_text("Unit tests are missing from the implementation")
        assert result["decision"] == "revise"

    def test_revise_insufficient(self) -> None:
        result = infer_evaluation_from_text("The documentation is insufficient")
        assert result["decision"] == "revise"

    def test_failed_cannot_succeed(self) -> None:
        result = infer_evaluation_from_text("This step cannot succeed — the API is down")
        assert result["decision"] == "failed"

    def test_failed_impossible(self) -> None:
        result = infer_evaluation_from_text("It is impossible to complete this step")
        assert result["decision"] == "failed"

    def test_failed_fundamentally(self) -> None:
        result = infer_evaluation_from_text("The approach is fundamentally broken")
        assert result["decision"] == "failed"

    def test_approved_explicit(self) -> None:
        result = infer_evaluation_from_text("The step is approved and looks great")
        assert result["decision"] == "approved"

    def test_approved_complete(self) -> None:
        result = infer_evaluation_from_text("The implementation is complete and well tested")
        assert result["decision"] == "approved"

    def test_approved_satisfactory(self) -> None:
        result = infer_evaluation_from_text("The output is satisfactory")
        assert result["decision"] == "approved"

    def test_approved_met_objective(self) -> None:
        result = infer_evaluation_from_text("The agent has met the objective successfully")
        assert result["decision"] == "approved"

    def test_ambiguous_defaults_to_approved(self) -> None:
        result = infer_evaluation_from_text("I looked at the output and it seems okay")
        assert result["decision"] == "approved"
        assert "Could not determine" in result["reasoning"]

    def test_empty_string(self) -> None:
        result = infer_evaluation_from_text("")
        assert result["decision"] == "approved"
        assert "Empty evaluator response" in result["reasoning"]

    def test_reasoning_is_truncated(self) -> None:
        long_text = "The step is incomplete. " * 100
        result = infer_evaluation_from_text(long_text)
        assert result["decision"] == "revise"
        assert len(result["reasoning"]) <= 520  # "Inferred from text: " + 500

    def test_feedback_set_for_revise(self) -> None:
        result = infer_evaluation_from_text("The step is incomplete, needs more work")
        assert result["decision"] == "revise"
        assert result["feedback"] is not None

    def test_feedback_none_for_approved(self) -> None:
        result = infer_evaluation_from_text("Everything is complete and approved")
        assert result["decision"] == "approved"
        assert result["feedback"] is None

    def test_failed_takes_priority_over_revise(self) -> None:
        """Failed keywords should win over revise keywords."""
        result = infer_evaluation_from_text(
            "The step is incomplete and cannot succeed due to fatal errors"
        )
        assert result["decision"] == "failed"

    def test_revise_takes_priority_over_approved(self) -> None:
        """Revise keywords should win over approved keywords."""
        result = infer_evaluation_from_text("The step is partially complete but tests are missing")
        assert result["decision"] == "revise"
