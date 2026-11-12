"""Privileged configuration tool adaptation over shared application services."""

from __future__ import annotations

import re
from dataclasses import dataclass
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from fastapi.encoders import jsonable_encoder
from starlette.exceptions import HTTPException

from cognis import json_codec as json
from cognis.api.common import api_exception
from cognis.api.models import (
    ExecutorCreateRequest,
    ExecutorUpdateRequest,
    LLMProviderOAuthStatusResponse,
    LLMProviderRequest,
    LLMProviderUpdateRequest,
    ModelRoutingUpdateRequest,
    StepProfileCreateRequest,
    StepProfileUpdateRequest,
)
from cognis.api.serializers import llm_provider_to_response, setting_to_response
from cognis.core import executor_management, route_management, routing_management
from cognis.core.executor_management import ExecutorManagementDependencies
from cognis.core.management_access import ManagementActor
from cognis.core.provider_management import ProviderManagement
from cognis.core.settings_management import SettingsManagement
from cognis.core.step_profile_management import StepProfileManagement
from cognis.guardrails_redaction import redact_runtime_evidence
from cognis.models.tool import ToolResult
from cognis.providers.llm.recovery_policy import NamedRouteConfig
from cognis.providers.llm.route_store import InferenceRouteStore
from cognis.runtime_context import RuntimeAccessContext
from cognis.settings_schema import get_setting_spec
from cognis.store.models import User
from cognis.store.queries import (
    get_setting,
    get_visible_llm_provider,
    list_llm_providers,
    list_settings,
)


@dataclass(frozen=True)
class SystemManagementDependencies:
    """Shared services and bounded provider-probe runtime state."""

    session_factory: Any
    llm: Any
    settings: SettingsManagement
    executors: ExecutorManagementDependencies
    provider_monitor: Any
    provider_test_cooldowns: dict[str, float]
    provider_test_results: dict[str, Any]
    step_profiles: StepProfileManagement | None = None
    usage_price_publisher: Any | None = None


_SECRET_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "password",
        "client_secret",
        "authorization",
        "cookie",
        "token",
        "secret",
        "private_key",
    }
)


