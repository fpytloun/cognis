"""Second-phase ledger population; never run before the original five cases."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.api.chat_v2.schemas import TimelineScope
from cognis.api.chat_v2.work_graph import resolve_authorized_work_graph
from cognis.core.usage.capture_models import UsageProviderSample
from cognis.core.usage.maintenance import UsageMaintenanceService
from cognis.core.usage.projector import UsageProjector
from cognis.core.usage.query_models import ProviderQuotaHistoryQuery, UsageSummaryRequest
from cognis.core.usage.service import UsageAccountingService
from cognis.models.usage import UsageAttemptFinalized, UsagePriceDefinition, UsagePriceRate
from cognis.store.database import create_engine
from cognis.store.models import Agent, Conversation, LLMProvider, ManagedConversationLink, Session
from cognis.store.queries import (
    create_agent,
    create_conversation,
    create_project,
    create_session,
)
from cognis.store.usage.models import UsageAttemptRow, UsageRequestStatsRow, UsageValuationRow
from tests.e2e.usage_checkpoint.live_sessions import register_live_sessions
from tests.e2e.usage_checkpoint.seed import MEMBER, runtime
from tests.usage_fixtures import finalized_attempt

PROJECT = "project_usage_browser_expansion"
CONVERSATION = "conv_usage_expansion_own"
WORKER = "agent_usage_expansion_worker"
OWNER = "usage_browser_expansion_seed"
QUOTA_PROVIDER = "provider_usage_expansion_usd"


def session_id(key: str) -> str:
    """Stable synthetic physical-session identity, not an event-store envelope."""
    return f"session_usage_expansion_{key}"


def expansion_query(manifest: dict[str, Any], *, combined: bool = False) -> dict[str, Any]:
    """Use a real admin query, with an explicit separate expansion population."""
    return {
        "scope": {"mode": "admin"},
        "start": manifest["start"] + "T00:00:00Z",
        "end": manifest["end_exclusive"] + "T00:00:00Z",
        "view": "cost_breakdowns",
        "metric": "cost",
        "filters": {} if combined else {"project_ids": [PROJECT]},
    }


def summary_requests(execution_id: str) -> dict[str, dict[str, Any]]:
    """Keep each direct/inclusive pair on exactly the same selector."""
    return {
        f"{kind}_{inclusion}": UsageSummaryRequest(
            items=[{"kind": kind, "id": identifier}], inclusion=inclusion
        ).model_dump(mode="json")
        for kind, identifier in (
            ("conversation", CONVERSATION),
            ("execution", execution_id),
        )
        for inclusion in ("direct", "inclusive")
    }


def quota_request(manifest: dict[str, Any]) -> dict[str, Any]:
    """Read only stored samples through the provider owner's supported API."""
    return ProviderQuotaHistoryQuery(
        scope={"mode": "personal"},
        start=manifest["start"] + "T00:00:00Z",
        end=manifest["end_exclusive"] + "T00:00:00Z",
    ).model_dump(mode="json")


def assert_quota(body: dict[str, Any], manifest: dict[str, Any]) -> None:
    """Historical maximum fields are never evidence of latest utilization."""
    assert body["provider_id"] == QUOTA_PROVIDER and body["next_cursor"] is None
    last, middle, first = body["samples"]  # Supported producer orders newest first.
    assert all(sample["sample_count"] == 1 for sample in body["samples"])
    assert Decimal(first["maximum_utilization"]) == 90
    assert Decimal(first["limit"]) == 100 and Decimal(first["remaining"]) == 10
    assert Decimal(middle["maximum_utilization"]) == 20
    assert Decimal(middle["limit"]) == 100 and Decimal(middle["remaining"]) == 80
    assert last["sample_count"] == 1 and last["maximum_utilization"] is None
    assert last["limit"] is None and last["remaining"] is None
    assert all(sample["utilization"] is None for sample in body["samples"])
    assert datetime.fromisoformat(last["observed_at"]) == datetime.fromisoformat(
        manifest["quota_last_observed"]
    )


