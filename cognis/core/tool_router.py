"""Tool routing logic for orchestration, Intaris MCP, and local executors."""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatchcase
from time import monotonic, perf_counter
from typing import Any
from urllib.parse import urlparse

from prometheus_client import Counter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.artifacts.store import sanitize_artifact_filename
from cognis.core.tool_arguments import validate_tool_arguments
from cognis.core.truncation import middle_truncate
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.credential import CredentialAccessError, CredentialResolution
from cognis.models.session import SessionModel
from cognis.models.tool import ExecutorHandle, Permission, ToolCall, ToolResult, stable_tool_id
from cognis.runtime_context import RuntimeAccessContext
from cognis.store.queries import create_artifact_record, get_artifact_record, get_setting_value
from cognis.tools.argument_normalization import strip_empty_optional_values
from cognis.tools.builtin.agent_management import (
    handle_agent_management_tool,
    is_agent_management_tool,
)
from cognis.tools.builtin.artifact_tools import (
    analyze_attachment_ref,
    handle_artifact_tool,
    is_artifact_tool,
)
from cognis.tools.builtin.image import handle_image_tool, is_image_tool
from cognis.tools.builtin.memory import handle_memory_tool, is_memory_tool
from cognis.tools.builtin.orchestration import handle_delegate_tool_call, is_orchestration_tool
from cognis.tools.builtin.schedule import handle_schedule_tool, is_schedule_tool
from cognis.tools.builtin.skill_management import (
    handle_skill_management_tool,
    is_skill_management_tool,
)
from cognis.tools.builtin.tool_output import handle_tool_output_tool, is_tool_output_tool
from cognis.tools.registry import ToolExecutionContext, ToolRegistry

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
TOOL_DECISION_CACHE_HITS = Counter(
    "cognis_tool_decision_cache_hits_total",
    "Short-lived local Intaris decision-cache hits",
    labelnames=("decision",),
)
IMAGE_GENERATION_TOTAL = Counter(
    "cognis_image_generation_total",
    "Image generation operations",
    labelnames=("model", "status"),
)

logger = get_logger(__name__)

_AUTH_STATE_KIND_HINT = (
    "Use browser_fill value_ref for raw credential fields; use auth_state_ref only "
    "for browser_storage_state credentials."
)


