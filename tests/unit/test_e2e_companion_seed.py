"""The E2E seed must configure the same global routes used by real companions."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.routing import COMPANION_ROUTES, route_chain
from scripts import seed_e2e


@pytest.mark.asyncio
async def test_e2e_seed_resolves_every_companion_role_globally(monkeypatch) -> None:
    upsert = AsyncMock()
    monkeypatch.setattr(seed_e2e, "upsert_model_routing", upsert)
    monkeypatch.setenv("COGNIS_LOCAL_CHAT_MODEL", "mock-model")
    await seed_e2e._seed_model_routing(AsyncMock(spec=AsyncSession))
    bindings = {call.kwargs["task_type"]: call.kwargs for call in upsert.await_args_list}

    for role in COMPANION_ROUTES:
        selected = next((bindings[key] for key in route_chain(role) if key in bindings), None)
        assert selected is not None, f"Missing global companion binding for {role}"
        assert selected["owner_email"] == SYSTEM_USER_EMAIL
        assert selected["provider_id"] == seed_e2e.E2E_PROVIDER_ID
        assert selected["model"] == (
            "mock-embedding" if role.endswith("_embedding") else "mock-model"
        )


@pytest.mark.asyncio
async def test_e2e_embedding_model_advertises_its_actual_modality(monkeypatch) -> None:
    create = AsyncMock()
    monkeypatch.setattr(seed_e2e, "get_llm_provider", AsyncMock(return_value=None))
    monkeypatch.setattr(seed_e2e, "create_llm_provider", create)
    monkeypatch.setenv("COGNIS_LOCAL_CHAT_MODEL", "mock-model")
    await seed_e2e._seed_provider(AsyncMock(spec=AsyncSession))
    models = create.await_args.kwargs["config"]["models"]
    embedding = next(model for model in models if model["model_id"] == "mock-embedding")
    assert embedding["model_mode"] == "embedding"
    assert embedding["supports_embedding"] is True
    assert embedding["supports_tools"] is False