def assert_breakdowns(body: dict[str, Any], manifest: dict[str, Any], *, combined=False) -> None:
    """Check all marginals against one exact response total, never across bands."""
    # Serialized responses contain computed timing fields, which are output-only
    # under Pydantic extra=forbid. Inspect their actual wire shape, not input DTOs.
    metrics = body["total"]
    assert metrics is not None and body["cost_breakdowns"] is not None
    expected = manifest["combined"] if combined else manifest["filtered"]
    assert metrics["finalized_attempts"] == expected["attempts"]
    assert metrics["partially_priced_attempts"] == 1
    assert metrics["unpriced_attempts"] == expected["unpriced"]
    assert metrics["unallocatable_cost_attempts"] == expected["unallocatable"]
    total = {
        (cost["currency"], cost["basis"]): Decimal(cost["known_amount"])
        for cost in metrics["costs"]
        if cost["known_amount"] is not None
    }
    assert total == {
        (currency, "api_estimate"): Decimal(amount)
        for currency, amount in expected["amounts"].items()
    }
    breakdowns = body["cost_breakdowns"]
    assert breakdowns["measure"] == "known_priced_subtotal"
    assert breakdowns["complete_groups"] is True
    assert {item["dimension"] for item in breakdowns["dimensions"]} == {
        "subject",
        "agent",
        "provider",
        "model",
    }
    for marginal in breakdowns["dimensions"]:
        assert len(marginal["groups"]) >= 2
        groups = expected["marginals"][marginal["dimension"]]
        assert {group["key"] for group in marginal["groups"]} == {
            group["key"] for group in groups
        }, (
            marginal["dimension"],
            combined,
            [group["key"] for group in marginal["groups"]],
            [group["key"] for group in groups],
        )
        for group in marginal["groups"]:
            cohort = next(item for item in groups if item["key"] == group["key"])
            expected_label = (
                manifest["agent_labels"].get(cohort["key"])
                if marginal["dimension"] == "agent"
                else cohort["key"]
            )
            assert group["label"] == expected_label
            observed = group["metrics"]
            assert observed["finalized_attempts"] == cohort["attempts"]
            assert observed["partially_priced_attempts"] == cohort["partial"]
            assert observed["unpriced_attempts"] == cohort["unpriced"]
            assert {
                cost["currency"]: Decimal(cost["known_amount"])
                for cost in observed["costs"]
                if cost["known_amount"] is not None
            } == {currency: Decimal(amount) for currency, amount in cohort["amounts"].items()}
        assert (
            sum(group["metrics"]["finalized_attempts"] for group in marginal["groups"])
            == expected["attempts"]
        )
        for band, amount in total.items():
            parts = [
                Decimal(cost["known_amount"])
                for group in marginal["groups"]
                for cost in group["metrics"]["costs"]
                if (cost["currency"], cost["basis"]) == band and cost["known_amount"] is not None
            ]
            assert sum(parts, Decimal(0)) == amount, marginal["dimension"]


def assert_summary(body: dict[str, Any], expected: dict[str, Any]) -> None:
    """Independent fixture expectations, including missing-unit coverage."""
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["available"] and item["metrics"] is not None
    assert item["selector"] == expected["selector"]
    assert item["inclusion"] == expected["inclusion"]
    assert item["physical_sessions"] == expected["sessions"]
    metrics = item["metrics"]
    assert metrics["finalized_attempts"] == expected["attempts"]
    assert (
        metrics["observed_attempts"] is None
    )  # Production request excludes pending capture identities.
    assert metrics["states"]["succeeded"] == expected["attempts"]
    assert metrics["unpriced_attempts"] == expected["unpriced"]
    assert metrics["partially_priced_attempts"] == expected["partial"]
    assert metrics["tokens"]["input"] == expected["input"]
    assert metrics["tokens"]["output"] == expected["output"]
    assert metrics["token_known_attempts"]["output"] == expected["attempts"] - expected["partial"]
    assert {cost["currency"]: cost["known_amount"] for cost in metrics["costs"]} == expected[
        "amounts"
    ]


