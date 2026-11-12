"""Settings, LLM providers, and model routing routes."""

from __future__ import annotations

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
    ModelRoutingResponse,
    ModelRoutingUpdateRequest,
    ProviderMonitorResponse,
    ProviderUsageResponse,
    SettingResponse,
    SettingsCategoryResponse,
    SettingUpdateRequest,
    StepProfileCreateRequest,
    StepProfileResponse,
    StepProfileUpdateRequest,
    UserPreferencesResponse,
    UserPreferencesUpdateRequest,
    WebBackendUpdateRequest,
    WebConfigStatusResponse,
    WebDefaultsUpdateRequest,
)
from cognis.api.serializers import llm_provider_to_response, setting_to_response
from cognis.api.user_preferences import (
    USER_PREFERENCES_STATE_KEY,
)
from cognis.api.user_preferences import (
    preferences_from_state as _preferences_from_state,
)
from cognis.api.web_reconfigure import (
    finalize_web_executor_reconfigure_for_app,
    run_web_mutation_cancellation_safe,
)
from cognis.api.web_reconfigure import (
    web_settings_distributed_lock as _web_settings_distributed_lock,
)
from cognis.core.step_profile_management import StepProfileManagement
from cognis.core.step_profiles import (
    STEP_PROFILE_CUSTOM_SETTING_KEY,
    STEP_PROFILE_OVERRIDES_SETTING_KEY,
)
from cognis.logging import get_logger
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.runtime_settings import apply_runtime_setting
from cognis.settings_schema import get_setting_spec, validate_setting_value
from cognis.store.models import (
    LLMProvider,
    LocalModelDeployment,
)
from cognis.store.queries import (
    AUTO_RECAP_ENABLED_STATE_KEY,
    get_setting,
    get_user_ui_state_value,
    get_visible_llm_provider,
    list_llm_providers,
    list_settings,
    upsert_setting,
    upsert_user_ui_state,
)

router = APIRouter(tags=["settings"])
logger = get_logger(__name__)
PROVIDER_TEST_COOLDOWN_SECONDS = 10.0
SAME_SESSION_MODEL_SENTINEL = "__same_session_model__"


def _apply_last_test_metadata(
    request: Request, response: LLMProviderResponse
) -> LLMProviderResponse:
    last_test = getattr(request.app.state, "provider_test_results", {}).get(response.provider_id)
    if last_test is None:
        return response
    return response.model_copy(update={"last_test": last_test})


def _validate_llm_provider_payload(location: str | None, config: dict[str, Any] | None) -> None:
    from cognis.core.provider_management import validate_provider

    validate_provider(location, config)


async def _validate_provider_executor_eligibility(
    session: Any,
    *,
    location: str | None,
    config: dict[str, Any],
    owner_email: str,
) -> None:
    """Reject new provider selectors that cannot target local inference."""

    from cognis.core.provider_management import validate_executor_eligibility

    await validate_executor_eligibility(
        session, location=location, config=config, owner_email=owner_email
    )


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
        previous = await get_user_ui_state_value(session, user.email, USER_PREFERENCES_STATE_KEY)
        previous_preferences = _preferences_from_state(previous)
        # Deployed clients send a full display object without this newer key.
        # Preserve their account's family, but retain top-level {} /
        # omitted-display reset semantics. New clients reset explicitly by
        # sending Classic. Remove when clients without theme_family retire.
        if (
            "display" in payload.model_fields_set
            and "theme_family" not in payload.display.model_fields_set
        ):
            preferences.display.theme_family = previous_preferences.display.theme_family
        was_enabled = previous_preferences.chat.auto_recap
        if was_enabled != preferences.chat.auto_recap:
            await upsert_user_ui_state(
                session,
                user.email,
                AUTO_RECAP_ENABLED_STATE_KEY,
                {"enabled": preferences.chat.auto_recap},
            )
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
        try:
            spec = get_setting_spec(row.key, require_exposed=True)
        except ValueError:
            continue
        grouped[spec.category].append(setting_to_response(row, spec))
    return [
        SettingsCategoryResponse(category=category, items=items)
        for category, items in sorted(grouped.items())
    ]


