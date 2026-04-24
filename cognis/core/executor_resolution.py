"""Executor selection and tool enablement resolution.

Resolves which executor to use for a given agent and whether a specific
tool is enabled on that executor.
"""

from __future__ import annotations

from typing import Any

from cognis.core.executor_policy import ExecutorPolicy, is_executor_row_usable
from cognis.logging import get_logger
from cognis.models.tool import ToolDefinition
from cognis.ownership import is_shared_owner_email

logger = get_logger(__name__)


def is_tool_enabled(
    tool: ToolDefinition,
    enabled_tools: list[str] | None,
    enabled_tool_groups: list[str] | None,
) -> bool:
    """Check if a tool is enabled on an executor.

    A tool is enabled if:
    - "*" is in enabled_tools, OR
    - tool.name is in enabled_tools, OR
    - tool.category is in enabled_tool_groups
    """
    tools = enabled_tools or []
    groups = enabled_tool_groups or []

    if "*" in tools:
        return True
    if tool.name in tools:
        return True
    return tool.category in groups


def filter_tools_by_executor(
    tools: list[ToolDefinition],
    enabled_tools: list[str] | None,
    enabled_tool_groups: list[str] | None,
) -> list[ToolDefinition]:
    """Filter tool definitions to only those enabled on an executor."""
    return [t for t in tools if is_tool_enabled(t, enabled_tools, enabled_tool_groups)]


def labels_match(
    executor_labels: dict[str, Any] | None,
    selector: dict[str, str],
) -> bool:
    """Check if executor labels satisfy an agent's label selector.

    All selector key-value pairs must match (AND logic).
    Empty selector matches any executor.
    """
    if not selector:
        return True
    if not executor_labels:
        return False
    return all(str(executor_labels.get(key)) == str(value) for key, value in selector.items())


def select_executor_for_agent(
    executors: list[Any],
    agent_execution: dict[str, Any] | None,
    *,
    owner_email: str | None = None,
    policy: ExecutorPolicy | None = None,
) -> Any | None:
    """Select the best executor for an agent based on its execution config.

    Resolution order:
    1. If executor_id is set -> use that specific executor
    2. If executor_selector is set -> find executor matching all labels
    3. Else -> use the default executor (is_default=True)

    Returns the executor row or None if no match.
    """
    execution = agent_execution or {}
    explicit_id = execution.get("executor_id")
    selector = execution.get("executor_selector") or {}

    def _usable(executor: Any) -> bool:
        return (
            is_executor_row_usable(executor, policy, owner_email=owner_email)
            if policy is not None
            else getattr(executor, "status", None) == "active"
            and (
                owner_email is None
                or getattr(executor, "owner_email", None) == owner_email
                or is_shared_owner_email(getattr(executor, "owner_email", None))
            )
        )

    # 1. Explicit executor ID
    if explicit_id:
        for ex in executors:
            if ex.executor_id == explicit_id and _usable(ex):
                return ex
        logger.warning(
            "executor_resolution: explicit executor not found or inactive",
            extra={"extra_data": {"executor_id": explicit_id}},
        )
        return None

    # 2. Label selector matching
    if selector:
        matches: list[Any] = []
        for ex in executors:
            usable = _usable(ex)
            if usable and labels_match(ex.labels, selector):
                matches.append(ex)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "executor_resolution: selector matched multiple executors",
                extra={"extra_data": {"selector": selector, "count": len(matches)}},
            )
            return None
        logger.warning(
            "executor_resolution: no executor matches selector",
            extra={"extra_data": {"selector": selector}},
        )
        return None

    # 3. Default executor: prefer private owner default, then shared default.
    for ex in executors:
        if ex.is_default and _usable(ex) and getattr(ex, "owner_email", None) == owner_email:
            return ex

    for ex in executors:
        if ex.is_default and _usable(ex) and is_shared_owner_email(getattr(ex, "owner_email", None)):
            return ex

    # 4. Fallback: first private owner executor, then shared executor.
    for ex in executors:
        if _usable(ex) and getattr(ex, "owner_email", None) == owner_email:
            return ex

    for ex in executors:
        if _usable(ex) and is_shared_owner_email(getattr(ex, "owner_email", None)):
            return ex

    return None