def persisted_marginals(
    attempts: list[UsageAttemptRow],
    values: dict[str, UsageValuationRow],
    requests: list[UsageRequestStatsRow],
) -> dict[str, list[dict[str, Any]]]:
    """Independent per-fact oracle, not the SQL summary/grouping implementation."""
    result = {}
    for dimension in ("subject", "agent", "provider", "model"):
        groups: dict[str | None, dict[str, Any]] = {}
        for attempt in attempts:
            key = {
                "subject": attempt.usage_subject_key,
                "agent": attempt.agent_id,
                "provider": attempt.provider_id,
                "model": attempt.effective_model,
            }[dimension]
            group = groups.setdefault(
                key, {"key": key, "attempts": 0, "partial": 0, "unpriced": 0, "amounts": {}}
            )
            value = values[attempt.attempt_id]
            group["attempts"] += 1
            group["partial"] += value.historical_status == "partial"
            group["unpriced"] += value.historical_status != "complete"
            if value.historical_cost is not None:
                assert value.basis == "configured_estimate" and value.currency is not None
                group["amounts"][value.currency] = (
                    group["amounts"].get(value.currency, Decimal(0)) + value.historical_cost
                )
        # Logical request timers can add a null provider/model cohort, never
        # inferred physical-attempt attribution or extra money/attempts.
        for request in requests:
            key = {
                "subject": request.usage_subject_key,
                "agent": (request.attribution.get("resources") or {}).get("agent_id"),
                "provider": None,
                "model": None,
            }[dimension]
            groups.setdefault(
                key, {"key": key, "attempts": 0, "partial": 0, "unpriced": 0, "amounts": {}}
            )
        for group in groups.values():
            group["amounts"] = {
                currency: f"{amount:.12f}" for currency, amount in group["amounts"].items()
            }
        result[dimension] = list(groups.values())
    return result


