"""Tool registry and runtime handler metadata."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from cognis.logging import get_logger
from cognis.models.tool import ExecutorHandle, ToolDefinition, ToolResult, stable_tool_id
from cognis.transfers.client import BinaryTransferClient
from cognis.transfers.models import TransferGrant

logger = get_logger(__name__)

ToolOutputChunkCallback = Callable[[str, str | None], Coroutine[Any, Any, None]]

SOURCE_PRIORITIES: dict[str, int] = {
    "builtin": 500,
    "executor": 400,
    "skill": 300,
    "local_mcp": 200,
    "intaris_mcp": 100,
}


class ToolBackgroundRequestOutcome(StrEnum):
    """Outcome of a user request to move a running tool call into the background."""

    ACCEPTED = "accepted"
    NOT_BACKGROUNDABLE = "not_backgroundable"
    ALREADY_REQUESTED = "already_requested"
    UNKNOWN_CALL = "unknown_call"


TOOL_BACKGROUND_RPC_TIMEOUT_SECONDS = 10.0


def parse_tool_background_rpc_result(result: Any) -> ToolBackgroundRequestOutcome:
    """Normalize a ``tool.background`` RPC result into a request outcome."""

    if isinstance(result, dict):
        raw = result.get("outcome")
        if isinstance(raw, str):
            try:
                return ToolBackgroundRequestOutcome(raw)
            except ValueError:
                pass
        if result.get("accepted") is True:
            return ToolBackgroundRequestOutcome.ACCEPTED
    return ToolBackgroundRequestOutcome.NOT_BACKGROUNDABLE


@dataclass(slots=True)
class ToolBackgroundControl:
    """Per-call channel that lets a running tool hand its work off to the background.

    A handler sets ``supported`` only while it is waiting on work it can detach.
    The executor runtime calls :meth:`request` when the user asks to move the
    call into the background; the handler observes ``requested`` and decides
    whether the handoff still applies (the work may have just finished).
    """

    requested: asyncio.Event = field(default_factory=asyncio.Event)
    supported: bool = False

    def request(self) -> ToolBackgroundRequestOutcome:
        """Signal a background handoff request when the call can honor it."""

        if self.requested.is_set():
            return ToolBackgroundRequestOutcome.ALREADY_REQUESTED
        if not self.supported:
            return ToolBackgroundRequestOutcome.NOT_BACKGROUNDABLE
        self.requested.set()
        return ToolBackgroundRequestOutcome.ACCEPTED


@dataclass(slots=True)
class ToolExecutionContext:
    """Execution-time context passed to tool handlers."""

    executor_handle: ExecutorHandle
    runtime_metadata: dict[str, Any] = field(default_factory=dict)
    shared_runtime_metadata: dict[str, Any] | None = None
    execution_scope_id: str | None = None
    output_chunk_callback: ToolOutputChunkCallback | None = None
    background_control: ToolBackgroundControl | None = None
    binary_transfers: BinaryTransferClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Construct one typed transfer client owned by this execution context."""
        raw = self.runtime_metadata.get("binary_transfer_grant")
        self.binary_transfers = BinaryTransferClient(
            TransferGrant.model_validate(raw) if raw else None
        )


type ToolHandlerResult = str | dict[str, Any] | list[Any] | ToolResult


class ToolHandler(Protocol):
    """Async tool handler interface used by the in-process executor."""

    async def __call__(
        self, arguments: dict[str, Any], context: ToolExecutionContext
    ) -> ToolHandlerResult: ...


@dataclass(slots=True)
class RegisteredTool:
    """Tool definition plus optional runtime handler."""

    definition: ToolDefinition
    handler: ToolHandler | None = None