class ToolRoute(StrEnum):
    """Tool routing categories."""

    ORCHESTRATION = "orchestration"
    MEMORY = "memory"
    TOOL_OUTPUT = "tool_output"
    ARTIFACT = "artifact"
    IMAGE = "image"
    AGENT_MANAGEMENT = "agent_management"
    SKILL_MANAGEMENT = "skill_management"
    SCHEDULE = "schedule"
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
        llm: Any | None = None,
        memory: Any | None = None,
        credentials_provider: Any | None = None,
        tool_output_store: Any | None = None,
        image_generation_provider: Any | None = None,
        artifact_store: Any | None = None,
        session_factory: Any | None = None,
        notification_service: Any | None = None,
        pause_waiter: Any | None = None,
        event_bus: Any | None = None,
        task_queue: Any | None = None,
    ) -> None:
        self.guardrails = guardrails
        self.llm = llm
        self.memory = memory
        self.credentials_provider = credentials_provider
        self.tool_output_store = tool_output_store
        self.image_generation_provider = image_generation_provider
        self.artifact_store = artifact_store
        self._session_factory = session_factory
        self.notification_service = notification_service
        self.pause_waiter = pause_waiter
        self.event_bus = event_bus
        self._task_queue = task_queue
        self._scheduler: Any | None = None
        self.non_bypassable_patterns = non_bypassable_patterns or []
        self._decision_cache_ttl_seconds = 15.0
        self._decision_cache: dict[tuple[str, str, str], tuple[float, PermissionDecision]] = {}

    @classmethod
    async def from_session_factory(
        cls,
        guardrails: Any,
        session_factory: async_sessionmaker[AsyncSession],
        llm: Any | None = None,
        memory: Any | None = None,
        credentials_provider: Any | None = None,
        tool_output_store: Any | None = None,
        image_generation_provider: Any | None = None,
        artifact_store: Any | None = None,
        notification_service: Any | None = None,
        pause_waiter: Any | None = None,
        event_bus: Any | None = None,
        task_queue: Any | None = None,
    ) -> ToolRouter:
        """Create a router with cached non-bypassable patterns from settings."""

        async with session_factory() as session:
            patterns = await get_setting_value(session, "security.non_bypassable_tools", [])
        return cls(
            guardrails=guardrails,
            llm=llm,
            non_bypassable_patterns=_coerce_patterns(patterns),
            memory=memory,
            credentials_provider=credentials_provider,
            tool_output_store=tool_output_store,
            image_generation_provider=image_generation_provider,
            artifact_store=artifact_store,
            session_factory=session_factory,
            notification_service=notification_service,
            pause_waiter=pause_waiter,
            event_bus=event_bus,
            task_queue=task_queue,
        )

    def classify(self, tool_name: str, registry: ToolRegistry) -> ToolRoute:
        """Classify a tool call by route category."""

        if is_orchestration_tool(tool_name):
            return ToolRoute.ORCHESTRATION
        if is_memory_tool(tool_name):
            return ToolRoute.MEMORY
        if is_tool_output_tool(tool_name):
            return ToolRoute.TOOL_OUTPUT
        if is_artifact_tool(tool_name):
            return ToolRoute.ARTIFACT
        if is_image_tool(tool_name):
            return ToolRoute.IMAGE
        if is_agent_management_tool(tool_name):
            registered_tool = registry.get(tool_name)
            if registered_tool is not None and registered_tool.definition.source.type == "builtin":
                return ToolRoute.AGENT_MANAGEMENT
            return ToolRoute.UNKNOWN
        if is_skill_management_tool(tool_name):
            return ToolRoute.SKILL_MANAGEMENT
        if is_schedule_tool(tool_name):
            return ToolRoute.SCHEDULE
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

        cached = self._get_cached_decision(
            session.session_id,
            tool_call.name,
            tool_call.arguments,
            registered_tool.definition.read_only,
        )
        if cached is not None:
            TOOL_DECISION_CACHE_HITS.labels(decision=cached.decision).inc()
            return cached

        evaluation = await self.guardrails.evaluate(
            session_id=_guardrails_session_id(session),
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            context={},
        )
        decision_result = PermissionDecision(
            decision=evaluation.decision,
            reasoning=evaluation.reasoning,
            source="guardrails",
            risk=evaluation.risk,
            path=evaluation.path,
            latency_ms=evaluation.latency_ms,
            call_id=evaluation.call_id,
        )
        self._cache_decision(
            session.session_id,
            tool_call.name,
            tool_call.arguments,
            registered_tool.definition.read_only,
            decision_result,
        )
        return decision_result

    def _get_cached_decision(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        read_only: bool,
    ) -> PermissionDecision | None:
        if not read_only:
            return None
        self._purge_stale_decision_cache()
        entry = self._decision_cache.get(
            self._decision_cache_key(session_id, tool_name, arguments, read_only=read_only)
        )
        if entry is None:
            return None
        expires_at, decision = entry
        if expires_at <= monotonic():
            return None
        return PermissionDecision(
            decision=decision.decision,
            reasoning=decision.reasoning,
            source="guardrails_cache",
            risk=decision.risk,
            path=decision.path,
            latency_ms=0,
            call_id=None,
        )

    def _cache_decision(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        read_only: bool,
        decision: PermissionDecision,
    ) -> None:
        if not read_only or decision.decision != "approve":
            return
        self._decision_cache[
            self._decision_cache_key(session_id, tool_name, arguments, read_only=read_only)
        ] = (
            monotonic() + self._decision_cache_ttl_seconds,
            decision,
        )

    def _purge_stale_decision_cache(self) -> None:
        now = monotonic()
        stale_keys = [
            key
            for key, (expires_at, _decision) in self._decision_cache.items()
            if expires_at <= now
        ]
        for key in stale_keys:
            self._decision_cache.pop(key, None)

    @staticmethod
    def _decision_cache_key(
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        read_only: bool = False,
    ) -> tuple[str, str, str]:
        # Read-only tool decisions are independent of arguments — Intaris
        # classifies a read regardless of the file path. Bucketing by tool
        # name lets a session's first read warm the cache for every
        # subsequent read of the same tool. Non-read-only callers retain
        # the per-argument key so write/destructive paths cannot share
        # cached approvals across distinct payloads.
        if read_only:
            return session_id, tool_name, "*"
        payload = json.dumps(arguments, sort_keys=True, default=str, separators=(",", ":"))
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]  # noqa: S324
        return session_id, tool_name, digest

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
                runtime_metadata=tool_call.runtime_metadata,
            )
        if route is ToolRoute.ORCHESTRATION:
            result, _child = await handle_delegate_tool_call(tool_call)
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="success").inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                50_000,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
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
            return self._sanitize_result(
                tool_call.name,
                result,
                50_000,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
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
            return self._sanitize_result(
                tool_call.name,
                result,
                50_000,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
        if route is ToolRoute.ARTIFACT:
            result = await handle_artifact_tool(
                tool_name=tool_call.name,
                arguments=dict(tool_call.arguments),
                llm=self.llm,
                artifact_store=self.artifact_store,
                session_factory=self._session_factory,
                user_email=session.user_email,
                current_model=(
                    str(tool_call.runtime_metadata.get("resolved_model"))
                    if isinstance(tool_call.runtime_metadata.get("resolved_model"), str)
                    else None
                ),
                current_provider_id=(
                    str(tool_call.runtime_metadata.get("resolved_provider_id"))
                    if isinstance(tool_call.runtime_metadata.get("resolved_provider_id"), str)
                    else None
                ),
            )
            outcome = "success" if not result.is_error else "failure"
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                100_000,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
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
                    session_factory=self._session_factory,
                )
                if result.attachments:
                    result = result.model_copy(
                        update={
                            "output": self._enrich_attachment_output(
                                result.output,
                                result.attachments,
                            )
                        }
                    )
            outcome = "success" if not result.is_error else "failure"
            IMAGE_GENERATION_TOTAL.labels(
                model=tool_call.arguments.get("model", "default"),
                status=outcome,
            ).inc()
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                100_000,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
        if route is ToolRoute.AGENT_MANAGEMENT:
            agent_tools = agent.tools if isinstance(agent.tools, dict) else {}
            opt_in_tools = agent_tools.get("opt_in_builtin_tools")
            if not isinstance(opt_in_tools, list) or tool_call.name not in opt_in_tools:
                TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="denied").inc()
                return self._sanitize_result(
                    tool_call.name,
                    ToolResult(
                        output="Agent management is not explicitly enabled for this agent.",
                        is_error=True,
                        metadata={"code": "agent_management_not_enabled"},
                    ),
                    50_000,
                    call_id=cid,
                    runtime_metadata=tool_call.runtime_metadata,
                )
            registered_tool = registry.get(tool_call.name)
            validation_error = validate_tool_arguments(
                tool_call.name,
                tool_call.arguments,
                schema=registered_tool.definition.parameters if registered_tool else None,
            )
            if validation_error is not None:
                TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="denied").inc()
                return self._sanitize_result(
                    tool_call.name,
                    ToolResult(
                        output=json.dumps(validation_error.as_tool_result()),
                        is_error=True,
                        metadata={"code": "invalid_tool_arguments"},
                    ),
                    50_000,
                    call_id=cid,
                    runtime_metadata=tool_call.runtime_metadata,
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
            if decision.decision in {"deny", "escalate"}:
                outcome = "denied" if decision.decision == "deny" else "escalated"
                TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
                return self._sanitize_result(
                    tool_call.name,
                    ToolResult(
                        output=decision.reasoning or "Agent management requires explicit approval.",
                        is_error=True,
                        metadata={"evaluation": eval_meta},
                    ),
                    50_000,
                    call_id=cid,
                    runtime_metadata=tool_call.runtime_metadata,
                )
            from cognis.core.agent_management import AgentManagementDependencies
            from cognis.runtime_context import current_agent_id, current_user_email

            if self._session_factory is None:
                result = ToolResult(output="Agent management not available.", is_error=True)
            else:
                result = await handle_agent_management_tool(
                    tool_name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    deps=AgentManagementDependencies(
                        session_factory=self._session_factory,
                        memory=self.memory,
                        event_bus=getattr(self, "event_bus", None),
                        artifact_store=self.artifact_store,
                        image_generation_provider=self.image_generation_provider,
                        llm=self.llm,
                        task_queue=getattr(self, "_task_queue", None),
                    ),
                    user_email=current_user_email.get() or session.user_email,
                    current_agent_id=current_agent_id.get() or agent.agent_id,
                    runtime_access=self._runtime_access_from_tool_call(tool_call),
                )
            combined_meta: dict[str, Any] = {"evaluation": eval_meta}
            if result.metadata is not None:
                combined_meta.update(result.metadata)
            result = result.model_copy(update={"metadata": combined_meta})
            outcome = "success" if not result.is_error else "failure"
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                100_000,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
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
                    runtime_metadata=tool_call.runtime_metadata,
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
                    runtime_metadata=tool_call.runtime_metadata,
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
                    llm=self.llm,
                    artifact_store=self.artifact_store,
                )
            # Signal same-turn refresh for mutation tools so the agent loop
            # can re-resolve skills before the next model call.
            _SKILL_MUTATION_TOOLS = {
                "skill_write",
                "skill_delete",
                "skill_import_url",
                "skill_restore_version",
                "skill_asset_write",
                "skill_asset_delete",
            }
            needs_refresh = tool_call.name in _SKILL_MUTATION_TOOLS and not result.is_error
            combined_meta: dict[str, Any] = {"evaluation": eval_meta}
            if needs_refresh:
                combined_meta["skill_epoch_stale"] = True
            if result.metadata is not None:
                combined_meta.update(result.metadata)
            result = result.model_copy(update={"metadata": combined_meta})
            outcome = "success" if not result.is_error else "failure"
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                50_000,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
        if route is ToolRoute.SCHEDULE:
            if self._session_factory is None:
                result = ToolResult(output="Schedule management not available.", is_error=True)
            else:
                from cognis.runtime_context import current_user_email

                result = await handle_schedule_tool(
                    tool_name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    session_factory=self._session_factory,
                    scheduler=self._scheduler,
                    user_email=current_user_email.get(),
                    agent_id=agent.agent_id if agent else None,
                )
            outcome = "success" if not result.is_error else "failure"
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                50_000,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
        if route is ToolRoute.INTARIS_MCP:
            result = await self._call_intaris_mcp(tool_call, session, registry)
            result = await self._materialize_inline_attachments(result, session, tool_call.name)
            if (
                result.is_error
                and result.metadata is not None
                and result.metadata.get("decision") == "escalate"
                and result.metadata.get("call_id")
            ):
                eval_meta: dict[str, Any] = {
                    "decision": "escalate",
                    "reasoning": result.metadata.get("reasoning"),
                    "source": "guardrails",
                    "risk": result.metadata.get("risk"),
                    "path": result.metadata.get("path"),
                    "latency_ms": result.metadata.get("latency_ms", 0),
                    "call_id": result.metadata["call_id"],
                }
                result = result.model_copy(
                    update={"metadata": {**result.metadata, "evaluation": eval_meta}}
                )
                outcome = "escalated"
            else:
                outcome = "success" if not result.is_error else "failure"
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome=outcome).inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                _tool_max_size(registry, tool_call.name),
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )

        guardrails_tool_call = tool_call
        try:
            if route is ToolRoute.LOCAL:
                guardrails_tool_call = await self._prepare_guardrails_tool_call(
                    tool_call, session, agent
                )
        except CredentialAccessError as exc:
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="failure").inc()
            return self._sanitize_result(
                tool_call.name,
                self._credential_error_result(exc),
                _tool_max_size(registry, tool_call.name),
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )

        decision = await self.evaluate_tool_call(guardrails_tool_call, agent, session, registry)
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
                runtime_metadata=tool_call.runtime_metadata,
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
                runtime_metadata=tool_call.runtime_metadata,
            )

        try:
            if route is ToolRoute.LOCAL:
                tool_call = await self._prepare_local_tool_call(tool_call, session, agent)
        except CredentialAccessError as exc:
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="failure").inc()
            return self._sanitize_result(
                tool_call.name,
                self._credential_error_result(exc),
                _tool_max_size(registry, tool_call.name),
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )

        registered_tool = registry.get(tool_call.name)
        if registered_tool is None:
            return self._sanitize_result(
                tool_call.name,
                ToolResult(output="Unknown tool.", is_error=True),
                50_000,
                runtime_metadata=tool_call.runtime_metadata,
            )
        scoped_tool_call = tool_call.model_copy(update={"execution_scope_id": session.session_id})

        if registered_tool.handler is not None:
            try:
                result = await self._execute_local_handler(
                    scoped_tool_call,
                    registered_tool=registered_tool,
                    executor=executor,
                )
                result = await self._persist_browser_auth_state_if_needed(result, session, agent)
            except CredentialAccessError as exc:
                result = self._credential_error_result(exc)
            result = await self._materialize_inline_attachments(result, session, tool_call.name)
            result = await self._postprocess_tool_result(result, scoped_tool_call, session)
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
                runtime_metadata=tool_call.runtime_metadata,
            )
        try:
            result = await asyncio.wait_for(
                executor.tool_execute(
                    scoped_tool_call, timeout_seconds=registered_tool.definition.timeout_seconds
                ),
                timeout=registered_tool.definition.timeout_seconds,
            )
            result = await self._persist_browser_auth_state_if_needed(result, session, agent)
            result = await self._materialize_inline_attachments(result, session, tool_call.name)
            result = await self._postprocess_tool_result(result, scoped_tool_call, session)
        except CredentialAccessError as exc:
            result = self._credential_error_result(exc)
        except TimeoutError:
            await executor.cancel_call(tool_call.call_id)
            result = ToolResult(output="Tool execution timed out.", is_error=True)
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="timeout").inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                registered_tool.definition.max_result_size,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
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
            runtime_metadata=tool_call.runtime_metadata,
        )

    async def _postprocess_tool_result(
        self,
        result: ToolResult,
        tool_call: ToolCall,
        session: SessionModel,
    ) -> ToolResult:
        metadata = result.metadata if isinstance(result.metadata, dict) else None
        if tool_call.name != "read" or metadata is None:
            return result
        request = metadata.get("attachment_analysis_request")
        if not isinstance(request, dict):
            return result
        attachments = result.attachments or []
        if not attachments:
            return ToolResult(
                output="Binary file was prepared for analysis, but no attachment was persisted.",
                is_error=True,
                metadata={k: v for k, v in metadata.items() if k != "attachment_analysis_request"},
            )
        first = attachments[0]
        try:
            attachment = AttachmentRef(
                artifact_id=str(first.get("artifact_id") or ""),
                kind=ArtifactKind(str(first.get("kind") or ArtifactKind.FILE.value)),
                mime_type=str(first.get("mime_type") or "application/octet-stream"),
                filename=str(first.get("filename") or "attachment"),
                size_bytes=int(first.get("size_bytes") or 0),
                url=str(first.get("url")) if isinstance(first.get("url"), str) else None,
            )
        except Exception as exc:
            return ToolResult(
                output=f"Binary file analysis setup failed: {exc}",
                is_error=True,
                metadata={k: v for k, v in metadata.items() if k != "attachment_analysis_request"},
            )
        try:
            content, _content_type, _filename = await self._load_binary_artifact(
                attachment.artifact_id,
                session.user_email,
            )
        except Exception as exc:
            return ToolResult(
                output=f"Failed to load binary artifact for analysis: {exc}",
                is_error=True,
                metadata={k: v for k, v in metadata.items() if k != "attachment_analysis_request"},
            )
        analysis = await analyze_attachment_ref(
            attachment=attachment,
            content=content,
            prompt=(str(request.get("prompt")) if isinstance(request.get("prompt"), str) else None),
            llm=self.llm,
            artifact_store=self.artifact_store,
            session_factory=self._session_factory,
            current_model=(
                str(tool_call.runtime_metadata.get("resolved_model"))
                if isinstance(tool_call.runtime_metadata.get("resolved_model"), str)
                else None
            ),
            current_provider_id=(
                str(tool_call.runtime_metadata.get("resolved_provider_id"))
                if isinstance(tool_call.runtime_metadata.get("resolved_provider_id"), str)
                else None
            ),
        )
        next_metadata = {k: v for k, v in metadata.items() if k != "attachment_analysis_request"}
        if analysis.metadata:
            next_metadata.update(analysis.metadata)
        return analysis.model_copy(update={"metadata": next_metadata, "attachments": None})

    async def _execute_local_handler(
        self,
        tool_call: ToolCall,
        *,
        registered_tool: Any,
        executor: Any,
    ) -> ToolResult:
        handler = registered_tool.handler
        if handler is None:
            return ToolResult(output="Unknown tool handler.", is_error=True)
        start = perf_counter()
        executor_handle = self._executor_handle_for_local_tool(executor)
        from cognis.runtime_context import (
            current_effective_working_directory,
            current_workspace_root,
        )

        executor_metadata = getattr(executor, "runtime_metadata", None)
        runtime_metadata: dict[str, Any] = (
            dict(executor_metadata) if isinstance(executor_metadata, dict) else {}
        )
        runtime_metadata.update(tool_call.runtime_metadata)
        workspace_root = current_workspace_root.get()
        working_directory = current_effective_working_directory.get()
        if workspace_root:
            runtime_metadata["workspace_root"] = workspace_root
        if working_directory:
            runtime_metadata["working_directory"] = working_directory
        context = ToolExecutionContext(
            executor_handle=executor_handle,
            runtime_metadata=runtime_metadata,
            shared_runtime_metadata=(
                executor_metadata if isinstance(executor_metadata, dict) else None
            ),
            execution_scope_id=tool_call.execution_scope_id,
        )
        normalized_arguments = strip_empty_optional_values(
            tool_call.arguments,
            registered_tool.definition.parameters,
        )
        raw = await handler(normalized_arguments, context)
        duration_ms = int((perf_counter() - start) * 1000)
        return self._normalize_local_tool_result(raw, duration_ms)

    def _executor_handle_for_local_tool(self, executor: Any) -> ExecutorHandle:
        handle = getattr(executor, "handle", None)
        if isinstance(handle, ExecutorHandle):
            return handle
        executor_id = getattr(executor, "executor_id", "controller")
        executor_type = getattr(executor, "executor_type", "controller")
        return ExecutorHandle(executor_id=executor_id, executor_type=executor_type)

    def _normalize_local_tool_result(self, result: Any, duration_ms: int) -> ToolResult:
        if isinstance(result, ToolResult):
            return result.model_copy(update={"duration_ms": result.duration_ms or duration_ms})
        if isinstance(result, (dict, list)):
            output = json.dumps(result, sort_keys=True, default=str)
        elif isinstance(result, str):
            output = result
        else:
            output = str(result)
        return ToolResult(output=output, duration_ms=duration_ms)

    async def _prepare_guardrails_tool_call(
        self, tool_call: ToolCall, session: SessionModel, agent: AgentDefinition
    ) -> ToolCall:
        arguments = dict(tool_call.arguments)
        if tool_call.name == "artifact_save":
            arguments.pop("source_artifact_content_b64", None)
            if artifact_id := arguments.get("source_artifact_id"):
                row = await self._get_accessible_artifact_record(
                    str(artifact_id), session.user_email
                )
                arguments.setdefault("source_artifact_filename", row.filename)
                arguments.setdefault("source_artifact_mime_type", row.mime_type)
                arguments.setdefault("source_artifact_size_bytes", row.size_bytes)
        assets = arguments.get("assets") if tool_call.name == "document_generate" else None
        if isinstance(assets, list):
            sanitized_assets: list[dict[str, Any]] = []
            for raw in assets:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                item.pop("content_b64", None)
                if artifact_id := item.get("artifact_id"):
                    row = await self._get_accessible_artifact_record(
                        str(artifact_id), session.user_email
                    )
                    item.setdefault("filename", row.filename)
                    item.setdefault("mime_type", row.mime_type)
                    item.setdefault("size_bytes", row.size_bytes)
                sanitized_assets.append(item)
            arguments["assets"] = sanitized_assets
        arguments = self._sanitize_sensitive_refs_for_guardrails(arguments, root=True)
        return tool_call.model_copy(update={"arguments": arguments})

    async def _prepare_local_tool_call(
        self, tool_call: ToolCall, session: SessionModel, agent: AgentDefinition
    ) -> ToolCall:
        arguments = dict(tool_call.arguments)
        if tool_call.name == "artifact_save" and (
            artifact_id := arguments.get("source_artifact_id")
        ):
            content, mime_type, filename = await self._load_binary_artifact(
                str(artifact_id), session.user_email
            )
            arguments["source_artifact_content_b64"] = base64.b64encode(content).decode("ascii")
            arguments.setdefault("source_artifact_filename", filename)
            arguments.setdefault("source_artifact_mime_type", mime_type)
        if tool_call.name == "document_generate" and (
            artifact_id := arguments.get("source_artifact_id")
        ):
            arguments["source_artifact_content"] = await self._load_text_artifact(
                str(artifact_id), session.user_email
            )
        assets = arguments.get("assets") if tool_call.name == "document_generate" else None
        if isinstance(assets, list):
            resolved_assets: list[dict[str, Any]] = []
            for raw in assets:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                if artifact_id := item.get("artifact_id"):
                    content, mime_type, filename = await self._load_binary_artifact(
                        str(artifact_id), session.user_email
                    )
                    item.setdefault("content_b64", base64.b64encode(content).decode("ascii"))
                    item.setdefault("mime_type", mime_type)
                    item.setdefault("filename", filename)
                resolved_assets.append(item)
            arguments["assets"] = resolved_assets
        arguments = await self._resolve_sensitive_refs(arguments, session, agent, tool_call)
        return tool_call.model_copy(update={"arguments": arguments})

    def _sanitize_sensitive_refs_for_guardrails(self, value: Any, *, root: bool = False) -> Any:
        if isinstance(value, dict):
            if not root and isinstance(value.get("value_ref"), str) and value["value_ref"].strip():
                return "<resolved-at-execution>"
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                if key in {"auth_state", "value"} and (
                    isinstance(value.get("value_ref"), str)
                    or isinstance(value.get("auth_state_ref"), str)
                ):
                    sanitized[key] = "<resolved-at-execution>"
                elif key == "env" and isinstance(item, dict):
                    sanitized[key] = {
                        str(env_key): "<resolved-at-execution>"
                        if isinstance(env_value, str)
                        and (
                            env_value.startswith("$credential:")
                            or env_value.startswith("$auth_challenge:")
                        )
                        else self._sanitize_sensitive_refs_for_guardrails(env_value)
                        for env_key, env_value in item.items()
                    }
                else:
                    sanitized[key] = self._sanitize_sensitive_refs_for_guardrails(item)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize_sensitive_refs_for_guardrails(item) for item in value]
        if isinstance(value, str) and (
            value.startswith("$credential:") or value.startswith("$auth_challenge:")
        ):
            return "<resolved-at-execution>"
        return value

    async def _resolve_sensitive_refs(
        self,
        arguments: dict[str, Any],
        session: SessionModel,
        agent: AgentDefinition,
        tool_call: ToolCall,
    ) -> dict[str, Any]:
        resolved = dict(arguments)
        value_ref = resolved.get("value_ref")
        if isinstance(value_ref, str) and value_ref.strip():
            value = await self._resolve_value_ref(
                value_ref.strip(),
                session=session,
                agent=agent,
                tool_call=tool_call,
                challenge_payload=resolved.get("auth_challenge")
                if isinstance(resolved.get("auth_challenge"), dict)
                else None,
            )
            resolved["value"] = str(value)
        elif isinstance(value_ref, str):
            resolved.pop("value_ref", None)
        args = resolved.get("args")
        if tool_call.name == "browser_eval" and isinstance(args, list):
            resolved["args"] = [
                await self._resolve_nested_sensitive_refs(
                    item, session=session, agent=agent, tool_call=tool_call
                )
                for item in args
            ]
        auth_state_ref_raw = resolved.get("auth_state_ref")
        if isinstance(auth_state_ref_raw, str) and auth_state_ref_raw.strip():
            auth_state_ref = auth_state_ref_raw.strip()
            cred = await self._resolve_credential_value(auth_state_ref, session, agent)
            credential_id = auth_state_ref[len("$credential:") :].split(".", 1)[0]
            record = await self.credentials_provider.get_credential(
                credential_id, session.user_email
            )
            if record is None or record.kind != "browser_storage_state":
                raise CredentialAccessError(
                    "credential_wrong_kind",
                    "auth_state_ref must reference a browser_storage_state credential",
                    credential_id=credential_id,
                    hint=_AUTH_STATE_KIND_HINT,
                )
            target_url = str(resolved.get("url", ""))
            origin = str((record.metadata or {}).get("origin") or "")
            if not origin:
                raise CredentialAccessError(
                    "credential_origin_missing",
                    "auth_state_ref is missing a bound origin",
                    credential_id=credential_id,
                )
            origin_parts = urlparse(origin)
            target_parts = urlparse(target_url)
            if (
                not target_url
                or origin_parts.scheme != target_parts.scheme
                or (origin_parts.hostname or "") != (target_parts.hostname or "")
                or (origin_parts.port or _default_port(origin_parts.scheme))
                != (target_parts.port or _default_port(target_parts.scheme))
            ):
                raise CredentialAccessError(
                    "credential_origin_mismatch",
                    "auth_state_ref origin does not match target URL",
                    credential_id=credential_id,
                )
            if isinstance(cred.value, dict):
                if isinstance(cred.value.get("storage_state"), dict):
                    resolved["auth_state"] = cred.value["storage_state"]
                else:
                    resolved["auth_state"] = cred.value
        elif isinstance(auth_state_ref_raw, str):
            resolved.pop("auth_state_ref", None)
        env = resolved.get("env")
        if isinstance(env, dict):
            new_env: dict[str, Any] = {}
            for key, value in env.items():
                if isinstance(value, str) and value.startswith("$credential:"):
                    cred = await self._resolve_credential_value(value, session, agent)
                    new_env[str(key)] = str(cred.value)
                elif isinstance(value, str) and value.startswith("$auth_challenge:"):
                    new_env[str(key)] = str(
                        await self._resolve_auth_challenge_value_ref(
                            value,
                            session=session,
                            agent=agent,
                            tool_call=tool_call,
                            challenge_payload=None,
                        )
                    )
                else:
                    new_env[str(key)] = value
            resolved["env"] = new_env
        return resolved

    async def _resolve_nested_sensitive_refs(
        self,
        value: Any,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        tool_call: ToolCall,
    ) -> Any:
        if isinstance(value, dict):
            value_ref = value.get("value_ref")
            if isinstance(value_ref, str) and value_ref.strip():
                return await self._resolve_value_ref(
                    value_ref.strip(),
                    session=session,
                    agent=agent,
                    tool_call=tool_call,
                    challenge_payload=value.get("auth_challenge")
                    if isinstance(value.get("auth_challenge"), dict)
                    else None,
                )
            return {
                str(key): await self._resolve_nested_sensitive_refs(
                    item, session=session, agent=agent, tool_call=tool_call
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                await self._resolve_nested_sensitive_refs(
                    item, session=session, agent=agent, tool_call=tool_call
                )
                for item in value
            ]
        return value

    async def _resolve_value_ref(
        self,
        ref: str,
        *,
        session: SessionModel,
        agent: AgentDefinition,
        tool_call: ToolCall,
        challenge_payload: dict[str, Any] | None = None,
    ) -> Any:
        if ref.startswith("$auth_challenge:"):
            return await self._resolve_auth_challenge_value_ref(
                ref,
                session=session,
                agent=agent,
                tool_call=tool_call,
                challenge_payload=challenge_payload,
            )
        cred = await self._resolve_credential_value(ref, session, agent)
        return cred.value

    async def _resolve_auth_challenge_value_ref(
        self,
        ref: str,
        *,
        session: SessionModel,
        agent: AgentDefinition,  # noqa: ARG002
        tool_call: ToolCall,
        challenge_payload: dict[str, Any] | None,
    ) -> Any:
        if self.notification_service is None or self.pause_waiter is None:
            raise CredentialAccessError(
                "auth_challenge_unavailable",
                "Auth challenge resolution is not available for this tool call.",
            )
        raw = ref[len("$auth_challenge:") :]
        challenge_id, field = raw, "response"
        if "." in raw:
            challenge_id, field = raw.split(".", 1)
        challenge_id = challenge_id.strip() or "auth_challenge"
        field = field.strip() or "response"
        payload = dict(challenge_payload or {})
        try:
            timeout_seconds = int(payload.get("timeout_seconds") or 180)
        except (TypeError, ValueError) as exc:
            raise CredentialAccessError(
                "auth_challenge_invalid",
                "Auth challenge timeout_seconds must be an integer.",
            ) from exc
        timeout_seconds = max(1, min(timeout_seconds, 600))
        payload.setdefault("kind", "otp_code" if field == "code" else "manual_continue")
        payload.setdefault("label", challenge_id.replace("_", " ").strip().title())
        payload.setdefault("message", "Authentication is required to continue.")
        payload.setdefault(
            "required_fields", [field] if field not in {"response", "completed"} else []
        )
        payload["expires_at"] = (datetime.now(UTC) + timedelta(seconds=timeout_seconds)).isoformat()
        pause_id = f"auth_{uuid.uuid4().hex[:12]}"
        metadata = dict(tool_call.runtime_metadata or {})
        await self.notification_service.create(
            notification_type="auth_challenge",
            user_email=session.user_email,
            conversation_id=session.conversation_id,
            task_id=metadata.get("task_id") if isinstance(metadata.get("task_id"), str) else None,
            step_name=metadata.get("step_name")
            if isinstance(metadata.get("step_name"), str)
            else None,
            step_run_id=metadata.get("step_run_id")
            if isinstance(metadata.get("step_run_id"), str)
            else None,
            session_id=session.session_id,
            notification_id=pause_id,
            payload=payload,
        )
        try:
            resolution = await self.pause_waiter.wait(pause_id, timeout=float(timeout_seconds))
        except TimeoutError as exc:
            if self.notification_service is not None:
                await self.notification_service.mark_orphaned(pause_id, reason="timeout")
            raise CredentialAccessError(
                "auth_challenge_timeout",
                "Authentication challenge timed out.",
            ) from exc
        if resolution.decision in {"cancel", "deny"}:
            raise CredentialAccessError(
                "auth_challenge_cancelled",
                "Authentication challenge was cancelled.",
            )
        response_payload = resolution.data.get("response_payload")
        if isinstance(response_payload, dict) and response_payload.get(field) is not None:
            return response_payload[field]
        if resolution.data.get(field) is not None:
            return resolution.data[field]
        if resolution.data.get("response") is not None:
            return resolution.data["response"]
        response_ref = resolution.data.get("response_ref")
        if isinstance(response_ref, str) and response_ref.startswith("$credential:"):
            cred = await self._resolve_credential_value(response_ref, session, agent)
            return cred.value
        if resolution.data.get("challenge_completed") is True:
            return ""
        raise CredentialAccessError(
            "auth_challenge_missing_response",
            "Authentication challenge did not provide the requested response.",
        )

    async def _resolve_credential_value(
        self, ref: str, session: SessionModel, agent: AgentDefinition
    ) -> CredentialResolution:
        if self.credentials_provider is None:
            raise ValueError("Credential resolution not available")
        return await self.credentials_provider.resolve_ref(
            ref, agent=agent, user_email=session.user_email
        )

    async def _persist_browser_auth_state_if_needed(
        self, result: ToolResult, session: SessionModel, agent: AgentDefinition
    ) -> ToolResult:
        if self.credentials_provider is None or not isinstance(result.metadata, dict):
            return result
        auth_state = result.metadata.get("browser_auth_state")
        if not isinstance(auth_state, dict):
            return result
        credential_id = str(auth_state.get("credential_id", "")).strip()
        label = str(auth_state.get("label", "")).strip()
        payload = auth_state.get("payload")
        if not credential_id or not label or not isinstance(payload, dict):
            return result.model_copy(
                update={
                    "metadata": {
                        k: v for k, v in result.metadata.items() if k != "browser_auth_state"
                    }
                }
            )
        if agent.permissions is not None and credential_id not in set(
            agent.permissions.allowed_credentials
        ):
            raise CredentialAccessError(
                "credential_not_allowed",
                f"Credential not allowed for agent: {credential_id}",
                credential_id=credential_id,
            )
        created = await self.credentials_provider.upsert_credential(
            credential_id=credential_id,
            user_email=session.user_email,
            kind=str(auth_state.get("kind", "browser_storage_state")),
            label=label,
            payload=payload,
            metadata=auth_state.get("metadata")
            if isinstance(auth_state.get("metadata"), dict)
            else {},
            description="Saved browser authentication state",
        )
        next_metadata = {k: v for k, v in result.metadata.items() if k != "browser_auth_state"}
        next_metadata["saved_credential_id"] = created.credential_id
        return result.model_copy(
            update={
                "metadata": next_metadata,
                "output": f"Saved browser auth state as credential '{created.credential_id}'.",
            }
        )

    def _credential_error_result(self, exc: CredentialAccessError) -> ToolResult:
        metadata: dict[str, Any] = {"code": exc.code, "recoverable": True}
        if exc.credential_id:
            metadata["credential_id"] = exc.credential_id
        if exc.field:
            metadata["field"] = exc.field
        if exc.hint:
            metadata["hint"] = exc.hint
        output = exc.message if not exc.hint else f"{exc.message} Hint: {exc.hint}"
        return ToolResult(output=output, is_error=True, metadata=metadata)

    @staticmethod
    def _runtime_access_from_tool_call(tool_call: ToolCall) -> RuntimeAccessContext | None:
        raw = tool_call.runtime_metadata.get("runtime_access")
        if not isinstance(raw, dict):
            return None
        return RuntimeAccessContext(
            user_email=raw.get("user_email") if isinstance(raw.get("user_email"), str) else None,
            agent_id=raw.get("agent_id") if isinstance(raw.get("agent_id"), str) else None,
            agent_owner_email=raw.get("agent_owner_email")
            if isinstance(raw.get("agent_owner_email"), str)
            else None,
            agent_type=str(raw.get("agent_type") or "primary"),
            session_id=raw.get("session_id") if isinstance(raw.get("session_id"), str) else None,
            parent_session_id=raw.get("parent_session_id")
            if isinstance(raw.get("parent_session_id"), str)
            else None,
            delegation_mode=raw.get("delegation_mode")
            if isinstance(raw.get("delegation_mode"), str)
            else None,
            workflow_step=bool(raw.get("workflow_step")),
        )

    async def _load_text_artifact(self, artifact_id: str, user_email: str) -> str:
        if self.artifact_store is None:
            raise ValueError("Artifact support not available")
        row = await self._get_accessible_artifact_record(artifact_id, user_email)
        content, _content_type = await self.artifact_store.async_load(
            row.namespace, row.object_id, row.filename
        )
        return content.decode("utf-8", errors="replace")

    async def _load_binary_artifact(
        self, artifact_id: str, user_email: str
    ) -> tuple[bytes, str, str]:
        if self.artifact_store is None:
            raise ValueError("Artifact support not available")
        row = await self._get_accessible_artifact_record(artifact_id, user_email)
        content, content_type = await self.artifact_store.async_load(
            row.namespace, row.object_id, row.filename
        )
        return content, content_type, row.filename

    async def _get_accessible_artifact_record(self, artifact_id: str, user_email: str) -> Any:
        if self._session_factory is None:
            raise ValueError("Artifact support not available")
        async with self._session_factory() as db_session:
            row = await get_artifact_record(db_session, artifact_id)
        if row is None or row.status == "deleted":
            raise ValueError(f"Artifact not found: {artifact_id}")
        if row.owner_email and row.owner_email != user_email:
            raise ValueError(f"Artifact access denied: {artifact_id}")
        return row

    async def _materialize_inline_attachments(
        self,
        result: ToolResult,
        session: SessionModel,
        tool_name: str,
    ) -> ToolResult:
        if not result.attachments or self.artifact_store is None or self._session_factory is None:
            return result
        changed = False
        materialized: list[dict[str, Any]] = []
        for raw in result.attachments:
            if not isinstance(raw, dict) or "content_b64" not in raw or raw.get("artifact_id"):
                materialized.append(raw)
                continue
            changed = True
            materialized.append(await self._persist_inline_attachment(raw, session, tool_name))
        enriched_output = self._enrich_attachment_output(result.output, materialized)
        if not changed and enriched_output == result.output:
            return result
        return result.model_copy(update={"attachments": materialized, "output": enriched_output})

    async def _persist_inline_attachment(
        self,
        raw: dict[str, Any],
        session: SessionModel,
        tool_name: str,
    ) -> dict[str, Any]:
        if self.artifact_store is None or self._session_factory is None:
            raise ValueError("Artifact support not available")
        content_b64 = raw.get("content_b64")
        if not isinstance(content_b64, str):
            raise ValueError("Inline attachment missing content_b64")
        content = base64.b64decode(content_b64)
        mime_type = str(raw.get("mime_type") or "application/octet-stream")
        filename = sanitize_artifact_filename(str(raw.get("filename") or "attachment"))
        kind = _kind_for_mime_type(mime_type)
        artifact_id = self.artifact_store.generate_id("doc" if kind is ArtifactKind.PDF else "att")
        namespace = "documents" if kind is ArtifactKind.PDF else "attachments"
        await self.artifact_store.async_save(
            namespace,
            artifact_id,
            filename,
            content,
            mime_type,
            owner_email=session.user_email,
        )
        async with self._session_factory() as db_session:
            await create_artifact_record(
                db_session,
                artifact_id=artifact_id,
                namespace=namespace,
                object_id=artifact_id,
                filename=filename,
                owner_email=session.user_email,
                purpose=str(raw.get("purpose") or tool_name),
                kind=kind.value,
                mime_type=mime_type,
                size_bytes=len(content),
                status="attached",
                expires_at=None,
                conversation_id=session.conversation_id,
                session_id=session.session_id,
                message_role="assistant",
            )
            await db_session.commit()
        url = await self.artifact_store.async_get_public_url(namespace, artifact_id, filename)
        return {
            "artifact_id": artifact_id,
            "url": url,
            "mime_type": mime_type,
            "filename": filename,
            "size_bytes": len(content),
            "kind": kind.value,
            "content_b64": content_b64,
        }

    def _enrich_attachment_output(self, output: str, attachments: list[dict[str, Any]]) -> str:
        if not attachments:
            return output
        primary = next((item for item in attachments if isinstance(item, dict)), None)
        if primary is None:
            return output
        enriched = {
            "artifact_id": primary.get("artifact_id"),
            "url": primary.get("url"),
            "mime_type": primary.get("mime_type"),
            "size_bytes": primary.get("size_bytes"),
        }
        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                parsed.update({k: v for k, v in enriched.items() if v is not None})
                return json.dumps(parsed, sort_keys=True, default=str)
        except Exception:
            pass
        try:
            parsed = ast.literal_eval(output)
            if isinstance(parsed, dict):
                parsed.update({k: v for k, v in enriched.items() if v is not None})
                return str(parsed)
        except Exception:
            pass
        summary = json.dumps({k: v for k, v in enriched.items() if v is not None}, sort_keys=True)
        if not output.strip():
            return summary
        return f"{output}\n\nAttachment metadata: {summary}"

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
        runtime_metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        raw_output = result.output
        token_counter = None
        max_tokens = None
        model = None
        if runtime_metadata is not None:
            resolved_model = runtime_metadata.get("resolved_model")
            if isinstance(resolved_model, str) and resolved_model.strip():
                model = resolved_model
        if self.llm is not None and model is not None:

            def token_counter(text: str, _model: str = model) -> int:
                return self.llm.count_tokens(text, _model)

            max_tokens = max(256, max_size // 4)
        anchor_names = _extract_output_anchor_names(result.metadata, raw_output)
        output, was_truncated = middle_truncate(
            raw_output,
            max_size,
            call_id=call_id,
            token_counter=token_counter,
            max_tokens=max_tokens,
            anchors=anchor_names,
        )
        wrapped = f'<tool_result name="{tool_name}" trust="untrusted">\n{output}\n</tool_result>'
        metadata = dict(result.metadata or {})
        metadata["wrapped"] = True
        metadata["truncated"] = was_truncated
        metadata["original_size"] = len(raw_output)
        if max_tokens is not None:
            metadata["token_budget"] = max_tokens
        # Preserve raw output for the ToolOutputStore (before wrapping/truncation).
        # The agent loop reads this to save the full output to disk.
        metadata["_raw_output"] = raw_output
        return result.model_copy(update={"output": wrapped, "metadata": metadata})


def _extract_output_anchor_names(
    metadata: dict[str, Any] | None,
    raw_output: str,
) -> list[str]:
    """Return anchor names available for a tool result, if any.

    The router prefers anchors a tool emitted explicitly via
    ``metadata.output_anchors``. As a fallback it parses inline
    ``[[anchor]]`` markers out of the raw output (the same convention the
    tool output store uses on save) so models still get real anchor names
    in the truncation marker even when the producing tool didn't surface
    them in metadata.
    """

    names: list[str] = []
    seen: set[str] = set()

    def _add(candidate: object) -> None:
        if isinstance(candidate, str):
            stripped = candidate.strip()
            if stripped and stripped not in seen:
                names.append(stripped)
                seen.add(stripped)

    if metadata is not None:
        raw_anchors = metadata.get("output_anchors")
        if isinstance(raw_anchors, list):
            for entry in raw_anchors:
                if isinstance(entry, dict):
                    _add(entry.get("anchor") or entry.get("name"))
                else:
                    _add(entry)

    if not names and raw_output:
        # Cheap inline scan; mirrors tool_output_store._parse_inline_anchors
        # without importing it (avoids a cycle).
        for line in raw_output.splitlines():
            stripped = line.strip()
            if stripped.startswith("[[") and stripped.endswith("]]") and len(stripped) > 4:
                _add(stripped[2:-2])

    return names


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


def _default_port(scheme: str) -> int | None:
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _kind_for_mime_type(mime_type: str) -> ArtifactKind:
    if mime_type.startswith("image/"):
        return ArtifactKind.IMAGE
    if mime_type.startswith("audio/"):
        return ArtifactKind.AUDIO
    if mime_type.startswith("video/"):
        return ArtifactKind.VIDEO
    if mime_type == "application/pdf":
        return ArtifactKind.PDF
    return ArtifactKind.FILE
