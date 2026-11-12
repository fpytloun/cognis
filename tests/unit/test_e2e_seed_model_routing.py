"""Local-stack fixtures must configure the actual companion inference boundary."""

from unittest.mock import AsyncMock

import pytest

from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.routing import COMPANION_DEFAULTS, EMBEDDING_ROUTES
from scripts import seed_e2e


async def test_seed_routes_configure_global_companion_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COGNIS_LOCAL_CHAT_MODEL", "mock-model")
    upsert = AsyncMock()
    monkeypatch.setattr(seed_e2e, "upsert_model_routing", upsert)
    await seed_e2e._seed_model_routing(AsyncMock())

    routes = {call.kwargs["task_type"]: call.kwargs for call in upsert.await_args_list}
    assert set(COMPANION_DEFAULTS) <= routes.keys()
    assert set(routes) == {*seed_e2e.TEXT_ROUTE_TYPES, *seed_e2e.EMBEDDING_ROUTE_TYPES}
    assert len(upsert.await_args_list) == len(routes)
    for role, config in routes.items():
        assert config["provider_id"] == seed_e2e.E2E_PROVIDER_ID
        assert config["owner_email"] == SYSTEM_USER_EMAIL
        assert config["model"] == ("mock-embedding" if role in EMBEDDING_ROUTES else "mock-model")
        assert config["config"] is None


async def test_seed_provider_declares_embedding_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COGNIS_HA_E2E_SEED", raising=False)
    monkeypatch.setattr(seed_e2e, "get_llm_provider", AsyncMock(return_value=None))
    create = AsyncMock()
    monkeypatch.setattr(seed_e2e, "create_llm_provider", create)
    await seed_e2e._seed_provider(AsyncMock())

    assert create.await_count == 1
    config = create.await_args.kwargs
    assert config["owner_email"] == SYSTEM_USER_EMAIL
    assert config["location"] == "controller"
    embedding = next(
        model for model in config["config"]["models"] if model["model_id"] == "mock-embedding"
    )
    assert embedding["supports_embedding"] is True
    assert embedding["supports_tools"] is False
