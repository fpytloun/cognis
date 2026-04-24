"""Settings, LLM providers, and model routing routes."""

from __future__ import annotations

import re
from collections import defaultdict
from time import monotonic
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from cognis.api.common import api_exception, require_admin
from cognis.api.models import (
    CursorPage,
    EnrichModelsPreviewRequest,
    EnrichModelsRequest,
    LLMProviderRequest,
    LLMProviderResponse,
    LLMProviderTestResponse,
    LLMProviderUpdateRequest,
    ModelRoutingResponse,
    ModelRoutingUpdateRequest,
    SettingResponse,
    SettingsCategoryResponse,
    SettingUpdateRequest,
    StepProfileCreateRequest,
    StepProfileResponse,
    StepProfileUpdateRequest,
    WebConfigStatusResponse,
)
from cognis.api.serializers import llm_provider_to_response, setting_to_response
from cognis.core.executor_policy import load_executor_policy
from cognis.core.step_profiles import (
    STEP_PROFILE_CUSTOM_SETTING_KEY,
    STEP_PROFILE_OVERRIDES_SETTING_KEY,
    StepProfileDefinition,
    StepProfileMode,
    serialize_step_profile_override,
)
from cognis.models.config import normalize_reasoning_level
from cognis.models.workflow import StepProfileConfig
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.settings_schema import setting_category, validate_setting_value
from cognis.store.models import SystemWorkflowOverride, WorkflowRow
from cognis.store.queries import (
    create_llm_provider,
    delete_llm_provider,
    delete_model_routing,
    delete_setting,
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
_ROUTING_TASK_TYPES: tuple[str, ...] = (
    "default",
    "classifier",
    "compaction",
    "evaluator",
    "speech_to_text",
    "image_generation",
    "attachment_analysis",
)
_TEXT_ROUTING_TASK_TYPES = frozenset({"default", "classifier", "compaction", "evaluator"})
_STEP_PROFILE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9:_-]{1,80}$")


def _step_profile_response(registry: Any, definition: StepProfileDefinition) -> StepProfileResponse:
    return StepProfileResponse(
        profile_id=definition.profile_id,
        name=definition.name,
        mode=str(definition.mode),
        config=definition.config.model_dump(mode="json"),
        has_override=registry.has_override(definition.profile_id),
        is_custom=registry.is_custom(definition.profile_id),
    )


def _profile_id_in_definition(definition: Any, profile_id: str) -> bool:
    if not isinstance(definition, dict):
        return False
    steps = definition.get("steps")
    if not isinstance(steps, list):
        return False
    return any(
        isinstance(step, dict) and step.get("step_profile_id") == profile_id for step in steps
    )


def _profile_id_in_step_overrides(step_overrides: Any, profile_id: str) -> bool:
    if not isinstance(step_overrides, dict):
        return False
    return any(
        isinstance(payload, dict) and payload.get("step_profile_id") == profile_id
        for payload in step_overrides.values()
    )


async def _ensure_step_profile_not_in_use(session: Any, profile_id: str) -> None:
    workflow_rows = (await session.execute(select(WorkflowRow.definition))).scalars().all()
    if any(_profile_id_in_definition(definition, profile_id) for definition in workflow_rows):
        raise api_exception(409, "conflict", "Step profile is still used by a workflow")
    override_rows = (
        (await session.execute(select(SystemWorkflowOverride.step_overrides))).scalars().all()
    )
    if any(
        _profile_id_in_step_overrides(step_overrides, profile_id)
        for step_overrides in override_rows
    ):
        raise api_exception(
            409,
            "conflict",
            "Step profile is still used by a system workflow override",
        )


def _looks_like_transcription_model(model_name: str) -> bool:
    normalized = model_name.strip().lower().replace("_", "-")
    return any(token in normalized for token in ("transcribe", "whisper", "speech-to-text"))