async def seed_expansion() -> dict[str, Any]:
    """Append ten canonical facts after the baseline, then verify fresh persistence."""
    root, facts = runtime()
    original = json.loads((root / "seed.json").read_text())
    assert original["attempt_count"] == 12 and original["priced_count"] == 10
    engine = create_engine(f"sqlite+aiosqlite:///{facts['database']}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    day = datetime.fromisoformat(original["end"]).replace(tzinfo=UTC)
    started = day - timedelta(days=2) + timedelta(hours=13)
    projector = UsageProjector(sessions, controller_owner_id=OWNER)
    maintenance = UsageMaintenanceService(sessions, projector=projector, controller_owner_id=OWNER)
    projector.attach_maintenance(maintenance)
    writer = UsageAccountingService(
        sessions, controller_owner_id=OWNER, projection_waker=projector.wake
    )
    # Previous-session links alone do not define a continuation lane. Renewed
    # current root gets its own activity identity; rotations explicitly retain it.
    layout = (
        ("prior", None, None, None),
        ("prior_rotated", None, "prior", "prior"),
        ("current", None, "prior_rotated", None),
        ("child", "current", None, "current"),
        ("child_rotated", "current", "child", "current"),
        ("grandchild", "child", None, "current"),
        ("sibling", "current", None, "current"),
        ("managed", None, None, None),
        ("member", None, None, None),
    )
    try:
        async with sessions.begin() as db:
            assert await db.get(Conversation, CONVERSATION) is None, "Expansion is one-shot"
            original_ids = set(await db.scalars(select(UsageAttemptRow.attempt_id)))
            assert len(original_ids) == 12
            original_valuations = {
                (row.source_kind, row.source_id): (
                    row.historical_status,
                    row.current_status,
                    row.historical_cost,
                    row.current_cost,
                    row.currency,
                    row.basis,
                )
                for row in await db.scalars(select(UsageValuationRow))
            }
            assert {key[1] for key in original_valuations if key[0] == "attempt"} == original_ids
            await create_project(
                db,
                project_id=PROJECT,
                owner_email=facts["actor"],
                name="Usage expansion acceptance",
            )
            await create_agent(db, agent_id=WORKER, owner_email=facts["actor"], name="Usage worker")
            db.add(
                LLMProvider(
                    provider_id=QUOTA_PROVIDER,
                    display_name="Usage expansion stored quota",
                    owner_email=facts["actor"],
                    location="cloud",
                    backend="litellm",
                    config={},
                    is_default=False,
                    status="disabled",
                )
            )
            for key, context, agent, owner in (
                ("own", "web", facts["agent"], facts["actor"]),
                ("managed", "agent_work", WORKER, facts["actor"]),
                ("member", "web", WORKER, MEMBER),
            ):
                await create_conversation(
                    db,
                    conversation_id=f"conv_usage_expansion_{key}",
                    context_type=context,
                    user_email=owner,
                    agent_id=agent,
                    project_id=PROJECT,
                    title=f"Usage expansion {key}",
                    visibility="project",
                )
            for key, parent, previous, activity in layout:
                conversation = key if key in {"managed", "member"} else "own"
                row = await create_session(
                    db,
                    session_id=session_id(key),
                    conversation_id=f"conv_usage_expansion_{conversation}",
                    user_email=MEMBER if key == "member" else facts["actor"],
                    agent_id=WORKER
                    if key
                    in {"child", "child_rotated", "grandchild", "sibling", "managed", "member"}
                    else facts["agent"],
                    parent_session_id=session_id(parent) if parent else None,
                    previous_session_id=session_id(previous) if previous else None,
                    activity_scope_id=session_id(activity) if activity else None,
                    delegation_mode="sync" if parent else None,
                    delegation_task=f"Usage expansion {'child' if key == 'child_rotated' else key}"
                    if parent
                    else None,
                )
                if key != "current":
                    row.status = "completed"
                    row.completed_at = day
            conversation = await db.get(Conversation, CONVERSATION)
            assert conversation is not None
            conversation.active_session_id = session_id("current")
            db.add(
                ManagedConversationLink(
                    link_id="link_usage_expansion_managed",
                    user_email=facts["actor"],
                    controller_agent_id=facts["agent"],
                    controller_conversation_id=CONVERSATION,
                    controller_session_id=session_id("current"),
                    target_agent_id=WORKER,
                    target_conversation_id="conv_usage_expansion_managed",
                    root_link_id="link_usage_expansion_managed",
                    depth=1,
                    kind="agent",
                    conversation_state="closed",
                    turn_state="completed",
                )
            )
        definitions = {}
        for band in ("USD", "EUR", "USD_worker"):
            currency = band.split("_")[0]
            definitions[band] = UsagePriceDefinition(
                revision_id=f"price_usage_expansion_{band.lower()}",
                provider_id=f"provider_usage_expansion_{band.lower()}",
                model=f"usage-expansion-{band.lower()}",
                effective_at=started - timedelta(days=1),
                recorded_at=day,
                currency=currency,
                basis="configured_estimate",
                provenance="configured",
                source_revision="usage_expansion_measured_partial",
                rates=(
                    UsagePriceRate(
                        dimension="input", per_million=Decimal("2"), service_tier="priority"
                    ),
                    UsagePriceRate(
                        dimension="output", per_million=Decimal("4"), service_tier="priority"
                    ),
                ),
            )
            await maintenance.publish_price(definitions[band])
        await writer.start()
        await projector.start()
        quota_at = started.replace(minute=0, second=0)
        for index, (offset, utilization, limit_value, remaining) in enumerate(
            (
                (1, Decimal("90"), Decimal("100"), Decimal("10")),
                (2, Decimal("20"), Decimal("100"), Decimal("80")),
                (3600, None, None, None),
            )
        ):
            assert writer.try_emit_provider_sample(
                UsageProviderSample(
                    sample_id=f"sample_usage_expansion_{index}",
                    provider_id=QUOTA_PROVIDER,
                    provider_owner=facts["actor"],
                    account_scope_id=QUOTA_PROVIDER,
                    configuration_generation="generation_usage_expansion_quota",
                    source="refresh",
                    observation_id=f"observation_usage_expansion_{index}",
                    window_key="primary",
                    observed_at=quota_at + timedelta(seconds=offset),
                    reset_at=quota_at + timedelta(days=1),
                    utilization=utilization,
                    limit_value=limit_value,
                    remaining=remaining,
                )
            )
        ledger = []
        for number, key in enumerate([row[0] for row in layout] + ["current"], start=1):
            currency = "EUR" if key == "managed" else "USD"
            price = definitions["USD_worker" if key == "member" else currency]
            missing = number == 10
            partial = number == 2
            data = finalized_attempt().model_dump()
            context = data["context"]
            attempt = f"attempt_usage_expansion_{number}"
            context["trace"].update(
                attempt_id=attempt,
                request_id=f"request_usage_expansion_{number}",
                cycle_id=f"cycle_usage_expansion_{number}",
            )
            context.update(
                started_at=started + timedelta(days=number % 3, seconds=number),
                controller_owner_id=OWNER,
                coverage_epoch_id=writer.coverage_epoch_id,
            )
            owner = MEMBER if key == "member" else facts["actor"]
            context["attribution"] = {
                "identity": {
                    "principal_subject": owner,
                    "principal_type": "user",
                    "usage_subject_kind": "user",
                    "usage_subject_id": owner,
                    "routing_owner": owner,
                    "provider_payer": owner,
                },
                "purpose": "evaluation",
                "resources": {
                    "provenance": "controller",
                    "project_id": PROJECT,
                    "agent_id": WORKER
                    if key
                    in {"child", "child_rotated", "grandchild", "sibling", "managed", "member"}
                    else facts["agent"],
                    "conversation_id": f"conv_usage_expansion_{key if key in {'managed', 'member'} else 'own'}",
                    "session_id": session_id(key),
                },
            }
            context["selection"].update(
                provider_id="provider_usage_expansion_missing" if missing else price.provider_id,
                effective_model=price.model,
                streaming=False,
                service_tier="priority",
                requested_parameters={"service_tier": "priority"},
                effective_parameters={"service_tier": "priority"},
                captured_price_revision_id=None if missing else price.revision_id,
            )
            data.update(
                observation_id=f"observation_usage_expansion_{number}",
                finalized_at=context["started_at"] + timedelta(milliseconds=120),
            )
            data["usage"].update(
                completeness="partial" if partial else "reported",
                cache_read_semantics="included_in_input",
                cache_write_semantics="included_in_input",
                reasoning_semantics="included_in_output",
            )
            for field, value in (
                ("input_tokens", number * 1000),
                ("output_tokens", number * 100),
                ("cache_read_tokens", 0),
                ("cache_write_tokens", 0),
                ("reasoning_tokens", 0),
            ):
                data["usage"][field] = (
                    {"value": None, "status": "unknown", "source": "provider"}
                    if partial and field == "output_tokens"
                    else {"value": value, "status": "reported", "source": "provider"}
                )
            event = UsageAttemptFinalized.model_validate(data)
            assert writer.try_emit(event)
            ledger.append(
                {
                    "attempt": attempt,
                    "session": session_id(key),
                    "currency": currency,
                    "amount": None
                    if missing
                    else f"{Decimal(number) * (Decimal('0.002') if partial else Decimal('0.0024')):.12f}",
                    "status": "unpriced" if missing else "partial" if partial else "complete",
                }
            )
        drained = await writer.drain()
        assert not drained.writer_failed and drained.pending_items == 0
        assert await projector.drain()
        async with sessions() as db:
            attempts = list(await db.scalars(select(UsageAttemptRow)))
            assert len(attempts) == 22
            assert {row.attempt_id for row in attempts} == original_ids | {
                row["attempt"] for row in ledger
            }
            all_values = list(await db.scalars(select(UsageValuationRow)))
            requests = list(await db.scalars(select(UsageRequestStatsRow)))
            agent_labels = dict(
                (
                    await db.execute(
                        select(Agent.agent_id, Agent.name).where(
                            Agent.owner_email == facts["actor"]
                        )
                    )
                ).all()
            )
            values = {row.source_id: row for row in all_values if row.source_kind == "attempt"}
            assert {
                (row.source_kind, row.source_id): (
                    row.historical_status,
                    row.current_status,
                    row.historical_cost,
                    row.current_cost,
                    row.currency,
                    row.basis,
                )
                for row in all_values
                if (row.source_kind, row.source_id) in original_valuations
            } == original_valuations
            filtered_marginals = persisted_marginals(
                [row for row in attempts if row.project_id == PROJECT],
                values,
                [
                    row
                    for row in requests
                    if (row.attribution.get("resources") or {}).get("project_id") == PROJECT
                ],
            )
            combined_marginals = persisted_marginals(attempts, values, requests)
            for item in ledger:
                value = values[item["attempt"]]
                assert value.historical_status == value.current_status == item["status"]
                assert (
                    value.historical_cost
                    == value.current_cost
                    == (Decimal(item["amount"]) if item["amount"] is not None else None)
                )
            physical = {
                row.session_id: row
                for row in await db.scalars(
                    select(Session).where(Session.conversation_id == CONVERSATION)
                )
            }
            for key, parent, previous, activity in layout[:7]:
                row = physical[session_id(key)]
                assert row.parent_session_id == (session_id(parent) if parent else None)
                assert row.previous_session_id == (session_id(previous) if previous else None)
                assert row.activity_scope_id == session_id(activity or key)
                assert row.delegation_mode == ("sync" if parent else None)
            link = await db.get(ManagedConversationLink, "link_usage_expansion_managed")
            assert link is not None and link.controller_session_id == session_id("current")
            own = await db.get(Conversation, CONVERSATION)
            assert own is not None and own.active_session_id == session_id("current")
            graph = await resolve_authorized_work_graph(
                db,
                user_email=facts["actor"],
                scope=TimelineScope(
                    kind="conversation",
                    key=f"conversation:{CONVERSATION}",
                    conversation_id=CONVERSATION,
                ),
            )
            focused_node = next(
                node
                for node in graph.nodes
                if set(node.backing_session_ids)
                == {
                    session_id("child"),
                    session_id("child_rotated"),
                }
            )
            assert focused_node.session_id == session_id("child_rotated")
            parent = next(node for node in graph.nodes if node.key == focused_node.parent_key)
        expected_summaries = {}
        for name, request in summary_requests(focused_node.session_id).items():
            execution = name.startswith("execution")
            inclusive = name.endswith("inclusive")
            expected_summaries[name] = {
                "selector": request["items"][0],
                "inclusion": request["inclusion"],
                "sessions": (3 if inclusive else 2) if execution else (8 if inclusive else 3),
                "attempts": (3 if inclusive else 2) if execution else (9 if inclusive else 4),
                "partial": 0 if execution else 1,
                "unpriced": 0 if execution else 2,
                "input": (15000 if inclusive else 9000)
                if execution
                else (46000 if inclusive else 16000),
                "output": (1500 if inclusive else 900)
                if execution
                else (4400 if inclusive else 1400),
                "amounts": ({"USD": "0.036000000000"} if inclusive else {"USD": "0.021600000000"})
                if execution
                else (
                    {"USD": "0.066400000000", "EUR": "0.019200000000"}
                    if inclusive
                    else {"USD": "0.013600000000"}
                ),
            }
        manifest = {
            "project": PROJECT,
            "conversation": CONVERSATION,
            "execution": focused_node.session_id,
            "execution_title": focused_node.title,
            "execution_parent_key": parent.key,
            "quota_last_observed": (quota_at + timedelta(hours=1)).isoformat(),
            "ledger": ledger,
            "agent_labels": agent_labels,
            "start": original["start"],
            "end": original["end"],
            "end_exclusive": (day + timedelta(days=1)).date().isoformat(),
            "filtered": {
                "marginals": filtered_marginals,
                "attempts": 10,
                "unpriced": 2,
                "unallocatable": 1,
                "amounts": {"USD": "0.088000000000", "EUR": "0.019200000000"},
            },
            "combined": {
                "marginals": combined_marginals,
                "attempts": 22,
                "unpriced": 4,
                "unallocatable": 2,
                "amounts": {"USD": "0.112000000000", "EUR": "0.019200000000"},
            },
            "summaries": expected_summaries,
            "canonical_writer": True,
            "canonical_projector": True,
            "original_population_unchanged": True,
            "real_app_acceptance": False,
            "runtime_qualified": False,
        }
        if "url" in facts:
            # Chat reads retained streams, delegated work, and the linked managed
            # branch. The unrelated member conversation is an authorization probe.
            await register_live_sessions(
                Path(facts["database"]),
                facts,
                tuple(session_id(key) for key, *_ in layout if key != "member"),
            )
        (root / "expansion-seed.json").write_text(json.dumps(manifest, indent=2))
        return manifest
    finally:
        await writer.aclose()
        await projector.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed_expansion())