@router.get("/api/v1/settings/step-profiles", response_model=list[StepProfileResponse])
async def settings_step_profiles(request: Request) -> list[StepProfileResponse]:
    require_admin(request)
    return (await _profile_service(request).read())["profiles"]


def _profile_service(request: Request) -> StepProfileManagement:
    return StepProfileManagement(
        request.app.state.session_factory, request.app.state.step_profile_registry
    )


@router.post("/api/v1/settings/step-profiles", response_model=StepProfileResponse)
async def settings_step_profile_create(
    request: Request,
    payload: StepProfileCreateRequest,
) -> StepProfileResponse:
    user = require_admin(request)
    from cognis.core.management_access import ManagementActor

    return StepProfileResponse.model_validate(
        await _profile_service(request).mutate(
            ManagementActor(user.email, user.role), "create", payload.profile_id.strip(), payload
        )
    )


@router.put("/api/v1/settings/step-profiles/{profile_id}", response_model=StepProfileResponse)
async def settings_step_profile_update(
    request: Request,
    profile_id: str,
    payload: StepProfileUpdateRequest,
) -> StepProfileResponse:
    user = require_admin(request)
    from cognis.core.management_access import ManagementActor

    return StepProfileResponse.model_validate(
        await _profile_service(request).mutate(
            ManagementActor(user.email, user.role), "update", profile_id, payload
        )
    )


@router.delete("/api/v1/settings/step-profiles/{profile_id}", response_model=StepProfileResponse)
async def settings_step_profile_reset(request: Request, profile_id: str) -> StepProfileResponse:
    user = require_admin(request)
    from cognis.core.management_access import ManagementActor

    return StepProfileResponse.model_validate(
        await _profile_service(request).mutate(
            ManagementActor(user.email, user.role), "reset", profile_id
        )
    )


@router.get("/api/v1/settings/{key}", response_model=SettingResponse)
async def setting_detail(request: Request, key: str) -> SettingResponse:
    require_admin(request)
    try:
        spec = get_setting_spec(key, require_exposed=True)
    except ValueError as exc:
        raise api_exception(404, "not_found", "Setting not found") from exc
    async with request.app.state.session_factory() as session:
        row = await get_setting(session, key)
    if row is None:
        raise api_exception(404, "not_found", "Setting not found")
    return setting_to_response(row, spec)


async def _persist_and_apply_setting(
    request: Request,
    *,
    key: str,
    value: object,
    updated_by: str,
) -> SettingResponse:
    """Serialize local mutations, persist, hot-apply, and converge on DB state."""

    from cognis.core.settings_management import SettingsManagement

    return SettingResponse.model_validate(
        await SettingsManagement(
            session_factory=request.app.state.session_factory,
            update_lock=request.app.state.settings_update_lock,
            apply=lambda setting_key, setting_value: apply_runtime_setting(
                request.app, setting_key, setting_value
            ),
            secrets=getattr(request.app.state.providers, "secrets", None),
            finalize_web=lambda: finalize_web_executor_reconfigure_for_app(
                request.app, reason="web_setting_update"
            ),
        ).put(key=key, value=value, updated_by=updated_by)
    )


@router.put("/api/v1/settings/{key}", response_model=SettingResponse)
async def setting_update(
    request: Request, key: str, payload: SettingUpdateRequest
) -> SettingResponse:
    user = require_admin(request)
    try:
        validate_setting_value(key, payload.value)
    except ValueError as exc:
        raise api_exception(400, "validation_error", str(exc)) from exc
    return await _persist_and_apply_setting(
        request,
        key=key,
        value=payload.value,
        updated_by=user.email,
    )


