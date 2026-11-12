"""Qualified untiered estimates through the real registry, SDK and durable ledger."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event as sql_event
from sqlalchemy import select

from cognis.core.usage import maintenance as maintenance_module
from cognis.core.usage.maintenance import UsageMaintenanceScope
from cognis.core.usage.price_publisher import UsagePricePinCache, UsagePricePublisher
from cognis.executor.inference import InferenceHandler
from cognis.executor.usage_observer import executor_observer
from cognis.models.usage import UsageAttemptFinalized
from cognis.models.usage_pricing import usage_pricing_fingerprint
from cognis.providers.auth.jwt import JWTAuthProvider
from cognis.providers.llm import usage_sdk
from cognis.providers.llm.usage_attribution import controller_request
from cognis.providers.llm.usage_binding import UsageBinding
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.llm.usage_executor import executor_metadata, ingest_executor
from cognis.providers.registry import build_provider_registry
from cognis.store.models import LLMProvider
from cognis.store.usage.codec import terminal_event
from cognis.store.usage.models import (
    UsageArchivedTotalRow,
    UsageAttemptRow,
    UsagePriceRevisionRow,
    UsageValuationRow,
)
from tests.unit.test_provider_registry import _test_config
from tests.usage_storage_harness import UsageStorageHarness

pytest_plugins = ("tests.usage_storage_harness",)


class RecordingSink:
    def __init__(self, writer: Any) -> None:
        self.writer = writer
        self.events: list[Any] = []

    def try_emit(self, event: Any) -> bool:
        self.events.append(event)
        return self.writer.try_emit(event)


@pytest_asyncio.fixture
async def untiered_peer() -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    """Only replace the paid HTTP peer, including either installed SDK transport."""
    wire: list[dict[str, Any]] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.partition(b":")[2])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            payload = json.loads(await reader.readexactly(length))
            wire.append(payload)
            if b"/embeddings " in headers.split(b"\r\n")[0]:
                data = {
                    "object": "list",
                    "model": payload["model"],
                    "data": [{"object": "embedding", "index": 0, "embedding": [0.25, 0.5]}],
                    "usage": {"prompt_tokens": 11, "total_tokens": 11},
                }
            else:
                data = {
                    "created": 1,
                    "data": [{"b64_json": "aW1hZ2U="}],
                    "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                }
            body = json.dumps(data).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1", wire


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "runtime", "warm_prices"),
    [
        ("embeddings.create", "controller", True),
        ("images.generate", "controller", True),
        ("images.generate", "executor", True),
        ("images.generate", "executor", False),
    ],
)
async def test_real_registry_sdk_untiered_price_durable_repricing_and_archive(
    usage_storage: UsageStorageHarness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    untiered_peer: tuple[str, list[dict[str, Any]]],
    resource: str,
    runtime: str,
    warm_prices: bool,
) -> None:
    h = usage_storage
    embedding = resource == "embeddings.create"
    model = "text-embedding-3-small" if embedding else "gpt-image-1"
    declaration = {
        "version": 1,
        "scope": {"version": 1, "resource": f"openai.{resource}", "abi_revision": 1},
        "billing_basis": "aggregate_input_tokens" if embedding else "aggregate_input_output_tokens",
    }
    configured_model = {
        "model_id": model,
        "model_mode": "embedding" if embedding else "image_generation",
        "input_cost_per_mtok": 2.0,
        "untiered_tariff": declaration,
    }
    if not embedding:
        configured_model["output_cost_per_mtok"] = 4.0
    provider_config = {
        "preset": "openai",
        "api_base": untiered_peer[0],
        "base_url": untiered_peer[0],
        "auth_config": {"mode": "env", "env_var": "COGNIS_UNTIERED_TEST_API_KEY"},
        "default_model": model,
        "models": [configured_model],
    }
    async with h.engine.begin() as connection:
        await connection.run_sync(lambda conn: LLMProvider.__table__.create(conn, checkfirst=True))
    async with h.sessions.begin() as session:
        session.add(
            LLMProvider(
                provider_id="untiered_boundary",
                display_name="Untiered boundary",
                location="controller",
                backend="litellm",
                owner_email="system@cognis.local",
                config=provider_config,
                is_default=True,
            )
        )
    sink = RecordingSink(h.writer)
    cache = UsagePricePinCache()
    capture = UsageCapture(
        sink=sink,
        controller_owner_id="untiered-boundary",
        coverage_epoch_id="untiered-epoch",
        price_lookup=cache,
    )
    config = _test_config(tmp_path, str(h.engine.url))
    auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    registry = build_provider_registry(config, h.sessions, auth, usage_capture=capture)
    publisher = UsagePricePublisher(
        sessions=h.sessions,
        maintenance=h.maintenance,
        resolve_model=registry.llm.get_model_info,
        cache=cache,
    )
    wire = untiered_peer[1]

    def assert_no_capture_sql(*_args: Any) -> None:
        assert usage_sdk._ACTIVE.get() is None, "Accounting attempted SQL during SDK dispatch"

    sql_event.listen(h.engine.sync_engine, "before_cursor_execute", assert_no_capture_sql)
    monkeypatch.setenv("COGNIS_UNTIERED_TEST_API_KEY", "local-test-transport-only")
    try:
        if warm_prices:
            await publisher.refresh()
        assert len(cache) == int(warm_prices), publisher.diagnostics
        current_multiplier = 1
        frozen = None
        if runtime == "executor":
            info = await registry.llm.get_model_info(model, provider_id="untiered_boundary")
            request = controller_request(
                capture,
                actor=None,
                purpose="image_generation",
                model=model,
                kwargs={},
            )
            binding = UsageBinding(
                request,
                "untiered_boundary",
                model,
                model_info=info,
                provider_config=provider_config,
            )
            metadata, seed = executor_metadata(
                binding,
                None,
                executor_id="untiered-executor",
                parameters={},
                streaming=False,
            )
            assert seed is not None and seed.executor is not None
            frozen = request.executor_price_candidates[seed.executor.inference_request_id][0]
            observer = executor_observer(metadata)
            assert observer is not None
            result = await InferenceHandler().image_generate(
                prompt="boundary input",
                model=f"openai/{model}",
                request_kwargs={
                    "api_key": "local-test-transport-only",
                    "api_base": untiered_peer[0],
                },
                usage_binding=observer.binding,
            )
            result = observer.attach(result)
            # A newer committed revision exists before observations return.
            configured_model["input_cost_per_mtok"] = 4.0
            configured_model["output_cost_per_mtok"] = 8.0
            async with h.sessions.begin() as session:
                provider = await session.get(LLMProvider, "untiered_boundary")
                provider.config = {**provider_config, "models": [dict(configured_model)]}
            await publisher.refresh()
            async with h.sessions() as session:
                newer = await session.scalar(
                    select(UsagePriceRevisionRow.revision_id)
                    .order_by(UsagePriceRevisionRow.recorded_at.desc())
                    .limit(1)
                )
            assert newer != (frozen.pin.captured_price_revision_id if frozen.pin else None)
            for observation in result["backend_metadata"]["cognis_usage_observations"]:
                observation["context"]["selection"]["captured_price_revision_id"] = newer
            ingest_executor(result, binding, seed)
            current_multiplier = 2
        elif embedding:
            await registry.llm.embed_response(["boundary input"], model=model)
        else:
            await registry.llm.image_generate("boundary input", model=model)
        assert len(wire) == 1
        assert "service_tier" not in wire[0]
        event = next(e for e in sink.events if isinstance(e, UsageAttemptFinalized))
        selection = event.context.selection
        assert selection.service_tier is None
        assert selection.effective_parameters.service_tier is None
        assert selection.service_tier_applicability is not None
        assert selection.service_tier_applicability.state == "not_applicable"
        assert selection.service_tier_applicability.scope.resource == f"openai.{resource}"
        assert (selection.captured_price_revision_id is not None) == warm_prices
        assert (selection.resolver_snapshot_id is not None) == warm_prices
        if frozen is not None:
            assert selection.captured_price_revision_id == (
                frozen.pin.captured_price_revision_id if frozen.pin else None
            )
            assert selection.resolver_snapshot_id == (frozen.fingerprint if frozen.pin else None)
            assert event.performance.clock_domain == "executor"
        expected = Decimal("0.000022" if embedding else "0.000026")
        booked = expected if warm_prices else None
        expected_status = "complete" if embedding else "partial"
        if not embedding:
            assert any(extra.name == "output_image_count" for extra in event.usage.extras)
        await h.pump()
        async with h.sessions() as session:
            row = (await session.scalars(select(UsageAttemptRow))).one()
            persisted = terminal_event(row)
            assert persisted.context.selection == selection
            receipt = (
                await session.scalars(
                    select(UsageValuationRow).where(UsageValuationRow.source_kind == "attempt")
                )
            ).one()
            assert receipt.historical_cost == booked
            assert receipt.current_cost == expected * current_multiplier
            assert receipt.historical_status == (expected_status if warm_prices else "unpriced")
            assert receipt.current_status == expected_status
            scope = UsageMaintenanceScope(owner_key=row.owner_key, bucket_day=row.bucket_day)
        configured_model["input_cost_per_mtok"] = 4.0 * current_multiplier
        if not embedding:
            configured_model["output_cost_per_mtok"] = 8.0 * current_multiplier
        async with h.sessions.begin() as session:
            provider = await session.get(LLMProvider, "untiered_boundary")
            provider.config = provider_config
        await publisher.refresh()
        await h.pump()
        async with h.sessions() as session:
            receipt = (
                await session.scalars(
                    select(UsageValuationRow).where(UsageValuationRow.source_kind == "attempt")
                )
            ).one()
            assert receipt.historical_cost == booked
            assert receipt.current_cost == expected * 2 * current_multiplier

        class ArchiveDatetime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return datetime.now(tz) + timedelta(days=40)

        # The inference/SDK and first durable read above use the real clock.
        # Only advance the retention owner's clock to exercise age admission.
        monkeypatch.setattr(maintenance_module, "datetime", ArchiveDatetime)
        job = await h.maintenance.request_archive(scope)
        await h.pump()
        assert (await h.maintenance.status(job.job_id)).status == "complete"
        async with h.sessions() as session:
            archive = (await session.scalars(select(UsageArchivedTotalRow))).one()
            assert archive.billing_units["service_tier_applicability"] == (
                selection.service_tier_applicability.model_dump(mode="json")
            )
        await h.maintenance.request_rebuild(scope)
        await h.pump()
        async with h.sessions() as session:
            receipt = (
                await session.scalars(
                    select(UsageValuationRow).where(UsageValuationRow.source_kind == "archive")
                )
            ).one()
            assert receipt.historical_cost == booked
            assert receipt.current_cost == expected * 2 * current_multiplier
    finally:
        sql_event.remove(h.engine.sync_engine, "before_cursor_execute", assert_no_capture_sql)
        await publisher.aclose()


@pytest.mark.asyncio
async def test_existing_api_domain_adopts_disjoint_estimate_without_relabeling_booked_costs(
    usage_storage: UsageStorageHarness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ledger adoption regression, not a new physical-provider qualification."""
    h = usage_storage
    model = "untiered-adoption-target"
    other_model = "untiered-adoption-reference"
    target = {
        "model_id": model,
        "model_mode": "embedding",
        "tier": "standard",
        "input_cost_per_mtok": 2.0,
        "output_cost_per_mtok": 4.0,
    }
    configured = {
        "preset": "openai",
        "default_model": model,
        "models": [
            target,
            {
                "model_id": other_model,
                "tier": "standard",
                "input_cost_per_mtok": 2.0,
                "output_cost_per_mtok": 4.0,
            },
        ],
    }
    provider_id = "untiered_adoption"
    async with h.engine.begin() as connection:
        await connection.run_sync(lambda conn: LLMProvider.__table__.create(conn, checkfirst=True))
    async with h.sessions.begin() as session:
        session.add(
            LLMProvider(
                provider_id=provider_id,
                display_name="Untiered adoption",
                owner_email="system@cognis.local",
                location="controller",
                backend="litellm",
                config=configured,
                is_default=True,
            )
        )
    cache = UsagePricePinCache()
    config = _test_config(tmp_path, str(h.engine.url))
    auth = JWTAuthProvider(config.jwt_private_key_path, config.jwt_public_key_path)
    capture = UsageCapture(
        sink=h.writer,
        controller_owner_id="adoption-test",
        coverage_epoch_id="adoption-epoch",
        price_lookup=cache,
    )
    registry = build_provider_registry(config, h.sessions, auth, usage_capture=capture)
    publisher = UsagePricePublisher(
        sessions=h.sessions,
        maintenance=h.maintenance,
        resolve_model=registry.llm.get_model_info,
        cache=cache,
    )
    try:
        await publisher.refresh()
        assert len(cache) == 2, publisher.diagnostics
        infos = {
            name: await registry.llm.get_model_info(name, provider_id=provider_id)
            for name in (model, other_model)
        }
        pins = {
            name: cache(
                provider_id=provider_id,
                model=name,
                metadata_fingerprint=usage_pricing_fingerprint(
                    provider_id=provider_id,
                    model=name,
                    model_info=info,
                    provider_config=configured,
                    service_tier="standard",
                ),
            )
            for name, info in infos.items()
        }
        assert pins[model] is not None and pins[other_model] is not None
        h.clock.now = datetime.now(UTC)
        event = h.event(pin=pins[model].captured_price_revision_id)
        event = event.model_copy(
            update={
                "context": event.context.model_copy(
                    update={
                        "selection": event.context.selection.model_copy(
                            update={
                                "provider_id": provider_id,
                                "effective_model": model,
                            }
                        ),
                    }
                ),
            }
        )
        assert h.writer.try_emit(event)
        await h.pump()
        async with h.sessions() as session:
            receipt = (await session.scalars(select(UsageValuationRow))).one()
            booked = receipt.historical_cost
            assert booked == receipt.current_cost == Decimal("0.0024")
            assert receipt.basis == "api_equivalent"
            fact = (await session.scalars(select(UsageAttemptRow))).one()
            scope = UsageMaintenanceScope(owner_key=fact.owner_key, bucket_day=fact.bucket_day)
        target["untiered_tariff"] = {
            "version": 1,
            "scope": {"version": 1, "resource": "openai.embeddings.create", "abi_revision": 1},
            "billing_basis": "aggregate_input_tokens",
        }
        async with h.sessions.begin() as session:
            provider = await session.get(LLMProvider, provider_id)
            provider.config = configured
        await publisher.refresh()
        assert len(cache) == 2 and publisher.diagnostics.paused_error_code is None
        assert pins[other_model] == cache(
            provider_id=provider_id,
            model=other_model,
            metadata_fingerprint=pins[other_model].resolver_snapshot_id,
        )
        await h.pump()
        async with h.sessions() as session:
            receipt = (await session.scalars(select(UsageValuationRow))).one()
            assert receipt.historical_cost == booked and receipt.current_cost is None
            assert receipt.basis == receipt.contribution["dimensions"]["basis"] == "api_equivalent"

        class ArchiveDatetime(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return datetime.now(tz) + timedelta(days=40)

        monkeypatch.setattr(maintenance_module, "datetime", ArchiveDatetime)
        await h.maintenance.request_archive(scope)
        await h.pump()
        await h.maintenance.request_rebuild(scope)
        await h.pump()
        async with h.sessions() as session:
            archive = (await session.scalars(select(UsageArchivedTotalRow))).one()
            receipt = (await session.scalars(select(UsageValuationRow))).one()
            assert archive.historical_cost == receipt.historical_cost == booked
            assert receipt.current_cost is None
            assert archive.basis == receipt.basis == "api_equivalent"
            assert receipt.contribution["dimensions"]["basis"] == "api_equivalent"
    finally:
        await publisher.aclose()
