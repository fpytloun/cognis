"""Typed executor delivery certainty shared by physical and forwarded transports."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class DeliveryState(StrEnum):
    NOT_SENT = "not_sent"
    ACCEPTED_UNKNOWN = "accepted_unknown"
    TERMINAL = "terminal"


class ExecutorDeliveryError(RuntimeError):
    """Executor call failed with an explicit delivery-certainty classification."""

    def __init__(
        self,
        message: str,
        delivery_state: DeliveryState | str = DeliveryState.ACCEPTED_UNKNOWN,
        *,
        code: str = "executor_delivery_failure",
        executor_id: str | None = None,
        generation: int | None = None,
        owner_id: str | None = None,
        epoch: int | None = None,
        same_executor_only: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.delivery_state = DeliveryState(delivery_state)
        self.code = code
        self.executor_id = executor_id
        self.generation = generation
        self.owner_id = owner_id
        self.epoch = epoch
        self.same_executor_only = same_executor_only
        self.retry_after = retry_after

    def metadata(self) -> dict[str, Any]:
        return {
            "error_code": self.code,
            "delivery_state": self.delivery_state.value,
            "executor_id": self.executor_id,
            "generation": self.generation,
            "owner_id": self.owner_id,
            "epoch": self.epoch,
            "same_executor_only": self.same_executor_only,
            "retry_after": self.retry_after,
        }


class AmbiguousToolOutcome(RuntimeError):
    """Unsafe tool may have executed; the durable turn must await operator resolution."""

    def __init__(
        self,
        *,
        tool_name: str,
        argument_fingerprint: str,
        executor_id: str | None,
        generation: int | None,
        epoch: int | None,
    ) -> None:
        super().__init__(
            f"Outcome of tool '{tool_name}' is ambiguous; it was not replayed automatically."
        )
        self.tool_name = tool_name
        self.argument_fingerprint = argument_fingerprint
        self.executor_id = executor_id
        self.generation = generation
        self.epoch = epoch
        self.uncertain_tool_calls: list[dict[str, Any]] = []

    def add_uncertain_tool_call(
        self,
        *,
        tool_name: str,
        argument_fingerprint: str,
    ) -> None:
        identity = {
            "tool_name": tool_name,
            "argument_fingerprint": argument_fingerprint,
        }
        if identity not in self.uncertain_tool_calls:
            self.uncertain_tool_calls.append(identity)

    def detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "tool_name": self.tool_name,
            "argument_fingerprint": self.argument_fingerprint,
            "executor_id": self.executor_id,
            "generation": self.generation,
            "epoch": self.epoch,
        }
        if self.uncertain_tool_calls:
            detail["uncertain_tool_calls"] = list(self.uncertain_tool_calls)
        return detail
