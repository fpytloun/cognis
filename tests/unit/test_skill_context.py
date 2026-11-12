"""Skill instruction availability is determined by the actual trusted context."""

from __future__ import annotations

import json

import pytest

from cognis.core.skill_context import deduplicate_skill_instructions
from cognis.core.tool_router import ToolRouter
from cognis.models.tool import ToolCall, ToolResult
from cognis.tools.builtin.skill_management import materialize_loaded_skill_context


def _loaded(instructions: str, *, templates: dict | None = None) -> ToolResult:
    output, metadata = materialize_loaded_skill_context(
        skill_id="skill_calendar",
        name="Calendar",
        description="Calendar operations.",
        instructions=instructions,
        tools=[],
        templates=templates,
        asset_refs=[],
        steps=[],
        tags=[],
        linked_tool_ids=[],
    )
    return ToolResult(output=json.dumps(output), metadata=metadata)


@pytest.mark.parametrize("source", ["auto", "replay", "compacted", "user"])
def test_load_requires_instructions_to_remain_in_trusted_context(source: str) -> None:
    result = _loaded("Read the calendar.")
    content = result.metadata["protected_context"]
    messages = [
        {
            "role": "user" if source == "user" else "system",
            "content": (
                "Earlier skill instructions were compacted."
                if source == "compacted"
                else f"<loaded_skills>\n{content}\n</loaded_skills>"
                if source == "auto"
                else content
            ),
        }
    ]

    loaded = deduplicate_skill_instructions(result, messages)

    assert not loaded.is_error
    expected_loaded = source in {"compacted", "user"}
    assert json.loads(loaded.output)["instructions_loaded"] is expected_loaded
    assert ("protected_context" in loaded.metadata) is expected_loaded


@pytest.mark.parametrize(
    ("new_instructions", "new_templates"),
    [
        ("Read the refreshed calendar.", None),
        ("Read the calendar.", {"summary": "Summarize upcoming events."}),
        ("Read <asset_guidance>new rules</asset_guidance>.", None),
    ],
)
def test_changed_instructions_or_templates_are_supplied_in_full(
    new_instructions: str, new_templates: dict | None
) -> None:
    first = _loaded("Read the calendar.")
    changed = _loaded(new_instructions, templates=new_templates)
    messages = [{"role": "system", "content": first.metadata["protected_context"]}]

    loaded = deduplicate_skill_instructions(changed, messages)

    assert json.loads(loaded.output)["instructions_loaded"] is True
    assert new_instructions in loaded.metadata["protected_context"]
    assert "replace the previously loaded instructions" in loaded.metadata["protected_context"]
    messages.append({"role": "system", "content": loaded.metadata["protected_context"]})
    assert (
        json.loads(deduplicate_skill_instructions(changed, messages).output)["instructions_loaded"]
        is False
    )
    # An older version still in history is not the current instruction set.
    reverted = deduplicate_skill_instructions(first, messages)
    assert json.loads(reverted.output)["instructions_loaded"] is True


def test_metadata_only_edit_does_not_repeat_instructions() -> None:
    original = _loaded("Read the calendar.")
    renamed = original.model_copy(deep=True)
    renamed.metadata["protected_context"] = (
        renamed.metadata["protected_context"]
        .replace("<name>Calendar</name>", "<name>Calendar operations</name>")
        .replace(
            "<description>Calendar operations.</description>",
            "<description>Updated catalog description.</description>",
        )
    )
    loaded = deduplicate_skill_instructions(
        renamed, [{"role": "system", "content": original.metadata["protected_context"]}]
    )
    assert json.loads(loaded.output)["status"] == "already_loaded"
    assert "protected_context" not in loaded.metadata


@pytest.mark.parametrize("large", [False, True])
def test_settled_skill_result_keeps_raw_payload_and_untrusted_presentation(large: bool) -> None:
    result = _loaded("Read the calendar.")
    payload = json.loads(result.output)
    closing_tag = "<" + "/tool_result>"
    warning = f"Asset warning {closing_tag} " + ("x" * 60_000 if large else "")
    payload["preparation_warning"] = warning
    result.output = json.dumps(payload)
    router = ToolRouter(guardrails=None)
    call = ToolCall(call_id="call_3", name="skill_load", arguments={"skill_id": "skill_calendar"})
    presented = router._sanitize_result(call.name, result, 50_000, call_id=call.call_id)

    settled = router.finalize_skill_result(
        call, presented, [{"role": "system", "content": result.metadata["protected_context"]}]
    )

    saved = json.loads(settled.metadata["_raw_output"])
    assert saved["status"] == "already_loaded"
    assert saved["instructions_loaded"] is False
    assert saved["preparation_warning"] == warning
    assert "protected_context" not in settled.metadata
    assert settled.output.startswith('<tool_result trust="untrusted">\n')
    body = settled.output.split("\n", 1)[1].rsplit("\n", 1)[0]
    assert closing_tag not in body
    assert settled.metadata["truncated"] is large
    if not large:
        assert json.loads(body)["status"] == "already_loaded"
