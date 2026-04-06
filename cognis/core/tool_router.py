"""Tool routing logic for orchestration, Intaris MCP, and local executors."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

from prometheus_client import Counter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.truncation import middle_truncate
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import SessionModel
from cognis.models.tool import Permission, ToolCall, ToolResult, stable_tool_id
from cognis.store.queries import get_setting_value
from cognis.tools.builtin.image import handle_image_tool, is_image_tool
from cognis.tools.builtin.memory import handle_memory_tool, is_memory_tool
from cognis.tools.builtin.orchestration import handle_delegate_tool_call, is_orchestration_tool
from cognis.tools.builtin.skill_management import (
    handle_skill_management_tool,
    is_skill_management_tool,
)
from cognis.tools.builtin.tool_output import handle_tool_output_tool, is_tool_output_tool
from cognis.tools.registry import ToolRegistry

TOOL_ROUTE_DECISIONS = Counter(
    "cognis_tool_route_decisions_total",
    "Tool route decisions",
    labelnames=("route",),
)
TOOL_ROUTE_OUTCOMES = Counter(
    "cognis_tool_route_outcomes_total",
    "Tool route outcomes",
    labelnames=("route", "outcome"),
)
IMAGE_GENERATION_TOTAL = Counter(
    "cognis_image_generation_total",
    "Image generation operations",
    labelnames=("model", "status"),
)

logger = get_logger(__name__)


class ToolRoute(StrEnum):
    """Tool routing categories."""

    ORCHESTRATION = "orchestration"
    MEMORY = "memory"
    TOOL_OUTPUT = "tool_output"
    IMAGE = "image"
    SKILL_MANAGEMENT = "skill_management"
    INTARIS_MCP = "intaris_mcp"
    LOCAL = "local"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class PermissionDecision:
    """Resolved permission for a tool call."""

    decision: str
    reasoning: str | None = None
    source: str | None = None
    risk: str | None = None
    path: str | None = None
    latency_ms: int = 0
    call_id: str | None = None  # Intaris evaluation call_id (for escalation tracking)


class ToolRouter:
    """Classify, evaluate, execute, and sanitize tool calls."""

    def __init__(
        self,
        guardrails: Any,
        non_bypassable_patterns: list[str] | None = None,
        memory: Any | None = None,
        tool_output_store: Any | None = None,
        image_generation_provider: Any | None = None,
        artifact_store: Any | None = None,
        session_factory: Any | None = None,
    ) -> None:
        self.guardrails = guardrails
        self.memory = memory
        self.tool_output_store = tool_output_store
        self.image_generation_provider = image_generation_provider
        self.artifact_store = artifact_store
        self._session_factory = session_factory
        self.non_bypassable_patterns = non_bypassable_patterns or []

    @classmethod
    async def from_session_factory(
        cls,
        guardrails: Any,
        session_factory: async_sessionmaker[AsyncSession],
        memory: Any | None = None,
        tool_output_store: Any | None = None,
        image_generation_provider: Any | None = None,
        artifact_store: Any | None = None,
    ) -> ToolRouter:
        """Create a router with cached non-bypassable patterns from settings."""

        async with session_factory() as session:
            patterns = await get_setting_value(session, "security.non_bypassable_tools", [])
        return cls(
            guardrails=guardrails,
            non_bypassable_patterns=_coerce_patterns(patterns),
            memory=memory,
            tool_output_store=tool_output_store,
            image_generation_provider=image_generation_provider,
            artifact_store=artifact_store,
            session_factory=session_factory,
        )

    def classify(self, tool_name: str, registry: ToolRegistry) -> ToolRoute:
        """Classify a tool call by route category."""

        if is_orchestration_tool(tool_name):
            return ToolRoute.ORCHESTRATION
        if is_memory_tool(tool_name):
            return ToolRoute.MEMORY
        if is_tool_output_tool(tool_name):
            return ToolRoute.TOOL_OUTPUT
        if is_image_tool(tool_name):
            return ToolRoute.IMAGE
        if is_skill_management_tool(tool_name):
            return ToolRoute.SKILL_MANAGEMENT
        registered_tool = registry.get(tool_name)
        if registered_tool is None:
            return ToolRoute.UNKNOWN
        if registered_tool.definition.source.type == "intaris_mcp":
            return ToolRoute.INTARIS_MCP
        return ToolRoute.LOCAL

    async def evaluate_tool_call(
        self,
        tool_call: ToolCall,
        agent: AgentDefinition,
        session: SessionModel,
        registry: ToolRegistry,
    ) -> PermissionDecision:
        """Resolve whether a local tool call may execute."""

        registered_tool = registry.get(tool_call.name)
        if registered_tool is None:
            return PermissionDecision(decision="deny", reasoning="Unknown tool", source="registry")
        if self._is_non_bypassable(
            registered_tool.definition.name, registered_tool.definition.non_bypassable
        ):
            evaluation = await self.guardrails.evaluate(
                session_id=_guardrails_session_id(session),
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                context={},
            )
            return PermissionDecision(
                decision=evaluation.decision,
                reasoning=evaluation.reasoning,
                source="guardrails",
                risk=evaluation.risk,
                path=evaluation.path,
                latency_ms=evaluation.latency_ms,
                call_id=evaluation.call_id,
            )

        permission = Permission.EVALUATE
        if agent.permissions is not None:
            permission = agent.permissions.resolve_permission(
                tool_call.name,
                tool_id=stable_tool_id(registered_tool.definition),
            )
        if permission is Permission.DENY:
            return PermissionDecision(
                decision="deny", reasoning="Tool denied by agent policy", source="agent"
            )
        if permission is Permission.ALLOW:
            return PermissionDecision(decision="approve", source="agent")

        evaluation = await self.guardrails.evaluate(
            session_id=_guardrails_session_id(session),
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            context={},
        )
        return PermissionDecision(
            decision=evaluation.decision,
            reasoning=evaluation.reasoning,
            source="guardrails",
            risk=evaluation.risk,
            path=evaluation.path,
            latency_ms=evaluation.latency_ms,
            call_id=evaluation.call_id,
        )

    async def execute(
        self,
        tool_call: ToolCall,
        session: SessionModel,
        agent: AgentDefinition,
        registry: ToolRegistry,
        executor: Any,
    ) -> ToolResult:
        """Execute a tool call using the appropriate route."""

        cid = tool_call.call_id
        route = self.classify(tool_call.name, registry)
        registered_tool = registry.get(tool_call.name)
        tool_id = (
            stable_tool_id(registered_tool.definition) if registered_tool is not None else None
        )
        logger.debug(
            "Tool route selected",
            extra={
                "extra_data": {
                    "call_id": cid,
                    "tool_name": tool_call.name,
                    "tool_id": tool_id,
                    "route": str(route),
                    "source_type": (
                        registered_tool.definition.source.type
                        if registered_tool is not None
                        else None
                    ),
                }
            },
        )
        TOOL_ROUTE_DECISIONS.labels(route=str(route)).inc()
        if route is ToolRoute.UNKNOWN:
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="unknown").inc()
            return self._sanitize_result(
                tool_call.name,
                ToolResult(output="Unknown tool.", is_error=True),
                50_000,
                call_id=cid,
            )
        if route is ToolRoute.ORCHESTRATION:
            result, _child = await handle_delegate_tool_call(tool_call)
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="success").inc()
            return self._sanitize_result(tool_call.name, result, 50_000, call_id=cid)
        if route is ToolRoute.MEMORY:
            if self.memory is None:
                result = ToolResult(output="Memory provider not available.", is_error=True)
            else:
                from cognis.runtime_context import current_user_email

                result = await handle_memory_tool(
                    tool_name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    memory_provider=self.memory,
                    agent_id=agent.agent_id if agent else None,
                    user_email=current_user_email.get(),
                )
            outcome = "success" if not result.is_error else "failure"
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(tool_call.name, result, 50_000, call_id=cid)
        if route is ToolRoute.TOOL_OUTPUT:
            if self.tool_output_store is None:
                result = ToolResult(output="Tool output store not available.", is_error=True)
            else:
                result = await handle_tool_output_tool(
                    tool_name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    store=self.tool_output_store,
                )
            outcome = "success" if not result.is_error else "failure"
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(tool_call.name, result, 50_000, call_id=cid)
        if route is ToolRoute.IMAGE:
            if self.image_generation_provider is None:
                result = ToolResult(
                    output="Image generation not available. No image generation model configured.",
                    is_error=True,
                )
            else:
                result = await handle_image_tool(
                    tool_name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    image_generation_provider=self.image_generation_provider,
                    artifact_store=self.artifact_store,
                )
            outcome = "success" if not result.is_error else "failure"
            IMAGE_GENERATION_TOTAL.labels(
                model=tool_call.arguments.get("model", "default"),
                status=outcome,
            ).inc()
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(tool_call.name, result, 100_000, call_id=cid)
        if route is ToolRoute.SKILL_MANAGEMENT:
            # Skill management mutations go through guardrails evaluation
            # (non-bypassable tools are always evaluated).
            decision = await self.evaluate_tool_call(tool_call, agent, session, registry)
            eval_meta: dict[str, Any] = {
                "decision": decision.decision,
                "reasoning": decision.reasoning,
                "source": decision.source,
                "risk": decision.risk,
                "path": decision.path,
                "latency_ms": decision.latency_ms,
                "call_id": decision.call_id,
            }
            if decision.decision == "deny":
                TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="denied").inc()
                return self._sanitize_result(
                    tool_call.name,
                    ToolResult(
                        output=decision.reasoning or "Skill operation denied.",
                        is_error=True,
                        metadata={"evaluation": eval_meta},
                    ),
                    50_000,
                    call_id=cid,
                )
            if decision.decision == "escalate":
                TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="escalated").inc()
                return self._sanitize_result(
                    tool_call.name,
                    ToolResult(
                        output=decision.reasoning or "Skill operation requires user approval.",
                        is_error=True,
                        metadata={"evaluation": eval_meta},
                    ),
                    50_000,
                    call_id=cid,
                )
            if self._session_factory is None:
                result = ToolResult(output="Skill management not available.", is_error=True)
            else:
                from cognis.runtime_context import current_user_email

                result = await handle_skill_management_tool(
                    tool_name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    session_factory=self._session_factory,
                    user_email=current_user_email.get(),
                )
            if result.metadata is None:
                result = result.model_copy(update={"metadata": {"evaluation": eval_meta}})
            else:
                result = result.model_copy(
                    update={"metadata": {**result.metadata, "evaluation": eval_meta}}
                )
            outcome = "success" if not result.is_error else "failure"
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(tool_call.name, result, 50_000, call_id=cid)
        if route is ToolRoute.INTARIS_MCP:
            result = await self._call_intaris_mcp(tool_call, session, registry)
            TOOL_ROUTE_OUTCOMES.labels(
                route=str(route), outcome="success" if not result.is_error else "failure"
            ).inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                _tool_max_size(registry, tool_call.name),
                call_id=cid,
            )

        decision = await self.evaluate_tool_call(tool_call, agent, session, registry)
        eval_meta: dict[str, Any] = {
            "decision": decision.decision,
            "reasoning": decision.reasoning,
            "source": decision.source,
            "risk": decision.risk,
            "path": decision.path,
            "latency_ms": decision.latency_ms,
            "call_id": decision.call_id,
        }
        if decision.decision == "deny":
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="denied").inc()
            denied_result = ToolResult(
                output=decision.reasoning or "Tool execution denied.",
                is_error=True,
                metadata={"evaluation": eval_meta},
            )
            return self._sanitize_result(
                tool_call.name,
                denied_result,
                _tool_max_size(registry, tool_call.name),
                call_id=cid,
            )
        if decision.decision == "escalate":
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="escalated").inc()
            escalated_result = ToolResult(
                output=decision.reasoning or "Tool requires user approval.",
                is_error=True,
                metadata={"evaluation": eval_meta},
            )
            return self._sanitize_result(
                tool_call.name,
                escalated_result,
                _tool_max_size(registry, tool_call.name),
                call_id=cid,
            )

        registered_tool = registry.get(tool_call.name)
        if registered_tool is None:
            return self._sanitize_result(
                tool_call.name, ToolResult(output="Unknown tool.", is_error=True), 50_000
            )
        try:
            result = await asyncio.wait_for(
                executor.tool_execute(
                    tool_call, timeout_seconds=registered_tool.definition.timeout_seconds
                ),
                timeout=registered_tool.definition.timeout_seconds,
            )
        except TimeoutError:
            await executor.cancel_call(tool_call.call_id)
            result = ToolResult(output="Tool execution timed out.", is_error=True)
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="timeout").inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                registered_tool.definition.max_result_size,
                call_id=cid,
            )
        # Attach evaluation metadata to the result
        if result.metadata is None:
            result = result.model_copy(update={"metadata": {"evaluation": eval_meta}})
        else:
            result = result.model_copy(
                update={"metadata": {**result.metadata, "evaluation": eval_meta}}
            )
        TOOL_ROUTE_OUTCOMES.labels(
            route=str(route), outcome="success" if not result.is_error else "failure"
        ).inc()
        return self._sanitize_result(
            tool_call.name,
            result,
            registered_tool.definition.max_result_size,
            call_id=cid,
        )

    def _is_non_bypassable(self, tool_name: str, explicit_flag: bool) -> bool:
        if explicit_flag:
            return True
        return any(fnmatchcase(tool_name, pattern) for pattern in self.non_bypassable_patterns)

    async def _call_intaris_mcp(
        self,
        tool_call: ToolCall,
        session: SessionModel,
        registry: ToolRegistry,
    ) -> ToolResult:
        registered_tool = registry.get(tool_call.name)
        source = registered_tool.definition.source if registered_tool is not None else None
        if registered_tool is None or source is None or source.server_name is None:
            return ToolResult(output="Unknown Intaris MCP tool.", is_error=True)
        raw_tool_name = source.raw_tool_name or tool_call.name
        result = await self.guardrails.call_mcp_tool(
            session_id=_guardrails_session_id(session),
            server_name=source.server_name,
            tool_name=raw_tool_name,
            arguments=tool_call.arguments,
        )
        if isinstance(result, ToolResult):
            return result
        return ToolResult.model_validate(result)

    def _sanitize_result(
        self,
        tool_name: str,
        result: ToolResult,
        max_size: int,
        *,
        call_id: str | None = None,
    ) -> ToolResult:
        raw_output = result.output
        output, was_truncated = middle_truncate(raw_output, max_size, call_id=call_id)
        wrapped = f'<tool_result name="{tool_name}" trust="untrusted">\n{output}\n</tool_result>'
        metadata = dict(result.metadata or {})
        metadata["wrapped"] = True
        metadata["truncated"] = was_truncated
        metadata["original_size"] = len(raw_output)
        # Preserve raw output for the ToolOutputStore (before wrapping/truncation).
        # The agent loop reads this to save the full output to disk.
        metadata["_raw_output"] = raw_output
        return result.model_copy(update={"output": wrapped, "metadata": metadata})


def _guardrails_session_id(session: SessionModel) -> str:
    return session.intaris_session_id or session.session_id


def _coerce_patterns(value: object) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return []


def _tool_max_size(registry: ToolRegistry, tool_name: str) -> int:
    registered_tool = registry.get(tool_name)
    if registered_tool is None:
        return 50_000
    return registered_tool.definition.max_result_size
