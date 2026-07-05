"""Settings, LLM providers, and model routing routes."""

from __future__ import annotations

import re
from collections import defaultdict
from time import monotonic
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from cognis.api.common import (
    api_exception,
    forbid_mutation_for_viewer,
    require_admin,
    require_current_user,
)
from cognis.api.models import (
    CodexUsageResponse,
    CursorPage,
    EnrichModelsPreviewRequest,
    EnrichModelsRequest,
    LLMProviderOAuthCompleteRequest,
    LLMProviderOAuthStatusResponse,
    LLMProviderRequest,
    LLMProviderResponse,
    LLMProviderTestResponse,
    LLMProviderUpdateRequest,
    ModelRoutingEntry,
    ModelRoutingResponse,
    ModelRoutingUpdateRequest,
    SettingResponse,
    SettingsCategoryResponse,
    SettingUpdateRequest,
    StepProfileCreateRequest,
    StepProfileResponse,
    StepProfileUpdateRequest,
    UserPreferencesResponse,
    UserPreferencesUpdateRequest,
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
from cognis.providers.llm.message_projection import VALID_MESSAGE_PROJECTION_POLICIES
from cognis.settings_schema import setting_category, validate_setting_value
from cognis.store.models import SystemWorkflowOverride, WorkflowRow
from cognis.store.queries import (
    create_llm_provider,
    delete_llm_provider,
    delete_model_routing,
    delete_setting,
    get_llm_provider,
    get_setting,
    get_user_ui_state_value,
    get_visible_llm_provider,
    list_llm_providers,
    list_model_routing,
    list_settings,
    update_llm_provider,
    upsert_model_routing,
    upsert_setting,
    upsert_user_ui_state,
)

router = APIRouter(tags=["settings"])
PROVIDER_TEST_COOLDOWN_SECONDS = 10.0
SAME_SESSION_MODEL_SENTINEL = "__same_session_model__"
USER_PREFERENCES_STATE_KEY = "ui.preferences"
_ROUTING_TASK_TYPES: tuple[str, ...] = (
    "default",
    "classifier",
    "compaction",
    "evaluator",
    "speech_to_text",
    "text_to_speech",
    "image_generation",
    "attachment_analysis",
    "embedding",
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


def _looks_like_tts_model(model_name: str) -> bool:
    normalized = model_name.strip().lower().replace("_", "-")
    return any(
        token in normalized
        for token in (
            "tts",
            "text-to-speech",
            "speech-1",
            "eleven",
            "elevenlabs",
            "piper",
        )
    )


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
    if task_type == "text_to_speech":
        display_name = getattr(model_info, "display_name", None)
        return _looks_like_tts_model(model_id) or (
            isinstance(display_name, str) and _looks_like_tts_model(display_name)
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
    if task_type == "embedding":
        return bool(getattr(model_info, "supports_embedding", False))
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


def _routing_entry_model(task_type: str, row: Any | None) -> ModelRoutingEntry:
    return ModelRoutingEntry(**_routing_entry_from_row(task_type, row))


def _apply_last_test_metadata(
    request: Request, response: LLMProviderResponse
) -> LLMProviderResponse:
    last_test = getattr(request.app.state, "provider_test_results", {}).get(response.provider_id)
    if last_test is None:
        return response
    return response.model_copy(update={"last_test": last_test})


def _validate_llm_provider_payload(location: str | None, config: dict[str, Any] | None) -> None:
    if not isinstance(config, dict):
        return
    preset = str(config.get("preset") or "").strip().lower()
    message_projection_policy = config.get("message_projection_policy")
    if message_projection_policy is not None:
        value = str(message_projection_policy).strip().lower()
        if value not in VALID_MESSAGE_PROJECTION_POLICIES:
            allowed = ", ".join(sorted(VALID_MESSAGE_PROJECTION_POLICIES))
            raise api_exception(
                400,
                "validation_error",
                f"message_projection_policy must be one of: {allowed}",
            )
    codex_transport = config.get("codex_transport")
    if codex_transport is not None:
        value = str(codex_transport).strip().lower()
        if value not in {"litellm", "direct"}:
            raise api_exception(
                400,
                "validation_error",
                "codex_transport must be either 'litellm' or 'direct'",
            )
        if value == "direct" and preset != "chatgpt":
            raise api_exception(
                400,
                "validation_error",
                "Direct Codex transport is only supported for ChatGPT providers",
            )
    if preset == "chatgpt" and location == "executor":
        raise api_exception(
            400,
            "validation_error",
            "ChatGPT OAuth providers must use controller execution location",
        )
    auth_config = config.get("auth_config")
    auth_provider = ""
    auth_mode = ""
    if isinstance(auth_config, dict):
        auth_mode = str(auth_config.get("mode") or "").strip().lower()
        auth_provider = str(auth_config.get("provider") or "").strip().lower()
    if (
        preset == "anthropic"
        and auth_mode == "oauth"
        and auth_provider in {"anthropic", "anthropic_subscription", "claude_subscription"}
        and location == "executor"
    ):
        raise api_exception(
            400,
            "validation_error",
            "Claude subscription OAuth providers must use controller execution location",
        )
    if location != "executor":
        return
    executor_id = config.get("executor_id")
    has_executor_id = isinstance(executor_id, str) and bool(executor_id.strip())
    executor_labels = config.get("executor_labels")
    valid_executor_labels = False
    if isinstance(executor_labels, dict):
        valid_executor_labels = bool(executor_labels) and all(
            isinstance(key, str)
            and bool(key.strip())
            and isinstance(value, str)
            and bool(value.strip())
            for key, value in executor_labels.items()
        )
        if executor_labels and not valid_executor_labels:
            raise api_exception(
                400,
                "validation_error",
                "Executor-routed provider executor_labels must contain only non-empty string keys and values",
            )
    has_executor_labels = valid_executor_labels
    if has_executor_id and has_executor_labels:
        raise api_exception(
            400,
            "validation_error",
            "Executor-routed providers must choose either executor_id or executor_labels, not both",
        )
    if not has_executor_id and not has_executor_labels:
        raise api_exception(
            400,
            "validation_error",
            "Executor-routed providers must specify executor_id or executor_labels",
        )


def _preferences_from_state(value: dict[str, Any] | None) -> UserPreferencesResponse:
    if not value:
        return UserPreferencesResponse()
    return UserPreferencesResponse.model_validate(value)


@router.get("/api/v1/user-preferences", response_model=UserPreferencesResponse)
async def user_preferences_detail(request: Request) -> UserPreferencesResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        value = await get_user_ui_state_value(session, user.email, USER_PREFERENCES_STATE_KEY)
    return _preferences_from_state(value)


@router.put("/api/v1/user-preferences", response_model=UserPreferencesResponse)
async def user_preferences_update(
    request: Request,
    payload: UserPreferencesUpdateRequest,
) -> UserPreferencesResponse:
    user = require_current_user(request)
    forbid_mutation_for_viewer(request)
    preferences = UserPreferencesResponse.model_validate(payload.model_dump(mode="json"))
    async with request.app.state.session_factory() as session:
        row = await upsert_user_ui_state(
            session,
            user.email,
            USER_PREFERENCES_STATE_KEY,
            preferences.model_dump(mode="json"),
        )
        await session.commit()
        await session.refresh(row)
    return preferences


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
        legacy_backend = await get_setting_value(session, "web.backend", "direct")
        search_backend = await get_setting_value(session, "web.search_backend", legacy_backend)
        fetch_backend = await get_setting_value(session, "web.fetch_backend", legacy_backend)
        fetch_fallback = await get_setting_value(session, "web.fetch_fallback_browser", True)
        browser_fetch_session_idle = await get_setting_value(
            session, "web.browser_fetch.session_idle_seconds", 60
        )
        browser_fetch_wait_timeout = await get_setting_value(
            session, "web.browser_fetch.wait_timeout_seconds", 30
        )
        browser_fetch_navigation_timeout = await get_setting_value(
            session, "web.browser_fetch.navigation_timeout_seconds", 60
        )
        browser_fetch_wait_until = await get_setting_value(
            session, "web.browser_fetch.wait_until", "domcontentloaded"
        )
        browser_fetch_network_idle = await get_setting_value(
            session, "web.browser_fetch.network_idle_after_dom_seconds", 3
        )
        browser_fetch_headed_fallback = await get_setting_value(
            session, "web.browser_fetch.headed_fallback_enabled", False
        )
        searxng_url = await get_setting_value(session, "web.searxng_url", "")

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

    searxng_configured = bool(isinstance(searxng_url, str) and searxng_url.strip())

    available_search = ["direct"]
    if tavily_configured:
        available_search.append("tavily")
    if brave_configured:
        available_search.append("brave")
    if searxng_configured:
        available_search.append("searxng")

    available_fetch = ["direct", "browser"]
    if tavily_configured:
        available_fetch.insert(1, "tavily")

    available_union = sorted({*available_search, *available_fetch})

    return WebConfigStatusResponse(
        backend=str(legacy_backend) if isinstance(legacy_backend, str) else "direct",
        search_backend=(str(search_backend) if isinstance(search_backend, str) else "direct"),
        fetch_backend=(str(fetch_backend) if isinstance(fetch_backend, str) else "direct"),
        fetch_fallback_browser=bool(fetch_fallback) if fetch_fallback is not None else True,
        browser_fetch_session_idle_seconds=(
            int(browser_fetch_session_idle) if isinstance(browser_fetch_session_idle, int) else 60
        ),
        browser_fetch_wait_timeout_seconds=(
            int(browser_fetch_wait_timeout) if isinstance(browser_fetch_wait_timeout, int) else 30
        ),
        browser_fetch_navigation_timeout_seconds=(
            int(browser_fetch_navigation_timeout)
            if isinstance(browser_fetch_navigation_timeout, int)
            else 60
        ),
        browser_fetch_wait_until=(
            str(browser_fetch_wait_until)
            if isinstance(browser_fetch_wait_until, str)
            else "domcontentloaded"
        ),
        browser_fetch_network_idle_after_dom_seconds=(
            int(browser_fetch_network_idle) if isinstance(browser_fetch_network_idle, int) else 3
        ),
        browser_fetch_headed_fallback_enabled=(
            bool(browser_fetch_headed_fallback)
            if browser_fetch_headed_fallback is not None
            else False
        ),
        tavily_configured=tavily_configured,
        brave_configured=brave_configured,
        searxng_url=str(searxng_url) if isinstance(searxng_url, str) else "",
        searxng_configured=searxng_configured,
        available_backends=available_union,
        available_search_backends=available_search,
        available_fetch_backends=available_fetch,
    )


@router.get("/api/v1/llm-providers", response_model=CursorPage[LLMProviderResponse])
async def llm_provider_list(request: Request) -> CursorPage[LLMProviderResponse]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        rows = await list_llm_providers(
            session, acting_user_email=user.email, include_inactive=True
        )
    items = [_apply_last_test_metadata(request, llm_provider_to_response(row)) for row in rows]
    return CursorPage(items=items, cursor=None, has_more=False)


def _provider_owner_for_create(user: Any, config: dict[str, Any]) -> str:
    scope = str(config.get("scope") or config.get("owner_scope") or "").strip().lower()
    if user.role == "admin" and scope in {"system", "shared"}:
        return SYSTEM_USER_EMAIL
    return user.email


def _provider_owner_for_update(
    request: Request, existing_owner_email: str, owner_scope: str | None
) -> str:
    normalized_scope = str(owner_scope or "").strip().lower()
    if not normalized_scope:
        return existing_owner_email
    if normalized_scope in {"system", "shared"}:
        require_admin(request)
        return SYSTEM_USER_EMAIL
    if normalized_scope == "user":
        user = require_current_user(request)
        return user.email
    raise api_exception(
        422, "validation_error", f"Unsupported provider owner_scope {owner_scope!r}"
    )


def _require_provider_manager(request: Request, provider_owner_email: str) -> Any:
    if provider_owner_email == SYSTEM_USER_EMAIL:
        return require_admin(request)
    user = require_current_user(request)
    if user.email != provider_owner_email:
        raise api_exception(403, "forbidden", "Resource access denied")
    return user


async def _require_visible_provider_manager(
    request: Request, provider_id: str
) -> tuple[Any, Any, str]:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        provider = await get_visible_llm_provider(session, provider_id, user.email)
    if provider is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    owner_email = provider.owner_email or SYSTEM_USER_EMAIL
    manager = _require_provider_manager(request, owner_email)
    return provider, manager, owner_email


@router.post("/api/v1/llm-providers", response_model=LLMProviderResponse)
async def llm_provider_create(request: Request, payload: LLMProviderRequest) -> LLMProviderResponse:
    user = require_current_user(request)
    from cognis.api.common import slugify

    _validate_llm_provider_payload(payload.location, payload.config)
    provider_id = payload.provider_id or slugify(payload.display_name)
    owner_email = _provider_owner_for_create(user, payload.config)
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
            owner_email=owner_email,
            config=payload.config,
            status=payload.status,
        )
        await session.commit()
        await session.refresh(row)
    return _apply_last_test_metadata(request, llm_provider_to_response(row))


@router.get("/api/v1/llm-providers/{provider_id}", response_model=LLMProviderResponse)
async def llm_provider_detail(request: Request, provider_id: str) -> LLMProviderResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_visible_llm_provider(session, provider_id, user.email)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    return _apply_last_test_metadata(request, llm_provider_to_response(row))


@router.put("/api/v1/llm-providers/{provider_id}", response_model=LLMProviderResponse)
async def llm_provider_update(
    request: Request,
    provider_id: str,
    payload: LLMProviderUpdateRequest,
) -> LLMProviderResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        existing = await get_visible_llm_provider(session, provider_id, user.email)
        if existing is None:
            raise api_exception(404, "not_found", "LLM provider not found")
        owner_email = existing.owner_email or SYSTEM_USER_EMAIL
        _require_provider_manager(request, owner_email)
        next_owner_email = _provider_owner_for_update(request, owner_email, payload.owner_scope)
        next_config = dict(payload.config or existing.config or {})
        if payload.owner_scope is not None:
            next_config["scope"] = "system" if next_owner_email == SYSTEM_USER_EMAIL else "user"
        _validate_llm_provider_payload(
            payload.location if payload.location is not None else existing.location,
            next_config,
        )
        ok = await update_llm_provider(
            session,
            provider_id,
            display_name=payload.display_name,
            location=payload.location,
            backend=payload.backend,
            owner_email=next_owner_email,
            config=next_config,
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
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        existing = await get_visible_llm_provider(session, provider_id, user.email)
        if existing is None:
            raise api_exception(404, "not_found", "LLM provider not found")
        owner_email = existing.owner_email or SYSTEM_USER_EMAIL
        _require_provider_manager(request, owner_email)
        ok = await delete_llm_provider(session, provider_id)
        await session.commit()
    return {"ok": ok}


@router.post("/api/v1/llm-providers/{provider_id}/set-default")
async def llm_provider_set_default(request: Request, provider_id: str) -> dict[str, Any]:
    """Mark a provider as the default. Clears any existing default."""
    user = require_current_user(request)
    from cognis.store.models import LLMProvider as LLMProviderRow

    async with request.app.state.session_factory() as session:
        target = await get_visible_llm_provider(session, provider_id, user.email)
        if target is None:
            raise api_exception(404, "not_found", "LLM provider not found")
        _require_provider_manager(request, target.owner_email or SYSTEM_USER_EMAIL)
        # Clear existing defaults
        from sqlalchemy import update

        await session.execute(
            update(LLMProviderRow)
            .where(LLMProviderRow.owner_email == target.owner_email)
            .values(is_default=False)
        )
        target.is_default = True
        await session.commit()
    return {"ok": True, "provider_id": provider_id}


@router.post("/api/v1/llm-providers/{provider_id}/test", response_model=LLMProviderTestResponse)
async def llm_provider_test(request: Request, provider_id: str) -> LLMProviderTestResponse:
    user = require_current_user(request)
    cooldowns: dict[str, float] = request.app.state.provider_test_cooldowns
    last_started_at = cooldowns.get(provider_id)
    if (
        last_started_at is not None
        and monotonic() - last_started_at < PROVIDER_TEST_COOLDOWN_SECONDS
    ):
        raise api_exception(429, "rate_limited", "Provider test cooldown is still active")
    async with request.app.state.session_factory() as session:
        row = await get_visible_llm_provider(session, provider_id, user.email)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    _require_provider_manager(request, row.owner_email or SYSTEM_USER_EMAIL)
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


@router.post(
    "/api/v1/llm-providers/{provider_id}/oauth/chatgpt/start",
    response_model=LLMProviderOAuthStatusResponse,
)
async def llm_provider_chatgpt_oauth_start(
    request: Request, provider_id: str
) -> LLMProviderOAuthStatusResponse:
    await _require_visible_provider_manager(request, provider_id)
    try:
        status = await request.app.state.providers.llm.start_chatgpt_oauth(provider_id)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except Exception as exc:
        raise api_exception(502, "provider_error", f"Failed to start OAuth: {exc!s}"[:300]) from exc
    return LLMProviderOAuthStatusResponse(provider_id=provider_id, **status)


@router.get(
    "/api/v1/llm-providers/{provider_id}/oauth/chatgpt/status",
    response_model=LLMProviderOAuthStatusResponse,
)
async def llm_provider_chatgpt_oauth_status(
    request: Request, provider_id: str
) -> LLMProviderOAuthStatusResponse:
    await _require_visible_provider_manager(request, provider_id)
    try:
        status = await request.app.state.providers.llm.get_chatgpt_oauth_status(provider_id)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except Exception as exc:
        raise api_exception(502, "provider_error", f"Failed to check OAuth: {exc!s}"[:300]) from exc
    status.pop("provider_id", None)
    return LLMProviderOAuthStatusResponse(provider_id=provider_id, **status)


@router.delete(
    "/api/v1/llm-providers/{provider_id}/oauth/chatgpt",
    response_model=dict,
)
async def llm_provider_chatgpt_oauth_clear(request: Request, provider_id: str) -> dict[str, bool]:
    await _require_visible_provider_manager(request, provider_id)
    try:
        ok = await request.app.state.providers.llm.clear_chatgpt_oauth(provider_id)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    return {"ok": ok}


@router.post(
    "/api/v1/llm-providers/{provider_id}/oauth/anthropic/start",
    response_model=LLMProviderOAuthStatusResponse,
)
async def llm_provider_anthropic_oauth_start(
    request: Request, provider_id: str
) -> LLMProviderOAuthStatusResponse:
    await _require_visible_provider_manager(request, provider_id)
    try:
        status = await request.app.state.providers.llm.start_anthropic_oauth(provider_id)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except Exception as exc:
        raise api_exception(502, "provider_error", f"Failed to start OAuth: {exc!s}"[:300]) from exc
    status.pop("provider_id", None)
    return LLMProviderOAuthStatusResponse(provider_id=provider_id, **status)


@router.post(
    "/api/v1/llm-providers/{provider_id}/oauth/anthropic/complete",
    response_model=LLMProviderOAuthStatusResponse,
)
async def llm_provider_anthropic_oauth_complete(
    request: Request, provider_id: str, payload: LLMProviderOAuthCompleteRequest
) -> LLMProviderOAuthStatusResponse:
    await _require_visible_provider_manager(request, provider_id)
    try:
        status = await request.app.state.providers.llm.complete_anthropic_oauth(
            provider_id, payload.callback_input
        )
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except Exception as exc:
        raise api_exception(
            502, "provider_error", f"Failed to complete OAuth: {exc!s}"[:300]
        ) from exc
    status.pop("provider_id", None)
    return LLMProviderOAuthStatusResponse(provider_id=provider_id, **status)


@router.get(
    "/api/v1/llm-providers/{provider_id}/oauth/anthropic/status",
    response_model=LLMProviderOAuthStatusResponse,
)
async def llm_provider_anthropic_oauth_status(
    request: Request, provider_id: str
) -> LLMProviderOAuthStatusResponse:
    await _require_visible_provider_manager(request, provider_id)
    try:
        status = await request.app.state.providers.llm.get_anthropic_oauth_status(provider_id)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except Exception as exc:
        raise api_exception(502, "provider_error", f"Failed to check OAuth: {exc!s}"[:300]) from exc
    status.pop("provider_id", None)
    return LLMProviderOAuthStatusResponse(provider_id=provider_id, **status)


@router.delete(
    "/api/v1/llm-providers/{provider_id}/oauth/anthropic",
    response_model=dict,
)
async def llm_provider_anthropic_oauth_clear(request: Request, provider_id: str) -> dict[str, bool]:
    await _require_visible_provider_manager(request, provider_id)
    try:
        ok = await request.app.state.providers.llm.clear_anthropic_oauth(provider_id)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    return {"ok": ok}


@router.get("/api/v1/llm-providers/{provider_id}/codex/usage", response_model=CodexUsageResponse)
async def llm_provider_codex_usage(request: Request, provider_id: str) -> CodexUsageResponse:
    require_admin(request)
    try:
        usage = await request.app.state.providers.llm.get_codex_usage(provider_id)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    except Exception as exc:
        raise api_exception(
            502, "provider_error", f"Failed to fetch Codex usage: {exc!s}"[:300]
        ) from exc
    return CodexUsageResponse(provider_id=provider_id, **usage)


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
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_visible_llm_provider(session, provider_id, user.email)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    _require_provider_manager(request, row.owner_email or SYSTEM_USER_EMAIL)
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
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        row = await get_visible_llm_provider(session, provider_id, user.email)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    _require_provider_manager(request, row.owner_email or SYSTEM_USER_EMAIL)

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
        rows = await list_model_routing(session, owner_email=SYSTEM_USER_EMAIL)
    route_by_task = {row.task_type: row for row in rows}
    return ModelRoutingResponse(
        default=_routing_entry_model("default", route_by_task.get("default")),
        classifier=_routing_entry_model("classifier", route_by_task.get("classifier")),
        compaction=_routing_entry_model("compaction", route_by_task.get("compaction")),
        evaluator=_routing_entry_model("evaluator", route_by_task.get("evaluator")),
        speech_to_text=_routing_entry_model("speech_to_text", route_by_task.get("speech_to_text")),
        text_to_speech=_routing_entry_model("text_to_speech", route_by_task.get("text_to_speech")),
        image_generation=_routing_entry_model(
            "image_generation", route_by_task.get("image_generation")
        ),
        attachment_analysis=_routing_entry_model(
            "attachment_analysis", route_by_task.get("attachment_analysis")
        ),
        embedding=_routing_entry_model("embedding", route_by_task.get("embedding")),
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

        normalized_model_input = entry.model.strip()
        if normalized_model_input == SAME_SESSION_MODEL_SENTINEL:
            if task_type != "compaction":
                raise api_exception(
                    422,
                    "validation_error",
                    f"{task_type} cannot use the same-session model sentinel",
                )
            if entry.reasoning_effort not in {None, "", "default"}:
                raise api_exception(
                    422,
                    "validation_error",
                    "compaction reasoning_effort cannot be set when using the same-session model",
                )
            prepared_updates[task_type] = (normalized_model_input, None, None)
            continue
        try:
            if hasattr(llm, "resolve_model_reference"):
                normalized_model, resolved_provider_id = await llm.resolve_model_reference(
                    normalized_model_input,
                    acting_user_email=SYSTEM_USER_EMAIL,
                )
            else:
                normalized_model = normalized_model_input
                resolved_provider_id = await llm.find_provider_for_model(
                    normalized_model, acting_user_email=SYSTEM_USER_EMAIL
                )
        except ValueError as exc:
            raise api_exception(
                422,
                "validation_error",
                f"{task_type} {exc}",
            ) from exc
        if resolved_provider_id is None:
            raise api_exception(
                422,
                "validation_error",
                f"{task_type} model {normalized_model!r} is not present in configured providers",
            )
        model_info = await llm.get_model_info(
            normalized_model,
            provider_id=resolved_provider_id,
            acting_user_email=SYSTEM_USER_EMAIL,
        )
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
        explicit_provider_reference = normalized_model_input != normalized_model
        prepared_updates[task_type] = (
            normalized_model,
            resolved_provider_id if explicit_provider_reference else None,
            config,
        )

    async with request.app.state.session_factory() as session:
        existing_rows = await list_model_routing(session, owner_email=SYSTEM_USER_EMAIL)
        for row in existing_rows:
            if row.task_type not in _ROUTING_TASK_TYPES:
                await delete_model_routing(session, row.task_type, owner_email=SYSTEM_USER_EMAIL)
        for task_type, (normalized_model, resolved_provider_id, config) in prepared_updates.items():
            if normalized_model is None:
                await delete_model_routing(session, task_type, owner_email=SYSTEM_USER_EMAIL)
                continue
            await upsert_model_routing(
                session,
                task_type=task_type,
                provider_id=resolved_provider_id,
                model=normalized_model,
                owner_email=SYSTEM_USER_EMAIL,
                config=config,
            )
        await session.commit()
    return await model_routing_get(request)