@router.delete("/api/v1/settings/{key}", response_model=SettingResponse)
async def setting_reset(request: Request, key: str) -> SettingResponse:
    """Reset a setting to its registry default and apply it to this worker."""

    user = require_admin(request)
    try:
        spec = get_setting_spec(key, require_exposed=True)
    except ValueError as exc:
        raise api_exception(404, "not_found", "Setting not found") from exc
    return await _persist_and_apply_setting(
        request,
        key=key,
        value=spec.default,
        updated_by=user.email,
    )


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
            session, "web.browser_fetch.headed_fallback_enabled", True
        )
        tavily_enabled = await get_setting_value(session, "web.tavily_enabled", True)
        brave_enabled = await get_setting_value(session, "web.brave_enabled", True)
        searxng_enabled = await get_setting_value(session, "web.searxng_enabled", True)
        searxng_url = await get_setting_value(session, "web.searxng_url", "")
        searxng_engines = await get_setting_value(session, "web.searxng_engines", "")
        searxng_categories = await get_setting_value(session, "web.searxng_categories", "")
        searxng_language = await get_setting_value(session, "web.searxng_language", "")
        from cognis.core.web_management import configured_web_backends

        configured = await configured_web_backends(
            session, getattr(request.app.state.providers, "secrets", None)
        )
    tavily_configured = configured["tavily"]
    brave_configured = configured["brave"]
    searxng_configured = configured["searxng"]
    tavily_enabled = bool(tavily_enabled)
    brave_enabled = bool(brave_enabled)
    searxng_enabled = bool(searxng_enabled)

    available_search = ["direct"]
    if tavily_configured and tavily_enabled:
        available_search.append("tavily")
    if brave_configured and brave_enabled:
        available_search.append("brave")
    if searxng_configured and searxng_enabled:
        available_search.append("searxng")

    available_fetch = ["direct", "browser"]
    if tavily_configured and tavily_enabled:
        available_fetch.insert(1, "tavily")

    available_union = sorted({*available_search, *available_fetch})
    normalized_search_backend = str(search_backend) if isinstance(search_backend, str) else "direct"
    if normalized_search_backend not in available_search:
        normalized_search_backend = "direct"
    normalized_fetch_backend = str(fetch_backend) if isinstance(fetch_backend, str) else "direct"
    if normalized_fetch_backend not in available_fetch:
        normalized_fetch_backend = "direct"

    return WebConfigStatusResponse(
        backend=str(legacy_backend) if isinstance(legacy_backend, str) else "direct",
        search_backend=normalized_search_backend,
        fetch_backend=normalized_fetch_backend,
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
            else True
        ),
        tavily_configured=tavily_configured,
        tavily_enabled=tavily_enabled,
        brave_configured=brave_configured,
        brave_enabled=brave_enabled,
        searxng_url=str(searxng_url) if isinstance(searxng_url, str) else "",
        searxng_engines=(str(searxng_engines) if isinstance(searxng_engines, str) else ""),
        searxng_categories=(str(searxng_categories) if isinstance(searxng_categories, str) else ""),
        searxng_language=(str(searxng_language) if isinstance(searxng_language, str) else ""),
        searxng_configured=searxng_configured,
        searxng_enabled=searxng_enabled,
        available_backends=available_union,
        available_search_backends=available_search,
        available_fetch_backends=available_fetch,
    )