def _route_model_is_eligible(
    task_type: str,
    *,
    model_id: str,
    model_info: Any,
) -> bool:
    if task_type == "speech_to_text":
        display_name = getattr(model_info, "display_name", None)
        return _looks_like_transcription_model(model_id) or (
            isinstance(display_name, str) and _looks_like_transcription_model(display_name)
        )
    if task_type == "image_generation":
        return bool(getattr(model_info, "supports_image_generation", False))
    if task_type == "attachment_analysis":
        return bool(
            getattr(model_info, "supports_vision", False)
            or getattr(model_info, "supports_pdf_input", False)
            or getattr(model_info, "supports_audio_input", False)
            or getattr(model_info, "supports_file_input", False)
        )
    return True


def _routing_entry_from_row(task_type: str, row: Any | None) -> dict[str, str | None]:
    if row is None:
        return {"model": None, "reasoning_effort": None}
    reasoning_effort: str | None = None
    if task_type in _TEXT_ROUTING_TASK_TYPES and isinstance(getattr(row, "config", None), dict):
        reasoning_effort = normalize_reasoning_level(row.config.get("reasoning_effort"))
        if reasoning_effort == "default":
            reasoning_effort = None
    return {"model": getattr(row, "model", None), "reasoning_effort": reasoning_effort}


def _apply_last_test_metadata(
    request: Request, response: LLMProviderResponse
) -> LLMProviderResponse:
    last_test = getattr(request.app.state, "provider_test_results", {}).get(response.provider_id)
    if last_test is None:
        return response
    return response.model_copy(update={"last_test": last_test})


@router.get("/api/v1/settings", response_model=list[SettingsCategoryResponse])
async def settings_list(request: Request) -> list[SettingsCategoryResponse]:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        rows = await list_settings(session)
    grouped: dict[str, list[SettingResponse]] = defaultdict(list)
    for row in rows:
        if row.key in {STEP_PROFILE_OVERRIDES_SETTING_KEY, STEP_PROFILE_CUSTOM_SETTING_KEY}:
            continue
        grouped[row.category].append(setting_to_response(row))
    return [
        SettingsCategoryResponse(category=category, items=items)
        for category, items in sorted(grouped.items())
    ]


@router.get("/api/v1/settings/step-profiles", response_model=list[StepProfileResponse])
async def settings_step_profiles(request: Request) -> list[StepProfileResponse]:
    require_admin(request)
    registry = request.app.state.step_profile_registry
    return [
        _step_profile_response(registry, definition) for definition in registry.list_definitions()
    ]


@router.post("/api/v1/settings/step-profiles", response_model=StepProfileResponse)
async def settings_step_profile_create(
    request: Request,
    payload: StepProfileCreateRequest,
) -> StepProfileResponse:
    user = require_admin(request)
    registry = request.app.state.step_profile_registry
    profile_id = payload.profile_id.strip()
    if not profile_id:
        raise api_exception(400, "validation_error", "profile_id is required")
    if not _STEP_PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise api_exception(400, "validation_error", "profile_id must match ^[a-zA-Z0-9:_-]{1,80}$")
    if registry.get_definition(profile_id) is not None:
        raise api_exception(409, "conflict", "Step profile already exists")
    try:
        mode = StepProfileMode(payload.mode)
        config = StepProfileConfig.model_validate(payload.config)
    except Exception as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    async with request.app.state.session_factory() as session:
        raw = await get_setting(session, STEP_PROFILE_CUSTOM_SETTING_KEY)
        custom_profiles = dict(raw.value) if raw is not None and isinstance(raw.value, dict) else {}
        custom_profiles[profile_id] = serialize_step_profile_override(
            name=payload.name or profile_id,
            mode=mode,
            config=config,
        )
        await upsert_setting(
            session,
            key=STEP_PROFILE_CUSTOM_SETTING_KEY,
            value=custom_profiles,
            category="workflow",
            updated_by=user.email,
        )
        await session.commit()
    await registry.refresh()
    definition = registry.get_definition(profile_id)
    assert definition is not None
    return _step_profile_response(registry, definition)


