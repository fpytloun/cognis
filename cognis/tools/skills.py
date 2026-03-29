"""Skill loader helpers.

MVP limitation: skills are inline `agent.skills.items[*].tool_names` references
only. They may reference builtin/static tool names. MCP tool references are
silently skipped and logged for visibility.
"""

from __future__ import annotations

from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition

logger = get_logger(__name__)


def load_skill_tool_names(agent: AgentDefinition | None) -> set[str]:
    """Return builtin/static tool names referenced by inline skill items."""

    if agent is None or not isinstance(agent.skills, dict):
        return set()

    raw_items = agent.skills.get("items")
    if not isinstance(raw_items, list):
        return set()

    tool_names: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            logger.warning(
                "Skipping malformed skill entry",
                extra={"extra_data": {"agent_id": agent.agent_id, "index": index}},
            )
            continue
        skill_id = item.get("skill_id")
        skill_name = item.get("name")
        if not isinstance(skill_id, str) or not isinstance(skill_name, str):
            logger.warning(
                "Skipping malformed skill metadata",
                extra={"extra_data": {"agent_id": agent.agent_id, "index": index}},
            )
            continue
        raw_tool_names = item.get("tool_names")
        if not isinstance(raw_tool_names, list):
            logger.warning(
                "Skipping skill without tool_names",
                extra={
                    "extra_data": {
                        "agent_id": agent.agent_id,
                        "skill_id": skill_id,
                        "skill_name": skill_name,
                    }
                },
            )
            continue
        for tool_name in raw_tool_names:
            if isinstance(tool_name, str) and tool_name.strip():
                tool_names.add(tool_name)
    return tool_names