@router.put(
    "/api/v1/web-config/defaults",
    response_model=WebConfigStatusResponse,
)
async def web_defaults_update(
    request: Request,
    payload: WebDefaultsUpdateRequest,
) -> WebConfigStatusResponse:
    """Atomically update canonical search and fetch backend defaults."""
    user = require_admin(request)

    async def _update() -> None:
        settings_committed = False
        try:
            async with (
                request.app.state.settings_update_lock,
                _web_settings_distributed_lock(request.app.state.session_factory),
                request.app.state.session_factory() as session,
            ):
                current = await web_config_status(request)
                if payload.search_backend not in current.available_search_backends:
                    raise api_exception(
                        400,
                        "validation_error",
                        f"Search backend '{payload.search_backend}' is not configured and enabled",
                    )
                if payload.fetch_backend not in current.available_fetch_backends:
                    raise api_exception(
                        400,
                        "validation_error",
                        f"Fetch backend '{payload.fetch_backend}' is not configured and enabled",
                    )

                for key, value in (
                    ("web.search_backend", payload.search_backend),
                    ("web.fetch_backend", payload.fetch_backend),
                ):
                    spec = get_setting_spec(key)
                    await upsert_setting(
                        session,
                        key=key,
                        value=value,
                        category=spec.category,
                        updated_by=user.email,
                    )
                await session.commit()
                settings_committed = True
        finally:
            if settings_committed:
                await finalize_web_executor_reconfigure_for_app(
                    request.app,
                    reason="web_defaults_update",
                )

    await run_web_mutation_cancellation_safe(_update, reason="web_defaults_update")
    return await web_config_status(request)


@router.put(
    "/api/v1/web-config/backends/{backend}",
    response_model=WebConfigStatusResponse,
)
async def web_backend_update(
    request: Request,
    backend: str,
    payload: WebBackendUpdateRequest,
) -> WebConfigStatusResponse:
    """Update one optional web backend while preserving runtime-safe invariants."""
    user = require_admin(request)
    if backend not in {"tavily", "brave", "searxng"}:
        raise api_exception(404, "not_found", "Web backend not found")
    if payload.remove_configuration and payload.enabled:
        raise api_exception(
            400,
            "validation_error",
            "A removed backend configuration cannot remain enabled",
        )

    secrets_provider = getattr(request.app.state.providers, "secrets", None)
    secret_name = None
    if backend != "searxng":
        secret_name = f"{backend}_api_key"
        if secrets_provider is None:
            raise api_exception(503, "provider_unavailable", "Secrets provider is unavailable")
        api_key = (payload.api_key or "").strip()
    else:
        api_key = ""
    if secrets_provider is None and secret_name is not None:
        raise api_exception(503, "provider_unavailable", "Secrets provider is unavailable")

    async def _update() -> None:
        if secret_name is not None:
            assert secrets_provider is not None
        settings_committed = False
        secret_mutated = False
        try:
            async with (
                request.app.state.settings_update_lock,
                _web_settings_distributed_lock(request.app.state.session_factory),
                request.app.state.session_factory() as session,
            ):
                from cognis.store.queries import get_setting_value

                if secret_name is not None:
                    assert secrets_provider is not None
                    secret_configured = False
                    try:
                        await secrets_provider.get_secret(secret_name, SYSTEM_USER_EMAIL)
                        secret_configured = True
                    except KeyError:
                        pass
                    except Exception as exc:
                        raise api_exception(
                            503,
                            "provider_unavailable",
                            "Unable to read web backend credentials",
                        ) from exc
                    if payload.enabled and not secret_configured and not api_key:
                        raise api_exception(
                            400,
                            "validation_error",
                            f"{backend.title()} requires an API key before it can be enabled",
                        )
                    if api_key:
                        await secrets_provider.set_secret(
                            secret_name,
                            api_key,
                            SYSTEM_USER_EMAIL,
                            scope="system",
                            agent_id=None,
                            description=f"API key for {backend.title()}",
                        )
                        secret_mutated = True

                updates: dict[str, object] = {f"web.{backend}_enabled": payload.enabled}
                if backend == "searxng":
                    for key, submitted in (
                        ("web.searxng_url", payload.searxng_url),
                        ("web.searxng_engines", payload.searxng_engines),
                        ("web.searxng_categories", payload.searxng_categories),
                        ("web.searxng_language", payload.searxng_language),
                    ):
                        current = await get_setting_value(session, key, "")
                        if payload.remove_configuration:
                            updates[key] = ""
                        elif submitted is not None:
                            updates[key] = submitted.strip()
                        else:
                            updates[key] = current if isinstance(current, str) else ""
                    if payload.enabled and not str(updates["web.searxng_url"]).strip():
                        raise api_exception(
                            400,
                            "validation_error",
                            "SearXNG requires an instance URL before it can be enabled",
                        )

                if not payload.enabled:
                    for key in ("web.search_backend", "web.fetch_backend"):
                        current = await get_setting_value(session, key, "direct")
                        if current == backend:
                            updates[key] = "direct"

                for key, value in updates.items():
                    await upsert_setting(
                        session,
                        key=key,
                        value=value,
                        category=get_setting_spec(key).category,
                        updated_by=user.email,
                    )
                await session.commit()
                settings_committed = True

                if secret_name is not None and payload.remove_configuration:
                    assert secrets_provider is not None
                    await secrets_provider.delete_secret(
                        secret_name,
                        SYSTEM_USER_EMAIL,
                        scope="system",
                        agent_id=None,
                    )
        finally:
            if settings_committed or secret_mutated:
                await finalize_web_executor_reconfigure_for_app(
                    request.app,
                    reason=f"web_backend_update:{backend}",
                )

    await run_web_mutation_cancellation_safe(
        _update,
        reason=f"web_backend_update:{backend}",
    )
    return await web_config_status(request)


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
    return str(user.email)


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


