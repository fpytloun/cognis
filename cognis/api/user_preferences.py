"""Shared normalization for stored account preferences."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_args

from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.models import UserDisplayPreferences, UserPreferencesResponse
from cognis.store.queries import get_user_ui_state_value

USER_PREFERENCES_STATE_KEY = "ui.preferences"


def preferences_from_state(value: dict[str, Any] | None) -> UserPreferencesResponse:
    """Read preferences without changing unknown stored theme families."""
    if not value:
        return UserPreferencesResponse()
    display = value.get("display")
    if isinstance(display, dict):
        family = UserDisplayPreferences.model_fields["theme_family"]
        if display.get("theme_family", family.default) not in get_args(family.annotation):
            value = {**value, "display": {**display, "theme_family": family.default}}
    return UserPreferencesResponse.model_validate(value)


async def theme_family_for_user(factory: Callable[[], AsyncSession], email: str | None) -> str:
    """Read only the public theme-family fallback, never the owner's appearance."""
    value = None
    if email:
        async with factory() as session:
            value = await get_user_ui_state_value(session, email, USER_PREFERENCES_STATE_KEY)
    return preferences_from_state(value).display.theme_family
