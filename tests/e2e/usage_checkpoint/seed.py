"""Coherent canonical-writer/projector seeds in the E2E fixture's own database."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.api.models import LoginRequest
from cognis.core.usage.maintenance import UsageMaintenanceService
from cognis.core.usage.projector import UsageProjector
from cognis.core.usage.service import UsageAccountingService
from cognis.models.usage import (
    UsageAttemptFinalized,
    UsagePriceDefinition,
    UsagePriceRate,
    UsageRequestStats,
    UsageSafeError,
)
from cognis.security import create_password_hasher
from cognis.store.database import create_engine
from cognis.store.models import ProjectGrantRow, User
from cognis.store.queries import (
    create_conversation,
    create_project,
    create_project_grant,
    create_session,
    create_user,
)
from cognis.store.usage.models import UsageAttemptRow, UsageValuationRow
from tests.usage_fixtures import finalized_attempt

PROJECT = "project_usage_browser_checkpoint"
MEMBER = "usage-browser-member@cognis-test.example.com"
PRICE = "price_usage_browser_priority"
PROVIDER = "provider_usage_browser_priced"
MODEL = "usage-browser-model"
SESSION = "session_usage_browser_admin"
PROXY = "usage-browser-proxy-admin"


def runtime() -> tuple[Path, dict[str, Any]]:
    """Reject every DB except the manifest's existing run-local fixture database."""
    assert os.environ.get("COGNIS_USAGE_CHECKPOINT") == "1"
    root = Path(os.environ["COGNIS_USAGE_RUN_DIR"]).resolve()
    facts = json.loads((root / "runtime.json").read_text())
    database = Path(facts["database"]).resolve()
    assert database.is_relative_to(root) and database.name == "cognis.db"
    assert database.is_file()
    return root, facts


async def validate_sign_in_identities(
    sessions: async_sessionmaker[AsyncSession], actor: str, password: str
) -> list[dict[str, Any]]:
    """Check every ordinary sign-in against the real request and persisted user."""
    assert actor != MEMBER
    identities = [
        (LoginRequest(email=email, password=password), role)
        for email, role in ((actor, "admin"), (MEMBER, "user"))
    ]
    async with sessions() as session:
        users = [await session.get(User, request.email) for request, _ in identities]
    hasher = create_password_hasher()
    proof = []
    for (request, role), user in zip(identities, users, strict=True):
        assert user is not None and user.is_active and user.role == role
        assert user.email == request.email and user.password_hash is not None
        assert request.mode == "browser"
        assert hasher.verify(user.password_hash, request.password)
        proof.append(
            {
                "email": request.email,
                "role": role,
                "active": True,
                "login_request_valid": True,
                "password_verified": True,
            }
        )
    return proof


