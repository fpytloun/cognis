"""Shared helper for switching the conversation's active executor (Stage 36).

A single backend used by both the ``switch_executor`` controller tool
(invoked by the agent) and the ``/executor`` slash command (invoked by
the user). The helper is the only mutator of
``conversations.active_executor_id`` outside the controller's one-time
initial pick.

Behaviour mirrors the spec:

- Validates that the target executor is in the agent's assigned pool
  (primary or additional).
- Validates that the target executor is currently usable
  (status=active, runtime_state in {active, degraded}, versions match,
  policy allows).
- On success: persists the new active to the conversation row and
  returns a structured ``SwitchOutcome``.
- On failure: leaves the conversation unchanged and returns a structured
  ``SwitchOutcome`` with ``status='error'`` and a factual reason.

The helper is transport-agnostic. Callers compose the user-facing
message from ``SwitchOutcome``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from cognis.core.executor_pool import (
    ExecutorAvailability,
    ExecutorPool,
    ResolvedExecutorTarget,
)
from cognis.logging import get_logger

logger = get_logger(__name__)


SwitchStatus = Literal["ok", "error"]
SwitchActor = Literal["agent", "user"]


@dataclass(frozen=True)
class SwitchOutcome:
    """Result of a switch_executor / /executor invocation."""

    status: SwitchStatus
    target: ResolvedExecutorTarget | None
    error_reason: str | None = None
    error_detail: str | None = None
    is_primary: bool = False
    available_tools: list[str] | None = None

    def to_tool_result(self) -> dict[str, Any]:
        """Render as a structured tool-result payload (for switch_executor)."""

        if self.status == "ok" and self.target is not None:
            return {
                "status": "ok",
                "executor_id": self.target.executor_id,
                "executor_type": self.target.executor_type,
                "is_primary": self.is_primary,
                "state": self.target.state.value,
                "description": self.target.description,
                "available_tools": list(self.available_tools or []),
            }
        return {
            "status": "error",
            "reason": self.error_reason or "unknown_error",
            "detail": self.error_detail or "",
            "executor_id": self.target.executor_id if self.target is not None else None,
        }

    def to_user_message(self) -> str:
        """Render as a chat-visible system message (for /executor)."""

        if self.status == "ok" and self.target is not None:
            kind = "primary" if self.is_primary else "additional"
            line = (
                f"Active executor switched to {self.target.executor_id} "
                f"({self.target.executor_type}) [{kind}]."
            )
            if not self.is_primary:
                line += (
                    " You are now routing to a non-primary executor; "
                    "switch_executor or /executor again to return to a primary."
                )
            return line
        if self.error_reason == "not_assigned":
            executor_id = self.target.executor_id if self.target else "<unknown>"
            return (
                f"Executor '{executor_id}' is not assigned to this agent. "
                "Active executor unchanged."
            )
        if self.error_reason == "unavailable":
            executor_id = self.target.executor_id if self.target else "<unknown>"
            state = self.target.state.value if self.target else "unknown"
            return (
                f"Executor '{executor_id}' is unavailable (state: {state}). "
                "Active executor unchanged."
            )
        return f"Could not switch executor: {self.error_detail or self.error_reason or 'error'}"


async def perform_executor_switch(
    *,
    conversation_id: str,
    pool: ExecutorPool,
    executor_id: str,
    actor: SwitchActor,
    session_factory: Any,
    reason: str | None = None,
) -> SwitchOutcome:
    """Switch the conversation's active executor.

    Returns a ``SwitchOutcome`` describing the result. The caller is
    responsible for any audit logging beyond this helper's structured
    log entry, and for surfacing the outcome to the agent (tool result)
    or user (system message).
    """

    target = pool.by_id(executor_id)
    if target is None:
        logger.info(
            "executor_switch: target not assigned",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "executor_id": executor_id,
                    "actor": actor,
                }
            },
        )
        return SwitchOutcome(
            status="error",
            target=ResolvedExecutorTarget(
                executor_id=executor_id,
                executor_type="",
                is_primary=False,
                selection_source="unknown",
                description=None,
                state=ExecutorAvailability.NOT_FOUND,
            ),
            error_reason="not_assigned",
            error_detail=(
                f"Executor '{executor_id}' is not in the agent's assigned pool."
            ),
        )

    if not target.usable:
        logger.info(
            "executor_switch: target unusable",
            extra={
                "extra_data": {
                    "conversation_id": conversation_id,
                    "executor_id": executor_id,
                    "state": target.state.value,
                    "actor": actor,
                }
            },
        )
        return SwitchOutcome(
            status="error",
            target=target,
            error_reason="unavailable",
            error_detail=(
                f"Executor '{executor_id}' is not usable "
                f"(state: {target.state.value})."
            ),
        )

    # Persist the new active executor
    from cognis.store.queries import set_conversation_active_executor

    async with session_factory() as session:
        ok = await set_conversation_active_executor(
            session, conversation_id, target.executor_id
        )
        if not ok:
            await session.rollback()
            return SwitchOutcome(
                status="error",
                target=target,
                error_reason="conversation_missing",
                error_detail=(
                    f"Conversation '{conversation_id}' not found; cannot persist switch."
                ),
            )
        await session.commit()

    available_tools = sorted(target.observed_tool_names)

    logger.info(
        "executor_switch: success",
        extra={
            "extra_data": {
                "conversation_id": conversation_id,
                "executor_id": target.executor_id,
                "is_primary": target.is_primary,
                "actor": actor,
                "reason": reason,
            }
        },
    )

    return SwitchOutcome(
        status="ok",
        target=target,
        is_primary=target.is_primary,
        available_tools=available_tools,
    )