@router.put("/api/v1/settings/step-profiles/{profile_id}", response_model=StepProfileResponse)
async def settings_step_profile_update(
    request: Request,
    profile_id: str,
    payload: StepProfileUpdateRequest,
) -> StepProfileResponse:
    user = require_admin(request)
    registry = request.app.state.step_profile_registry
    existing = registry.get_definition(profile_id)
    if existing is None:
        raise api_exception(404, "not_found", "Step profile not found")
    if not _STEP_PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise api_exception(400, "validation_error", "invalid profile_id")
    try:
        mode = StepProfileMode(payload.mode)
        config = StepProfileConfig.model_validate(payload.config)
    except Exception as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc

    async with request.app.state.session_factory() as session:
        if registry.is_custom(profile_id):
            raw = await get_setting(session, STEP_PROFILE_CUSTOM_SETTING_KEY)
            custom_profiles = (
                dict(raw.value) if raw is not None and isinstance(raw.value, dict) else {}
            )
            custom_profiles[profile_id] = serialize_step_profile_override(
                name=payload.name or existing.name,
                mode=mode,
                config=config,
            )
            await upsert_setting(
                session,
                key=STEP_PROFILE_CUSTOM_SETTING_KEY,
                value=custom_profiles,
                category="workflow",
                updated_by=user.email,
            )
        else:
            raw = await get_setting(session, STEP_PROFILE_OVERRIDES_SETTING_KEY)
            overrides = dict(raw.value) if raw is not None and isinstance(raw.value, dict) else {}
            overrides[profile_id] = serialize_step_profile_override(
                name=payload.name,
                mode=mode,
                config=config,
            )
            await upsert_setting(
                session,
                key=STEP_PROFILE_OVERRIDES_SETTING_KEY,
                value=overrides,
                category="workflow",
                updated_by=user.email,
            )
        await session.commit()
    await registry.refresh()
    definition = registry.get_definition(profile_id)
    assert definition is not None
    return _step_profile_response(registry, definition)


@router.delete("/api/v1/settings/step-profiles/{profile_id}", response_model=StepProfileResponse)
async def settings_step_profile_reset(request: Request, profile_id: str) -> StepProfileResponse:
    user = require_admin(request)
    registry = request.app.state.step_profile_registry
    existing = registry.get_definition(profile_id)
    if existing is None:
        raise api_exception(404, "not_found", "Step profile not found")
    was_custom = registry.is_custom(profile_id)
    async with request.app.state.session_factory() as session:
        if was_custom:
            await _ensure_step_profile_not_in_use(session, profile_id)
            raw = await get_setting(session, STEP_PROFILE_CUSTOM_SETTING_KEY)
            custom_profiles = (
                dict(raw.value) if raw is not None and isinstance(raw.value, dict) else {}
            )
            custom_profiles.pop(profile_id, None)
            if custom_profiles:
                await upsert_setting(
                    session,
                    key=STEP_PROFILE_CUSTOM_SETTING_KEY,
                    value=custom_profiles,
                    category="workflow",
                    updated_by=user.email,
                )
            else:
                await delete_setting(session, STEP_PROFILE_CUSTOM_SETTING_KEY)
        else:
            raw = await get_setting(session, STEP_PROFILE_OVERRIDES_SETTING_KEY)
            overrides = dict(raw.value) if raw is not None and isinstance(raw.value, dict) else {}
            overrides.pop(profile_id, None)
            if overrides:
                await upsert_setting(
                    session,
                    key=STEP_PROFILE_OVERRIDES_SETTING_KEY,
                    value=overrides,
                    category="workflow",
                    updated_by=user.email,
                )
            else:
                await delete_setting(session, STEP_PROFILE_OVERRIDES_SETTING_KEY)
        await session.commit()
    await registry.refresh()
    if was_custom:
        return StepProfileResponse(
            profile_id=existing.profile_id,
            name=existing.name,
            mode=str(existing.mode),
            config=existing.config.model_dump(mode="json"),
            has_override=False,
            is_custom=True,
        )
    definition = registry.get_definition(profile_id)
    assert definition is not None
    return _step_profile_response(registry, definition)