class ToolRegistry:
    """Registry for tool definitions and optional runtime handlers."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        """Register a tool by its fully-qualified name."""

        self._validate_definition(tool.definition)
        if tool.definition.name in self._tools:
            raise ValueError(f"Duplicate tool registration: {tool.definition.name}")
        self._tools[tool.definition.name] = tool

    @staticmethod
    def _validate_definition(definition: ToolDefinition) -> None:
        from cognis.tools.introspection import audit_tool_registration

        failures = audit_tool_registration(definition)
        if failures:
            raise ValueError(
                f"Invalid native tool contract for {definition.name}: " + "; ".join(failures)
            )

    def register_many(self, tools: list[RegisteredTool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, tool_name: str) -> RegisteredTool | None:
        return self._tools.get(tool_name)

    def items(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def list_tools(self) -> list[ToolDefinition]:
        return [item.definition for item in self._tools.values()]

    def export(self) -> list[dict[str, Any]]:
        return [item.definition.model_dump(mode="json") for item in self._tools.values()]

    def with_definition_overrides(self, definitions: dict[str, ToolDefinition]) -> ToolRegistry:
        """Read through classifications instead of re-registering the inventory.

        Definitions are audited on lookup/enumeration. Callable name or source
        changes retain eager registration, including duplicate-name rejection.
        """
        if not definitions:
            return self
        changed = False
        remapped = False
        for registered in self.items():
            replacement = definitions.get(stable_tool_id(registered.definition))
            if replacement is None:
                continue
            changed = True
            if (
                replacement.name != registered.definition.name
                or replacement.source != registered.definition.source
            ):
                remapped = True
        if not changed:
            return self
        if remapped:
            result = ToolRegistry()
            for registered in self.items():
                definition = definitions.get(
                    stable_tool_id(registered.definition), registered.definition
                )
                result.register(RegisteredTool(definition=definition, handler=registered.handler))
            return result
        return _DefinitionOverlayRegistry(self, definitions)

    def merge(self, *others: ToolRegistry) -> ToolRegistry:
        """Merge registries by fully-qualified tool name.

        The dedup key is ``tool.definition.name``. Duplicates from the same
        source are treated as configuration errors. Cross-source duplicates are
        resolved by ``SOURCE_PRIORITIES``.
        """

        merged = ToolRegistry()
        for registry in (self, *others):
            for tool in registry.items():
                current = merged.get(tool.definition.name)
                if current is None:
                    merged.register(tool)
                    continue
                current_source = current.definition.source.type
                next_source = tool.definition.source.type
                if current_source == next_source:
                    raise ValueError(
                        f"Duplicate tool name from the same source: {tool.definition.name}"
                    )
                if _source_priority(next_source) > _source_priority(current_source):
                    logger.info(
                        "Tool registration shadowed by higher priority source",
                        extra={
                            "extra_data": {
                                "tool_name": tool.definition.name,
                                "preferred_source": next_source,
                            }
                        },
                    )
                    merged._tools[tool.definition.name] = tool
                    continue
                logger.info(
                    "Tool registration shadowed by existing source",
                    extra={
                        "extra_data": {
                            "tool_name": tool.definition.name,
                            "preferred_source": current_source,
                        }
                    },
                )
        return merged


class _DefinitionOverlayRegistry(ToolRegistry):
    """Fresh read-only view; never memoize definitions, handlers or permissions."""

    def __init__(self, base: ToolRegistry, definitions: dict[str, ToolDefinition]) -> None:
        self._base = base
        self._definitions = definitions

    def register(self, tool: RegisteredTool) -> None:
        raise TypeError("A classified registry view is read-only")

    def register_many(self, tools: list[RegisteredTool]) -> None:
        raise TypeError("A classified registry view is read-only")

    def _effective(self, registered: RegisteredTool) -> RegisteredTool:
        original = registered.definition
        definition = self._definitions.get(stable_tool_id(original), original)
        if definition.name != original.name or definition.source != original.source:
            raise ValueError("Tool identity changed after classified registry view creation")
        self._validate_definition(definition)
        if definition is original:
            return registered
        return RegisteredTool(definition=definition, handler=registered.handler)

    def get(self, tool_name: str) -> RegisteredTool | None:
        registered = self._base.get(tool_name)
        if registered is None:
            return None
        if registered.definition.name != tool_name:
            raise ValueError("Tool name changed after registration")
        return self._effective(registered)

    def items(self) -> list[RegisteredTool]:
        return [self._effective(registered) for registered in self._base.items()]

    def list_tools(self) -> list[ToolDefinition]:
        return [registered.definition for registered in self.items()]

    def export(self) -> list[dict[str, Any]]:
        return [definition.model_dump(mode="json") for definition in self.list_tools()]


def _source_priority(source_type: str) -> int:
    return SOURCE_PRIORITIES.get(source_type, 0)