def _llm_provider_preview_requires_admin(
    *,
    saved_provider_location: str | None,
    saved_provider_preset: str | None,
    preset: str,
    location: str,
    has_auth_override: bool,
) -> bool:
    """Return whether provider preview discovery needs admin privileges.

    Existing provider managers may use preview discovery only for the safe
    executor-Ollama case needed to apply current form executor selector values.
    Auth overrides stay admin-only because preview discovery can resolve
    controller env vars/secrets before making outbound provider requests.
    """

    return not (
        saved_provider_location == "executor"
        and str(saved_provider_preset or "").strip().lower() == "ollama"
        and preset.strip().lower() == "ollama"
        and location == "executor"
        and not has_auth_override
    )


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


async def _provider_deployment_ids(session: Any, provider_id: str) -> list[str]:
    result = await session.execute(
        select(LocalModelDeployment.deployment_id)
        .where(LocalModelDeployment.provider_id == provider_id)
        .order_by(LocalModelDeployment.deployment_id.asc())
    )
    return [str(value) for value in result.scalars().all()]


async def _guard_local_provider_update(
    session: Any,
    existing: LLMProvider,
    *,
    next_owner_email: str,
    next_location: str,
    next_config: dict[str, Any],
    next_status: str,
) -> None:
    from cognis.core.provider_management import guard_local_update

    await guard_local_update(
        session,
        existing,
        next_owner_email=next_owner_email,
        next_location=next_location,
        next_config=next_config,
        next_status=next_status,
    )


@router.post("/api/v1/llm-providers", response_model=LLMProviderResponse)
async def llm_provider_create(request: Request, payload: LLMProviderRequest) -> LLMProviderResponse:
    user = require_current_user(request)
    from cognis.core.management_access import ManagementActor
    from cognis.core.provider_management import ProviderManagement

    row = await ProviderManagement(
        request.app.state.session_factory,
        request.app.state.providers.llm,
        usage_price_publisher=getattr(request.app.state, "usage_price_publisher", None),
    ).create(ManagementActor(user.email, user.role), payload)
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
    from cognis.core.management_access import ManagementActor
    from cognis.core.provider_management import ProviderManagement

    row = await ProviderManagement(
        request.app.state.session_factory,
        request.app.state.providers.llm,
        usage_price_publisher=getattr(request.app.state, "usage_price_publisher", None),
    ).update(ManagementActor(user.email, user.role), provider_id, payload)
    if row is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    return _apply_last_test_metadata(request, llm_provider_to_response(row))