@router.get("/api/v1/settings/{key}", response_model=SettingResponse)
async def setting_detail(request: Request, key: str) -> SettingResponse:
    require_admin(request)
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
    elif key in {"executors.allow_in_process", "executors.allow_subprocess"}:
        policy = await load_executor_policy(request.app.state.session_factory)
        await request.app.state.providers.executor.apply_policy(policy)
    elif key == "session.step_timeout_seconds":
        request.app.state.agent_loop.default_step_timeout_seconds = max(1, int(payload.value))
    elif key == "evaluator.timeout_ms":
        request.app.state.step_evaluator.evaluator_timeout_seconds = (
            max(1, int(payload.value)) / 1000
        )
    elif key in {STEP_PROFILE_OVERRIDES_SETTING_KEY, STEP_PROFILE_CUSTOM_SETTING_KEY}:
        await request.app.state.step_profile_registry.refresh()
    return setting_to_response(row)


# -- Web config ----------------------------------------------------------------


@router.get("/api/v1/web-config/status", response_model=WebConfigStatusResponse)
async def web_config_status(request: Request) -> WebConfigStatusResponse:
    """Return web backend configuration status."""
    require_admin(request)
    from cognis.store.queries import get_setting_value

    async with request.app.state.session_factory() as session:
        backend = await get_setting_value(session, "web.backend", "direct")

    tavily_configured = False
    brave_configured = False
    secrets_provider = getattr(request.app.state.providers, "secrets", None)
    if secrets_provider is not None:
        for secret_name in ("tavily_api_key", "brave_api_key"):
            try:
                await secrets_provider.get_secret(secret_name, SYSTEM_USER_EMAIL)
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
    # Admin edits may fix the very issue (e.g. a tier cap, a different
    # endpoint, toggling use_responses_api) that caused a model to be marked
    # as broken for JSON mode or native OpenAI tool search. Clear the
    # provider's cached runtime downgrades so the next call re-probes.
    llm_provider_impl = getattr(request.app.state.providers, "llm", None)
    invalidate = getattr(
        llm_provider_impl,
        "invalidate_runtime_capability_cache_for_provider",
        None,
    )
    if not callable(invalidate):
        invalidate = getattr(llm_provider_impl, "invalidate_json_mode_cache_for_provider", None)
    if callable(invalidate):
        invalidate(provider_id)
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


@router.post("/api/v1/llm-providers/{provider_id}/enrich-models")
async def llm_provider_enrich_models(
    request: Request, provider_id: str, payload: EnrichModelsRequest
) -> dict[str, Any]:
    """Enrich model IDs with metadata from a saved provider."""
    require_admin(request)
    async with request.app.state.session_factory() as session:
        row = await get_llm_provider(session, provider_id)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")

    llm = request.app.state.providers.llm
    models: list[dict[str, Any]] = []
    for mid in payload.model_ids:
        mid = mid.strip()
        if not mid:
            continue
        try:
            info = await llm.enrich_model_info(mid, provider_id=provider_id)
            models.append(info.model_dump())
        except Exception:
            models.append({"model_id": mid, "error": "enrichment_failed"})
    return {"models": models}


@router.post("/api/v1/llm-providers/enrich-models-preview")
async def llm_provider_enrich_models_preview(
    request: Request, payload: EnrichModelsPreviewRequest
) -> dict[str, Any]:
    """Enrich model IDs without a saved provider (preview mode)."""
    require_admin(request)
    import contextlib
    import os

    resolved_key = payload.api_key or ""
    if not resolved_key and payload.env_var:
        resolved_key = os.environ.get(payload.env_var, "")
    if not resolved_key and payload.secret_name:
        secrets = getattr(request.app.state.providers, "secrets", None)
        if secrets:
            with contextlib.suppress(Exception):
                resolved_key = await secrets.get_secret(payload.secret_name, "system", None)

    llm = request.app.state.providers.llm
    models: list[dict[str, Any]] = []
    for mid in payload.model_ids:
        mid = mid.strip()
        if not mid:
            continue
        try:
            info = await llm.enrich_model_info(
                mid,
                preset=payload.preset,
                base_url=payload.base_url,
                api_key=resolved_key or None,
            )
            models.append(info.model_dump())
        except Exception:
            models.append({"model_id": mid, "error": "enrichment_failed"})
    return {"models": models}


