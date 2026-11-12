"""No-service SQLite witnesses for the additive browser fixture and expectations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.core.usage.query_models import (
    ProviderQuotaHistoryQuery,
    UsageQuery,
    UsageSummaryRequest,
)
from cognis.core.usage.query_service import create_usage_query_service
from cognis.core.usage.query_service_errors import UsageQueryError
from cognis.store.database import create_engine
from cognis.store.models import Base
from cognis.store.queries import create_agent, create_user
from tests.e2e.usage_checkpoint.expansion import (
    QUOTA_PROVIDER,
    assert_breakdowns,
    assert_quota,
    assert_summary,
    expansion_query,
    quota_request,
    seed_expansion,
    summary_requests,
)
from tests.e2e.usage_checkpoint.seed import revoke, seed


@pytest.mark.asyncio
async def test_expansion_preserves_original_seed_and_uses_real_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise actual writer/projector/query owners, not hand-built aggregate rows."""
    monkeypatch.setenv("COGNIS_USAGE_CHECKPOINT", "1")
    monkeypatch.setenv("COGNIS_USAGE_RUN_DIR", str(tmp_path))
    actor = "usage-expansion-admin@cognis-test.example.com"
    database = tmp_path / "cognis.db"
    engine = create_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as db:
            await create_user(db, actor, "Expansion admin", "", role="admin")
            await create_agent(db, agent_id="agent_expansion_test", owner_email=actor, name="Admin")
        # The original seed validates the ordinary login contract, including hash.
        from cognis.security import create_password_hasher
        from cognis.store.models import User

        password = "synthetic-expansion-acceptance-password"
        async with sessions.begin() as db:
            user = await db.get(User, actor)
            assert user is not None
            user.password_hash = create_password_hasher().hash(password)
        (tmp_path / "runtime.json").write_text(
            json.dumps(
                {
                    "actor": actor,
                    "agent": "agent_expansion_test",
                    "database": str(database),
                    "fixture_password": password,
                }
            )
        )
        original = await seed()
        original_bytes = (tmp_path / "seed.json").read_bytes()
        assert original["attempt_count"] == 12
        # Match the original browser's revoke before the explicit second phase.
        await revoke()
        manifest = await seed_expansion()
        assert (tmp_path / "seed.json").read_bytes() == original_bytes
        service = await create_usage_query_service(
            sessions, cursor_secret=b"usage-expansion-test-secret-32-bytes"
        )
        for combined in (False, True):
            response = await service.query(
                actor, UsageQuery.model_validate(expansion_query(manifest, combined=combined))
            )
            body = response.model_dump(mode="json")
            assert_breakdowns(body, manifest, combined=combined)
            # The oracle must reject a missing marginal or a double-counted cost.
            broken = json.loads(json.dumps(body))
            broken["cost_breakdowns"]["dimensions"].pop()
            with pytest.raises(AssertionError):
                assert_breakdowns(broken, manifest, combined=combined)
            broken = json.loads(json.dumps(body))
            groups = broken["cost_breakdowns"]["dimensions"][0]["groups"]
            groups[0]["key"], groups[1]["key"] = groups[1]["key"], groups[0]["key"]
            groups[0]["label"], groups[1]["label"] = groups[1]["label"], groups[0]["label"]
            with pytest.raises(AssertionError):
                assert_breakdowns(broken, manifest, combined=combined)
            broken = json.loads(json.dumps(body))
            broken["total"]["costs"][0]["known_amount"] = "100"
            with pytest.raises(AssertionError):
                assert_breakdowns(broken, manifest, combined=combined)
        for group in ("provider", "service"):
            request = expansion_query(manifest)
            request.update(view="ranking", group_by=group, metric="cost")
            with pytest.raises(UsageQueryError) as refusal:
                await service.query(actor, UsageQuery.model_validate(request))
            assert refusal.value.code == "mixed_cost_ranking" and refusal.value.status == 422
        for name, request in summary_requests(manifest["execution"]).items():
            response = await service.session_summaries(
                actor, UsageSummaryRequest.model_validate(request)
            )
            assert_summary(response.model_dump(mode="json"), manifest["summaries"][name])
        quota = await service.provider_history(
            actor, QUOTA_PROVIDER, ProviderQuotaHistoryQuery.model_validate(quota_request(manifest))
        )
        assert_quota(quota.model_dump(mode="json"), manifest)
        broken = quota.model_dump(mode="json")
        broken["samples"][0]["utilization"] = broken["samples"][2]["maximum_utilization"]
        with pytest.raises(AssertionError):
            assert_quota(broken, manifest)
        with pytest.raises(AssertionError, match="one-shot"):
            await seed_expansion()
    finally:
        await engine.dispose()
