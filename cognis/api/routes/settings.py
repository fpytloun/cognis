"""Settings, LLM providers, and model routing routes."""

from __future__ import annotations

from collections import defaultdict
from time import monotonic
from typing import Any

from fastapi import APIRouter, Request

from cognis.api.common import api_exception, require_admin, require_current_user
from cognis.api.models import (
    CursorPage,
    LLMProviderRequest,
    LLMProviderResponse,
    LLMProviderTestResponse,
    LLMProviderUpdateRequest,
    ModelRoutingResponse,
    ModelRoutingUpdateRequest,
    SettingResponse,
    SettingsCategoryResponse,
    SettingUpdateRequest,
    WebConfigStatusResponse,
)
from cognis.api.serializers import llm_provider_to_response, setting_to_response
from cognis.settings_schema import setting_category, validate_setting_value
from cognis.store.queries import (
    create_llm_provider,
    delete_llm_provider,
    delete_model_routing,
    get_llm_provider,
    get_setting,
    list_llm_providers,
    list_model_routing,
    list_settings,
    update_llm_provider,
    upsert_model_routing,
    upsert_setting,
)

router = APIRouter(tags=["settings"])
PROVIDER_TEST_COOLDOWN_SECONDS = 10.0


def _apply_last_test_metadata(
    request: Request, response: LLMProviderResponse
) -> LLMProviderResponse:
    last_test = getattr(request.app.state, "provider_test_results", {}).get(response.provider_id)
    if last_test is None:
        return response
    return response.model_copy(update={"last_test": last_test})


@router.get("/api/v1/settings", response_model=list[SettingsCategoryResponse])
async def settings_list(request: Request) -> list[SettingsCategoryResponse]:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_settings(session)
    grouped: dict[str, list[SettingResponse]] = defaultdict(list)
    for row in rows:
        grouped[row.category].append(setting_to_response(row))
    return [
        SettingsCategoryResponse(category=category, items=items)
        for category, items in sorted(grouped.items())
    ]


@router.get("/api/v1/settings/{key}", response_model=SettingResponse)
async def setting_detail(request: Request, key: str) -> SettingResponse:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_setting(session, key)
    if row is None:
        raise api_exception(404, "not_found", "Setting not found")
    return setting_to_response(row)


@router.put("/api/v1/settings/{key}", response_model=SettingResponse)
async def setting_update(
    request: Request, key: str, payload: SettingUpdateRequest
) -> SettingResponse:
    user = require_admin(request)
    try:
        validate_setting_value(key, payload.value)
        category = setting_category(key)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    async with request.app.state.session_factory() as session:
        row = await upsert_setting(
            session,
            key=key,
            value=payload.value,
            category=category,
            updated_by=user.email,
        )
        await session.commit()
        await session.refresh(row)
    if key == "security.api_read_requests_per_minute":
        request.app.state.api_rate_limiter.update_limits(
            read_requests_per_minute=int(payload.value)
        )
    elif key == "security.api_write_requests_per_minute":
        request.app.state.api_rate_limiter.update_limits(
            write_requests_per_minute=int(payload.value)
        )
    return setting_to_response(row)


# -- Web config ----------------------------------------------------------------


@router.get("/api/v1/web-config/status", response_model=WebConfigStatusResponse)
async def web_config_status(request: Request) -> WebConfigStatusResponse:
    """Return web backend configuration status."""
    user = require_current_user(request)
    from cognis.store.queries import get_setting_value

    async with request.app.state.session_factory() as session:
        backend = await get_setting_value(session, "web.backend", "direct")

    tavily_configured = False
    brave_configured = False
    secrets_provider = getattr(request.app.state.providers, "secrets", None)
    if secrets_provider is not None:
        for secret_name in ("tavily_api_key", "brave_api_key"):
            try:
                await secrets_provider.get_secret(secret_name, user.email)
                if secret_name == "tavily_api_key":
                    tavily_configured = True
                else:
                    brave_configured = True
            except KeyError:
                pass  # Secret not configured — expected
            except Exception:
                pass  # Provider error — treat as not configured

    available = ["direct"]
    if tavily_configured:
        available.append("tavily")
    if brave_configured:
        available.append("brave")

    return WebConfigStatusResponse(
        backend=str(backend) if isinstance(backend, str) else "direct",
        tavily_configured=tavily_configured,
        brave_configured=brave_configured,
        available_backends=available,
    )


@router.get("/api/v1/llm-providers", response_model=CursorPage[LLMProviderResponse])
async def llm_provider_list(request: Request) -> CursorPage[LLMProviderResponse]:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        rows = await list_llm_providers(session)
    items = [_apply_last_test_metadata(request, llm_provider_to_response(row)) for row in rows]
    return CursorPage(items=items, cursor=None, has_more=False)


@router.post("/api/v1/llm-providers", response_model=LLMProviderResponse)
async def llm_provider_create(request: Request, payload: LLMProviderRequest) -> LLMProviderResponse:
    require_admin(request)
    from cognis.api.common import slugify

    provider_id = payload.provider_id or slugify(payload.display_name)
    async with request.app.state.session_factory() as session:
        existing = await get_llm_provider(session, provider_id)
        if existing is not None:
            raise api_exception(409, "conflict", "LLM provider already exists")
        row = await create_llm_provider(
            session,
            provider_id=provider_id,
            display_name=payload.display_name,
            location=payload.location,
            backend=payload.backend,
            config=payload.config,
            status=payload.status,
        )
        await session.commit()
        await session.refresh(row)
    return _apply_last_test_metadata(request, llm_provider_to_response(row))