@router.get("/api/v1/model-routing", response_model=ModelRoutingResponse)
async def model_routing_get(request: Request) -> ModelRoutingResponse:
    require_admin(request)
    async with request.app.state.session_factory() as session:
        rows = await list_model_routing(session)
    route_by_task = {row.task_type: row for row in rows}
    return ModelRoutingResponse(
        default=_routing_entry_from_row("default", route_by_task.get("default")),
        classifier=_routing_entry_from_row("classifier", route_by_task.get("classifier")),
        compaction=_routing_entry_from_row("compaction", route_by_task.get("compaction")),
        evaluator=_routing_entry_from_row("evaluator", route_by_task.get("evaluator")),
        speech_to_text=_routing_entry_from_row(
            "speech_to_text", route_by_task.get("speech_to_text")
        ),
        image_generation=_routing_entry_from_row(
            "image_generation", route_by_task.get("image_generation")
        ),
        attachment_analysis=_routing_entry_from_row(
            "attachment_analysis", route_by_task.get("attachment_analysis")
        ),
    )


@router.put("/api/v1/model-routing", response_model=ModelRoutingResponse)
async def model_routing_put(
    request: Request,
    payload: ModelRoutingUpdateRequest,
) -> ModelRoutingResponse:
    require_admin(request)
    updates = {task_type: getattr(payload, task_type) for task_type in _ROUTING_TASK_TYPES}
    llm = request.app.state.providers.llm
    prepared_updates: dict[str, tuple[str | None, str | None, dict[str, str] | None]] = {}
    for task_type, entry in updates.items():
        if entry.model is None or not entry.model.strip():
            if entry.reasoning_effort not in {None, "", "default"}:
                raise api_exception(
                    422,
                    "validation_error",
                    f"{task_type} reasoning_effort requires an explicit model",
                )
            prepared_updates[task_type] = (None, None, None)
            continue

        normalized_model = entry.model.strip()
        resolved_provider_id = await llm.find_provider_for_model(normalized_model)
        model_info = await llm.get_model_info(normalized_model, provider_id=resolved_provider_id)
        if not _route_model_is_eligible(
            task_type,
            model_id=normalized_model,
            model_info=model_info,
        ):
            raise api_exception(
                422,
                "validation_error",
                f"{task_type} model {normalized_model!r} is not eligible for that route",
            )
        config: dict[str, str] | None = None
        if isinstance(entry.reasoning_effort, str) and entry.reasoning_effort.strip():
            normalized_effort = normalize_reasoning_level(entry.reasoning_effort)
            if normalized_effort is None:
                raise api_exception(
                    422,
                    "validation_error",
                    f"{task_type} reasoning_effort {entry.reasoning_effort!r} is invalid",
                )
        else:
            normalized_effort = None
        if normalized_effort == "default":
            normalized_effort = None
        if normalized_effort is not None:
            if task_type not in _TEXT_ROUTING_TASK_TYPES:
                raise api_exception(
                    422,
                    "validation_error",
                    f"{task_type} does not support reasoning_effort",
                )
            if normalized_effort not in model_info.reasoning_efforts:
                raise api_exception(
                    422,
                    "validation_error",
                    f"{task_type} reasoning_effort {normalized_effort!r} is not supported by model {normalized_model!r}",
                )
            config = {"reasoning_effort": normalized_effort}
        prepared_updates[task_type] = (normalized_model, resolved_provider_id, config)

    async with request.app.state.session_factory() as session:
        existing_rows = await list_model_routing(session)
        for row in existing_rows:
            if row.task_type not in _ROUTING_TASK_TYPES:
                await delete_model_routing(session, row.task_type)
        for task_type, (normalized_model, resolved_provider_id, config) in prepared_updates.items():
            if normalized_model is None:
                await delete_model_routing(session, task_type)
                continue
            await upsert_model_routing(
                session,
                task_type=task_type,
                provider_id=resolved_provider_id,
                model=normalized_model,
                config=config,
            )
        await session.commit()
    return await model_routing_get(request)
