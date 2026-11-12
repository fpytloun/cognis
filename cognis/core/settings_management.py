"""Registry-backed settings operations shared by HTTP and controller tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.common import api_exception
from cognis.api.serializers import setting_to_response
from cognis.api.web_reconfigure import (
    run_web_mutation_cancellation_safe,
    web_settings_distributed_lock,
)
from cognis.core.management_access import updated_at_matches
from cognis.core.web_management import validate_web_setting
from cognis.logging import get_logger
from cognis.settings_schema import get_setting_spec, validate_setting_value
from cognis.store.local_models import lock_local_model_dispatch_guard
from cognis.store.models import Setting
from cognis.store.queries import get_setting, get_setting_value, upsert_setting

logger = get_logger(__name__)


@dataclass(frozen=True)
class SettingsManagement:
    """Explicit persistence and runtime-application dependencies."""

    session_factory: Any
    update_lock: Any
    apply: Callable[[str, object], Awaitable[None]]
    secrets: Any = None
    finalize_web: Callable[[], Awaitable[Any]] | None = None
    notify_usage_settings: Callable[[AsyncSession], Awaitable[None]] | None = None
    usage_settings_committed: Callable[[], None] | None = None

    async def put(
        self,
        *,
        key: str,
        value: object,
        updated_by: str,
        expected_updated_at: str | None = None,
        preview: bool = False,
    ) -> Any:
        if not key.startswith("web."):
            return await self._put(
                key=key,
                value=value,
                updated_by=updated_by,
                expected_updated_at=expected_updated_at,
                preview=preview,
            )

        async def mutate() -> Any:
            async with self.update_lock, web_settings_distributed_lock(self.session_factory):
                return await self._put(
                    key=key,
                    value=value,
                    updated_by=updated_by,
                    expected_updated_at=expected_updated_at,
                    preview=preview,
                    acquire_lock=False,
                )

        return await run_web_mutation_cancellation_safe(mutate, reason="web_setting_update")

    async def _put(
        self,
        *,
        key: str,
        value: object,
        updated_by: str,
        expected_updated_at: str | None,
        preview: bool,
        acquire_lock: bool = True,
    ) -> Any:
        """Persist and hot-apply a validated value, restoring failed local application."""
        spec = get_setting_spec(key, require_exposed=True)
        validate_setting_value(key, value)
        async with self.update_lock if acquire_lock else nullcontext():
            async with self.session_factory() as session:
                await lock_local_model_dispatch_guard(session)
                previous = await get_setting(session, key)
                if expected_updated_at is not None and (
                    previous is None
                    or not updated_at_matches(previous.updated_at, expected_updated_at)
                ):
                    raise api_exception(409, "settings_conflict", "Setting changed; read it again")
                previous_value = previous.value if previous is not None else spec.default
                previous_updated_by = previous.updated_by if previous is not None else None
                if key.startswith("web."):
                    await validate_web_setting(session, self.secrets, key, value)
                if preview:
                    return {
                        "preview": True,
                        "key": key,
                        "before": previous_value,
                        "after": value,
                        "application_scope": spec.application_scope,
                    }
                row = await upsert_setting(
                    session,
                    key=key,
                    value=value,
                    category=spec.category,
                    updated_by=updated_by,
                )
                disabled = (
                    key.removeprefix("web.").removesuffix("_enabled")
                    if (key.endswith("_enabled") and value is False)
                    else "searxng"
                    if key == "web.searxng_url" and not value
                    else None
                )
                if disabled in {"tavily", "brave", "searxng"}:
                    for default_key in ("web.backend", "web.search_backend", "web.fetch_backend"):
                        if await get_setting_value(session, default_key, "direct") == disabled:
                            await upsert_setting(
                                session,
                                key=default_key,
                                value="direct",
                                category="web",
                                updated_by=updated_by,
                            )
                if key.startswith("usage.") and self.notify_usage_settings is not None:
                    await self.notify_usage_settings(session)
                await session.commit()
                if key.startswith("usage.") and self.usage_settings_committed is not None:
                    self.usage_settings_committed()
                if key.startswith("web.") and self.finalize_web is not None:
                    await self.finalize_web()
                await session.refresh(row)
                committed_updated_at = row.updated_at

            if spec.application_scope == "hot":
                try:
                    await self.apply(key, value)
                except Exception as exc:
                    async with self.session_factory() as session:
                        result = await session.execute(
                            update(Setting)
                            .where(
                                Setting.key == key,
                                Setting.updated_at == committed_updated_at,
                            )
                            .values(
                                value=previous_value,
                                category=spec.category,
                                updated_by=previous_updated_by,
                            )
                        )
                        if result.rowcount == 1:
                            await session.commit()
                            recovery_value = previous_value
                        else:
                            await session.rollback()
                            latest = await get_setting(session, key)
                            recovery_value = latest.value if latest is not None else spec.default
                    try:
                        await self.apply(key, recovery_value)
                    except Exception:
                        logger.exception(
                            "settings: failed to restore runtime value",
                            extra={"extra_data": {"key": key}},
                        )
                    raise api_exception(
                        500,
                        "runtime_apply_failed",
                        "Setting could not be applied locally; recovery was attempted. Read current state before retrying.",
                    ) from exc
                async with self.session_factory() as session:
                    latest = await get_setting(session, key)
                if latest is not None and latest.value != value:
                    try:
                        await self.apply(key, latest.value)
                    except Exception as exc:
                        raise api_exception(
                            500,
                            "runtime_apply_failed",
                            "A newer durable value could not be applied locally.",
                        ) from exc
                    row = latest
            return setting_to_response(row, spec)