@router.get("/api/v1/llm-providers/{provider_id}", response_model=LLMProviderResponse)
async def llm_provider_detail(request: Request, provider_id: str) -> LLMProviderResponse:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        row = await get_llm_provider(session, provider_id)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    return _apply_last_test_metadata(request, llm_provider_to_response(row))


@router.put("/api/v1/llm-providers/{provider_id}", response_model=LLMProviderResponse)
async def llm_provider_update(
    request: Request,
    provider_id: str,
    payload: LLMProviderUpdateRequest,
) -> LLMProviderResponse:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        ok = await update_llm_provider(
            session,
            provider_id,
            display_name=payload.display_name,
            location=payload.location,
            backend=payload.backend,
            config=payload.config,
            status=payload.status,
        )
        if not ok:
            raise api_exception(404, "not_found", "LLM provider not found")
        row = await get_llm_provider(session, provider_id)
        await session.commit()
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    return _apply_last_test_metadata(request, llm_provider_to_response(row))


@router.delete("/api/v1/llm-providers/{provider_id}", response_model=dict)
async def llm_provider_delete(request: Request, provider_id: str) -> dict[str, bool]:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        ok = await delete_llm_provider(session, provider_id)
        await session.commit()
    return {"ok": ok}


@router.post("/api/v1/llm-providers/{provider_id}/set-default")
async def llm_provider_set_default(request: Request, provider_id: str) -> dict[str, Any]:
    """Mark a provider as the default. Clears any existing default."""
    require_admin(request)
    from cognis.store.models import LLMProvider as LLMProviderRow

    async with request.app.state.session_factory() as session:
        target = await get_llm_provider(session, provider_id)
        if target is None:
            raise api_exception(404, "not_found", "LLM provider not found")
        # Clear existing defaults
        from sqlalchemy import update

        await session.execute(update(LLMProviderRow).values(is_default=False))
        target.is_default = True
        await session.commit()
    return {"ok": True, "provider_id": provider_id}


@router.post("/api/v1/llm-providers/{provider_id}/test", response_model=LLMProviderTestResponse)
async def llm_provider_test(request: Request, provider_id: str) -> LLMProviderTestResponse:
    require_admin(request)
    cooldowns: dict[str, float] = request.app.state.provider_test_cooldowns
    last_started_at = cooldowns.get(provider_id)
    if (
        last_started_at is not None
        and monotonic() - last_started_at < PROVIDER_TEST_COOLDOWN_SECONDS
    ):
        raise api_exception(429, "rate_limited", "Provider test cooldown is still active")
    async with request.app.state.session_factory() as session:
        row = await get_llm_provider(session, provider_id)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    cooldowns[provider_id] = monotonic()
    try:
        result = await request.app.state.providers.llm.test_provider(
            provider_id, timeout_seconds=15
        )
    finally:
        cooldowns[provider_id] = monotonic()

    response = LLMProviderTestResponse(provider_id=provider_id, **result)
    request.app.state.provider_test_results[provider_id] = response.model_dump(mode="json")
    return response


@router.post("/api/v1/llm-providers/discover-models-preview")
async def llm_provider_discover_models_preview(request: Request) -> dict[str, Any]:
    """Discover models without a saved provider (preview mode)."""
    require_admin(request)
    body = await request.json()
    preset = str(body.get("preset", ""))
    base_url = str(body.get("base_url", ""))
    api_key = body.get("api_key") or None
    secret_name = body.get("secret_name") or None
    env_var = body.get("env_var") or None
    try:
        models = await request.app.state.providers.llm.discover_models_preview(
            preset=preset,
            base_url=base_url,
            api_key=api_key,
            secret_name=secret_name,
            env_var=env_var,
        )
    except Exception as exc:
        raise api_exception(
            502,
            "provider_error",
            f"Failed to discover models: {exc!s}"[:300],
        ) from exc
    return {"models": models}


@router.post("/api/v1/llm-providers/{provider_id}/discover-models")
async def llm_provider_discover_models(request: Request, provider_id: str) -> dict[str, Any]:
    """Query a saved provider for available models."""
    require_admin(request)
    async with request.app.state.session_factory() as session:
        row = await get_llm_provider(session, provider_id)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    try:
        models = await request.app.state.providers.llm.discover_models(provider_id)
    except Exception as exc:
        raise api_exception(
            502,
            "provider_error",
            f"Failed to discover models: {exc!s}"[:300],
        ) from exc
    return {"provider_id": provider_id, "models": models}


@router.get("/api/v1/model-routing", response_model=ModelRoutingResponse)
async def model_routing_get(request: Request) -> ModelRoutingResponse:
    require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_model_routing(session)
    items = {row.task_type: row.model for row in rows}
    return ModelRoutingResponse(
        default=items.get("default"),
        classifier=items.get("classifier"),
        compaction=items.get("compaction"),
        simple_inline=items.get("simple_inline"),
        items=items,
    )


@router.put("/api/v1/model-routing", response_model=ModelRoutingResponse)
async def model_routing_put(
    request: Request,
    payload: ModelRoutingUpdateRequest,
) -> ModelRoutingResponse:
    require_admin(request)
    updates = {
        "default": payload.default,
        "classifier": payload.classifier,
        "compaction": payload.compaction,
        "simple_inline": payload.simple_inline,
        **payload.items,
    }
    async with request.app.state.session_factory() as session:
        for task_type, model in updates.items():
            if model is None:
                await delete_model_routing(session, task_type)
                continue
            await upsert_model_routing(
                session,
                task_type=task_type,
                provider_id=None,
                model=model,
            )
        await session.commit()
    return await model_routing_get(request)