@router.delete("/api/v1/llm-providers/{provider_id}", response_model=dict)
async def llm_provider_delete(request: Request, provider_id: str) -> dict[str, bool]:
    user = require_current_user(request)
    from cognis.core.management_access import ManagementActor
    from cognis.core.provider_management import ProviderManagement

    return await ProviderManagement(
        request.app.state.session_factory,
        request.app.state.providers.llm,
        usage_price_publisher=getattr(request.app.state, "usage_price_publisher", None),
    ).delete(ManagementActor(user.email, user.role), provider_id)


@router.post("/api/v1/llm-providers/{provider_id}/set-default")
async def llm_provider_set_default(request: Request, provider_id: str) -> dict[str, Any]:
    """Mark a provider as the default. Clears any existing default."""
    user = require_current_user(request)
    from cognis.core.management_access import ManagementActor
    from cognis.core.provider_management import ProviderManagement

    return await ProviderManagement(
        request.app.state.session_factory, request.app.state.providers.llm
    ).set_default(ManagementActor(user.email, user.role), provider_id)


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
            provider_id, timeout_seconds=15, acting_user_email=user.email
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
    if status.pop("authorization_completed", False):
        await request.app.state.provider_monitor.invalidate(provider_id)
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
    await request.app.state.provider_monitor.invalidate(provider_id)
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
    await request.app.state.provider_monitor.invalidate(provider_id)
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
    await request.app.state.provider_monitor.invalidate(provider_id)
    return {"ok": ok}


@router.get("/api/v1/llm-providers/{provider_id}/codex/usage", response_model=CodexUsageResponse)
async def llm_provider_codex_usage(request: Request, provider_id: str) -> CodexUsageResponse:
    require_admin(request)
    return await llm_provider_usage(request, provider_id)


@router.get("/api/v1/llm-providers/{provider_id}/usage", response_model=ProviderUsageResponse)
async def llm_provider_usage(request: Request, provider_id: str) -> ProviderUsageResponse:
    """Return hosted usage windows for any visible provider that reports them."""

    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        provider = await get_visible_llm_provider(session, provider_id, user.email)
    if provider is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    result = await _provider_monitor_result(request, provider_id)
    return result.usage or ProviderUsageResponse(
        provider_id=provider_id,
        ok=False,
        source="unsupported",
        unavailable_reason=result.error or "not_checked",
    )


@router.get("/api/v1/provider-monitor")
async def provider_monitor_list(request: Request) -> list[ProviderMonitorResponse]:
    """Return cached, user-visible enabled providers without upstream calls."""
    user = require_current_user(request)
    from cognis.core.provider_monitor import ProviderMonitor

    monitor: ProviderMonitor = request.app.state.provider_monitor
    return await monitor.list_visible(user.email)


async def _provider_monitor_result(
    request: Request, provider_id: str, *, force: bool = False
) -> ProviderMonitorResponse:
    from cognis.core.provider_monitor import ProviderMonitor

    monitor: ProviderMonitor = request.app.state.provider_monitor
    try:
        return await monitor.get(provider_id, force=force)
    except TimeoutError:
        cached = await monitor.get(provider_id, refresh_if_due=False)
        return cached.model_copy(
            update={"error": "Refresh still in progress; try again shortly", "stale": True}
        )


@router.get("/api/v1/provider-monitor/{provider_id}", response_model=ProviderMonitorResponse)
async def provider_monitor_detail(request: Request, provider_id: str) -> ProviderMonitorResponse:
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        provider = await get_visible_llm_provider(session, provider_id, user.email)
    if provider is None:
        raise api_exception(404, "not_found", "LLM provider not found")
    return await _provider_monitor_result(request, provider_id)


@router.post(
    "/api/v1/provider-monitor/{provider_id}/refresh", response_model=ProviderMonitorResponse
)
async def provider_monitor_refresh(request: Request, provider_id: str) -> ProviderMonitorResponse:
    """Explicit manual refresh, even when automatic checks are disabled."""
    from cognis.api.common import forbid_mutation_for_viewer

    forbid_mutation_for_viewer(request)
    user = require_current_user(request)
    async with request.app.state.session_factory() as session:
        provider = await get_visible_llm_provider(session, provider_id, user.email)
    if provider is None or provider.status != "active":
        raise api_exception(404, "not_found", "Enabled LLM provider not found")
    return await _provider_monitor_result(request, provider_id, force=True)


