"""Intaris HTTP provider with retry and circuit breaker protection."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, TypeVar, cast, get_args

import httpx
from prometheus_client import Counter, Histogram
from pydantic_core.core_schema import ErrorType

from cognis.core.workflow_step_scope import (
    WorkflowEvaluationBinding,
    WorkflowStepDeclaration,
    WorkflowStepDeclarationError,
    WorkflowStepReceipt,
    canonical_digest,
)
from cognis.logging import get_logger
from cognis.models.config import ProviderHealth
from cognis.models.search import (
    SearchHealth,
    SearchRequest,
    SearchResponse,
    SearchSessionsRequest,
    SearchSessionsResponse,
)
from cognis.models.session import (
    EventAppendResult,
    EventReadResult,
    IntarisSession,
    IntarisSessionSummaries,
    ReasoningReportResult,
    SessionEvent,
)
from cognis.models.tool import EscalationRecord, EvaluationResult, ToolResult
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.circuit_breaker import CircuitBreaker
from cognis.providers.guardrails.conflicts import EvaluationConflict
from cognis.providers.guardrails.errors import IntarisHTTPError, IntarisTransportError
from cognis.providers.guardrails.event_keys import validate_event_idempotency_key
from cognis.providers.guardrails.events import (
    EventAppendListener,
    EventAppendNotification,
    EventStoreAuthority,
)
from cognis.providers.guardrails.mcp_listing_cache import MCPListingCache
from cognis.providers.guardrails.process_scope import ProcessEvaluationBinding
from cognis.providers.guardrails.resolution_errors import ResolutionRejected
from cognis.providers.retry import is_retryable_http_error, with_retry
from cognis.runtime_context import current_agent_id, current_agent_owner_email, current_user_email

logger = get_logger(__name__)

T = TypeVar("T")
MAX_EVENT_APPEND_LISTENERS = 16
DEFAULT_EVENT_APPEND_LISTENER_TIMEOUT = 0.1
INTARIS_OPERATION_DURATION = Histogram(
    "cognis_intaris_operation_duration_seconds",
    "Duration of Intaris operations including retries.",
    ["operation", "outcome"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
INTARIS_OPERATION_FAILURES = Counter(
    "cognis_intaris_operation_failures_total",
    "Exhausted Intaris operation failures.",
    ["operation", "error_type"],
)


def _coerce_attachment_b64(value: str) -> str:
    try:
        base64.b64decode(value, validate=True)
        return value
    except Exception:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _safe_evaluation_error_detail(response: httpx.Response) -> dict[str, Any]:
    """Project only known validation fields and numeric model capacity diagnostics."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if response.status_code == 422 and isinstance(detail, list):
        fields = {
            "body",
            "session_id",
            "agent_id",
            "tool",
            "args",
            "context",
            "minimum_outcome",
            "approval_call_id",
            "intention_pending",
            "evaluation_policy_version",
            "logical_call_id",
            "expected_policy_revision",
            "expected_policy_hash",
            "controller_context",
        }
        errors = []
        for error in detail[:8]:
            if not isinstance(error, dict):
                continue
            loc = error.get("loc")
            safe_loc = (
                [
                    part if isinstance(part, str) and part in fields else "[redacted]"
                    for part in loc[:2]
                ]
                if isinstance(loc, list)
                else []
            )
            error_type = error.get("type")
            size_error = isinstance(error_type, str) and error_type in {
                "args_too_large",
                "context_too_large",
            }
            safe_error: dict[str, Any] = {
                "loc": safe_loc,
                "type": error_type
                if size_error or error_type in get_args(ErrorType)
                else "unknown",
            }
            if size_error and isinstance(error.get("ctx"), dict):
                for key in ("size", "limit"):
                    count = error["ctx"].get(key)
                    if type(count) is int and 0 <= count <= 2**63 - 1:
                        safe_error[key] = count
            errors.append(safe_error)
        return {"validation": errors}
    if (
        response.status_code == 413
        and isinstance(detail, dict)
        and detail.get("code") == "context_insufficient"
    ):
        view = detail.get("model_view")
        counts = {
            key: value
            for key, value in (view.items() if isinstance(view, dict) else ())
            if key
            in {
                "bytes",
                "characters",
                "estimated_input_tokens",
                "framing_tokens",
                "margin_tokens",
                "input_ceiling_tokens",
                "requested_output_reserve",
                "application_fallback_bytes",
            }
            and type(value) is int
            and 0 <= value <= 2**63 - 1
        }
        return {"code": "context_insufficient", "model_view": counts}
    return {}