async def seed() -> dict[str, Any]:
    """Publish declared fixture prices, emit canonical facts and verify fresh SQL."""
    root, facts = runtime()
    engine = create_engine(f"sqlite+aiosqlite:///{facts['database']}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor = facts["actor"]
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    started = day - timedelta(days=2) + timedelta(hours=12)
    projector = UsageProjector(sessions, controller_owner_id="usage_browser_seed")
    maintenance = UsageMaintenanceService(
        sessions, projector=projector, controller_owner_id="usage_browser_seed"
    )
    projector.attach_maintenance(maintenance)
    writer = UsageAccountingService(
        sessions,
        controller_owner_id="usage_browser_seed",
        projection_waker=projector.wake,
    )
    try:
        async with sessions.begin() as session:
            admin = await session.get(User, actor)
            assert admin is not None and admin.role == "admin"
            assert await session.get(User, MEMBER) is None, "Never seed an existing ledger twice"
            await create_user(
                session,
                MEMBER,
                "Usage browser member",
                create_password_hasher().hash(facts["fixture_password"]),
            )
            await create_project(
                session,
                project_id=PROJECT,
                owner_email=MEMBER,
                name="Usage browser checkpoint",
            )
            await create_project_grant(
                session,
                project_id=PROJECT,
                granted_by=MEMBER,
                grantee_user_email=actor,
                permission="viewer",
            )
            for key, owner, visibility, context in (
                ("admin", actor, "project", "web"),
                ("member", MEMBER, "project", "web"),
                ("private", MEMBER, "private", "web"),
                ("managed", MEMBER, "project", "agent_work"),
            ):
                await create_conversation(
                    session,
                    user_email=owner,
                    agent_id=facts["agent"],
                    context_type=context,
                    conversation_id=f"conv_usage_browser_{key}",
                    title=f"Usage checkpoint {key}",
                    project_id=PROJECT,
                    visibility=visibility,
                )
                await create_session(
                    session,
                    conversation_id=f"conv_usage_browser_{key}",
                    user_email=owner,
                    agent_id=facts["agent"],
                    session_id=f"session_usage_browser_{key}",
                )
        sign_in_preflight = await validate_sign_in_identities(
            sessions, actor, facts["fixture_password"]
        )
        price = UsagePriceDefinition(
            revision_id=PRICE,
            provider_id=PROVIDER,
            model=MODEL,
            effective_at=started - timedelta(days=1),
            recorded_at=day,
            currency="USD",
            basis="configured_estimate",
            provenance="configured",
            source_revision="usage_browser_explicit_priority_tariff",
            rates=(
                UsagePriceRate(
                    dimension="input", per_million=Decimal("2"), service_tier="priority"
                ),
                UsagePriceRate(
                    dimension="output", per_million=Decimal("4"), service_tier="priority"
                ),
            ),
        )
        await maintenance.publish_price(price)
        await writer.start()
        await projector.start()
        events: list[UsageAttemptFinalized] = []
        for index, (key, proxy, cost_kind) in enumerate(
            (
                ("admin", False, "priced"),
                ("admin", False, "priced"),
                ("admin", True, "priced"),
                ("admin", True, "priced"),
                ("admin", True, "retry"),
                ("admin", True, "retry"),
                ("member", False, "priced"),
                ("member", True, "priced"),
                ("private", False, "priced"),
                ("managed", False, "priced"),
                ("admin", False, "auto"),
                ("admin", False, "missing"),
            )
        ):
            owner = actor if key == "admin" else MEMBER
            number = index + 1
            data = finalized_attempt().model_dump()
            context = data["context"]
            request = (
                "request_usage_browser_retry"
                if cost_kind == "retry"
                else f"request_usage_browser_{number}"
            )
            context["trace"].update(
                attempt_id=f"attempt_usage_browser_{number}",
                request_id=request,
                cycle_id=f"cycle_usage_browser_{key}",
                attempt_ordinal=number - 5 if cost_kind == "retry" else 0,
            )
            context.update(
                started_at=started + timedelta(days=index % 3, seconds=number),
                controller_owner_id=writer.controller_owner_id,
                coverage_epoch_id=writer.coverage_epoch_id,
            )
            context["attribution"] = {
                "identity": {
                    "principal_subject": owner,
                    "principal_type": "user",
                    "usage_subject_kind": "user",
                    "usage_subject_id": owner,
                    "routing_owner": owner,
                    "provider_payer": owner,
                    "client_id": f"usage-browser-client-{key}" if proxy else None,
                },
                "purpose": "evaluation",
                "resources": {
                    "provenance": "controller",
                    "project_id": PROJECT,
                    "agent_id": facts["agent"],
                    "conversation_id": f"conv_usage_browser_{key}",
                    "session_id": None if proxy else f"session_usage_browser_{key}",
                },
                "client_labels": {"session_id": f"usage-browser-proxy-{key}"} if proxy else None,
            }
            context["selection"].update(
                provider_id="provider_usage_browser_missing"
                if cost_kind == "missing"
                else PROVIDER,
                effective_model=MODEL,
                streaming=False,
                service_tier=None if cost_kind == "auto" else "priority",
                requested_parameters={
                    "service_tier": "auto" if cost_kind == "auto" else "priority"
                },
                effective_parameters={
                    "service_tier": "auto" if cost_kind == "auto" else "priority"
                },
                captured_price_revision_id=None if cost_kind == "missing" else PRICE,
            )
            data.update(
                observation_id=f"observation_usage_browser_{number}",
                finalized_at=context["started_at"] + timedelta(milliseconds=120),
            )
            data["usage"].update(
                completeness="reported",
                cache_read_semantics="included_in_input",
                cache_write_semantics="included_in_input",
                reasoning_semantics="included_in_output",
            )
            for name, value in (
                ("input_tokens", 1000),
                ("output_tokens", 100),
                ("cache_read_tokens", 0),
                ("cache_write_tokens", 0),
                ("reasoning_tokens", 0),
            ):
                data["usage"][name] = {"value": value, "status": "reported", "source": "provider"}
            if number == 5:
                data["outcome"] = "error"
                data["error"] = UsageSafeError(
                    category="rate_limit", summary_code="provider_rate_limited"
                ).model_dump()
            event = UsageAttemptFinalized.model_validate(data)
            assert writer.try_emit(event)
            events.append(event)
            if not proxy:
                assert writer.try_emit(
                    UsageRequestStats(
                        observation_id=f"stats_usage_browser_{number}",
                        request_id=request,
                        attempt_id=event.context.trace.attempt_id,
                        cycle_id=event.context.trace.cycle_id,
                        request_started_at=event.context.started_at,
                        observed_at=event.finalized_at,
                        attribution=event.context.attribution,
                        controller_owner_id=writer.controller_owner_id,
                        coverage_epoch_id=writer.coverage_epoch_id,
                        stats_scope="server",
                        logical_outcome="success",
                        server={"elapsed_ms": 150},
                    )
                )
        drained = await writer.drain()
        assert drained.pending_items == 0 and not drained.writer_failed
        assert await projector.drain()
        async with sessions() as session:
            attempts = list(
                (
                    await session.scalars(
                        select(UsageAttemptRow).where(
                            UsageAttemptRow.controller_owner_id == writer.controller_owner_id
                        )
                    )
                ).all()
            )
            valuations = list(
                (
                    await session.scalars(
                        select(UsageValuationRow).where(
                            UsageValuationRow.source_kind == "attempt",
                            UsageValuationRow.source_id.in_([item.attempt_id for item in attempts]),
                        )
                    )
                ).all()
            )
            assert len(attempts) == len(valuations) == 12
            by_id = {row.source_id: row for row in valuations}
            for event in events[:10]:
                value = by_id[event.context.trace.attempt_id]
                assert value.historical_cost == Decimal("0.002400000000")
                assert value.historical_status == value.current_status == "complete"
                assert value.current_cost == value.historical_cost
                assert value.historical_revision_id == value.current_revision_id == PRICE
                assert (value.currency, value.basis) == ("USD", "configured_estimate")
                persisted = next(
                    row for row in attempts if row.attempt_id == event.context.trace.attempt_id
                )
                assert persisted.context_payload["selection"]["service_tier"] == "priority"
                assert persisted.context_payload["selection"]["captured_price_revision_id"] == PRICE
            for event in events[10:]:
                value = by_id[event.context.trace.attempt_id]
                assert value.historical_cost is None and value.historical_status == "unpriced"
            assert (
                next(row for row in attempts if row.attempt_id == "attempt_usage_browser_11")
                .context_payload["selection"]
                .get("service_tier")
                is None
            )
        manifest = {
            "project": PROJECT,
            "actor": actor,
            "member": MEMBER,
            "sign_in_preflight": sign_in_preflight,
            "session": SESSION,
            "proxy": PROXY,
            "provider": PROVIDER,
            "start": (day - timedelta(days=2)).date().isoformat(),
            "end": day.date().isoformat(),
            "attempt_count": len(attempts),
            "priced_count": 10,
            "unpriced_count": 2,
            "price_definition": price.model_dump(mode="json"),
            "priced_attempt_cost": "0.002400000000",
            "canonical_writer": True,
            "canonical_projector": True,
            "inference": False,
            "final_capture_qualification": False,
        }
        (root / "seed.json").write_text(json.dumps(manifest, indent=2))
        return manifest
    finally:
        await writer.aclose()
        await projector.aclose()
        await engine.dispose()


async def revoke() -> None:
    """Revoke only the seeded viewer grant; refs must not preserve authority."""
    root, facts = runtime()
    engine = create_engine(f"sqlite+aiosqlite:///{facts['database']}")
    try:
        async with async_sessionmaker(engine).begin() as session:
            grant = (
                await session.scalars(
                    select(ProjectGrantRow).where(
                        ProjectGrantRow.project_id == PROJECT,
                        ProjectGrantRow.grantee_user_email == facts["actor"],
                    )
                )
            ).one()
            assert grant.revoked_at is None
            grant.revoked_at = datetime.now(UTC)
        (root / "grant-revoked.json").write_text(json.dumps({"project": PROJECT, "revoked": True}))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "seed":
        asyncio.run(seed())
    elif action == "revoke":
        asyncio.run(revoke())
    else:
        raise ValueError("Unsupported checkpoint seed operation")