def _safe_configuration(value: Any, *, writing: bool = False, parent: str = "") -> Any:
    """Never accept literal credential fields or echo credential-bearing configuration."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            sensitive = key.lower() in _SECRET_KEYS or parent.lower() in {
                "env",
                "headers",
                "extra_headers",
            }
            if sensitive and item:
                if writing:
                    raise ValueError(
                        "Use stored secret references, never literal credentials or environment/header values"
                    )
                result[key] = "[REDACTED]"
            else:
                result[key] = _safe_configuration(item, writing=writing, parent=key)
        return result
    if isinstance(value, list):
        return [_safe_configuration(item, writing=writing, parent=parent) for item in value]
    if isinstance(value, str) and "://" in value:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            if writing:
                raise ValueError(
                    "Configuration URLs must not contain credentials, query parameters, or fragments"
                )
            return "[REDACTED:credential-bearing-url]"
    return value


@dataclass(frozen=True)
class _OAuthResponse:
    status: LLMProviderOAuthStatusResponse


@dataclass(frozen=True)
class _ExecutorProvisioningResponse:
    payload: dict[str, Any]


def _output(value: Any) -> Any:
    if isinstance(value, _ExecutorProvisioningResponse):
        payload = dict(value.payload)
        reference = payload.pop("token_ref")
        if not isinstance(reference, str) or not re.fullmatch(
            r"\$credential:executor-token-[0-9a-f]{32}\.token", reference
        ):
            raise ValueError("Invalid executor provisioning reference")
        # Only this typed operation may return its generated, value-free reference.
        return {**_output(payload), "token_ref": reference}
    if isinstance(value, _OAuthResponse):
        status = value.status.model_dump(mode="json")
        urls = {}
        for key in ("authorization_url", "verification_url", "redirect_uri"):
            url = status.pop(key, None)
            if not url:
                continue
            parsed = urlsplit(url)
            if parsed.scheme in {"https", "http"} and not parsed.username and not parsed.password:
                urls[key] = url
        return {**_output(status), **urls}
    return redact_runtime_evidence(_safe_configuration(jsonable_encoder(value)))


async def handle_system_management_tool(
    *,
    name: str,
    arguments: dict[str, Any],
    deps: SystemManagementDependencies,
    access: RuntimeAccessContext | None,
) -> ToolResult:
    """Enforce trusted runtime context and current DB identity before any operation."""
    if not access or not access.is_root_owner_primary_chat or access.task_id:
        return ToolResult(
            output="System management requires a root owner primary chat.",
            is_error=True,
            metadata={"code": "system_management_context_denied"},
        )
    async with deps.session_factory() as session:
        user = await session.get(User, access.user_email)
    if user is None or not user.is_active:
        return ToolResult(
            output="Active user authentication required.",
            is_error=True,
            metadata={"code": "system_management_context_denied"},
        )
    actor = ManagementActor(user.email, user.role)
    try:
        value = await _dispatch(name, arguments, deps, actor, agent_id=access.agent_id or "")
        return ToolResult(output=json.dumps(_output(value), sort_keys=True))
    except HTTPException as exc:
        detail = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {"message": "Management operation failed"}
        )
        return ToolResult(
            output=json.dumps(_output(detail)),
            is_error=True,
            metadata={"code": "system_management_error", "status_code": exc.status_code},
        )
    except ValueError:
        # Validation errors may embed credential values. Never echo exception input.
        return ToolResult(
            output="Invalid management configuration; inspect the action schema and current resource.",
            is_error=True,
            metadata={"code": "invalid_management_configuration"},
        )


async def _dispatch(
    name: str,
    args: dict[str, Any],
    deps: SystemManagementDependencies,
    actor: ManagementActor,
    *,
    agent_id: str,
) -> Any:
    action = args["action"]
    if name == "manage_providers":
        return await _providers(action, args, deps, actor)
    if name == "manage_routes":
        return await _routes(action, args, deps, actor)
    if name == "manage_executors":
        return await _executors(action, args, deps, actor, agent_id=agent_id)
    if name == "manage_settings":
        return await _settings(action, args, deps, actor)
    raise ValueError("Unknown management tool")


async def _providers(
    action: str, args: dict[str, Any], deps: SystemManagementDependencies, actor: ManagementActor
) -> Any:
    if action == "schema":
        return {
            "create": LLMProviderRequest.model_json_schema(),
            "update": LLMProviderUpdateRequest.model_json_schema(),
            "secrets": "Use config.api_key_secret to reference a stored credential; never pass inline credentials. config replaces, not merges.",
        }
    if action == "list":
        async with deps.session_factory() as session:
            rows = await list_llm_providers(
                session, acting_user_email=actor.email, include_inactive=True
            )
        return [llm_provider_to_response(row) for row in rows]
    service = ProviderManagement(
        deps.session_factory, deps.llm, usage_price_publisher=deps.usage_price_publisher
    )
    preview = args.get("preview", False)
    if action == "create":
        payload = LLMProviderRequest.model_validate(
            _safe_configuration(args["payload"], writing=True)
        )
        if not payload.provider_id:
            raise ValueError("An explicit provider_id is required for retry reconciliation")
        result = await service.create(actor, payload, preview=preview)
        return result if preview else llm_provider_to_response(result)
    provider_id = args["provider_id"]
    async with deps.session_factory() as session:
        row = await get_visible_llm_provider(session, provider_id, actor.email)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    if action == "get":
        return llm_provider_to_response(row)
    if action == "health_get":
        return await deps.provider_monitor.get(provider_id, refresh_if_due=False)
    if action == "health_refresh":
        actor.require_mutation()
        return await deps.provider_monitor.get(provider_id, force=True)
    actor.require_owner(row.owner_email)
    if action == "update":
        update_payload = LLMProviderUpdateRequest.model_validate(
            _safe_configuration(args["payload"], writing=True)
        )
        result = await service.update(
            actor,
            provider_id,
            update_payload,
            expected_updated_at=args["expected_updated_at"],
            preview=preview,
        )
        return result if preview else llm_provider_to_response(result)
    if action in {"delete", "set_default"}:
        return await getattr(service, action)(
            actor, provider_id, expected_updated_at=args["expected_updated_at"], preview=preview
        )
    if action == "discover_models":
        return {"models": await deps.llm.discover_models(provider_id)}
    if action == "enrich_models":
        return {
            "models": [
                (await deps.llm.enrich_model_info(mid, provider_id=provider_id)).model_dump(
                    mode="json"
                )
                for mid in args["model_ids"]
            ]
        }
    if action == "test":
        last = deps.provider_test_cooldowns.get(provider_id)
        if last is not None and monotonic() - last < 10:
            raise api_exception(429, "rate_limited", "Provider test cooldown is still active")
        deps.provider_test_cooldowns[provider_id] = monotonic()
        try:
            result = await deps.llm.test_provider(
                provider_id,
                timeout_seconds=15,
                acting_user_email=actor.email,
            )
        finally:
            deps.provider_test_cooldowns[provider_id] = monotonic()
        deps.provider_test_results[provider_id] = result
        return {"provider_id": provider_id, **result}
    if action.startswith("auth_"):
        auth = args["auth_provider"]
        if preview:
            return {"preview": True, "provider_id": provider_id, "action": action}
        if action == "auth_disconnect":
            ok = await getattr(deps.llm, f"clear_{auth}_oauth")(provider_id)
            await deps.provider_monitor.invalidate(provider_id)
            return {"ok": ok}
        method = f"start_{auth}_oauth" if action == "auth_start" else f"get_{auth}_oauth_status"
        status = await getattr(deps.llm, method)(provider_id)
        if status.pop("authorization_completed", False):
            await deps.provider_monitor.invalidate(provider_id)
        status.pop("provider_id", None)
        # Typed allowlist prevents tokens from an adapter entering tool history.
        response = LLMProviderOAuthStatusResponse(provider_id=provider_id, **status)
        # OAuth authorization links legitimately contain state and PKCE query
        # parameters. Preserve only typed authorization URLs, never raw adapters.
        return _OAuthResponse(response)
    raise ValueError("Unknown provider operation")


async def _routes(
    action: str, args: dict[str, Any], deps: SystemManagementDependencies, actor: ManagementActor
) -> Any:
    preview = args.get("preview", False)
    if action == "named_list":
        return await route_management.list_routes(deps.session_factory, actor)
    if action == "named_get":
        routes = await InferenceRouteStore(deps.session_factory).list(actor.email)
        route = next((route for route in routes if route.route_id == args["route_id"]), None)
        if route is None:
            raise api_exception(404, "not_found", "Inference route not found")
        return route_management.result(route)
    if action == "named_create":
        return await route_management.create_route(
            deps.session_factory,
            deps.llm,
            actor,
            NamedRouteConfig.model_validate(args["config"]),
            preview=preview,
        )
    if action == "named_update":
        return await route_management.update_route(
            deps.session_factory,
            deps.llm,
            actor,
            args["route_id"],
            route_management.RouteUpdate(
                config=NamedRouteConfig.model_validate(args["config"]),
                expected_revision=args["expected_revision"],
            ),
            preview=preview,
        )
    if action == "named_delete":
        return await route_management.delete_route(
            deps.session_factory,
            actor,
            args["route_id"],
            args["expected_revision"],
            preview=preview,
        )
    if action == "resolve":
        plan = await deps.llm.inference_plan(
            model=args["model"],
            task_type=args.get("task_type", "default"),
            acting_user_email=actor.email,
        )
        return {
            "reference": plan.reference,
            "candidates": [
                candidate.target.model_dump(mode="json") for candidate in plan.candidates
            ],
            "policy": plan.policy.model_dump(mode="json"),
            "policy_scope": plan.policy_scope,
        }
    actor.require_admin()
    bindings, revision = await routing_management.get_bindings(deps.session_factory)
    if action == "bindings_get":
        return {"bindings": bindings.model_dump(mode="json"), "revision": revision}
    if args["expected_revision"] != revision:
        raise api_exception(409, "routing_conflict", "Routing bindings changed; read them again")
    document = bindings.model_dump(mode="json")
    if action == "bindings_update":
        patch = ModelRoutingUpdateRequest.model_validate(args["bindings"])
        document.update(patch.model_dump(mode="json", exclude_unset=True))
    elif action == "bindings_reset":
        for task_type in args["task_types"]:
            if task_type not in document:
                raise ValueError("Unknown routing task type")
            document[task_type] = {"model": None, "reasoning_effort": None}
    else:
        raise ValueError("Unknown route operation")
    result = await routing_management.put_bindings(
        deps.session_factory,
        deps.llm,
        actor,
        ModelRoutingUpdateRequest.model_validate(document),
        expected_revision=revision,
        preview=preview,
    )
    if preview:
        return result
    current, revision = await routing_management.get_bindings(deps.session_factory)
    return {"bindings": current.model_dump(mode="json"), "revision": revision}


async def _executors(
    action: str,
    args: dict[str, Any],
    deps: SystemManagementDependencies,
    actor: ManagementActor,
    *,
    agent_id: str,
) -> Any:
    service = executor_management
    if action == "schema":
        return {
            "create": ExecutorCreateRequest.model_json_schema(),
            "update": ExecutorUpdateRequest.model_json_schema(),
            "tokens": "Use provision_token for an encrypted agent-scoped reference, or the secure Settings UI for manual rotation.",
        }
    if action == "list":
        return await service.list_executors_route(deps.executors, actor)
    if action == "get":
        return await service.get_executor_route(deps.executors, actor, args["executor_id"])
    preview = args.get("preview", False)
    if action == "create":
        payload = ExecutorCreateRequest.model_validate(
            _safe_configuration(args["payload"], writing=True)
        )
        if not payload.executor_id:
            raise ValueError("An explicit executor_id is required for reconciliation")
        result = await service.create_executor_route(
            deps.executors, actor, payload, preview=preview
        )
    elif action in {"update", "set_default"}:
        update_payload = (
            {"is_default": True}
            if action == "set_default"
            else _safe_configuration(args["payload"], writing=True)
        )
        result = await service.update_executor_route(
            deps.executors,
            actor,
            args["executor_id"],
            ExecutorUpdateRequest.model_validate(update_payload),
            expected_updated_at=args["expected_updated_at"],
            preview=preview,
        )
    elif action == "delete":
        await service.delete_executor_route(
            deps.executors,
            actor,
            args["executor_id"],
            expected_updated_at=args["expected_updated_at"],
            preview=preview,
        )
        return {"preview": preview, "deleted": not preview}
    elif action == "reconfigure":
        return await service.reconfigure_executor_route(deps.executors, actor, args["executor_id"])
    elif action == "provision_token":
        return _ExecutorProvisioningResponse(
            await service.provision_executor_token(
                deps.executors,
                actor,
                args["executor_id"],
                agent_id=agent_id,
                operation_id=args["operation_id"],
                expected_updated_at=args["expected_updated_at"],
            )
        )
    else:
        raise ValueError("Unknown executor operation")
    return {"preview": preview, "executor": result}


async def _settings(
    action: str, args: dict[str, Any], deps: SystemManagementDependencies, actor: ManagementActor
) -> Any:
    actor.require_admin()
    if action.startswith("step_profiles_"):
        if deps.step_profiles is None:
            raise api_exception(503, "unavailable", "Step profile registry unavailable")
        operation = action.removeprefix("step_profiles_")
        if operation in {"list", "get"}:
            snapshot = await deps.step_profiles.read()
            if operation == "get":
                profile = next(
                    (p for p in snapshot["profiles"] if p.profile_id == args["profile_id"]), None
                )
                if profile is None:
                    raise api_exception(404, "not_found", "Step profile not found")
                return {"profile": profile, "revision": snapshot["revision"]}
            return snapshot
        payload = None
        if operation in {"create", "update"}:
            cls = StepProfileCreateRequest if operation == "create" else StepProfileUpdateRequest
            payload = cls.model_validate(_safe_configuration(args["payload"], writing=True))
        if operation == "create":
            assert isinstance(payload, StepProfileCreateRequest)
            profile_id = payload.profile_id
        else:
            profile_id = args["profile_id"]
        result = await deps.step_profiles.mutate(
            actor,
            operation,
            profile_id,
            payload,
            expected_revision=args["expected_revision"],
            preview=args.get("preview", False),
        )
        return {"result": result, "revision": (await deps.step_profiles.read())["revision"]}
    if action == "web_update":
        if not args["key"].startswith("web."):
            raise ValueError("web_update requires an exposed web setting")
        action = "update"
    async with deps.session_factory() as session:
        rows = await list_settings(session)
    exposed = []
    for row in rows:
        try:
            spec = get_setting_spec(row.key, require_exposed=True)
        except ValueError:
            continue
        exposed.append(setting_to_response(row, spec))
    if action == "web_get":
        return [row for row in exposed if row.key.startswith("web.")]
    if action in {"list", "schema"}:
        if "key" in args:
            get_setting_spec(args["key"], require_exposed=True)
            return next((row for row in exposed if row.key == args["key"]), None)
        return exposed
    if action == "diagnostics":
        async with deps.session_factory() as session:
            providers = await list_llm_providers(
                session, acting_user_email=actor.email, include_inactive=True
            )
        return {
            "exposed_settings": len(exposed),
            "visible_providers": len(providers),
            "active_providers": sum(row.status == "active" for row in providers),
        }
    key = args["key"]
    spec = get_setting_spec(key, require_exposed=True)
    async with deps.session_factory() as session:
        setting_row = await get_setting(session, key)
    if setting_row is None:
        raise api_exception(404, "not_found", "Setting not found")
    if action == "get":
        return setting_to_response(setting_row, spec)
    value = spec.default if action == "reset" else args["value"]
    return await deps.settings.put(
        key=key,
        value=value,
        updated_by=actor.email,
        expected_updated_at=args["expected_updated_at"],
        preview=args.get("preview", False),
    )
