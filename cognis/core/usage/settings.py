"""Typed settings are the authority for every destructive maintenance page."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.core.usage.maintenance import UsageRetentionPolicy
from cognis.settings_schema import get_setting_spec, validate_setting_value
from cognis.store.models import Setting
from cognis.store.queries import get_setting_value

RETENTION_KEYS = (
    "usage.retention_enabled",
    "usage.detail_retention_days",
    "usage.quota_history_retention_days",
    "usage.bookkeeping_retention_days",
)


async def load_usage_enabled(session: AsyncSession) -> bool:
    """Read the startup-only master switch without adding work to inference."""
    spec = get_setting_spec("usage.enabled")
    assert spec is not None
    value = await get_setting_value(session, spec.key, spec.default)
    validate_setting_value(spec.key, value)
    return bool(value)


async def load_usage_retention_policy(session: AsyncSession) -> UsageRetentionPolicy:
    """Share-lock persisted policy through page commit, including cross-controller disable."""
    rows = (
        await session.scalars(
            select(Setting)
            .where(Setting.key.in_(RETENTION_KEYS))
            .order_by(Setting.key)
            .with_for_update(read=True)
        )
    ).all()
    stored = {row.key: row.value for row in rows}
    values = {key: stored.get(key, get_setting_spec(key).default) for key in RETENTION_KEYS}
    for key, value in values.items():
        validate_setting_value(key, value)
    if values["usage.retention_enabled"] and len(stored) != len(RETENTION_KEYS):
        raise ValueError("Enabled usage retention requires a complete persisted policy")
    return UsageRetentionPolicy(
        enabled=values["usage.retention_enabled"],
        detail_days=values["usage.detail_retention_days"],
        quota_days=values["usage.quota_history_retention_days"],
        bookkeeping_days=values["usage.bookkeeping_retention_days"],
    )
