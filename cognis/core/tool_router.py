"""Tool routing logic for orchestration, Intaris MCP, and local executors."""

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatchcase
from time import monotonic, perf_counter
from typing import Any, cast
from urllib.parse import urlparse

from prometheus_client import Counter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.artifacts.store import sanitize_artifact_filename
from cognis.core.anchored_output import markdown_heading_anchors
from cognis.core.chat_modes import is_plan_hidden_tool
from cognis.core.content_refs import (
    build_deliverable_public_url,
    continuation_scope_task_id,
    get_accessible_deliverable_ref,
    is_deliverable_ref,
)
from cognis.core.credential_grants import (
    grant_credential_to_agent,
    grant_credential_to_agent_definition,
)
from cognis.core.mcp_oauth import MCPOAuthError
from cognis.core.session import executor_home_from_workspace_root
from cognis.core.tool_arguments import validate_tool_arguments
from cognis.core.tool_output_presentation import present_tool_output
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.artifact import ArtifactKind, AttachmentRef
from cognis.models.credential import CredentialAccessError, CredentialResolution
from cognis.models.session import SessionModel
from cognis.models.tool import (
    ExecutorHandle,
    MCPAuthConfig,
    MCPServerConfig,
    Permission,
    ToolCall,
    ToolDefinition,
    ToolResult,
    effective_mcp_auth_config,
    mcp_headers_have_authorization,
    stable_tool_id,
    tool_capabilities,
)
from cognis.runtime_context import RuntimeAccessContext, current_runtime_access_context
from cognis.store.queries import (
    create_artifact_record,
    get_artifact_record,
    get_mcp_server,
    get_setting_value,
)
from cognis.tools.argument_normalization import strip_empty_optional_values
from cognis.tools.builtin.agent_management import (
    handle_agent_management_tool,
    is_agent_management_tool,
)
from cognis.tools.builtin.artifact_tools import (
    analyze_attachment_ref,
    attachment_supports_model,
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
from cognis.tools.mcp import (
    HTTP_MCP_TRANSPORTS,
    MCPClientError,
    _normalize_call_result,
    build_mcp_client,
)
from cognis.tools.registry import RegisteredTool, ToolExecutionContext, ToolRegistry

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


def _mcp_oauth_setup_failed_result(
    *,
    server_id: str,
    server_name: str,
    message: str,
    retryable: bool = False,
) -> ToolResult:
    return ToolResult(
        output=f"MCP OAuth setup failed for {server_name}: {message[:500]}",
        is_error=True,
        metadata={
            "code": "mcp_oauth_setup_failed",
            "server_id": server_id,
            "server_name": server_name,
            "retryable": retryable,
        },
    )


def _effective_content_trust(
    registered_tool: RegisteredTool, result: ToolResult | None = None
) -> str:
    definition = registered_tool.definition
    if definition.content_trust == "untrusted":
        return "untrusted"
    if definition.category == "mcp" or definition.source.type in {"intaris_mcp", "local_mcp"}:
        return "untrusted"
    if (
        isinstance(result, ToolResult)
        and isinstance(result.metadata, dict)
        and result.metadata.get("content_trust") == "untrusted"
    ):
        return "untrusted"
    return definition.content_trust


def _mcp_oauth_authorization_required_result(
    *,
    server_id: str,
    server_name: str,
    reason: str | None,
    transaction_id: str | None,
    authorization_url: str | None,
    authorization_expires_at: datetime | None,
    flow: str | None = None,
    verification_uri: str | None = None,
    verification_uri_complete: str | None = None,
    user_code: str | None = None,
) -> ToolResult:
    if not authorization_url:
        return _mcp_oauth_setup_failed_result(
            server_id=server_id,
            server_name=server_name,
            message=(
                "authorization is required, but Cognis could not generate an OAuth "
                "authorization URL. Check the MCP OAuth server configuration and retry."
            ),
        )
    expires_at = authorization_expires_at.isoformat() if authorization_expires_at else None
    expires_text = f"\nThe authorization link expires at {expires_at}." if expires_at else ""
    if flow == "device_code":
        instruction = (
            "Open the provider verification page and enter the user code:\n"
            f"{authorization_url}\nCode: {user_code or ''}{expires_text}\n"
        )
    else:
        instruction = (
            "Open this controller-generated URL to authorize the MCP server:\n"
            f"{authorization_url}{expires_text}\n"
        )
    return ToolResult(
        output=(
            f"MCP authorization is required for {server_name}.\n"
            f"{instruction}"
            "After completing authorization, retry the tool call."
        ),
        is_error=True,
        metadata={
            "code": "mcp_authorization_required",
            "server_id": server_id,
            "server_name": server_name,
            "transaction_id": transaction_id,
            "authorization_url": authorization_url,
            "authorization_expires_at": expires_at,
            "flow": flow,
            "verification_uri": verification_uri,
            "verification_uri_complete": verification_uri_complete,
            "user_code": user_code,
            "reason": reason,
            "retryable": False,
        },
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

ToolOutputChunkCallback = Callable[[str, str | None], Coroutine[Any, Any, None]]
_MAX_BROWSER_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_BROWSER_UPLOAD_FILES = 10
_MAX_ARTIFACT_VALUE_REF_BYTES = 50 * 1024 * 1024
_ARTIFACT_VALUE_REF_PREFIX = "$artifact:"
_ARTIFACT_VALUE_REF_FIELDS = frozenset(
    {
        "content_b64",
        "filename",
        "mime_type",
        "size_bytes",
        "signed_url",
        "public_url",
    }
)
_GUARDRAILS_RUNTIME_CONTEXT_KEYS = (
    "workspace_root",
    "working_directory",
    "chat_mode",
    "chat_mode_source",
    "read_only_required",
)
_GUARDRAILS_EXECUTOR_ENVIRONMENT_KEYS = (
    "available",
    "executor_id",
    "executor_type",
    "cwd",
    "home",
)
_GUARDRAILS_CONTEXT_STRING_LIMIT = 1200

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


def _guardrails_context_value(value: Any) -> Any:
    """Return a bounded JSON-like value for Intaris guardrails context."""

    if isinstance(value, str):
        return value[:_GUARDRAILS_CONTEXT_STRING_LIMIT]
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, list | tuple):
        return [_guardrails_context_value(item) for item in value[:20]]
    if isinstance(value, dict):
        return {
            str(key)[:120]: _guardrails_context_value(item)
            for key, item in list(value.items())[:30]
        }
    return str(value)[:_GUARDRAILS_CONTEXT_STRING_LIMIT]


def _compact_executor_environment(value: Any) -> dict[str, Any] | None:
    """Keep only executor facts that are useful for guardrails decisions."""

    if not isinstance(value, dict):
        return None
    compact = {
        key: _guardrails_context_value(value.get(key))
        for key in _GUARDRAILS_EXECUTOR_ENVIRONMENT_KEYS
        if value.get(key) is not None
    }
    return compact or None


def _tool_parameters_summary(parameters: dict[str, Any]) -> dict[str, Any] | None:
    """Return a small guardrails-oriented summary of a tool JSON schema."""

    if not isinstance(parameters, dict):
        return None
    summary: dict[str, Any] = {}
    required = parameters.get("required")
    if isinstance(required, list):
        summary["required"] = [str(item)[:120] for item in required[:12] if isinstance(item, str)]

    properties = parameters.get("properties")
    if isinstance(properties, dict):
        compact_properties: dict[str, dict[str, str]] = {}
        for name, spec in list(properties.items())[:12]:
            if not isinstance(name, str) or not isinstance(spec, dict):
                continue
            entry: dict[str, str] = {}
            schema_type = spec.get("type")
            if isinstance(schema_type, str):
                entry["type"] = schema_type[:80]
            description = spec.get("description")
            if isinstance(description, str) and description.strip():
                entry["description"] = description.strip()[:240]
            if entry:
                compact_properties[name[:120]] = entry
        if compact_properties:
            summary["properties"] = compact_properties

    return summary or None


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
        mcp_oauth_service: Any | None = None,
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
        self._mcp_oauth_service = mcp_oauth_service
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
        mcp_oauth_service: Any | None = None,
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
            mcp_oauth_service=mcp_oauth_service,
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
        evaluation_context = await self._evaluation_context(tool_call, registered_tool.definition)
        if evaluation_context.get("read_only_required") is True and is_plan_hidden_tool(
            registered_tool.definition
        ):
            return PermissionDecision(
                decision="deny",
                reasoning=(
                    "Plan mode is active for this turn. Write tools are disabled "
                    "because the agent must not make changes while planning."
                ),
                source="chat_mode",
            )

        # When guardrails are disabled for this agent, auto-approve all tools
        # (including non-bypassable ones — guardrails=none means no guardrails).
        capabilities = getattr(agent, "capabilities", None)
        if capabilities is not None and not capabilities.guardrails_enabled:
            return PermissionDecision(
                decision="approve",
                reasoning="Guardrails disabled for this agent (capability-disabled).",
                source="capability-disabled",
            )

        if self._is_non_bypassable(
            registered_tool.definition.name, registered_tool.definition.non_bypassable
        ):
            evaluation = await self.guardrails.evaluate(
                session_id=_guardrails_session_id(session),
                tool_name=tool_call.name,
                arguments=tool_call.arguments,
                context=evaluation_context,
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
            context=evaluation_context,
        )
        if cached is not None:
            TOOL_DECISION_CACHE_HITS.labels(decision=cached.decision).inc()
            return cached

        evaluation = await self.guardrails.evaluate(
            session_id=_guardrails_session_id(session),
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            context=evaluation_context,
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
            context=evaluation_context,
        )
        return decision_result

    async def _evaluation_context(
        self,
        tool_call: ToolCall,
        tool_definition: ToolDefinition | None = None,
    ) -> dict[str, Any]:
        """Build non-content runtime context for Intaris tool evaluation."""

        runtime = tool_call.runtime_metadata or {}
        context: dict[str, Any] = {}
        for key in _GUARDRAILS_RUNTIME_CONTEXT_KEYS:
            value = runtime.get(key)
            if value is not None:
                context[key] = _guardrails_context_value(value)
        executor_env = _compact_executor_environment(runtime.get("executor_environment"))
        if executor_env is not None:
            context["executor_environment"] = executor_env
        runtime_access = current_runtime_access_context.get()
        if runtime_access and runtime_access.interaction_mode:
            context["interaction_mode"] = _guardrails_context_value(runtime_access.interaction_mode)
        executor_env = context.get("executor_environment")
        if isinstance(executor_env, dict) and not executor_env.get("home"):
            inferred_home = executor_home_from_workspace_root(context.get("workspace_root"))
            if inferred_home:
                context["executor_environment"] = {**executor_env, "home": inferred_home}
        if tool_definition is not None:
            source = getattr(tool_definition, "source", None)
            classification = {
                key: value
                for key, value in {
                    "status": getattr(tool_definition, "classification_status", None),
                    "source": getattr(tool_definition, "classification_source", None),
                    "confidence": getattr(tool_definition, "classification_confidence", None),
                }.items()
                if value is not None
            }
            tool_context = {
                "id": stable_tool_id(tool_definition),
                "name": getattr(tool_definition, "name", tool_call.name),
                "description": getattr(tool_definition, "description", None),
                "read_only": getattr(tool_definition, "read_only", None),
                "capabilities": [
                    str(capability) for capability in tool_capabilities(tool_definition)
                ],
                "category": getattr(tool_definition, "category", None),
                "profile_group": getattr(tool_definition, "profile_group", None),
                "risk_level": getattr(tool_definition, "risk_level", None),
                "non_bypassable": getattr(tool_definition, "non_bypassable", None),
                "classification": classification or None,
                "parameters_summary": _tool_parameters_summary(
                    getattr(tool_definition, "parameters", {})
                ),
                "source": {
                    key: value
                    for key, value in {
                        "type": getattr(source, "type", None),
                        "server_name": getattr(source, "server_name", None),
                        "raw_tool_name": getattr(source, "raw_tool_name", None),
                    }.items()
                    if value is not None
                }
                if source is not None
                else None,
            }
            context["tool"] = {
                k: _guardrails_context_value(v) for k, v in tool_context.items() if v is not None
            }
        if tool_call.name == "bash":
            description = tool_call.arguments.get("description")
            if isinstance(description, str) and description.strip():
                context["intent"] = {
                    "description": _guardrails_context_value(description.strip()),
                    "source": "bash.description",
                }
        skill_context = await self._skill_evaluation_context(tool_call)
        if skill_context:
            context["skill"] = skill_context
        return context

    async def _skill_evaluation_context(self, tool_call: ToolCall) -> dict[str, Any] | None:
        if tool_call.name != "skill_load":
            return None
        skill_id = tool_call.arguments.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            return None
        try:
            from cognis.store.queries import get_skill_scoped

            if self._session_factory is None:
                return {"skill_id": skill_id}
            async with self._session_factory() as db:
                skill = await get_skill_scoped(db, skill_id, owner_email=None)
        except Exception:
            logger.debug("Failed to resolve skill metadata for evaluation", exc_info=True)
            return {"skill_id": skill_id}
        if skill is None:
            return {"skill_id": skill_id}
        return {
            key: value
            for key, value in {
                "skill_id": getattr(skill, "skill_id", skill_id),
                "name": getattr(skill, "name", None),
                "description": getattr(skill, "description", None),
                "tags": getattr(skill, "tags", None),
            }.items()
            if value is not None
        }

    def _get_cached_decision(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        read_only: bool,
        *,
        context: dict[str, Any] | None = None,
    ) -> PermissionDecision | None:
        if not read_only:
            return None
        self._purge_stale_decision_cache()
        entry = self._decision_cache.get(
            self._decision_cache_key(
                session_id,
                tool_name,
                arguments,
                read_only=read_only,
                context=context,
            )
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
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        if not read_only or decision.decision != "approve":
            return
        self._decision_cache[
            self._decision_cache_key(
                session_id,
                tool_name,
                arguments,
                read_only=read_only,
                context=context,
            )
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
        context: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        # Read-only tool decisions are independent of arguments — Intaris
        # classifies a read regardless of the file path. Bucketing by tool
        # name lets a session's first read warm the cache for every
        # subsequent read of the same tool. Non-read-only callers retain
        # the per-argument key so write/destructive paths cannot share
        # cached approvals across distinct payloads.
        if read_only:
            payload = json.dumps(
                {
                    "executor_environment": (context or {}).get("executor_environment"),
                    "chat_mode": (context or {}).get("chat_mode"),
                    "chat_mode_source": (context or {}).get("chat_mode_source"),
                    "read_only_required": (context or {}).get("read_only_required"),
                    "working_directory": (context or {}).get("working_directory"),
                    "workspace_root": (context or {}).get("workspace_root"),
                },
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]  # noqa: S324
            return session_id, tool_name, digest
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
        output_chunk_callback: ToolOutputChunkCallback | None = None,
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
        plan_denial = self._plan_mode_denial_result(tool_call, registry)
        if plan_denial is not None:
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="denied").inc()
            return plan_denial
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
            capabilities = getattr(agent, "capabilities", None)
            if capabilities is not None and not capabilities.memory_enabled:
                result = ToolResult(
                    output="Memory backend is disabled for this agent.",
                    is_error=True,
                )
            elif self.memory is None:
                result = ToolResult(output="Memory provider not available.", is_error=True)
            else:
                from cognis.runtime_context import current_user_email

                result = await handle_memory_tool(
                    tool_name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    memory_provider=self.memory,
                    agent_id=agent.agent_id if agent else None,
                    user_email=current_user_email.get() or session.user_email,
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
            from cognis.runtime_context import current_user_email

            artifact_runtime_metadata = dict(tool_call.runtime_metadata)
            if self.tool_output_store is not None:
                artifact_runtime_metadata["tool_output_store"] = self.tool_output_store
            result = await handle_artifact_tool(
                tool_name=tool_call.name,
                arguments=dict(tool_call.arguments),
                llm=self.llm,
                artifact_store=self.artifact_store,
                session_factory=self._session_factory,
                user_email=current_user_email.get() or session.user_email,
                current_model=(
                    str(artifact_runtime_metadata.get("resolved_model"))
                    if isinstance(artifact_runtime_metadata.get("resolved_model"), str)
                    else None
                ),
                current_provider_id=(
                    str(artifact_runtime_metadata.get("resolved_provider_id"))
                    if isinstance(artifact_runtime_metadata.get("resolved_provider_id"), str)
                    else None
                ),
                runtime_metadata=artifact_runtime_metadata,
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
            eval_meta = {
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
                        guardrails=self.guardrails,
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
            eval_meta = {
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
                from cognis.runtime_context import current_agent_id, current_user_email

                result = await handle_skill_management_tool(
                    tool_name=tool_call.name,
                    arguments=dict(tool_call.arguments),
                    session_factory=self._session_factory,
                    user_email=current_user_email.get() or session.user_email,
                    llm=self.llm,
                    artifact_store=self.artifact_store,
                    current_agent_id=current_agent_id.get() or agent.agent_id,
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
            combined_meta = {"evaluation": eval_meta}
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
                    user_email=current_user_email.get() or session.user_email,
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
            try:
                tool_call = await self._prepare_intaris_mcp_tool_call(tool_call, session)
            except ValueError as exc:
                TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="failure").inc()
                return self._sanitize_result(
                    tool_call.name,
                    ToolResult(output=str(exc), is_error=True),
                    _tool_max_size(registry, tool_call.name),
                    call_id=cid,
                    runtime_metadata=tool_call.runtime_metadata,
                )
            result = await self._call_intaris_mcp(tool_call, session, registry)
            result = await self._materialize_inline_attachments(result, session, tool_call.name)
            if (
                result.is_error
                and result.metadata is not None
                and result.metadata.get("decision") == "escalate"
                and result.metadata.get("call_id")
            ):
                eval_meta = {
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
        except ValueError as exc:
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="failure").inc()
            return self._sanitize_result(
                tool_call.name,
                ToolResult(output=str(exc), is_error=True),
                _tool_max_size(registry, tool_call.name),
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
        if route is ToolRoute.LOCAL and registered_tool is not None:
            validation_error = validate_tool_arguments(
                guardrails_tool_call.name,
                guardrails_tool_call.arguments,
                schema=registered_tool.definition.parameters,
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
                    _tool_max_size(registry, tool_call.name),
                    call_id=cid,
                    runtime_metadata=tool_call.runtime_metadata,
                )

        decision = await self.evaluate_tool_call(guardrails_tool_call, agent, session, registry)
        eval_meta = {
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
        except ValueError as exc:
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="failure").inc()
            return self._sanitize_result(
                tool_call.name,
                ToolResult(output=str(exc), is_error=True),
                _tool_max_size(registry, tool_call.name),
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
            )
        if route is ToolRoute.LOCAL and registered_tool is not None:
            validation_error = validate_tool_arguments(
                tool_call.name,
                tool_call.arguments,
                schema=registered_tool.definition.parameters,
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
                    output_chunk_callback=output_chunk_callback,
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
                content_trust=_effective_content_trust(registered_tool, result),
            )
        try:
            controller_result = await self._execute_controller_oauth_mcp_if_applicable(
                scoped_tool_call,
                registered_tool=registered_tool,
                session=session,
            )
            if controller_result is not None:
                result = controller_result
            else:
                inner_timeout = registered_tool.definition.timeout_seconds
                outer_timeout = inner_timeout + 10 if inner_timeout > 0 else inner_timeout
                result = await asyncio.wait_for(
                    executor.tool_execute(
                        scoped_tool_call,
                        timeout_seconds=inner_timeout,
                        output_chunk_callback=output_chunk_callback,
                    ),
                    timeout=outer_timeout,
                )
                result = await self._persist_browser_auth_state_if_needed(result, session, agent)
            result = await self._materialize_inline_attachments(result, session, tool_call.name)
            result = await self._postprocess_tool_result(result, scoped_tool_call, session)
        except CredentialAccessError as exc:
            result = self._credential_error_result(exc)
        except TimeoutError:
            await executor.cancel_call(tool_call.call_id)
            result = ToolResult(
                output=(
                    "Tool execution timed out. If this was a write/edit operation, "
                    "the write may have succeeded on the executor; read the target file "
                    "before retrying."
                ),
                is_error=True,
                metadata={"code": "tool_execution_timeout", "retryable": False},
            )
            TOOL_ROUTE_OUTCOMES.labels(route=str(route), outcome="timeout").inc()
            return self._sanitize_result(
                tool_call.name,
                result,
                registered_tool.definition.max_result_size,
                call_id=cid,
                runtime_metadata=tool_call.runtime_metadata,
                content_trust=_effective_content_trust(registered_tool, result),
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
            content_trust=_effective_content_trust(registered_tool, result),
        )

    async def _execute_controller_oauth_mcp_if_applicable(
        self,
        tool_call: ToolCall,
        *,
        registered_tool: Any,
        session: SessionModel,
    ) -> ToolResult | None:
        """Execute OAuth HTTP MCP tools in the controller with per-call token refresh."""

        source = registered_tool.definition.source
        if source.type != "local_mcp" or not source.server_id:
            return None
        if self._session_factory is None or self._mcp_oauth_service is None:
            return None

        async with self._session_factory() as store_session:
            mcp_row = await get_mcp_server(
                store_session,
                source.server_id,
                owner_email=session.user_email,
                include_shared=True,
            )
            tool_timeout_raw = await get_setting_value(
                store_session, "mcp.tool_timeout_seconds", 300
            )
            connect_timeout_raw = await get_setting_value(
                store_session, "mcp.connect_timeout_seconds", 15
            )
        if mcp_row is None or mcp_row.status != "active":
            return None
        if mcp_row.transport not in HTTP_MCP_TRANSPORTS:
            return None

        base_headers = mcp_row.headers or {}
        auth_config = effective_mcp_auth_config(mcp_row.auth_config, base_headers)
        if auth_config.type != "oauth2":
            return None
        if mcp_headers_have_authorization(base_headers):
            return None

        try:
            token_result = await self._mcp_oauth_service.inject_authorization_header(
                user_email=session.user_email,
                server=mcp_row,
                headers={k: v for k, v in base_headers.items() if k.lower() != "authorization"},
                conversation_id=session.conversation_id,
                session_id=session.session_id,
                task_id=tool_call.runtime_metadata.get("task_id"),
                step_name=tool_call.runtime_metadata.get("step_name"),
                step_run_id=tool_call.runtime_metadata.get("step_run_id"),
                delivery_mode="same_conversation",
            )
        except MCPOAuthError as exc:
            return _mcp_oauth_setup_failed_result(
                server_id=source.server_id or mcp_row.server_id,
                server_name=mcp_row.name,
                message=str(exc),
                retryable=bool(getattr(exc, "retryable", False)),
            )

        if token_result.authorization_required:
            return _mcp_oauth_authorization_required_result(
                server_id=source.server_id or mcp_row.server_id,
                server_name=mcp_row.name,
                reason=token_result.reason,
                transaction_id=token_result.transaction_id,
                authorization_url=token_result.authorization_url,
                authorization_expires_at=getattr(token_result, "authorization_expires_at", None),
                flow=getattr(token_result, "flow", None),
                verification_uri=getattr(token_result, "verification_uri", None),
                verification_uri_complete=getattr(token_result, "verification_uri_complete", None),
                user_code=getattr(token_result, "user_code", None),
            )

        if isinstance(tool_timeout_raw, int | float | str):
            try:
                tool_timeout = int(tool_timeout_raw)
            except (TypeError, ValueError):
                tool_timeout = 300
        else:
            tool_timeout = 300
        if isinstance(connect_timeout_raw, int | float | str):
            try:
                connect_timeout = int(connect_timeout_raw)
            except (TypeError, ValueError):
                connect_timeout = 15
        else:
            connect_timeout = 15
        timeout_seconds = max(int(mcp_row.timeout_seconds or 0), tool_timeout, 1)
        connect_timeout_seconds = max(connect_timeout, 1)

        config = MCPServerConfig(
            name=mcp_row.name,
            transport=mcp_row.transport,
            command=mcp_row.command,
            url=mcp_row.url,
            args=mcp_row.args or [],
            env={},
            headers=token_result.headers,
            auth_config=MCPAuthConfig(type="static_headers"),
            timeout_seconds=timeout_seconds,
            connect_timeout_seconds=connect_timeout_seconds,
            server_id=mcp_row.server_id,
        )
        client = build_mcp_client(config, secrets={})
        try:
            await client.connect()
            raw = await client.call_tool(
                source.raw_tool_name or tool_call.name, tool_call.arguments
            )
            result = raw if isinstance(raw, ToolResult) else _normalize_call_result(raw)
            metadata = dict(result.metadata or {})
            metadata.update(
                {
                    "executed_by": "controller_oauth_mcp",
                    "server_id": source.server_id,
                    "server_name": mcp_row.name,
                    "raw_tool_name": source.raw_tool_name or tool_call.name,
                    "timeout_seconds": timeout_seconds,
                    "connect_timeout_seconds": connect_timeout_seconds,
                }
            )
            return result.model_copy(update={"metadata": metadata})
        except MCPClientError as exc:
            return ToolResult(
                output=f"MCP tool call failed: {str(exc)[:500]}",
                is_error=True,
                metadata={
                    "code": "mcp_tool_call_failed",
                    "server_id": source.server_id,
                    "server_name": exc.server_name,
                    "phase": exc.phase,
                    "error_class": exc.error_class,
                    "timed_out": exc.timed_out,
                    "status_code": exc.status_code,
                    "auth_error": exc.auth_error,
                    "authorization_required": exc.authorization_required,
                    "retryable": exc.timed_out,
                },
            )
        finally:
            await client.close(suppress_cancelled=True)

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
        next_metadata = {k: v for k, v in metadata.items() if k != "attachment_analysis_request"}
        resolved_model = (
            str(tool_call.runtime_metadata.get("resolved_model"))
            if isinstance(tool_call.runtime_metadata.get("resolved_model"), str)
            else None
        )
        resolved_provider_id = (
            str(tool_call.runtime_metadata.get("resolved_provider_id"))
            if isinstance(tool_call.runtime_metadata.get("resolved_provider_id"), str)
            else None
        )
        if resolved_model and self.llm is not None:
            try:
                model_info = await self.llm.get_model_info(
                    resolved_model,
                    provider_id=resolved_provider_id,
                    acting_user_email=session.user_email,
                )
            except TypeError:
                model_info = await self.llm.get_model_info(
                    resolved_model,
                    resolved_provider_id,
                )
            if attachment_supports_model(attachment, model_info):
                next_metadata.update(
                    {
                        "artifact_id": attachment.artifact_id,
                        "filename": attachment.filename,
                        "mime_type": attachment.mime_type,
                        "kind": attachment.kind.value,
                        "url": attachment.url,
                        "native_attachment": True,
                        "analysis_model": resolved_model,
                        "analysis_provider_id": resolved_provider_id,
                    }
                )
                return result.model_copy(
                    update={
                        "output": (
                            f"Prepared binary file '{attachment.filename}' for native model "
                            "inspection. The next model cycle receives it as an attachment; "
                            "use the attachment content directly to answer the user's request."
                        ),
                        "metadata": next_metadata,
                    }
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
                metadata=next_metadata,
            )
        analysis = await analyze_attachment_ref(
            attachment=attachment,
            content=content,
            prompt=(str(request.get("prompt")) if isinstance(request.get("prompt"), str) else None),
            llm=self.llm,
            artifact_store=self.artifact_store,
            session_factory=self._session_factory,
            current_model=resolved_model,
            current_provider_id=resolved_provider_id,
            owner_email=session.user_email,
        )
        if analysis.metadata:
            next_metadata.update(analysis.metadata)
        return analysis.model_copy(
            update={"metadata": next_metadata, "attachments": analysis.attachments}
        )

    async def _execute_local_handler(
        self,
        tool_call: ToolCall,
        *,
        registered_tool: Any,
        executor: Any,
        output_chunk_callback: ToolOutputChunkCallback | None = None,
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
            output_chunk_callback=output_chunk_callback,
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
                metadata = await self._get_accessible_content_ref_metadata(
                    str(artifact_id),
                    session.user_email,
                    scope_task_id=self._content_ref_scope_task_id(tool_call),
                )
                arguments.setdefault("source_artifact_filename", metadata["filename"])
                arguments.setdefault("source_artifact_mime_type", metadata["mime_type"])
                arguments.setdefault("source_artifact_size_bytes", metadata["size_bytes"])
        if tool_call.name.startswith("office_"):
            arguments.pop("source_artifact_content_b64", None)
            if artifact_id := arguments.get("source_artifact_id"):
                metadata = await self._get_accessible_content_ref_metadata(
                    str(artifact_id),
                    session.user_email,
                    scope_task_id=self._content_ref_scope_task_id(tool_call),
                )
                arguments.setdefault("source_artifact_filename", metadata["filename"])
                arguments.setdefault("source_artifact_mime_type", metadata["mime_type"])
                arguments.setdefault("source_artifact_size_bytes", metadata["size_bytes"])
        if tool_call.name == "browser_upload":
            arguments.pop("source_artifacts", None)
            artifact_ids = arguments.get("source_artifact_ids")
            if isinstance(artifact_ids, list):
                if len(artifact_ids) > _MAX_BROWSER_UPLOAD_FILES:
                    raise ValueError(
                        f"browser_upload supports at most {_MAX_BROWSER_UPLOAD_FILES} files per call"
                    )
                source_artifacts: list[dict[str, Any]] = []
                total_bytes = 0
                for artifact_id in artifact_ids:
                    if not isinstance(artifact_id, str) or not artifact_id.strip():
                        continue
                    metadata = await self._get_accessible_content_ref_metadata(
                        artifact_id,
                        session.user_email,
                        scope_task_id=self._content_ref_scope_task_id(tool_call),
                    )
                    total_bytes += int(metadata["size_bytes"] or 0)
                    if total_bytes > _MAX_BROWSER_UPLOAD_BYTES:
                        raise ValueError(
                            "browser_upload artifact payload is too large: "
                            f"{total_bytes} bytes exceeds {_MAX_BROWSER_UPLOAD_BYTES} bytes"
                        )
                    source_artifacts.append(
                        {
                            "artifact_id": artifact_id,
                            "filename": metadata["filename"],
                            "mime_type": metadata["mime_type"],
                            "size_bytes": metadata["size_bytes"],
                        }
                    )
                arguments["source_artifacts"] = source_artifacts
        assets = arguments.get("assets") if tool_call.name == "document_generate" else None
        if isinstance(assets, list):
            sanitized_assets: list[dict[str, Any]] = []
            for raw in assets:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                item.pop("content_b64", None)
                if artifact_id := item.get("artifact_id"):
                    metadata = await self._get_accessible_content_ref_metadata(
                        str(artifact_id),
                        session.user_email,
                        scope_task_id=self._content_ref_scope_task_id(tool_call),
                    )
                    item.setdefault("filename", metadata["filename"])
                    item.setdefault("mime_type", metadata["mime_type"])
                    item.setdefault("size_bytes", metadata["size_bytes"])
                sanitized_assets.append(item)
            arguments["assets"] = sanitized_assets
        arguments = await self._sanitize_artifact_value_refs_for_guardrails(
            arguments, session, tool_call
        )
        arguments = self._sanitize_sensitive_refs_for_guardrails(arguments, root=True)
        return tool_call.model_copy(update={"arguments": arguments})

    async def _prepare_local_tool_call(
        self, tool_call: ToolCall, session: SessionModel, agent: AgentDefinition
    ) -> ToolCall:
        arguments = dict(tool_call.arguments)
        if tool_call.name == "artifact_save" and (
            artifact_id := arguments.get("source_artifact_id")
        ):
            content, mime_type, filename = await self._load_binary_content_ref(
                str(artifact_id),
                session.user_email,
                scope_task_id=self._content_ref_scope_task_id(tool_call),
            )
            arguments["source_artifact_content_b64"] = base64.b64encode(content).decode("ascii")
            arguments.setdefault("source_artifact_filename", filename)
            arguments.setdefault("source_artifact_mime_type", mime_type)
        if tool_call.name.startswith("office_") and (
            artifact_id := arguments.get("source_artifact_id")
        ):
            content, mime_type, filename = await self._load_binary_content_ref(
                str(artifact_id),
                session.user_email,
                scope_task_id=self._content_ref_scope_task_id(tool_call),
            )
            arguments["source_artifact_content_b64"] = base64.b64encode(content).decode("ascii")
            arguments.setdefault("source_artifact_filename", filename)
            arguments.setdefault("source_artifact_mime_type", mime_type)
            arguments.setdefault("source_artifact_size_bytes", len(content))
        if tool_call.name == "document_generate" and (
            artifact_id := arguments.get("source_artifact_id")
        ):
            arguments["source_artifact_content"] = await self._load_text_content_ref(
                str(artifact_id),
                session.user_email,
                scope_task_id=self._content_ref_scope_task_id(tool_call),
            )
        if tool_call.name == "browser_upload":
            arguments.pop("source_artifacts", None)
            artifact_ids = arguments.get("source_artifact_ids")
            if isinstance(artifact_ids, list):
                if len(artifact_ids) > _MAX_BROWSER_UPLOAD_FILES:
                    raise ValueError(
                        f"browser_upload supports at most {_MAX_BROWSER_UPLOAD_FILES} files per call"
                    )
                source_artifacts: list[dict[str, Any]] = []
                total_bytes = 0
                for artifact_id in artifact_ids:
                    if not isinstance(artifact_id, str) or not artifact_id.strip():
                        continue
                    content, mime_type, filename = await self._load_binary_content_ref(
                        artifact_id,
                        session.user_email,
                        scope_task_id=self._content_ref_scope_task_id(tool_call),
                    )
                    total_bytes += len(content)
                    if total_bytes > _MAX_BROWSER_UPLOAD_BYTES:
                        raise ValueError(
                            "browser_upload artifact payload is too large: "
                            f"{total_bytes} bytes exceeds {_MAX_BROWSER_UPLOAD_BYTES} bytes"
                        )
                    source_artifacts.append(
                        {
                            "artifact_id": artifact_id,
                            "filename": filename,
                            "mime_type": mime_type,
                            "content_b64": base64.b64encode(content).decode("ascii"),
                        }
                    )
                arguments["source_artifacts"] = source_artifacts
        assets = arguments.get("assets") if tool_call.name == "document_generate" else None
        if isinstance(assets, list):
            resolved_assets: list[dict[str, Any]] = []
            for raw in assets:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                if artifact_id := item.get("artifact_id"):
                    content, mime_type, filename = await self._load_binary_content_ref(
                        str(artifact_id),
                        session.user_email,
                        scope_task_id=self._content_ref_scope_task_id(tool_call),
                    )
                    item.setdefault("content_b64", base64.b64encode(content).decode("ascii"))
                    item.setdefault("mime_type", mime_type)
                    item.setdefault("filename", filename)
                resolved_assets.append(item)
            arguments["assets"] = resolved_assets
        arguments = await self._resolve_artifact_value_refs(arguments, session, tool_call)
        arguments = await self._resolve_sensitive_refs(arguments, session, agent, tool_call)
        return tool_call.model_copy(update={"arguments": arguments})

    def _content_ref_scope_task_id(self, tool_call: ToolCall) -> str | None:
        return continuation_scope_task_id(tool_call.runtime_metadata)

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

    async def _sanitize_artifact_value_refs_for_guardrails(
        self,
        value: Any,
        session: SessionModel,
        tool_call: ToolCall,
    ) -> Any:
        """Resolve artifact value refs to bounded guardrails-safe metadata."""

        if isinstance(value, dict):
            return {
                str(key): await self._sanitize_artifact_value_refs_for_guardrails(
                    item, session, tool_call
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                await self._sanitize_artifact_value_refs_for_guardrails(item, session, tool_call)
                for item in value
            ]
        if not isinstance(value, str) or not value.startswith(_ARTIFACT_VALUE_REF_PREFIX):
            return value
        content_ref, field = self._parse_artifact_value_ref(value)
        metadata = await self._get_accessible_content_ref_metadata(
            content_ref,
            session.user_email,
            scope_task_id=self._content_ref_scope_task_id(tool_call),
        )
        if field == "filename":
            return metadata["filename"]
        if field == "mime_type":
            return metadata["mime_type"]
        if field == "size_bytes":
            return metadata["size_bytes"]
        if field in {"content_b64", "signed_url", "public_url"}:
            size = int(metadata.get("size_bytes") or 0)
            label = "artifact-content-b64" if field == "content_b64" else "artifact-public-url"
            return (
                f"<{label} resolved at execution: artifact_id={content_ref} "
                f"filename={metadata['filename']} mime_type={metadata['mime_type']} "
                f"size_bytes={size}>"
            )
        raise ValueError(f"Unsupported artifact value ref field: {field}")

    async def _resolve_artifact_value_refs(
        self,
        value: Any,
        session: SessionModel,
        tool_call: ToolCall,
    ) -> Any:
        """Resolve exact-string artifact value refs in arbitrary tool arguments."""

        if isinstance(value, dict):
            return {
                str(key): await self._resolve_artifact_value_refs(item, session, tool_call)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                await self._resolve_artifact_value_refs(item, session, tool_call) for item in value
            ]
        if not isinstance(value, str) or not value.startswith(_ARTIFACT_VALUE_REF_PREFIX):
            return value
        content_ref, field = self._parse_artifact_value_ref(value)
        scope_task_id = self._content_ref_scope_task_id(tool_call)
        if field == "content_b64":
            content, _mime_type, _filename = await self._load_binary_content_ref(
                content_ref,
                session.user_email,
                scope_task_id=scope_task_id,
            )
            if len(content) > _MAX_ARTIFACT_VALUE_REF_BYTES:
                raise ValueError(
                    "Artifact value ref payload is too large: "
                    f"{len(content)} bytes exceeds {_MAX_ARTIFACT_VALUE_REF_BYTES} bytes"
                )
            return base64.b64encode(content).decode("ascii")
        if field in {"filename", "mime_type", "size_bytes"}:
            metadata = await self._get_accessible_content_ref_metadata(
                content_ref,
                session.user_email,
                scope_task_id=scope_task_id,
            )
            return metadata[field]
        if field in {"signed_url", "public_url"}:
            return await self._get_content_ref_public_url(
                content_ref,
                session.user_email,
                scope_task_id=scope_task_id,
            )
        raise ValueError(f"Unsupported artifact value ref field: {field}")

    async def _prepare_intaris_mcp_tool_call(
        self,
        tool_call: ToolCall,
        session: SessionModel,
    ) -> ToolCall:
        arguments = await self._resolve_artifact_value_refs(tool_call.arguments, session, tool_call)
        return tool_call.model_copy(update={"arguments": arguments})

    @staticmethod
    def _parse_artifact_value_ref(ref: str) -> tuple[str, str]:
        raw = ref[len(_ARTIFACT_VALUE_REF_PREFIX) :].strip()
        if "." not in raw:
            raise ValueError("Artifact value ref must use $artifact:<artifact_id>.<field> syntax")
        content_ref, field = raw.rsplit(".", 1)
        content_ref = content_ref.strip()
        field = field.strip()
        if not content_ref:
            raise ValueError("Artifact value ref is missing artifact_id")
        if field not in _ARTIFACT_VALUE_REF_FIELDS:
            supported = ", ".join(sorted(_ARTIFACT_VALUE_REF_FIELDS))
            raise ValueError(
                f"Unsupported artifact value ref field: {field} (expected {supported})"
            )
        return content_ref, field

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
            if self.credentials_provider is None:
                raise CredentialAccessError(
                    "credentials_unavailable",
                    "Credential resolution not available for auth_state_ref.",
                    hint=_AUTH_STATE_KIND_HINT,
                )
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
        resolution = await self.credentials_provider.resolve_ref(
            ref, agent=agent, user_email=session.user_email
        )
        return cast(CredentialResolution, resolution)

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
        allowed = set(agent.permissions.allowed_credentials if agent.permissions else [])
        existing = None
        get_credential = getattr(self.credentials_provider, "get_credential", None)
        if callable(get_credential):
            existing = await get_credential(credential_id, session.user_email)
        if existing is not None and credential_id not in allowed:
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
        grant_credential_to_agent_definition(agent, created.credential_id)
        granted = False
        if self._session_factory is not None:
            async with self._session_factory() as db:
                granted = await grant_credential_to_agent(
                    db,
                    agent_id=agent.agent_id,
                    credential_id=created.credential_id,
                    owner_email=session.user_email,
                )
                if granted:
                    await db.commit()
        next_metadata = {k: v for k, v in result.metadata.items() if k != "browser_auth_state"}
        next_metadata["saved_credential_id"] = created.credential_id
        next_metadata["credential_granted_to_agent"] = True
        if granted:
            next_metadata["agent_permissions_updated"] = True
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
            conversation_id=raw.get("conversation_id")
            if isinstance(raw.get("conversation_id"), str)
            else None,
            task_id=raw.get("task_id") if isinstance(raw.get("task_id"), str) else None,
            step_name=raw.get("step_name") if isinstance(raw.get("step_name"), str) else None,
            step_run_id=raw.get("step_run_id") if isinstance(raw.get("step_run_id"), str) else None,
            parent_session_id=raw.get("parent_session_id")
            if isinstance(raw.get("parent_session_id"), str)
            else None,
            delegation_mode=raw.get("delegation_mode")
            if isinstance(raw.get("delegation_mode"), str)
            else None,
            workflow_step=bool(raw.get("workflow_step")),
            interaction_mode=raw.get("interaction_mode")
            if isinstance(raw.get("interaction_mode"), str)
            else None,
            session_policy=raw.get("session_policy")
            if isinstance(raw.get("session_policy"), dict)
            else None,
        )

    async def _load_text_content_ref(
        self,
        artifact_id: str,
        user_email: str,
        *,
        scope_task_id: str | None = None,
    ) -> str:
        if is_deliverable_ref(artifact_id):
            if self._session_factory is None:
                raise ValueError("Artifact support not available")
            async with self._session_factory() as db_session:
                ref = await get_accessible_deliverable_ref(
                    db_session, artifact_id, user_email, scope_task_id=scope_task_id
                )
            if ref is None:
                raise ValueError(f"Artifact not found: {artifact_id}")
            return ref.deliverable.content
        return await self._load_text_artifact(artifact_id, user_email)

    async def _load_binary_content_ref(
        self,
        artifact_id: str,
        user_email: str,
        *,
        scope_task_id: str | None = None,
    ) -> tuple[bytes, str, str]:
        if is_deliverable_ref(artifact_id):
            if self._session_factory is None:
                raise ValueError("Artifact support not available")
            async with self._session_factory() as db_session:
                ref = await get_accessible_deliverable_ref(
                    db_session, artifact_id, user_email, scope_task_id=scope_task_id
                )
            if ref is None:
                raise ValueError(f"Artifact not found: {artifact_id}")
            return ref.content_bytes, ref.mime_type, ref.filename
        return await self._load_binary_artifact(artifact_id, user_email)

    async def _get_accessible_content_ref_metadata(
        self,
        artifact_id: str,
        user_email: str,
        *,
        scope_task_id: str | None = None,
    ) -> dict[str, Any]:
        if is_deliverable_ref(artifact_id):
            if self._session_factory is None:
                raise ValueError("Artifact support not available")
            async with self._session_factory() as db_session:
                ref = await get_accessible_deliverable_ref(
                    db_session, artifact_id, user_email, scope_task_id=scope_task_id
                )
            if ref is None:
                raise ValueError(f"Artifact not found: {artifact_id}")
            return {
                "filename": ref.filename,
                "mime_type": ref.mime_type,
                "size_bytes": ref.size_bytes,
            }
        row = await self._get_accessible_artifact_record(artifact_id, user_email)
        return {
            "filename": row.filename,
            "mime_type": row.mime_type,
            "size_bytes": row.size_bytes,
        }

    async def _get_content_ref_public_url(
        self,
        artifact_id: str,
        user_email: str,
        *,
        scope_task_id: str | None = None,
        ttl_seconds: int = 3600,
    ) -> str:
        if self.artifact_store is None:
            raise ValueError("Artifact support not available")
        ttl_seconds = max(60, min(int(ttl_seconds), 604800))
        if is_deliverable_ref(artifact_id):
            if self._session_factory is None:
                raise ValueError("Artifact support not available")
            async with self._session_factory() as db_session:
                ref = await get_accessible_deliverable_ref(
                    db_session, artifact_id, user_email, scope_task_id=scope_task_id
                )
            if ref is None:
                raise ValueError(f"Artifact not found: {artifact_id}")
            return build_deliverable_public_url(
                self.artifact_store,
                ref,
                ttl_seconds=ttl_seconds,
            )
        row = await self._get_accessible_artifact_record(artifact_id, user_email)
        url = await self.artifact_store.async_get_public_url(
            row.namespace,
            row.object_id,
            row.filename,
            ttl_seconds=ttl_seconds,
        )
        return str(url)

    async def _load_text_artifact(self, artifact_id: str, user_email: str) -> str:
        if self.artifact_store is None:
            raise ValueError("Artifact support not available")
        row = await self._get_accessible_artifact_record(artifact_id, user_email)
        content, _content_type = await self.artifact_store.async_load(
            row.namespace, row.object_id, row.filename
        )
        return str(content.decode("utf-8", errors="replace"))

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
        if row is None or row.status == "deleted" or self._artifact_row_expired(row):
            raise ValueError(f"Artifact not found: {artifact_id}")
        if row.owner_email and row.owner_email != user_email:
            raise ValueError(f"Artifact access denied: {artifact_id}")
        return row

    @staticmethod
    def _artifact_row_expired(row: Any) -> bool:
        expires_at = getattr(row, "expires_at", None)
        if expires_at is None:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return bool(expires_at <= datetime.now(UTC))

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
        attachment_anchors = self._attachment_output_anchors(enriched_output, materialized)
        next_metadata = dict(result.metadata or {})
        if attachment_anchors:
            existing_anchors = next_metadata.get("output_anchors")
            merged_anchors = list(existing_anchors) if isinstance(existing_anchors, list) else []
            seen_anchor_names = {
                item.get("anchor")
                for item in merged_anchors
                if isinstance(item, dict) and isinstance(item.get("anchor"), str)
            }
            for anchor in attachment_anchors:
                if anchor["anchor"] not in seen_anchor_names:
                    merged_anchors.append(anchor)
                    seen_anchor_names.add(anchor["anchor"])
            next_metadata["output_anchors"] = merged_anchors
        metadata_changed = next_metadata != (result.metadata or {})
        if not changed and enriched_output == result.output and not metadata_changed:
            return result
        return result.model_copy(
            update={
                "attachments": materialized,
                "output": enriched_output,
                "metadata": next_metadata,
            }
        )

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
        materialized = [item for item in attachments if isinstance(item, dict)]
        primary = next(iter(materialized), None)
        if primary is None:
            return output
        enriched = {
            "artifact_id": primary.get("artifact_id"),
            "url": primary.get("url"),
            "mime_type": primary.get("mime_type"),
            "size_bytes": primary.get("size_bytes"),
        }
        output = self._replace_generic_attachment_guidance(output, primary)
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
        guidance = self._attachment_guidance_block(materialized)
        if not output.strip():
            return guidance or summary
        sections = [output, f"Attachment metadata: {summary}"]
        if guidance:
            sections.append(guidance)
        return "\n\n".join(sections)

    @staticmethod
    def _replace_generic_attachment_guidance(output: str, attachment: dict[str, Any]) -> str:
        artifact_id = attachment.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            return output
        updated = output.replace(
            "Binary content: attached as artifact",
            f'Binary content: attached as artifact (artifact_id="{artifact_id}")',
            1,
        )
        updated = updated.replace(
            "Use artifact_read to analyze this image with a vision-capable model.",
            f'Use artifact_read with artifact_id="{artifact_id}" to analyze this image with a vision-capable model.',
            1,
        )
        return updated

    @staticmethod
    def _attachment_output_anchors(
        output: str,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        lines = output.splitlines() or [""]
        start_line = 1
        for index, line in enumerate(lines, start=1):
            if line.strip() in {"[[attachments]]", "[[metadata]]"} or "Binary content:" in line:
                start_line = index
                break
        end_line = max(start_line, len(lines))

        anchors: list[dict[str, Any]] = []
        for index, item in enumerate(attachments, start=1):
            artifact_id = item.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                continue
            candidate = {
                "source_type": "artifact_id",
                "artifact_id": artifact_id,
                "mime_hint": item.get("mime_type"),
                "filename_hint": item.get("filename"),
            }
            if index == 1:
                anchors.append(
                    {
                        "anchor": "binary",
                        "kind": str(item.get("kind") or "attachment"),
                        "label": str(item.get("filename") or artifact_id),
                        "start_line": start_line,
                        "end_line": end_line,
                        "artifact_candidate": candidate,
                    }
                )
            anchors.append(
                {
                    "anchor": f"attachment:{index}",
                    "kind": str(item.get("kind") or "attachment"),
                    "label": str(item.get("filename") or artifact_id),
                    "start_line": start_line,
                    "end_line": end_line,
                    "artifact_candidate": candidate,
                }
            )
        return anchors

    def _attachment_guidance_block(self, attachments: list[dict[str, Any]]) -> str:
        lines: list[str] = ["[[attachments]]"]
        count = 0
        for item in attachments:
            artifact_id = item.get("artifact_id")
            if not artifact_id:
                continue
            count += 1
            if count > 1:
                lines.append("")
            lines.append(f"Artifact ID: {artifact_id}")
            filename = item.get("filename")
            if filename:
                lines.append(f"Filename: {filename}")
            mime_type = item.get("mime_type")
            if mime_type:
                lines.append(f"MIME type: {mime_type}")
            kind = item.get("kind")
            if kind:
                lines.append(f"Kind: {kind}")
            size_bytes = item.get("size_bytes")
            if size_bytes is not None:
                lines.append(f"Size bytes: {size_bytes}")
            url = item.get("url")
            if url:
                lines.append(f"URL: {url}")
        if count == 0:
            return ""
        first_id = next(
            str(item["artifact_id"])
            for item in attachments
            if isinstance(item.get("artifact_id"), str) and item.get("artifact_id")
        )
        lines.extend(
            [
                "",
                f'Use artifact_read with artifact_id="{first_id}" to inspect or analyze the attachment.',
                f'Use artifact_get_url with artifact_id="{first_id}" to view or download it.',
            ]
        )
        return "\n".join(lines)

    def _is_non_bypassable(self, tool_name: str, explicit_flag: bool) -> bool:
        if explicit_flag:
            return True
        return any(fnmatchcase(tool_name, pattern) for pattern in self.non_bypassable_patterns)

    def _plan_mode_denial_result(
        self, tool_call: ToolCall, registry: ToolRegistry
    ) -> ToolResult | None:
        registered_tool = registry.get(tool_call.name)
        if registered_tool is None:
            return None
        if tool_call.runtime_metadata.get("read_only_required") is not True:
            return None
        if not is_plan_hidden_tool(registered_tool.definition):
            return None
        return self._sanitize_result(
            tool_call.name,
            ToolResult(
                output=(
                    "Plan mode is active for this turn. Write tools are disabled "
                    "because the agent must not make changes while planning."
                ),
                is_error=True,
                metadata={"code": "plan_mode_mutation_denied"},
            ),
            _tool_max_size(registry, tool_call.name),
            call_id=tool_call.call_id,
            runtime_metadata=tool_call.runtime_metadata,
        )

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
            context=await self._evaluation_context(tool_call, registered_tool.definition),
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
        content_trust: str = "untrusted",
    ) -> ToolResult:
        del tool_name
        raw_output = result.output
        token_counter = None
        max_tokens = None
        model = None
        if runtime_metadata is not None:
            resolved_model = runtime_metadata.get("resolved_model")
            if isinstance(resolved_model, str) and resolved_model.strip():
                model = resolved_model
        if self.llm is not None and model is not None:
            model_id = model
            llm = self.llm

            def token_counter(text: str) -> int:
                return int(llm.count_tokens(text, model_id))

            max_tokens = max(256, max_size // 4)
        if call_id and _has_artifact_candidate_anchor(result.metadata):
            raw_output = raw_output.replace("tool_artifact:<call_id>:", f"tool_artifact:{call_id}:")
            raw_output = raw_output.replace(
                "tool_artifact:<tool_call_id>:", f"tool_artifact:{call_id}:"
            )
            if isinstance(result.metadata, dict) and isinstance(
                result.metadata.get("stored_output"), str
            ):
                stored_output = result.metadata["stored_output"]
                result = result.model_copy(
                    update={
                        "metadata": {
                            **result.metadata,
                            "stored_output": stored_output.replace(
                                "tool_artifact:<call_id>:", f"tool_artifact:{call_id}:"
                            ).replace("tool_artifact:<tool_call_id>:", f"tool_artifact:{call_id}:"),
                        }
                    }
                )
        anchor_names = _extract_output_anchor_names(result.metadata, raw_output)
        presentation = present_tool_output(
            raw_output,
            max_size,
            recovery_call_id=call_id,
            has_full_output=True,
            token_counter=token_counter,
            max_tokens=max_tokens,
            anchors=anchor_names,
        )
        rendered_output = presentation.result
        if content_trust == "untrusted":
            rendered_output = (
                '<tool_result trust="untrusted">\n'
                f"{_neutralize_tool_result_closing_tags(rendered_output)}\n"
                "</tool_result>"
            )
        metadata = dict(result.metadata or {})
        metadata["wrapped"] = content_trust == "untrusted"
        metadata["content_trust"] = content_trust
        metadata["truncated"] = presentation.truncated
        metadata["original_size"] = len(raw_output)
        metadata.update(presentation.event_fields())
        if max_tokens is not None:
            metadata["token_budget"] = max_tokens
        # Preserve raw output for the ToolOutputStore (before wrapping/truncation).
        # The agent loop reads this to save the full output to disk.
        metadata["_raw_output"] = raw_output
        return result.model_copy(update={"output": rendered_output, "metadata": metadata})


_TOOL_RESULT_CLOSE_TAG_RE = re.compile(r"</\s*tool_result\s*>", re.IGNORECASE)


def _neutralize_tool_result_closing_tags(text: str) -> str:
    """Prevent embedded tool_result closing tags from escaping the trust wrapper."""

    return _TOOL_RESULT_CLOSE_TAG_RE.sub("<\u200b/tool_result>", text)


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

    if raw_output:
        existing_anchor_refs: list[dict[str, Any]] = []
        if metadata is not None and isinstance(metadata.get("output_anchors"), list):
            for entry in metadata["output_anchors"]:
                if isinstance(entry, dict):
                    existing_anchor_refs.append(entry)
                elif isinstance(entry, str):
                    existing_anchor_refs.append({"anchor": entry})
        else:
            existing_anchor_refs = [{"anchor": name} for name in names]
        for entry in markdown_heading_anchors(
            raw_output,
            existing_anchors=existing_anchor_refs,
        ):
            _add(entry.get("anchor"))

    return names


def _has_artifact_candidate_anchor(metadata: dict[str, Any] | None) -> bool:
    if metadata is None:
        return False
    raw_anchors = metadata.get("output_anchors")
    if not isinstance(raw_anchors, list):
        return False
    return any(
        isinstance(entry, dict) and isinstance(entry.get("artifact_candidate"), dict)
        for entry in raw_anchors
    )


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
