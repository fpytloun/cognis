from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cognis.api.routes import conversations


@pytest.mark.asyncio
async def test_nested_managed_target_resolves_validated_outermost_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaf = SimpleNamespace()
    middle = SimpleNamespace(controller_conversation_id="middle")
    root = SimpleNamespace(controller_conversation_id="controller-root")
    monkeypatch.setattr(
        conversations,
        "get_managed_conversation_ancestry",
        AsyncMock(return_value=[leaf, middle, root]),
    )
    monkeypatch.setattr(
        conversations,
        "get_conversation",
        AsyncMock(
            return_value=SimpleNamespace(
                conversation_id="controller-root",
                user_email="owner@example.com",
                status="active",
            )
        ),
    )

    result = await conversations._validated_root_controller_conversation_id(
        object(),
        leaf,
        user_email="owner@example.com",
    )

    assert result == "controller-root"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "cross_user", "deleted", "missing"])
async def test_managed_root_navigation_degrades_without_id_leak(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    link = SimpleNamespace()
    root = SimpleNamespace(controller_conversation_id="private-controller")
    ancestry = AsyncMock(
        side_effect=ValueError("invalid") if failure == "malformed" else None,
        return_value=[link, root],
    )
    monkeypatch.setattr(conversations, "get_managed_conversation_ancestry", ancestry)
    row = {
        "cross_user": SimpleNamespace(user_email="other@example.com", status="active"),
        "deleted": SimpleNamespace(user_email="owner@example.com", status="deleted"),
        "missing": None,
        "malformed": None,
    }[failure]
    monkeypatch.setattr(conversations, "get_conversation", AsyncMock(return_value=row))

    result = await conversations._validated_root_controller_conversation_id(
        object(),
        link,
        user_email="owner@example.com",
    )

    assert result is None