@router.post("/api/v1/llm-providers/discover-models-preview")
async def llm_provider_discover_models_preview(request: Request) -> dict[str, Any]:
    """Discover models without a saved provider (preview mode)."""
    body = await request.json()
    provider_id = body.get("provider_id") if isinstance(body.get("provider_id"), str) else None
    preset = str(body.get("preset", ""))
    base_url = str(body.get("base_url", ""))
    api_key = body.get("api_key") or None
    secret_name = body.get("secret_name") or None
    env_var = body.get("env_var") or None
    location = str(body.get("location", "controller"))
    has_auth_override = bool(api_key or secret_name or env_var)
    saved_provider_location: str | None = None
    saved_provider_preset: str | None = None
    if provider_id:
        saved_provider, _, _ = await _require_visible_provider_manager(request, provider_id)
        saved_provider_location = saved_provider.location
        saved_provider_config = dict(saved_provider.config or {})
        saved_provider_preset = str(saved_provider_config.get("preset", ""))
    if _llm_provider_preview_requires_admin(
        saved_provider_location=saved_provider_location,
        saved_provider_preset=saved_provider_preset,
        preset=preset,
        location=location,
        has_auth_override=has_auth_override,
    ):
        require_admin(request)
    executor_id = body.get("executor_id") if isinstance(body.get("executor_id"), str) else None
    raw_executor_labels = body.get("executor_labels")
    if raw_executor_labels is not None and not isinstance(raw_executor_labels, dict):
        raise api_exception(
            400,
            "validation_error",
            "Executor-routed provider executor_labels must be an object",
        )
    executor_labels = raw_executor_labels if isinstance(raw_executor_labels, dict) else None
    preview_config: dict[str, Any] = {"preset": preset}
    if executor_id:
        preview_config["executor_id"] = executor_id
    if raw_executor_labels is not None:
        preview_config["executor_labels"] = raw_executor_labels
    _validate_llm_provider_payload(location, preview_config)
    try:
        models = await request.app.state.providers.llm.discover_models_preview(
            preset=preset,
            base_url=base_url,
            api_key=api_key,
            aws_config={
                key: body[key].strip()
                for key in ("aws_region_name", "aws_profile_name")
                if isinstance(body.get(key), str) and body[key].strip()
            },
            secret_name=secret_name,
            env_var=env_var,
            location=location,
            executor_id=executor_id,
            executor_labels=executor_labels,
            **(
                {"catalog_provider": body["catalog_provider"]}
                if isinstance(body.get("catalog_provider"), str)
                else {}
            ),
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
                **(
                    {"catalog_provider": payload.catalog_provider}
                    if payload.catalog_provider
                    else {}
                ),
            )
            models.append(info.model_dump())
        except Exception:
            models.append({"model_id": mid, "error": "enrichment_failed"})
    return {"models": models}


@router.get("/api/v1/model-routing", response_model=ModelRoutingResponse)
async def model_routing_get(request: Request) -> ModelRoutingResponse:
    require_admin(request)
    from cognis.core.routing_management import get_bindings

    response, _ = await get_bindings(request.app.state.session_factory)
    return response


@router.put("/api/v1/model-routing", response_model=ModelRoutingResponse)
async def model_routing_put(
    request: Request,
    payload: ModelRoutingUpdateRequest,
) -> ModelRoutingResponse:
    user = require_admin(request)
    from cognis.core.management_access import ManagementActor
    from cognis.core.routing_management import put_bindings

    return ModelRoutingResponse.model_validate(
        await put_bindings(
            request.app.state.session_factory,
            request.app.state.providers.llm,
            ManagementActor(user.email, user.role),
            payload,
        )
    )
