"""Deduplicate skill instructions against the context actually supplied to the model."""

from __future__ import annotations

import re
from typing import Any

from cognis import json_codec as json
from cognis.models.tool import ToolResult

_LOADED_SKILL = re.compile(r"<loaded_skill>\n.*?</loaded_skill>", re.DOTALL)
_INSTRUCTION_SECTIONS = re.compile(
    r"<instructions>.*?</instructions>|<prompt_templates>.*?</prompt_templates>",
    re.DOTALL,
)


def deduplicate_skill_instructions(
    result: ToolResult, messages: list[dict[str, Any]]
) -> ToolResult:
    """Keep asset results but inject instructions only when not already current.

    The live message list, including canonical replay, owns availability. Historical
    loaded-skill snapshots cannot prove that instructions survived compaction.
    """
    if result.is_error or not result.metadata:
        return result
    activation = result.metadata.get("skill_activation")
    context = result.metadata.get("protected_context")
    if not isinstance(activation, dict) or not isinstance(context, str):
        return result
    skill_id = activation.get("skill_id")
    if not isinstance(skill_id, str) or not skill_id:
        return result
    if _LOADED_SKILL.fullmatch(context) is None:
        return result
    identity = f"<skill_id>{skill_id}</skill_id>"
    current = _INSTRUCTION_SECTIONS.findall(context)
    previous = None
    for message in messages:
        if message.get("role") not in {"system", "developer"}:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        for match in _LOADED_SKILL.finditer(content):
            block = match.group()
            if block.startswith(f"<loaded_skill>\n{identity}\n"):
                previous = _INSTRUCTION_SECTIONS.findall(block)

    output = json.loads(result.output)
    metadata = dict(result.metadata)
    unchanged = previous == current
    output["status"] = "already_loaded" if unchanged else "loaded"
    output["instructions_loaded"] = not unchanged
    if unchanged:
        metadata.pop("protected_context")
        output["message"] = (
            "These skill instructions are already loaded and remain current. "
            "No duplicate instructions were added. Asset preparation was still performed; "
            "use the returned asset status and paths. Do not retry to reload instructions."
        )
    elif previous is not None:
        metadata["protected_context"] = (
            f"The following instructions replace the previously loaded instructions "
            f"for skill {skill_id}. Use this complete version, not the earlier version.\n\n"
            + context
        )
    return result.model_copy(
        update={"output": json.dumps(output, indent=2, default=str), "metadata": metadata}
    )