class IntarisProvider:
    """HTTP client for Intaris with fail-closed evaluate semantics.

    All methods are protected by retry with exponential backoff and a
    circuit breaker.  ``evaluate()`` uses a dedicated circuit breaker
    to prevent data-plane failures (e.g. large event batch timeouts)
    from blocking tool safety evaluation.
    """

    def __init__(
        self,
        base_url: str,
        auth_provider: Any,
        user_email: str = SYSTEM_USER_EMAIL,
        *,
        event_append_listener_timeout: float = DEFAULT_EVENT_APPEND_LISTENER_TIMEOUT,
        evaluation_timeout: float = 65.0,
    ) -> None:
        if event_append_listener_timeout <= 0:
            raise ValueError("event_append_listener_timeout must be positive")
        if evaluation_timeout <= 0:
            raise ValueError("evaluation_timeout must be positive")
        self.base_url = base_url.rstrip("/")
        self.auth_provider = auth_provider
        self._process_scope_supported: bool | None = None
        self.user_email = user_email
        self.client = httpx.AsyncClient(base_url=self.base_url, timeout=10)
        # Only evaluation needs the sequential review budget; other endpoints
        # retain 10 s. This exceeds Intaris's shared 60 s request deadline.
        self._evaluation_timeout = evaluation_timeout
        # Separate circuit breakers by endpoint family so a failure in one
        # operational path does not unnecessarily poison unrelated calls.
        self.eval_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            should_trip=lambda exc: not isinstance(exc, EvaluationConflict),
        )
        self.reasoning_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self.session_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            should_trip=lambda exc: not isinstance(exc, ResolutionRejected),
        )
        self.events_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            should_trip=is_retryable_http_error,
        )
        self.mcp_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self.search_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self._mcp_listing_cache = MCPListingCache()
        self._event_append_listeners: list[EventAppendListener] = []
        self._event_append_listener_timeout = event_append_listener_timeout

    def add_event_append_listener(self, listener: EventAppendListener) -> bool:
        """Register one listener by identity within the fixed provider bound."""
        if any(registered is listener for registered in self._event_append_listeners):
            return False
        if len(self._event_append_listeners) >= MAX_EVENT_APPEND_LISTENERS:
            raise RuntimeError("maximum event append listener count reached")
        self._event_append_listeners.append(listener)
        return True

    def remove_event_append_listener(self, listener: EventAppendListener) -> bool:
        """Remove one listener by identity."""
        for index, registered in enumerate(self._event_append_listeners):
            if registered is listener:
                del self._event_append_listeners[index]
                return True
        return False

    @staticmethod
    def _event_store_authority(
        *,
        user_email: str | None,
        agent_id: str | None,
        agent_owner_email: str | None,
    ) -> EventStoreAuthority | None:
        values = (
            user_email if user_email is not None else current_user_email.get(),
            agent_id if agent_id is not None else current_agent_id.get(),
            (
                agent_owner_email
                if agent_owner_email is not None
                else current_agent_owner_email.get()
            ),
        )
        if not all(isinstance(value, str) and value.strip() for value in values):
            return None
        try:
            complete_values = cast(tuple[str, str, str], values)
            return EventStoreAuthority(*complete_values)
        except ValueError:
            return None

    async def _notify_event_append(self, notification: EventAppendNotification) -> None:
        listeners = tuple(self._event_append_listeners)
        if not listeners:
            return

        async def _invoke(listener: EventAppendListener) -> str:
            try:
                await asyncio.wait_for(
                    listener(notification),
                    timeout=self._event_append_listener_timeout,
                )
            except TimeoutError:
                return "timeout"
            except Exception:
                return "error"
            return "ok"

        statuses = await asyncio.gather(*(_invoke(listener) for listener in listeners))
        failed = sum(status != "ok" for status in statuses)
        if failed:
            logger.warning(
                "intaris: event append listener notification incomplete",
                extra={
                    "extra_data": {
                        "listener_count": len(listeners),
                        "failed_count": failed,
                        "timeout_count": statuses.count("timeout"),
                    }
                },
            )

    async def _call_with_retry(
        self,
        fn: Callable[..., Awaitable[T]],
        *args: Any,
        max_retries: int = 3,
        base_delay: float = 1.0,
        operation: str = "intaris call",
        breaker: CircuitBreaker | None = None,
        **kwargs: Any,
    ) -> T:
        """Execute a provider call with retry inside circuit breaker.

        Retry handles transient failures (timeouts, 5xx).  The circuit
        breaker trips after repeated exhausted-retry sequences to avoid
        hammering a down service.
        """

        async def _with_retries() -> T:
            return await with_retry(
                fn,
                *args,
                max_retries=max_retries,
                base_delay=base_delay,
                operation=operation,
                **kwargs,
            )

        started = perf_counter()
        metric_operation = operation.split("(", 1)[0].removeprefix("intaris ")
        try:
            result = (
                await breaker.call(_with_retries) if breaker is not None else await _with_retries()
            )
        except Exception as exc:
            INTARIS_OPERATION_DURATION.labels(
                operation=metric_operation,
                outcome="failure",
            ).observe(perf_counter() - started)
            INTARIS_OPERATION_FAILURES.labels(
                operation=metric_operation,
                error_type=type(exc).__name__,
            ).inc()
            if isinstance(exc, httpx.ConnectError):
                request = getattr(exc, "request", None)
                target = getattr(request, "url", self.client.base_url)
                logger.warning(
                    "intaris: connection failed",
                    extra={
                        "extra_data": {
                            "operation": metric_operation,
                            "scheme": target.scheme,
                            "host": target.host,
                            "port": target.port,
                            "error_type": type(exc).__name__,
                        }
                    },
                )
            if isinstance(exc, httpx.HTTPStatusError):
                raise IntarisHTTPError(exc, operation=metric_operation) from exc
            if isinstance(exc, httpx.RequestError):
                raise IntarisTransportError(exc, operation=metric_operation) from exc
            raise
        INTARIS_OPERATION_DURATION.labels(
            operation=metric_operation,
            outcome="success",
        ).observe(perf_counter() - started)
        return result

    def _headers(
        self,
        agent_id: str = "system",
        user_email: str | None = None,
        agent_owner_email: str | None = None,
    ) -> dict[str, str]:
        subject = user_email or current_user_email.get() or self.user_email
        resolved_agent_id = (
            agent_id if agent_id != "system" else (current_agent_id.get() or "system")
        )
        resolved_agent_owner = agent_owner_email or current_agent_owner_email.get() or subject
        return {
            "Authorization": f"Bearer {self.auth_provider.sign_service_jwt(subject, resolved_agent_id, ['intaris'], agent_owner_email=resolved_agent_owner)}",
            "X-Agent-Id": resolved_agent_id,
            "X-Agent-Owner": resolved_agent_owner,
        }

    async def declare_workflow_step(
        self,
        declaration: WorkflowStepDeclaration,
        *,
        user_email: str,
        agent_id: str,
    ) -> WorkflowStepReceipt:
        """Declare exact controller scope without creating or updating an intention."""
        from cognis.core.workflow_step_scope import (
            WorkflowStepDeclarationError,
            WorkflowStepReceipt,
        )

        try:
            token = self.auth_provider.sign_workflow_step_jwt(
                user_email,
                agent_id,
                session_id=declaration.session_id,
                governing_execution_id=declaration.step_run_id,
                governing_snapshot_id=declaration.workflow_definition_digest,
                declaration_digest=declaration.declaration_digest,
            )
        except (AttributeError, ValueError, RuntimeError) as exc:
            raise WorkflowStepDeclarationError("workflow_step.declaration_auth_failed") from exc

        async def _do() -> WorkflowStepReceipt:
            response = await self.client.post(
                "/api/v1/intention",
                json={
                    "session_id": declaration.session_id,
                    "workflow_step": declaration.model_dump(mode="json"),
                },
                headers={"Authorization": f"Bearer {token}", "X-Agent-Id": agent_id},
            )
            if not response.is_success:
                code = (
                    "workflow_step_conflict"
                    if response.status_code == 409
                    else "workflow_step.declaration_auth_failed"
                    if response.status_code in {401, 403}
                    else "workflow_step.declaration_schema_failed"
                    if response.status_code in {400, 422}
                    else "workflow_step.declaration_http_failed"
                )
                raise WorkflowStepDeclarationError(code)
            try:
                receipt = WorkflowStepReceipt.model_validate(response.json())
            except ValueError as exc:
                raise WorkflowStepDeclarationError(
                    "workflow_step.declaration_schema_failed"
                ) from exc
            if receipt.declaration_digest != declaration.declaration_digest:
                raise WorkflowStepDeclarationError("workflow_step.declaration_receipt_mismatch")
            return receipt

        try:
            # A failed declaration never falls back to evaluate or session creation.
            return await self._call_with_retry(
                _do, max_retries=0, operation="intaris declare_workflow_step"
            )
        except httpx.HTTPError as exc:
            raise WorkflowStepDeclarationError(
                "workflow_step.declaration_transport_failed"
            ) from exc

    async def create_session(
        self,
        session_id: str,
        intention: str,
        agent_id: str,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        policy: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
        analytics_enabled: bool = True,
        guardrails_enabled: bool = True,
    ) -> None:
        flags = self._session_flags(analytics_enabled, guardrails_enabled)
        if len(flags) != 2:
            raise ValueError("Session creation requires boolean analytics and guardrails flags")
        logger.info(
            "intaris: create_session",
            extra={
                "extra_data": {
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "parent_session_id": parent_session_id,
                }
            },
        )

        async def _verify_existing_session() -> None:
            response = await self.client.get(
                f"/api/v1/session/{session_id}",
                headers=self._headers(agent_id, user_id),
            )
            response.raise_for_status()
            stored = response.json()
            self._verify_session_flags(flags, stored)
            if parent_session_id and stored.get("parent_session_id") != parent_session_id:
                raise ValueError("Intaris did not acknowledge the delegated parent.")
            self._verify_policy_ack(policy or {}, stored)

        async def _do() -> None:
            response = await self.client.post(
                "/api/v1/intention",
                json={
                    "session_id": session_id,
                    "intention": intention,
                    "details": details or {},
                    "policy": policy or {},
                    "parent_session_id": parent_session_id,
                    **flags,
                },
                headers=self._headers(agent_id, user_id),
            )
            if response.status_code == 409:
                # A retry may follow a timeout after Intaris durably created
                # the requested session. Verify that exact session rather
                # than turning an ambiguous create into workflow failure.
                await self._call_with_retry(
                    _verify_existing_session,
                    max_retries=2,
                    operation=f"intaris verify existing session({session_id})",
                )
                logger.info(
                    "intaris: session already existed after create retry",
                    extra={"extra_data": {"session_id": session_id, "agent_id": agent_id}},
                )
                return
            if not response.is_success:
                logger.error(
                    "intaris: create_session failed",
                    extra={
                        "extra_data": {
                            "session_id": session_id,
                            "status_code": response.status_code,
                        }
                    },
                )
            response.raise_for_status()
            created = response.json()
            self._verify_session_flags(flags, created)
            if parent_session_id:
                # Verify canonical source composition, including the stored
                # parent relationship and session-level inherited bounds.
                await _verify_existing_session()
                return
            if not isinstance(created.get("policy"), dict) and any(
                (policy or {}).get(field) is not None
                for field in ("minimum_outcome", "maximum_outcome")
            ):
                # The compact create response contains only active_policy;
                # request/session bounds are deliberately not in that snapshot.
                await _verify_existing_session()
                return
            if isinstance(created.get("active_policy"), dict) and (
                isinstance(created.get("policy"), dict)
                or (policy or {}).get("maximum_outcome") is None
            ):
                self._verify_policy_ack(policy or {}, created, delegated=bool(parent_session_id))
            elif (policy or {}).get("evaluation_policy_version") == "hybrid_v1" and not (
                isinstance(created.get("active_policy"), dict)
            ):
                raise ValueError("Intaris did not acknowledge active_policy.")
            elif (policy or {}).get("maximum_outcome") is not None:
                # Intaris responds to intention declaration with {"ok": true};
                # check the persisted session before accepting a permissive policy.
                await _verify_existing_session()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris create_session({session_id})",
            breaker=self.session_breaker,
        )
        logger.info(
            "intaris: session created",
            extra={"extra_data": {"session_id": session_id, "agent_id": agent_id}},
        )

    async def update_session_policy(
        self,
        session_id: str,
        *,
        agent_id: str,
        user_id: str | None = None,
        details: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
        agent_owner_email: str | None = None,
        analytics_enabled: bool | None = None,
        guardrails_enabled: bool | None = None,
    ) -> dict[str, Any]:
        flags = self._session_flags(analytics_enabled, guardrails_enabled)
        logger.info(
            "intaris: update_session_policy",
            extra={"extra_data": {"session_id": session_id, "agent_id": agent_id}},
        )

        async def _do() -> dict[str, Any]:
            response = await self.client.patch(
                f"/api/v1/session/{session_id}",
                json={"details": details or {}, "policy": policy or {}, **flags},
                headers=self._headers(agent_id, user_id, agent_owner_email),
            )
            if not response.is_success:
                logger.error(
                    "intaris: update_session_policy failed",
                    extra={
                        "extra_data": {
                            "session_id": session_id,
                            "status_code": response.status_code,
                        }
                    },
                )
            response.raise_for_status()
            self._verify_policy_ack(policy or {}, response.json())
            if flags:
                # Read the durable owner, not a local provider preference cache.
                canonical = await self.client.get(
                    f"/api/v1/session/{session_id}",
                    headers=self._headers(agent_id, user_id, agent_owner_email),
                )
                canonical.raise_for_status()
                self._verify_session_flags(flags, canonical.json())
            return dict(response.json().get("policy") or policy or {})

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris update_session_policy({session_id})",
            breaker=self.session_breaker,
        )

    @staticmethod
    def _session_flags(
        analytics_enabled: bool | None, guardrails_enabled: bool | None
    ) -> dict[str, bool]:
        flags = {}
        for name, value in (
            ("analytics_enabled", analytics_enabled),
            ("guardrails_enabled", guardrails_enabled),
        ):
            if value is not None:
                if type(value) is not bool:
                    raise ValueError(f"{name} must be a boolean")
                flags[name] = value
        return flags

    @staticmethod
    def _verify_session_flags(flags: dict[str, bool], response: dict[str, Any]) -> None:
        for name, expected in flags.items():
            actual = response.get(name)
            if type(actual) is not bool or actual is not expected:
                raise ValueError(f"Intaris did not acknowledge {name}.")

    @staticmethod
    def _verify_policy_ack(
        policy: dict[str, Any], response: dict[str, Any], *, delegated: bool = False
    ) -> None:
        """Validate activated policy; tolerate the deployed legacy schema's omitted version.

        The hybrid ``active_policy`` snapshot carries version and profile, but
        ``maximum_outcome`` is a session policy field returned in ``policy``.
        """
        acknowledged = response.get("active_policy")
        stored = response.get("policy")
        delegated = delegated or bool(response.get("parent_session_id"))
        if (
            not delegated
            and policy.get("evaluation_policy_version") == "hybrid_v1"
            and not isinstance(acknowledged, dict)
        ):
            raise ValueError("Intaris did not acknowledge active_policy.")
        if not isinstance(acknowledged, dict):
            acknowledged = stored or {}
        if not isinstance(acknowledged, dict):
            raise ValueError("Intaris did not acknowledge policy.")
        if not isinstance(stored, dict):
            stored = acknowledged
        version = policy.get("evaluation_policy_version")
        if delegated:
            # Activation, not an earlier potentially stale parent GET, owns
            # inherited mode. This also handles nested parent-mode changes.
            version = acknowledged.get("evaluation_policy_version", "legacy")
            if version not in {"legacy", "hybrid_v1"} or (
                version == "hybrid_v1"
                and acknowledged.get("profile") not in {"strict", "balanced", "bounded_autonomy"}
            ):
                raise ValueError("Intaris did not acknowledge a valid inherited policy.")
            policy = {**policy, "evaluation_policy_version": version}
            if version == "hybrid_v1":
                policy["profile"] = acknowledged["profile"]
        if version == "hybrid_v1":
            for field in ("evaluation_policy_version", "profile"):
                if acknowledged.get(field) != policy.get(field):
                    raise ValueError(f"Intaris did not acknowledge {field}.")
        elif version == "legacy" and acknowledged.get("evaluation_policy_version") not in (
            None,
            "legacy",
        ):
            raise ValueError("Intaris did not acknowledge legacy evaluation.")
        actual_cap, requested_cap = stored.get("maximum_outcome"), policy.get("maximum_outcome")
        caps = {None: 0, "deny": 0, "escalate": 1, "approve": 2}
        valid_cap = actual_cap == requested_cap
        if delegated and actual_cap in caps and requested_cap in caps:
            valid_cap = (
                caps[actual_cap] <= caps[requested_cap] if requested_cap is not None else True
            )
        if not valid_cap:
            raise ValueError("Intaris did not acknowledge maximum_outcome.")
        actual_floor, requested_floor = stored.get("minimum_outcome"), policy.get("minimum_outcome")
        floors = {None: 0, "approve": 0, "escalate": 1, "deny": 2}
        if (
            actual_floor not in floors
            or requested_floor not in floors
            or floors[actual_floor] < floors[requested_floor]
        ):
            raise ValueError("Intaris did not acknowledge minimum_outcome.")

    async def evaluate(
        self,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
        *,
        workflow_binding: WorkflowEvaluationBinding | None = None,
        process_binding: ProcessEvaluationBinding | None = None,
    ) -> EvaluationResult:
        logger.debug(
            "intaris: evaluate",
            extra={"extra_data": {"session_id": session_id, "tool_name": tool_name}},
        )
        from cognis.providers.guardrails.evaluation_context import fit_evaluation_context

        # Fit after the router adds floors/receipts; arguments and binding hashes
        # remain untouched. Oversized protected context fails before dispatch.
        context = fit_evaluation_context(context)

        async def _do() -> EvaluationResult:
            extensions: dict[str, Any] = {}
            if workflow_binding is None:
                headers = self._headers(user_email=current_user_email.get())
            else:
                declaration = workflow_binding.declaration
                if (
                    declaration.session_id != session_id
                    or declaration.tool != tool_name
                    or declaration.evaluated_args_digest != canonical_digest(arguments)
                ):
                    raise WorkflowStepDeclarationError("workflow_step.declaration_mismatch")
                extensions = {
                    "logical_call_id": declaration.logical_call_id,
                    "controller_context": workflow_binding.controller_context,
                }
                token = self.auth_provider.sign_workflow_step_jwt(
                    workflow_binding.user_email,
                    workflow_binding.agent_id,
                    session_id=declaration.session_id,
                    governing_execution_id=declaration.step_run_id,
                    governing_snapshot_id=declaration.workflow_definition_digest,
                    declaration_digest=declaration.declaration_digest,
                )
                headers = {
                    "Authorization": f"Bearer {token}",
                    "X-Agent-Id": workflow_binding.agent_id,
                }
            if process_binding is not None:
                process_declaration = process_binding.declaration
                if (
                    process_declaration["session_id"] != session_id
                    or process_declaration["tool"] != tool_name
                    or process_declaration["evaluated_args_digest"] != canonical_digest(arguments)
                ):
                    raise ValueError("Process lifecycle binding mismatch")
                if self._process_scope_supported is None:
                    capability = await self.client.get(
                        "/api/v1/process-lifecycle-capabilities", headers=headers
                    )
                    if capability.status_code in {404, 405}:
                        self._process_scope_supported = False
                    else:
                        capability.raise_for_status()
                        self._process_scope_supported = 1 in capability.json().get("versions", [])
                if self._process_scope_supported:
                    workflow_claims = None
                    if workflow_binding is not None:
                        configured = workflow_binding.declaration
                        workflow_claims = {
                            "governing_execution_id": configured.step_run_id,
                            "governing_snapshot_id": configured.workflow_definition_digest,
                            "declaration_digest": configured.declaration_digest,
                        }
                    token = self.auth_provider.sign_process_scope_jwt(
                        process_binding.user_email,
                        process_binding.agent_id,
                        session_id=session_id,
                        process_scope_digest=process_binding.digest,
                        workflow_claims=workflow_claims,
                    )
                    headers = {
                        "Authorization": f"Bearer {token}",
                        "X-Agent-Id": process_binding.agent_id,
                    }
                    extensions["process_scope"] = process_declaration
            response = await self.client.post(
                "/api/v1/evaluate",
                json={
                    "session_id": session_id,
                    "tool": tool_name,
                    "args": arguments,
                    "context": context or {},
                    **(
                        {
                            "minimum_outcome": context["minimum_outcome"],
                        }
                        if context and context.get("minimum_outcome") is not None
                        else {}
                    ),
                    **(
                        {"approval_call_id": context["approval_call_id"]}
                        if context and context.get("approval_call_id") is not None
                        else {}
                    ),
                    **extensions,
                },
                headers=headers,
                timeout=self._evaluation_timeout,
            )
            if response.status_code == 409:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                conflict = EvaluationConflict.from_payload(payload)
                logger.warning(
                    "intaris: evaluate conflict",
                    extra={
                        "extra_data": {
                            "code": conflict.code,
                            "status_code": 409,
                            "session_id": session_id,
                            "tool_name": tool_name,
                            "call_id": (context or {}).get("tool_call_id"),
                        }
                    },
                )
                raise conflict
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if response.status_code not in {413, 422}:
                    raise
                detail = _safe_evaluation_error_detail(response)
                raise httpx.HTTPStatusError(
                    f"Intaris evaluation failed with HTTP {response.status_code}: "
                    f"{json.dumps(detail, separators=(',', ':'))}",
                    request=exc.request,
                    response=exc.response,
                ) from None
            data = response.json()
            if process_binding is not None and "process_scope" in extensions:
                acknowledged = (data.get("evaluation_metadata") or {}).get(
                    "process_lifecycle"
                ) or {}
                if (
                    acknowledged.get("authenticated") is True
                    and acknowledged.get("declaration") == process_binding.declaration
                ):
                    data["process_scope_digest"] = process_binding.digest
                elif data.get("decision") == "approve":
                    # A successful ordinary evaluation is not lifecycle admission.
                    data["process_scope_digest"] = None
            if (
                context
                and context.get("minimum_outcome") is not None
                and data.get("minimum_outcome") != context["minimum_outcome"]
            ):
                raise ValueError("Intaris did not acknowledge minimum_outcome.")
            return EvaluationResult.model_validate(data)

        return await self._call_with_retry(
            _do,
            max_retries=2,
            base_delay=0.5,
            respect_retry_after=True,
            operation=f"intaris evaluate({tool_name})",
            breaker=self.eval_breaker,
        )

    async def report_reasoning(
        self,
        session_id: str,
        content: str = "",
        context: str | None = None,
        *,
        from_events: bool = False,
        wait_for_intention: bool = False,
        wait_timeout_ms: int | None = None,
    ) -> ReasoningReportResult:
        body: dict[str, Any] = {"session_id": session_id, "content": content}
        if context is not None:
            body["context"] = context
        if from_events:
            body["from_events"] = True
        if wait_for_intention:
            body["wait_for_intention"] = True
        if wait_for_intention and wait_timeout_ms is not None:
            body["wait_timeout_ms"] = wait_timeout_ms

        async def _do() -> ReasoningReportResult:
            response = await self.client.post(
                "/api/v1/reasoning",
                json=body,
                headers=self._headers(user_email=current_user_email.get()),
            )
            if wait_for_intention and response.status_code in {400, 422}:
                fallback_body = dict(body)
                fallback_body.pop("wait_for_intention", None)
                fallback_body.pop("wait_timeout_ms", None)
                fallback = await self.client.post(
                    "/api/v1/reasoning",
                    json=fallback_body,
                    headers=self._headers(user_email=current_user_email.get()),
                )
                fallback.raise_for_status()
                return ReasoningReportResult.model_validate(fallback.json())
            response.raise_for_status()
            return ReasoningReportResult.model_validate(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation="intaris report_reasoning",
            breaker=self.reasoning_breaker,
        )

    async def update_session_status(
        self,
        session_id: str,
        status: str,
        status_reason: str | None = None,
        user_email: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"status": status}
        if status_reason is not None:
            body["status_reason"] = status_reason

        async def _do() -> None:
            response = await self.client.patch(
                f"/api/v1/session/{session_id}/status",
                json=body,
                headers=self._headers(user_email=user_email or current_user_email.get()),
            )
            response.raise_for_status()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris update_session_status({session_id})",
            breaker=self.session_breaker,
        )

    async def checkpoint(self, session_id: str, content: str) -> None:
        async def _do() -> None:
            response = await self.client.post(
                f"/api/v1/session/{session_id}/checkpoint",
                json={"content": content},
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris checkpoint({session_id})",
            breaker=self.session_breaker,
        )

    async def get_session(
        self,
        session_id: str,
        *,
        user_email: str | None = None,
        agent_id: str = "system",
        agent_owner_email: str | None = None,
    ) -> IntarisSession:
        async def _do() -> IntarisSession:
            response = await self.client.get(
                f"/api/v1/session/{session_id}",
                headers=self._headers(
                    agent_id, user_email or current_user_email.get(), agent_owner_email
                ),
            )
            response.raise_for_status()
            return IntarisSession.model_validate(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris get_session({session_id})",
            breaker=self.session_breaker,
        )

    async def get_session_summaries(self, session_id: str) -> IntarisSessionSummaries:
        async def _do() -> IntarisSessionSummaries:
            response = await self.client.get(
                f"/api/v1/session/{session_id}/summary",
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()
            return IntarisSessionSummaries.model_validate(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris get_session_summaries({session_id})",
            breaker=self.session_breaker,
        )

    async def submit_decision(self, call_id: str, decision: str, note: str | None = None) -> None:
        async def _do() -> None:
            response = await self.client.post(
                "/api/v1/decision",
                json={"call_id": call_id, "decision": decision, "note": note},
                headers=self._headers(user_email=current_user_email.get()),
            )
            if 400 <= response.status_code < 500 and response.status_code != 429:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                raise ResolutionRejected(response.status_code, payload)
            response.raise_for_status()

        await self._call_with_retry(
            _do,
            max_retries=2,
            operation="intaris submit_decision",
            breaker=self.session_breaker,
        )

    async def list_pending_escalations(
        self, session_id: str | None = None
    ) -> list[EscalationRecord]:
        params = {"decision": "escalate", "resolved": "false"}
        if session_id is not None:
            params["session_id"] = session_id

        async def _do() -> list[EscalationRecord]:
            response = await self.client.get(
                "/api/v1/audit",
                params=params,
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            return [EscalationRecord.model_validate(item) for item in items]

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation="intaris list_pending_escalations",
            breaker=self.session_breaker,
        )

    async def get_escalation(self, call_id: str) -> EscalationRecord | None:
        async def _do() -> EscalationRecord | None:
            response = await self.client.get(
                "/api/v1/audit",
                params={"call_id": call_id},
                headers=self._headers(user_email=current_user_email.get()),
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            if not isinstance(items, list):
                return None
            for item in items:
                record = EscalationRecord.model_validate(item)
                if record.call_id == call_id:
                    return record
            return None

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation="intaris get_escalation",
            breaker=self.session_breaker,
        )

    async def record_events(
        self,
        session_id: str,
        events: list[SessionEvent],
        source: str = "cognis",
        idempotency_key: str | None = None,
        retry_missing_session: bool = False,
        user_email: str | None = None,
        agent_id: str | None = None,
        agent_owner_email: str | None = None,
    ) -> EventAppendResult:
        if idempotency_key is not None:
            validate_event_idempotency_key(session_id, idempotency_key)
        authority = self._event_store_authority(
            user_email=user_email,
            agent_id=agent_id,
            agent_owner_email=agent_owner_email,
        )
        headers = {
            **self._headers(
                agent_id=authority.agent_id if authority else (agent_id or "system"),
                user_email=authority.user_email if authority else user_email,
                agent_owner_email=(authority.agent_owner_email if authority else agent_owner_email),
            ),
            "X-Intaris-Source": source,
        }
        params = {"idempotency_key": idempotency_key} if idempotency_key is not None else None
        missing_session_delays = (0.05, 0.1, 0.25) if retry_missing_session else ()
        missing_session_attempt = 0

        async def _do() -> EventAppendResult:
            nonlocal missing_session_attempt
            response = await self.client.post(
                f"/api/v1/session/{session_id}/events",
                json=[event.model_dump() for event in events],
                headers=headers,
                params=params,
                timeout=30.0,
            )
            # Older receivers have no code. Do not infer session absence from
            # message text: disabled recording also historically returned 404.
            event_error = getattr(response, "headers", {}).get("X-Intaris-Event-Error")
            if response.status_code == 404 and event_error == "session_not_found":
                if missing_session_attempt < len(missing_session_delays):
                    delay = missing_session_delays[missing_session_attempt]
                    missing_session_attempt += 1
                    logger.warning(
                        "intaris: record_events 404 — session not found, retrying",
                        extra={
                            "extra_data": {
                                "event_count": len(events),
                                "attempt": missing_session_attempt,
                                "delay_seconds": delay,
                            }
                        },
                    )
                    await asyncio.sleep(delay)
                    return await _do()
                logger.warning(
                    "intaris: record_events 404 — session not found",
                    extra={"extra_data": {"event_count": len(events)}},
                )
                return EventAppendResult(ok=False, count=0, first_seq=0, last_seq=0)
            response.raise_for_status()
            return EventAppendResult.model_validate(response.json())

        def retryable_append(exc: Exception) -> bool:
            response = getattr(exc, "response", None)
            if getattr(response, "headers", {}).get("X-Intaris-Event-Error") == "append_uncertain":
                return False
            return is_retryable_http_error(exc)

        result = await self._call_with_retry(
            _do,
            max_retries=3,
            operation="intaris record_events",
            breaker=self.events_breaker,
            retryable_check=retryable_append,
        )
        logger.debug(
            "intaris: events recorded",
            extra={
                "extra_data": {
                    "count": result.count,
                    "last_seq": result.last_seq,
                }
            },
        )
        if result.ok and authority is not None:
            try:
                notification = EventAppendNotification(
                    authority=authority,
                    session_id=session_id,
                    first_seq=result.first_seq,
                    last_seq=result.last_seq,
                    event_count=result.count,
                    events=tuple(events) if len(events) == result.count else (),
                )
            except ValueError:
                logger.warning(
                    "intaris: event append notification skipped",
                    extra={
                        "extra_data": {
                            "listener_count": len(self._event_append_listeners),
                            "reason": "invalid_append_range",
                        }
                    },
                )
            else:
                await self._notify_event_append(notification)
        elif result.ok and self._event_append_listeners:
            logger.warning(
                "intaris: event append notification skipped",
                extra={
                    "extra_data": {
                        "listener_count": len(self._event_append_listeners),
                        "reason": "authority_unavailable",
                    }
                },
            )
        return result

    async def read_events(
        self,
        session_id: str,
        after_seq: int = 0,
        limit: int = 0,
        types: list[str] | None = None,
        last_n: int | None = None,
        before_seq: int | None = None,
        seqs: list[int] | None = None,
        allow_missing_stream: bool = False,
    ) -> EventReadResult:
        params: dict[str, Any] = {}
        if seqs:
            params["seqs"] = ",".join(str(seq) for seq in seqs)
        elif last_n is not None:
            params["last_n"] = last_n
        elif before_seq is not None:
            params["before_seq"] = before_seq
            if limit:
                params["limit"] = limit
        else:
            params["after_seq"] = after_seq
            if limit:
                params["limit"] = limit
        if types:
            params["type"] = ",".join(types)
        logger.debug(
            "intaris: read_events",
            extra={
                "extra_data": {
                    "session_id": session_id,
                    "after_seq": after_seq,
                    "seqs": seqs,
                    "allow_missing_stream": allow_missing_stream,
                }
            },
        )

        async def _do() -> EventReadResult:
            response = await self.client.get(
                f"/api/v1/session/{session_id}/events",
                params=params,
                headers=self._headers(user_email=current_user_email.get()),
                timeout=15.0,
            )
            if response.status_code == 404:
                if allow_missing_stream:
                    session_response = await self.client.get(
                        f"/api/v1/session/{session_id}",
                        headers=self._headers(user_email=current_user_email.get()),
                        timeout=15.0,
                    )
                    session_response.raise_for_status()
                    logger.debug(
                        "intaris: read_events 404 — existing session has no event stream",
                        extra={"extra_data": {"session_id": session_id}},
                    )
                    return EventReadResult(
                        events=[],
                        last_seq=0,
                        has_more=False,
                        missing_stream_fallback_used=True,
                    )
                logger.warning(
                    "intaris: read_events 404 — event stream not found",
                    extra={"extra_data": {"session_id": session_id, "after_seq": after_seq}},
                )
            response.raise_for_status()
            return EventReadResult.model_validate(response.json())

        result = await self._call_with_retry(
            _do,
            max_retries=3,
            operation=f"intaris read_events({session_id})",
            breaker=self.events_breaker,
        )
        logger.debug(
            "intaris: read_events complete",
            extra={
                "extra_data": {
                    "session_id": session_id,
                    "event_count": len(result.events),
                    "last_seq": result.last_seq,
                }
            },
        )
        return result

    async def get_last_seq(self, session_id: str, *, allow_missing_stream: bool = False) -> int:
        result = await self.read_events(
            session_id=session_id,
            last_n=1,
            allow_missing_stream=allow_missing_stream,
        )
        return result.last_seq

    async def call_mcp_tool(
        self,
        session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> ToolResult:
        async def _do() -> ToolResult:
            response = await self.client.post(
                "/api/v1/mcp/call",
                json={
                    "session_id": session_id,
                    "server": server_name,
                    "tool": tool_name,
                    "arguments": arguments,
                    "context": context or {},
                },
                headers=self._headers(user_email=current_user_email.get()),
                timeout=60.0,
            )
            response.raise_for_status()
            return self._normalize_mcp_tool_result(response.json())

        return await self._call_with_retry(
            _do,
            max_retries=2,
            operation=f"intaris call_mcp_tool({tool_name})",
            breaker=self.mcp_breaker,
        )

    def _normalize_mcp_tool_result(self, payload: Any) -> ToolResult:
        """Normalize Intaris MCP REST responses into Cognis ToolResult."""

        if isinstance(payload, dict) and "output" in payload:
            return ToolResult.model_validate(payload)

        if not isinstance(payload, dict):
            return ToolResult(output=str(payload), is_error=True)

        content = payload.get("content")
        text_chunks: list[str] = []
        attachments: list[dict[str, Any]] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    text_chunks.append(str(block))
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text:
                        text_chunks.append(text)
                elif block_type == "image":
                    attachment = self._normalize_mcp_image_block(block)
                    if attachment is not None:
                        attachments.append(attachment)
                        text_chunks.append("[image attachment returned]")
                elif block_type == "resource":
                    attachment = self._normalize_mcp_resource_block(block)
                    if attachment is not None:
                        attachments.append(attachment)
                        text_chunks.append("[resource attachment returned]")
                    else:
                        text_chunks.append(str(block))
                else:
                    text_chunks.append(str(block))
        elif isinstance(content, str):
            text_chunks.append(content)

        output = "\n".join(chunk for chunk in text_chunks if chunk).strip()
        if not output:
            output = "[No content returned from Intaris MCP proxy]"

        metadata = {
            key: value for key, value in payload.items() if key not in {"content", "isError"}
        }
        return ToolResult(
            output=output,
            is_error=bool(payload.get("isError", False)),
            duration_ms=(
                int(payload["latency_ms"])
                if isinstance(payload.get("latency_ms"), (int, float))
                else None
            ),
            metadata=metadata or None,
            attachments=attachments or None,
        )

    def _normalize_mcp_image_block(self, block: dict[str, Any]) -> dict[str, Any] | None:
        data = block.get("data") or block.get("image") or block.get("bytes")
        if isinstance(data, bytes):
            content_b64 = base64.b64encode(data).decode("ascii")
        elif isinstance(data, str):
            content_b64 = _coerce_attachment_b64(data)
        else:
            return None
        mime_type = str(block.get("mimeType") or block.get("mime_type") or "image/png")
        return {
            "content_b64": content_b64,
            "mime_type": mime_type,
            "filename": str(block.get("filename") or "mcp-image"),
            "kind": "image",
        }

    def _normalize_mcp_resource_block(self, block: dict[str, Any]) -> dict[str, Any] | None:
        resource = block.get("resource")
        if isinstance(resource, dict):
            data = resource.get("blob") or resource.get("data")
            mime_type = str(
                resource.get("mimeType") or resource.get("mime_type") or "application/octet-stream"
            )
            filename = str(resource.get("filename") or block.get("filename") or "mcp-resource")
        else:
            data = block.get("data")
            mime_type = str(
                block.get("mimeType") or block.get("mime_type") or "application/octet-stream"
            )
            filename = str(block.get("filename") or "mcp-resource")
        if isinstance(data, bytes):
            content_b64 = base64.b64encode(data).decode("ascii")
        elif isinstance(data, str):
            content_b64 = _coerce_attachment_b64(data)
        else:
            return None
        return {
            "content_b64": content_b64,
            "mime_type": mime_type,
            "filename": filename,
            "kind": "pdf" if mime_type == "application/pdf" else "file",
        }

    async def list_mcp_servers(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        """List available MCP servers from Intaris."""
        params: dict[str, str] = {}
        if enabled_only:
            params["enabled_only"] = "true"
        try:

            async def _do() -> list[dict[str, Any]]:
                response = await self.client.get(
                    "/api/v1/mcp/servers",
                    params=params,
                    headers=self._headers(user_email=current_user_email.get()),
                )
                response.raise_for_status()
                data = response.json()
                items: list[dict[str, Any]] = (
                    data.get("items", []) if isinstance(data, dict) else list(data)
                )
                return items

            user_email = current_user_email.get()
            return await self._mcp_listing_cache.get(
                ("servers", enabled_only, user_email),
                lambda: self._call_with_retry(
                    _do,
                    max_retries=1,
                    operation="intaris list_mcp_servers",
                    breaker=self.mcp_breaker,
                ),
            )
        except Exception:
            logger.warning("intaris: list_mcp_servers failed", exc_info=True)
            return []

    async def list_mcp_tools(self) -> list[dict[str, Any]]:
        """List all aggregated MCP tools from Intaris."""
        try:

            async def _do() -> list[dict[str, Any]]:
                response = await self.client.get(
                    "/api/v1/mcp/tools",
                    headers=self._headers(user_email=current_user_email.get()),
                )
                response.raise_for_status()
                data = response.json()
                tools: list[dict[str, Any]] = (
                    data.get("tools", []) if isinstance(data, dict) else list(data)
                )
                return tools

            user_email = current_user_email.get()
            return await self._mcp_listing_cache.get(
                ("tools", user_email),
                lambda: self._call_with_retry(
                    _do,
                    max_retries=1,
                    operation="intaris list_mcp_tools",
                    breaker=self.mcp_breaker,
                ),
            )
        except Exception:
            logger.warning("intaris: list_mcp_tools failed", exc_info=True)
            return []

    async def search_health(self, user_email: str | None = None) -> SearchHealth:
        """Return Intaris search health, treating missing search routes as disabled."""

        try:

            async def _do() -> SearchHealth:
                response = await self.client.get(
                    "/api/v1/search/health",
                    headers=self._headers(user_email=user_email or current_user_email.get()),
                    timeout=5.0,
                )
                if response.status_code == 404:
                    return SearchHealth(enabled=False, notes=["search_disabled"])
                response.raise_for_status()
                return SearchHealth.model_validate(response.json())

            return await self._call_with_retry(
                _do,
                max_retries=1,
                base_delay=0.5,
                operation="intaris search_health",
                breaker=self.search_breaker,
            )
        except Exception:
            logger.warning("intaris: search_health failed", exc_info=True)
            return SearchHealth(enabled=False, notes=["search_unavailable"])

    async def search(
        self, request: SearchRequest, *, user_email: str | None = None
    ) -> SearchResponse:
        """Run flat Intaris search with fail-soft semantics."""

        try:

            async def _do() -> SearchResponse:
                response = await self.client.post(
                    "/api/v1/search",
                    json=request.model_dump(mode="json", exclude_none=True),
                    headers=self._headers(user_email=user_email or current_user_email.get()),
                    timeout=15.0,
                )
                if response.status_code == 404:
                    return SearchResponse()
                response.raise_for_status()
                return SearchResponse.model_validate(response.json())

            return await self._call_with_retry(
                _do,
                max_retries=1,
                base_delay=0.5,
                operation="intaris search",
                breaker=self.search_breaker,
            )
        except Exception:
            logger.warning("intaris: search failed", exc_info=True)
            return SearchResponse()

    async def search_sessions(
        self, request: SearchSessionsRequest, *, user_email: str | None = None
    ) -> SearchSessionsResponse:
        """Run aggregated Intaris search with fail-soft semantics."""

        try:

            async def _do() -> SearchSessionsResponse:
                response = await self.client.post(
                    "/api/v1/search/sessions",
                    json=request.model_dump(mode="json", exclude_none=True),
                    headers=self._headers(user_email=user_email or current_user_email.get()),
                    timeout=15.0,
                )
                if response.status_code == 404:
                    return SearchSessionsResponse()
                response.raise_for_status()
                return SearchSessionsResponse.model_validate(response.json())

            return await self._call_with_retry(
                _do,
                max_retries=1,
                base_delay=0.5,
                operation="intaris search_sessions",
                breaker=self.search_breaker,
            )
        except Exception:
            logger.warning("intaris: search_sessions failed", exc_info=True)
            return SearchSessionsResponse()

    async def health(self) -> ProviderHealth:
        start = perf_counter()
        try:
            response = await self.client.get("/health", headers=self._headers())
            latency_ms = (perf_counter() - start) * 1000
            if response.is_success:
                return ProviderHealth(
                    name="intaris",
                    status="healthy",
                    latency_ms=latency_ms,
                    circuit_state=self.eval_breaker.state,
                    details={
                        "reasoning_circuit_state": self.reasoning_breaker.state,
                        "session_circuit_state": self.session_breaker.state,
                        "events_circuit_state": self.events_breaker.state,
                        "mcp_circuit_state": self.mcp_breaker.state,
                        "search_circuit_state": self.search_breaker.state,
                    },
                )
        except Exception as exc:
            return ProviderHealth(
                name="intaris",
                status="degraded",
                error=str(exc),
                circuit_state=self.eval_breaker.state,
                details={
                    "reasoning_circuit_state": self.reasoning_breaker.state,
                    "session_circuit_state": self.session_breaker.state,
                    "events_circuit_state": self.events_breaker.state,
                    "mcp_circuit_state": self.mcp_breaker.state,
                    "search_circuit_state": self.search_breaker.state,
                },
            )
        return ProviderHealth(
            name="intaris",
            status="degraded",
            circuit_state=self.eval_breaker.state,
            details={
                "reasoning_circuit_state": self.reasoning_breaker.state,
                "session_circuit_state": self.session_breaker.state,
                "events_circuit_state": self.events_breaker.state,
                "mcp_circuit_state": self.mcp_breaker.state,
                "search_circuit_state": self.search_breaker.state,
            },
        )
