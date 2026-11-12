"""Provider configuration operations shared by HTTP and controller tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update

from cognis.api.common import api_exception, slugify
from cognis.api.models import LLMProviderRequest, LLMProviderUpdateRequest
from cognis.core.configuration_dependencies import assert_provider_unreferenced
from cognis.core.executor_resolution import labels_match
from cognis.core.management_access import ManagementActor, check_updated_at
from cognis.models.executor_inference import executor_local_inference_configured
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.message_projection import VALID_MESSAGE_PROJECTION_POLICIES
from cognis.store.local_models import lock_local_model_dispatch_guard
from cognis.store.models import LLMProvider, LocalModelDeployment, LocalModelOperation, User
from cognis.store.queries import (
    create_llm_provider,
    delete_llm_provider,
    get_executor_row,
    get_llm_provider,
    get_visible_llm_provider,
    list_executors,
    update_llm_provider,
)

if TYPE_CHECKING:
    from cognis.core.usage.price_publisher import UsagePricePublisher


def validate_provider(location: str | None, config: dict[str, Any] | None) -> None:
    """Validate supported protocols and executor selectors at one boundary."""
    if not isinstance(config, dict):
        return
    preset = str(config.get("preset") or "").strip().lower()
    for model in config.get("models", []) or []:
        if not isinstance(model, dict):
            continue
        for field in ("primary_cache_ttl", "auxiliary_cache_ttl"):
            if field in model and model[field] not in ("5m", "1h"):
                raise api_exception(400, "validation_error", f"{field} must be '5m' or '1h'")
    protocol = str(config.get("protocol", "auto")).strip().lower()
    if preset == "anthropic" and protocol not in {"auto", "anthropic_messages", "litellm"}:
        raise api_exception(
            400,
            "validation_error",
            "Anthropic protocol must be 'auto', 'anthropic_messages', or 'litellm'",
        )
    projection = config.get("message_projection_policy")
    if (
        projection is not None
        and str(projection).strip().lower() not in VALID_MESSAGE_PROJECTION_POLICIES
    ):
        raise api_exception(
            400,
            "validation_error",
            f"message_projection_policy must be one of: {', '.join(sorted(VALID_MESSAGE_PROJECTION_POLICIES))}",
        )
    transport = config.get("codex_transport")
    if transport is not None:
        value = str(transport).strip().lower()
        if value not in {"litellm", "direct"}:
            raise api_exception(
                400, "validation_error", "codex_transport must be either 'litellm' or 'direct'"
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
    auth = config.get("auth_config")
    if (
        isinstance(auth, dict)
        and preset == "anthropic"
        and str(auth.get("mode", "")).lower() == "oauth"
    ):
        if (
            str(auth.get("provider", "")).lower()
            in {"anthropic", "anthropic_subscription", "claude_subscription"}
            and location == "executor"
        ):
            raise api_exception(
                400,
                "validation_error",
                "Claude subscription OAuth providers must use controller execution location",
            )
        if protocol == "litellm":
            raise api_exception(
                400,
                "validation_error",
                "Claude subscription OAuth requires the native Anthropic Messages protocol",
            )
    if location != "executor":
        return
    executor_id = config.get("executor_id")
    has_id = isinstance(executor_id, str) and bool(executor_id.strip())
    labels = config.get("executor_labels")
    valid_labels = (
        isinstance(labels, dict)
        and bool(labels)
        and all(
            isinstance(k, str) and bool(k.strip()) and isinstance(v, str) and bool(v.strip())
            for k, v in labels.items()
        )
    )
    if isinstance(labels, dict) and labels and not valid_labels:
        raise api_exception(
            400,
            "validation_error",
            "Executor-routed provider executor_labels must contain only non-empty string keys and values",
        )
    if has_id and valid_labels:
        raise api_exception(
            400,
            "validation_error",
            "Executor-routed providers must choose either executor_id or executor_labels, not both",
        )
    if not has_id and not valid_labels:
        raise api_exception(
            400,
            "validation_error",
            "Executor-routed providers must specify executor_id or executor_labels",
        )


async def validate_executor_eligibility(
    session: Any,
    *,
    location: str | None,
    config: dict[str, Any],
    owner_email: str,
) -> None:
    """Reject selectors that cannot target authorized local inference."""
    if location != "executor":
        return
    executor_id = config.get("executor_id")
    if isinstance(executor_id, str) and executor_id.strip():
        row = await get_executor_row(
            session, executor_id.strip(), owner_email=owner_email, include_shared=True
        )
        if row is None or not executor_local_inference_configured(row):
            raise api_exception(
                400,
                "validation_error",
                "Selected executor is unavailable or has local inference disabled",
            )
        return
    selector = config.get("executor_labels")
    if not isinstance(selector, dict):
        return
    rows = await list_executors(session, owner_email=owner_email, include_shared=True)
    if not any(
        executor_local_inference_configured(row) and labels_match(row.labels or {}, selector)
        for row in rows
    ):
        raise api_exception(
            400,
            "validation_error",
            "Executor labels match no active executor with local inference enabled",
        )


async def deployment_ids(session: Any, provider_id: str) -> list[str]:
    """Find declarative deployments that depend on a provider."""
    return list(
        await session.scalars(
            select(LocalModelDeployment.deployment_id).where(
                LocalModelDeployment.provider_id == provider_id
            )
        )
    )


async def guard_local_update(
    session: Any,
    existing: LLMProvider,
    *,
    next_owner_email: str,
    next_location: str,
    next_config: dict[str, Any],
    next_status: str,
) -> None:
    """Keep managed provider routing and referenced deployment targets valid."""
    config = existing.config if isinstance(existing.config, dict) else {}
    routing_changed = (
        next_owner_email != (existing.owner_email or SYSTEM_USER_EMAIL)
        or next_location != existing.location
        or next_status != existing.status
        or any(
            next_config.get(k) != config.get(k)
            for k in ("preset", "executor_id", "executor_labels")
        )
    )
    ids = await deployment_ids(session, existing.provider_id)
    if routing_changed and existing.managed_local_key is not None:
        raise api_exception(
            409,
            "local_model_dependencies",
            "Cognis-managed local provider routing is immutable",
            details={"deployment_ids": ids},
        )
    if not ids or not routing_changed:
        return
    active = await session.scalar(
        select(LocalModelOperation.operation_id)
        .where(
            LocalModelOperation.deployment_id.in_(ids),
            LocalModelOperation.state.in_(["queued", "running", "cancel_requested", "interrupted"]),
        )
        .limit(1)
    )
    if active is not None:
        raise api_exception(
            409,
            "local_model_dependencies",
            "cannot change provider routing while local-model operations are active",
            details={"deployment_ids": ids},
        )
    from cognis.core.local_model_providers import LocalModelProviderResolver
    from cognis.core.local_model_service import resolve_authorized_deployment_executors

    proposed = LLMProvider(
        provider_id=existing.provider_id,
        display_name=existing.display_name,
        location=next_location,
        backend=existing.backend,
        owner_email=next_owner_email,
        config=next_config,
        status=next_status,
    )
    deployments = await session.scalars(
        select(LocalModelDeployment).where(LocalModelDeployment.deployment_id.in_(ids))
    )
    for deployment in deployments:
        shared = deployment.owner_email == SYSTEM_USER_EMAIL
        owner = await session.get(User, deployment.owner_email)
        resolved = await LocalModelProviderResolver().resolve(
            session,
            proposed,
            actor_email=deployment.owner_email,
            actor_role=owner.role if owner else ("admin" if shared else "user"),
            shared=shared,
        )
        host_ids = {row.executor_id for row in resolved.hosts} if resolved else set()
        target_ids = {
            row.executor_id
            for row in await resolve_authorized_deployment_executors(session, deployment)
        }
        if not target_ids or not target_ids.issubset(host_ids):
            raise api_exception(
                409,
                "local_model_dependencies",
                "provider routing would exclude referenced deployment targets",
                details={"deployment_ids": ids},
            )


@dataclass(frozen=True)
class ProviderManagement:
    """Short configuration transactions and post-commit capability invalidation."""

    session_factory: Any
    llm: Any
    usage_price_publisher: UsagePricePublisher | None = None

    async def create(
        self, actor: ManagementActor, payload: LLMProviderRequest, *, preview: bool = False
    ) -> Any:
        """Create a provider without taking ownership of another user's resources."""
        actor.require_mutation()
        validate_provider(payload.location, payload.config)
        provider_id = payload.provider_id or slugify(payload.display_name)
        if provider_id == "cognis":
            raise api_exception(
                422, "validation_error", "cognis is reserved for named inference routes"
            )
        scope = (
            str(payload.config.get("scope") or payload.config.get("owner_scope") or "")
            .strip()
            .lower()
        )
        if scope in {"shared", "system"}:
            actor.require_admin()
            owner = SYSTEM_USER_EMAIL
        else:
            owner = actor.email
        async with self.session_factory() as session:
            await lock_local_model_dispatch_guard(session)
            if await get_llm_provider(session, provider_id) is not None:
                raise api_exception(409, "conflict", "LLM provider already exists")
            await validate_executor_eligibility(
                session, location=payload.location, config=payload.config, owner_email=owner
            )
            if preview:
                return {
                    "preview": True,
                    **payload.model_dump(mode="json"),
                    "provider_id": provider_id,
                }
            row = await create_llm_provider(
                session,
                provider_id=provider_id,
                display_name=payload.display_name,
                location=payload.location,
                backend=payload.backend,
                owner_email=owner,
                config=payload.config,
                status=payload.status,
            )
            if self.usage_price_publisher is not None:
                await self.usage_price_publisher.notify_configuration(session)
            await session.commit()
            await session.refresh(row)
        if self.usage_price_publisher is not None:
            self.usage_price_publisher.wake()
        return row

    async def update(
        self,
        actor: ManagementActor,
        provider_id: str,
        payload: LLMProviderUpdateRequest,
        *,
        expected_updated_at: str | None = None,
        preview: bool = False,
    ) -> Any:
        """Update an authorized provider under the local-model dispatch write guard."""
        async with self.session_factory() as session:
            await lock_local_model_dispatch_guard(session)
            existing = await get_visible_llm_provider(session, provider_id, actor.email)
            if existing is None:
                raise api_exception(404, "not_found", "LLM provider not found")
            owner = existing.owner_email or SYSTEM_USER_EMAIL
            actor.require_owner(owner)
            check_updated_at(existing, expected_updated_at)
            scope = str(payload.owner_scope or "").strip().lower()
            next_owner = owner
            if scope in {"system", "shared"}:
                actor.require_admin()
                next_owner = SYSTEM_USER_EMAIL
            elif scope == "user":
                next_owner = actor.email
            elif scope:
                raise api_exception(
                    422, "validation_error", f"Unsupported provider owner_scope {scope!r}"
                )
            config = dict(payload.config if payload.config is not None else existing.config or {})
            if payload.owner_scope is not None:
                config["scope"] = "system" if next_owner == SYSTEM_USER_EMAIL else "user"
            location = payload.location if payload.location is not None else existing.location
            validate_provider(location, config)
            old = existing.config or {}
            if location != existing.location or any(
                config.get(k) != old.get(k) for k in ("executor_id", "executor_labels")
            ):
                await validate_executor_eligibility(
                    session, location=location, config=config, owner_email=next_owner
                )
            await guard_local_update(
                session,
                existing,
                next_owner_email=next_owner,
                next_location=location,
                next_config=config,
                next_status=payload.status if payload.status is not None else existing.status,
            )
            if preview:
                return {
                    "preview": True,
                    "provider_id": provider_id,
                    "changes": payload.model_dump(exclude_unset=True),
                    "config": config,
                }
            await update_llm_provider(
                session,
                provider_id,
                display_name=payload.display_name,
                location=payload.location,
                backend=payload.backend,
                owner_email=next_owner,
                config=config,
                status=payload.status,
            )
            row = await get_llm_provider(session, provider_id)
            if self.usage_price_publisher is not None:
                await self.usage_price_publisher.notify_configuration(session)
            await session.commit()
        if self.usage_price_publisher is not None:
            self.usage_price_publisher.wake()
        invalidate = getattr(self.llm, "invalidate_runtime_capability_cache_for_provider", None)
        if not callable(invalidate):
            invalidate = getattr(self.llm, "invalidate_json_mode_cache_for_provider", None)
        if callable(invalidate):
            invalidate(provider_id)
        return row

    async def delete(
        self,
        actor: ManagementActor,
        provider_id: str,
        *,
        expected_updated_at: str | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Delete only an unreferenced provider."""
        async with self.session_factory() as session:
            await lock_local_model_dispatch_guard(session)
            row = await get_visible_llm_provider(session, provider_id, actor.email)
            if row is None:
                raise api_exception(404, "not_found", "LLM provider not found")
            actor.require_owner(row.owner_email)
            check_updated_at(row, expected_updated_at)
            ids = await deployment_ids(session, provider_id)
            if ids:
                raise api_exception(
                    409,
                    "local_model_dependencies",
                    "cannot delete a provider referenced by local-model deployments",
                    details={"deployment_ids": ids},
                )
            await assert_provider_unreferenced(session, provider_id)
            if preview:
                return {"preview": True, "provider_id": provider_id, "deleted": False}
            ok = await delete_llm_provider(session, provider_id)
            if self.usage_price_publisher is not None:
                await self.usage_price_publisher.notify_configuration(session)
            await session.commit()
        if self.usage_price_publisher is not None:
            self.usage_price_publisher.wake()
        return {"ok": ok}

    async def set_default(
        self,
        actor: ManagementActor,
        provider_id: str,
        *,
        expected_updated_at: str | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Atomically replace the default in the target owner's scope."""
        async with self.session_factory() as session:
            await lock_local_model_dispatch_guard(session)
            row = await get_visible_llm_provider(session, provider_id, actor.email)
            if row is None:
                raise api_exception(404, "not_found", "LLM provider not found")
            actor.require_owner(row.owner_email)
            check_updated_at(row, expected_updated_at)
            if preview:
                return {"preview": True, "provider_id": provider_id, "is_default": True}
            await session.execute(
                update(LLMProvider)
                .where(LLMProvider.owner_email == row.owner_email)
                .values(is_default=False)
            )
            row.is_default = True
            await session.commit()
        return {"ok": True, "provider_id": provider_id}
