"""Offline lifecycle tests: no provider credentials or billable calls."""

import base64
import json
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from cognis.artifacts.store import ArtifactStore, ArtifactStoreConfig
from cognis.core.video import VideoService
from cognis.models.usage import UsageAttemptFinalized, UsageAttemptStarted
from cognis.models.video import (
    VideoJobCreate,
    VideoMutation,
    VideoOutput,
    VideoRemoteResult,
    VideoRequest,
    VideoSpendPolicy,
)
from cognis.providers.llm.usage_capture import UsageCapture
from cognis.providers.llm.usage_scope import UsageExecutionBinding, current_usage_execution
from cognis.providers.video import VideoConnection, VideoHTTPAdapter, VideoProviderError
from cognis.store.database import create_engine, create_session_factory
from cognis.store.models import Base, VideoBudgetRow
from cognis.store.video_jobs import job_view
from tests.unit.test_usage_capture_runtime import Sink


@pytest_asyncio.fixture
async def service(tmp_path, usage_capture):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/video.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    service = VideoService(
        session_factory=create_session_factory(engine),
        artifacts=ArtifactStore(
            ArtifactStoreConfig(
                path=str(tmp_path / "artifacts"),
                base_url="https://cognis.test",
                signing_secret="test-signature",
            )
        ),
        llm=SimpleNamespace(usage_capture=usage_capture),
    )
    service.context = AsyncMock(
        return_value=SimpleNamespace(
            conversation_id="video-conversation",
            agent_id="video-agent",
            project_id=None,
        )
    )
    service.connection = AsyncMock(
        return_value=(
            VideoConnection(
                adapter="veo",
                model="veo-3.1-generate-preview",
                api_key="test-key",
                base_url="https://generativelanguage.googleapis.com/v1beta",
            ),
            {
                "provider_id": "google",
                "provider_owner": "owner@test",
                "model": "veo-3.1-generate-preview",
                "adapter": "veo",
                "runtime": "controller",
            },
        )
    )
    service.policy = AsyncMock(
        return_value=VideoSpendPolicy(
            enabled=True,
            model_max_cost_usd={"google/veo-3.1-generate-preview": Decimal("2")},
        )
    )
    yield service
    await engine.dispose()


def create_input(key=None, **changes):
    request = VideoRequest(prompt="A waterfall", max_cost_usd=3, **changes)
    return VideoJobCreate(
        conversation_id="video-conversation",
        idempotency_key=key or uuid.uuid4().hex,
        request=request,
    )


@pytest.mark.parametrize("runtime", ["controller", "executor"])
async def test_real_video_capture_counts_submit_not_poll_or_download(service, runtime):
    from cognis.executor.usage_observer import executor_observer

    sink = Sink()
    capture = UsageCapture(
        sink=sink,
        controller_owner_id="video-controller",
        coverage_epoch_id="video-epoch",
    )
    service.llm.usage_capture = capture
    job = await service.create("owner@test", create_input())
    row = await service.store.get(job["job_id"])
    connection, configured = service.connection.return_value
    binding = {**configured, "runtime": runtime, "executor_id": "video-executor"}
    calls = []
    output = VideoOutput(locator="https://generativelanguage.googleapis.com/fixture.mp4")

    async def peer(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"name": "operations/render"})
        if request.url.path.endswith(".mp4"):
            return httpx.Response(
                200,
                headers={"content-type": "video/mp4"},
                content=b"video-bytes",
            )
        return httpx.Response(200, json={"name": "operations/render", "done": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(peer)) as http:

        async def video_rpc(_binding, payload):
            observer = executor_observer(payload.get("backend_metadata"))
            remote = VideoHTTPAdapter(
                connection,
                http,
                usage_binding=observer.binding if observer else None,
            )
            if payload["operation"] == "submit":
                result = await remote.submit(VideoRequest.model_validate(payload["request"]), [])
                encoded = result.model_dump(mode="json")
            elif payload["operation"] == "poll":
                result = await remote.poll(payload["handle"])
                encoded = result.model_dump(mode="json")
            else:
                data = b"".join(
                    [chunk async for chunk in remote.output_stream(output, max_bytes=1024)]
                )
                assert data == b"video-bytes"
                encoded = {"artifact_id": "art_" + uuid.uuid4().hex}
            return observer.attach(encoded) if observer else encoded

        service.llm.video_rpc = AsyncMock(side_effect=video_rpc)
        adapter = service.adapter(connection, http, binding, row)
        submitted = await adapter.submit(create_input().request, [])
        assert submitted.handle == "operations/render"
        assert len(sink.offers) == 2
        await adapter.poll(submitted.handle)
        if runtime == "executor":
            await adapter.call("collect", output=output.model_dump())
        else:
            assert (
                b"".join([chunk async for chunk in adapter.output_stream(output, max_bytes=1024)])
                == b"video-bytes"
            )
    assert calls == ["POST", "GET", "GET"]
    assert len(sink.offers) == 2
    start, final = sink.offers
    assert isinstance(start, UsageAttemptStarted) and isinstance(final, UsageAttemptFinalized)
    assert final.outcome == "success"
    assert final.performance.clock_domain == runtime
    assert final.context.attribution.purpose == "video"
    assert final.context.attribution.resources.conversation_id == "video-conversation"
    assert capture.failed_events == 0
    assert "waterfall" not in json.dumps([event.model_dump(mode="json") for event in sink.offers])


async def step(service, job_id):
    row = await service.store.get(job_id)
    await service.advance(row)
    return await service.store.get(job_id)


@pytest.mark.asyncio
async def test_duplicate_admission_reserves_once(service):
    request = create_input()
    first = await service.create("owner@test", request)
    second = await service.create("owner@test", request)
    assert first["job_id"] == second["job_id"]
    async with service.sessions() as db:
        budgets = list(await db.scalars(select(VideoBudgetRow)))
    assert sum(row.exposure_microusd for row in budgets) == 2_000_000
    with pytest.raises(ValueError, match="different"):
        await service.create(
            "owner@test",
            request.model_copy(
                update={"request": request.request.model_copy(update={"prompt": "Different"})}
            ),
        )


@pytest.mark.asyncio
async def test_default_policy_allows_video_without_pricing_setup(service, monkeypatch):
    service.policy.return_value = VideoSpendPolicy()
    request = create_input()
    request.request.max_cost_usd = None
    job = await service.create("owner@test", request)
    assert job["estimated_cost_usd"] is None
    assert job["cost_status"] == "unknown"
    submit = AsyncMock(return_value=VideoRemoteResult(handle="operations/render", state="running"))
    monkeypatch.setattr(VideoHTTPAdapter, "submit", submit)
    await step(service, job["job_id"])
    row = await step(service, job["job_id"])
    assert row.status == "running"
    submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_request_limit_still_requires_estimate_when_budgets_off(service):
    service.policy.return_value = VideoSpendPolicy()
    with pytest.raises(ValueError, match="estimate is required"):
        await service.create("owner@test", create_input())


@pytest.mark.asyncio
async def test_opt_in_budget_cannot_ignore_unknown_previous_charges(service):
    service.policy.return_value = VideoSpendPolicy()
    request = create_input()
    request.request.max_cost_usd = None
    job = await service.create("owner@test", request)
    service.policy.return_value = VideoSpendPolicy(
        enabled=True, model_max_cost_usd={"google/veo-3.1-generate-preview": Decimal("2")}
    )
    with pytest.raises(ValueError, match="Unresolved video charges"):
        await service.create("owner@test", create_input())
    await service.store.settle(job["job_id"], 1_000_000)
    async with service.sessions() as session:
        assert (
            sum(b.exposure_microusd for b in await session.scalars(select(VideoBudgetRow)))
            == 1_000_000
        )
    assert (await service.create("owner@test", create_input()))["status"] == "queued"


@pytest.mark.asyncio
async def test_enabling_budget_before_dispatch_blocks_unpriced_job(service, monkeypatch):
    service.policy.return_value = VideoSpendPolicy()
    request = create_input()
    request.request.max_cost_usd = None
    job = await service.create("owner@test", request)
    await step(service, job["job_id"])
    service.policy.return_value = VideoSpendPolicy(enabled=True)
    submit = AsyncMock()
    monkeypatch.setattr(VideoHTTPAdapter, "submit", submit)
    assert (await step(service, job["job_id"])).status == "blocked"
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_cap_enforced_even_with_account_limits_disabled(service):
    service.policy.return_value = VideoSpendPolicy(
        enabled=False, model_max_cost_usd={"google/veo-3.1-generate-preview": Decimal("5")}
    )
    with pytest.raises(ValueError, match="per-job limit"):
        await service.create("owner@test", create_input())


@pytest.mark.asyncio
async def test_dispatch_timeout_never_resubmits(service, monkeypatch):
    submit = AsyncMock(side_effect=httpx.ReadTimeout("lost response"))
    monkeypatch.setattr(VideoHTTPAdapter, "submit", submit)
    job = await service.create("owner@test", create_input())
    await step(service, job["job_id"])
    row = await step(service, job["job_id"])
    assert row.status == "needs_reconciliation"
    await step(service, job["job_id"])
    assert submit.await_count == 1
    assert (await service.store.get(row.job_id)).cost_status == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,rejected", [(400, True), (403, True), (408, False), (429, True), (500, False)]
)
async def test_http_diagnostics_preserve_submission_accounting_and_idempotency(
    service, monkeypatch, status, rejected
):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(
            status,
            json={
                "error": {
                    "message": "private prompt https://private.test/?key=private-key",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "domain": "googleapis.com",
                            "reason": "BILLING_DISABLED",
                            "metadata": {"consumer": "private-account"},
                        }
                    ],
                }
            },
        )

    request = create_input()
    job = await service.create("owner@test", request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            service,
            "adapter",
            lambda connection, _client, binding, row: VideoHTTPAdapter(connection, client),
        )
        await step(service, job["job_id"])
        row = await step(service, job["job_id"])
        assert row.status == ("failed" if rejected else "needs_reconciliation")
        assert row.error_code == f"provider_http_{status}:reason=BILLING_DISABLED"
        view = await service.get("owner@test", row.job_id)
        assert view["error_code"] == row.error_code
        assert view["cost_status"] == ("not_submitted" if rejected else "unknown")
        assert view["actual_cost_usd"] == ("0" if rejected else None)
        assert view["output_artifact_ids"] == []
        assert "private" not in str(view)
        replay = await service.create("owner@test", request)
        assert replay["job_id"] == row.job_id
        await step(service, job["job_id"])
    assert calls == ["POST"]
    assert await service.store.result(row.attempt_id) is None
    async with service.sessions() as db:
        budgets = list(await db.scalars(select(VideoBudgetRow)))
    assert sum(b.exposure_microusd for b in budgets) == (0 if rejected else 2_000_000)


@pytest.mark.asyncio
async def test_poll_http_rejection_diagnostics_do_not_settle_or_resubmit(service, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"name": "operations/accepted-render"})
        return httpx.Response(
            403,
            json={
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "domain": "googleapis.com",
                            "reason": "API_KEY_SERVICE_BLOCKED",
                        }
                    ]
                }
            },
        )

    job = await service.create("owner@test", create_input())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            service,
            "adapter",
            lambda connection, _client, binding, row: VideoHTTPAdapter(connection, client),
        )
        await step(service, job["job_id"])
        assert (await step(service, job["job_id"])).status == "running"
        row = await step(service, job["job_id"])
    assert calls == ["POST", "GET"]
    assert row.status == "running"
    assert row.error_code == "provider_http_403:reason=API_KEY_SERVICE_BLOCKED"
    assert row.cost_status == "unknown" and row.actual_microusd is None
    async with service.sessions() as db:
        assert (
            sum(b.exposure_microusd for b in await db.scalars(select(VideoBudgetRow))) == 2_000_000
        )


@pytest.mark.asyncio
async def test_crash_after_dispatch_blocks_resubmit(service, monkeypatch):
    submit = AsyncMock()
    monkeypatch.setattr(VideoHTTPAdapter, "submit", submit)
    job = await service.create("owner@test", create_input())
    row = await step(service, job["job_id"])
    assert await service.store.dispatch(row)
    row = await step(service, job["job_id"])
    assert row.status == "needs_reconciliation"
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_queued_releases_exposure(service):
    job = await service.create("owner@test", create_input())
    await service.cancel(
        "owner@test",
        job["job_id"],
        VideoMutation(
            expected_version=job["version"],
            idempotency_key=uuid.uuid4().hex,
        ),
    )
    row = await step(service, job["job_id"])
    assert row.status == "cancelled"
    assert row.cost_status == "not_submitted"
    assert row.actual_microusd == 0


@pytest.mark.asyncio
async def test_cancel_commit_failure_rolls_back_state_receipt_and_budget(service, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    job = await service.create("owner@test", create_input())
    mutation = VideoMutation(expected_version=job["version"], idempotency_key=uuid.uuid4().hex)
    with monkeypatch.context() as patch:
        patch.setattr(
            AsyncSession, "commit", AsyncMock(side_effect=RuntimeError("commit interrupted"))
        )
        with pytest.raises(RuntimeError, match="commit interrupted"):
            await service.cancel("owner@test", job["job_id"], mutation)
    row = await service.store.get(job["job_id"])
    assert row.status == "queued" and row.actual_microusd is None
    result = await service.cancel("owner@test", row.job_id, mutation)
    assert result["status"] == "cancelled" and result["cost_status"] == "not_submitted"
    assert await service.cancel("owner@test", row.job_id, mutation) == result
    async with service.sessions() as session:
        assert sum(b.exposure_microusd for b in await session.scalars(select(VideoBudgetRow))) == 0


@pytest.mark.asyncio
async def test_foreign_owner_or_conversation_cannot_read(service):
    job = await service.create("owner@test", create_input())
    with pytest.raises(PermissionError):
        await service.get("other@test", job["job_id"])
    with pytest.raises(PermissionError):
        await service.get("owner@test", job["job_id"], conversation_id="other-conversation")


@pytest.mark.asyncio
async def test_admission_budget_and_dry_run(service):
    request = create_input()
    result = await service.create("owner@test", request.model_copy(update={"dry_run": True}))
    assert result["dry_run"]
    assert (
        await service.store.existing("owner@test", request.conversation_id, request.idempotency_key)
        is None
    )
    await service.create("owner@test", request)
    await service.create("owner@test", create_input())
    with pytest.raises(ValueError, match="concurrency"):
        await service.create("owner@test", create_input())


@pytest.mark.asyncio
async def test_stale_cancel_cannot_overwrite_state(service):
    job = await service.create("owner@test", create_input())
    await step(service, job["job_id"])
    with pytest.raises(ValueError, match="version changed"):
        await service.cancel(
            "owner@test",
            job["job_id"],
            VideoMutation(
                expected_version=job["version"],
                idempotency_key=uuid.uuid4().hex,
            ),
        )


@pytest.mark.asyncio
async def test_changed_price_blocks_before_paid_call(service, monkeypatch):
    submit = AsyncMock()
    monkeypatch.setattr(VideoHTTPAdapter, "submit", submit)
    job = await service.create("owner@test", create_input())
    await step(service, job["job_id"])
    service.policy.return_value.model_max_cost_usd["google/veo-3.1-generate-preview"] = Decimal("4")
    row = await step(service, job["job_id"])
    assert row.status == "blocked"
    submit.assert_not_called()
    cancelled = await service.cancel(
        "owner@test",
        row.job_id,
        VideoMutation(
            expected_version=row.version,
            idempotency_key=uuid.uuid4().hex,
        ),
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["cost_status"] == "not_submitted"


@pytest.mark.asyncio
async def test_cancel_blocked_after_dispatch_preserves_exposure(service):
    job = await service.create("owner@test", create_input())
    row = await step(service, job["job_id"])
    assert await service.store.dispatch(row)
    await service.store.transition(await service.store.get(row.job_id), "blocked")
    row = await service.store.get(row.job_id)
    cancelled = await service.cancel(
        "owner@test",
        row.job_id,
        VideoMutation(
            expected_version=row.version,
            idempotency_key=uuid.uuid4().hex,
        ),
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["actual_cost_usd"] is None
    async with service.sessions() as session:
        assert (
            sum(b.exposure_microusd for b in await session.scalars(select(VideoBudgetRow)))
            == 2_000_000
        )


@pytest.mark.asyncio
async def test_staging_retry_reuses_object_and_terminal_cleanup(service):
    from cognis.core.video_transfer import VideoTransfers
    from cognis.store.models import VideoTransferRow

    transfers = VideoTransfers(service)
    job = await service.create("owner@test", create_input())
    row = await service.store.get(job["job_id"])

    async def content():
        yield b"\x00\x00\x00\x18ftypmp42media-content"

    artifacts = set()
    for _ in range(4):
        ticket = await transfers.issue(row)
        result = await transfers.receive(ticket["token"], content())
        artifacts.add(result["artifact_id"])
    assert len(artifacts) == 1
    await service.cancel(
        "owner@test",
        row.job_id,
        VideoMutation(
            expected_version=row.version,
            idempotency_key=uuid.uuid4().hex,
        ),
    )
    await transfers.cleanup()
    async with service.sessions() as session:
        assert list(await session.scalars(select(VideoTransferRow))) == []
    for artifact_id in artifacts:
        assert (
            await service.artifacts.async_load_metadata("video_staging", artifact_id, "video-1.mp4")
            is None
        )


@pytest.mark.asyncio
async def test_known_charge_settles_even_without_delivery(service):
    job = await service.create("owner@test", create_input())
    row = await service.store.get(job["job_id"])
    await service.store.transition(row, "failed")
    await service.store.settle(row.job_id, 1_500_000)
    await service.store.settle(row.job_id, 1_500_000)
    async with service.sessions() as db:
        budgets = list(await db.scalars(select(VideoBudgetRow)))
    assert sum(b.exposure_microusd for b in budgets) == 1_500_000
    view = job_view(await service.store.get(row.job_id))
    assert view.actual_cost_usd == "1.5"


@pytest.mark.asyncio
async def test_success_streams_and_publishes_once(service, monkeypatch):
    result = VideoRemoteResult(
        handle="operations/completed-render",
        state="completed",
        outputs=[VideoOutput(locator="https://generativelanguage.googleapis.com/video")],
        cost_usd=Decimal("1.5"),
    )
    monkeypatch.setattr(VideoHTTPAdapter, "submit", AsyncMock(return_value=result))

    async def stream(self, output, max_bytes, *, output_index=None):
        assert output_index == 0
        yield b"\x00\x00\x00\x18ftypmp42"
        yield b"video-content"

    monkeypatch.setattr(VideoHTTPAdapter, "output_stream", stream)
    job = await service.create("owner@test", create_input())
    await step(service, job["job_id"])
    row = await step(service, job["job_id"])
    assert row.status == "ingesting"
    row = await step(service, job["job_id"])
    assert row.status == "succeeded" and len(row.outputs) == 1
    assert row.actual_microusd == 1_500_000
    data = b"".join(
        [
            chunk
            async for chunk in service.artifacts.async_read_range(
                "videos", row.outputs[0]["artifact_id"], "video-1.mp4", 4, 7
            )
        ]
    )
    assert data == b"ftyp"


@pytest.mark.asyncio
async def test_video_preserves_origin_execution_after_session_rotation(service):
    token = current_usage_execution.set(
        UsageExecutionBinding(
            actor="owner@test",
            session_id="original-session",
            conversation_id="video-conversation",
            agent_id="video-agent",
            task_id="origin-task",
            step_run_id="origin-step",
            project_id="origin-project",
        )
    )
    try:
        job = await service.create("owner@test", create_input())
    finally:
        current_usage_execution.reset(token)
    row = await service.store.get(job["job_id"])
    origin = row.binding["usage_origin"]
    assert origin["session_id"] == "original-session"
    assert origin["task_id"] == "origin-task"
    assert origin["project_id"] == "origin-project"


async def test_terminal_evidence_cannot_regress(service):
    job = await service.create("owner@test", create_input())
    row = await service.store.get(job["job_id"])
    completed = VideoRemoteResult(handle="operations/completed-render", state="completed")
    await service.store.record_result(row.attempt_id, completed)
    await service.store.record_result(
        row.attempt_id, completed.model_copy(update={"state": "running"})
    )
    assert (await service.store.result(row.attempt_id)).state == "completed"


@pytest.mark.asyncio
async def test_transfer_is_scoped_expiring_and_cancellation_aware(service):
    import hashlib
    from datetime import UTC, datetime, timedelta

    from cognis.core.video_transfer import VideoTransfers
    from cognis.store.models import VideoTransferRow

    job = await service.create("owner@test", create_input())
    row = await service.store.get(job["job_id"])
    transfers = VideoTransfers(service)
    ticket = await transfers.issue(row)

    async def content():
        yield b"\x00\x00\x00\x18ftypmp42"
        yield b"video-content"

    received = await transfers.receive(ticket["token"], content())
    assert await transfers.receive(ticket["token"], content()) == received
    metadata = await transfers.staged(row, received["artifact_id"])
    assert metadata.content_type == "video/mp4"
    other = await service.create("owner@test", create_input())
    with pytest.raises(PermissionError):
        await transfers.staged(await service.store.get(other["job_id"]), received["artifact_id"])
    with pytest.raises(PermissionError):
        await transfers.authorize("not-a-valid-ticket", "output")
    async with service.sessions() as session:
        token = await session.get(
            VideoTransferRow, hashlib.sha256(ticket["token"].encode()).hexdigest()
        )
        token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    with pytest.raises(PermissionError):
        await transfers.authorize(ticket["token"], "output")
    ticket = await transfers.issue(row)
    await service.cancel(
        "owner@test",
        row.job_id,
        VideoMutation(
            expected_version=row.version,
            idempotency_key=uuid.uuid4().hex,
        ),
    )
    with pytest.raises(PermissionError):
        await transfers.receive(ticket["token"], content())


@pytest.mark.asyncio
async def test_mutation_replay_is_idempotent(service):
    from cognis.models.video import VideoRecovery

    job = await service.create("owner@test", create_input())
    await service.store.transition(await service.store.get(job["job_id"]), "blocked")
    job = await service.get("owner@test", job["job_id"])
    mutation = VideoRecovery(
        expected_version=job["version"],
        idempotency_key=uuid.uuid4().hex,
        action="abandon",
    )
    first = await service.recover("owner@test", job["job_id"], mutation)
    second = await service.recover("owner@test", job["job_id"], mutation)
    assert first == second
    assert first["status"] == "abandoned"


@pytest.mark.asyncio
async def test_executor_adapter_sends_metadata_and_collects_via_ticket(service):
    from cognis.core.video_transfer import VideoTransfers

    policy = service.policy.return_value
    service.connection.return_value[1]["runtime"] = "executor"
    result = VideoRemoteResult(
        handle="operations/remote-render",
        state="completed",
        outputs=[VideoOutput(locator="https://generativelanguage.googleapis.com/video")],
    )
    from cognis.providers.video import native_video_capabilities

    async def rpc(binding, params):
        assert "api_key" in params["connection"]  # transient dispatch only
        if params["operation"] == "capabilities":
            return native_video_capabilities("veo").model_dump()
        if params["operation"] == "submit":
            assert params["inputs"] == []
            return result.model_dump()
        assert params["operation"] == "collect"
        assert "output_index" not in params  # Legacy URI collection stays wire-compatible.
        assert params["output_ticket"]["max_bytes"] == policy.max_output_bytes

        async def content():
            yield b"\x00\x00\x00\x18ftypmp42video-content"

        return await VideoTransfers(service).receive(params["output_ticket"]["token"], content())

    service.llm = SimpleNamespace(
        video_rpc=AsyncMock(side_effect=rpc),
        usage_capture=service.llm.usage_capture,
    )
    job = await service.create("owner@test", create_input())
    await step(service, job["job_id"])
    await step(service, job["job_id"])
    row = await step(service, job["job_id"])
    assert row.status == "succeeded"
    assert len(row.outputs) == 1
    assert "api_key" not in row.binding


def configure_inline_runtime(service, runtime):
    connection, binding = service.connection.return_value
    service.connection.return_value = (
        connection.model_copy(update={"adapter": "omni", "model": "gemini-omni-1.1-flash"}),
        {**binding, "adapter": "omni", "model": "gemini-omni-1.1-flash", "runtime": runtime},
    )
    service.policy.return_value.model_max_cost_usd = {"google/gemini-omni-1.1-flash": Decimal("2")}


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["controller", "executor"])
@pytest.mark.parametrize("retry", [False, True, "second"])
async def test_inline_restart_collection_retry_parity_never_resubmits_or_persists_media(
    service, monkeypatch, caplog, runtime, retry
):
    from cognis.core.video_transfer import VideoTransfers
    from cognis.executor.video import video_rpc
    from cognis.models.video import VideoRecovery
    from cognis.store.models import VideoAttemptRow

    configure_inline_runtime(service, runtime)
    media = [b"\x00\x00\x00\x18ftypmp42first-video", b"\x00\x00\x00\x18ftypmp42second-video"]
    encoded = [base64.b64encode(item).decode() for item in media]
    body = {
        "id": "retained-inline-render",
        "status": "completed",
        "steps": [
            {"type": "user_input", "content": [{"type": "video", "data": "private-input"}]},
            {
                "type": "model_output",
                "content": [
                    {"type": "video", "mime_type": "video/mp4", "data": item} for item in encoded
                ],
            },
        ],
    }
    posts, gets, rpc_metadata, collection_indices = [], [], [], []
    fail_next_collection = retry

    async def handler(request):
        nonlocal fail_next_collection
        if request.url.host == "cognis.test":
            assert request.method == "PUT"
            assert "x-goog-api-key" not in request.headers
            binary = await request.aread()
            assert binary in media

            async def chunks():
                yield binary

            result = await VideoTransfers(service).receive(
                request.headers["x-cognis-video-transfer"], chunks()
            )
            return httpx.Response(200, json=result)
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "test-key"
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"id": body["id"], "status": "in_progress"})
        gets.append(request)
        if len(gets) == (3 if retry == "second" else 2) and fail_next_collection:
            fail_next_collection = False
            return httpx.Response(503)
        return httpx.Response(200, json=body)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    async def rpc(binding, params):
        rpc_metadata.append(json.dumps(params))
        if params["operation"] == "collect":
            collection_indices.append(params["output_index"])
        return await video_rpc(params)

    service.llm = SimpleNamespace(
        video_rpc=AsyncMock(side_effect=rpc),
        usage_capture=service.llm.usage_capture,
    )
    job = await service.create("owner@test", create_input())
    await step(service, job["job_id"])
    await step(service, job["job_id"])
    row = await step(service, job["job_id"])
    assert row.status == "ingesting"
    # A new service instance has no adapter-local memory: ordered descriptors,
    # handle and staging identities must be sufficient for collection recovery.
    restarted = VideoService(
        session_factory=service.sessions, artifacts=service.artifacts, llm=service.llm
    )
    restarted.context, restarted.connection, restarted.policy = (
        service.context,
        service.connection,
        service.policy,
    )
    if retry:
        with pytest.raises(VideoProviderError, match="^provider_http_503$"):
            await step(restarted, row.job_id)
        row = await restarted.store.get(row.job_id)
        assert row.cost_status == "unknown" and row.actual_microusd is None
        # Ingesting jobs offer collection recovery without changing submission state.
        await restarted.recover(
            "owner@test",
            row.job_id,
            VideoRecovery(
                action="retry_collection",
                expected_version=row.version,
                idempotency_key=uuid.uuid4().hex,
            ),
        )
    row = await step(restarted, row.job_id)
    assert row.status == "succeeded" and len(row.outputs) == 2
    assert len({item["artifact_id"] for item in row.outputs}) == 2
    for item, expected in zip(row.outputs, media, strict=True):
        assert (
            await service.artifacts.async_load("videos", item["artifact_id"], item["filename"])
        )[0] == expected
    assert len(posts) == 1
    if runtime == "executor":
        expected_indices = [0, 1, 1] if retry == "second" else [0, 0, 1] if retry else [0, 1]
        assert collection_indices == expected_indices
    async with service.sessions() as db:
        attempt = await db.get(VideoAttemptRow, row.attempt_id)
        metadata = json.dumps(attempt.result)
    metadata += json.dumps(row.outputs) + "".join(rpc_metadata) + caplog.text
    assert "private-input" not in metadata
    for item in encoded:
        assert item not in metadata
    service.scheduler = SimpleNamespace(submit_turn=AsyncMock(return_value=None))
    await service.notify()
    assert all(item not in str(service.scheduler.submit_turn.call_args) for item in encoded)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "revoke"])
@pytest.mark.parametrize("runtime", ["controller", "executor"])
async def test_inline_collection_does_not_publish_after_cancellation_or_revocation(
    service, monkeypatch, action, runtime
):
    from cognis.core.video_transfer import VideoTransfers
    from cognis.executor.video import video_rpc

    configure_inline_runtime(service, runtime)
    media = b"\x00\x00\x00\x18ftypmp42private-generated-video"
    body = {
        "id": "cancelled-inline-render",
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "video",
                        "mime_type": "video/mp4",
                        "data": base64.b64encode(media).decode(),
                    }
                ],
            }
        ],
    }
    posts = []
    interrupted = False

    async def handler(request):
        nonlocal interrupted
        if request.url.host == "cognis.test":

            async def chunks():
                yield await request.aread()

            try:
                result = await VideoTransfers(service).receive(
                    request.headers["x-cognis-video-transfer"], chunks()
                )
            except PermissionError:
                return httpx.Response(403)
            return httpx.Response(200, json=result)
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json=body)
        if not interrupted:
            interrupted = True
            if action == "revoke":
                service.context.side_effect = PermissionError("Conversation access revoked")
            else:
                row = await service.store.get(job["job_id"])
                await service.cancel(
                    "owner@test",
                    row.job_id,
                    VideoMutation(expected_version=row.version, idempotency_key=uuid.uuid4().hex),
                )
        return httpx.Response(200, json=body)

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    async def rpc(binding, params):
        return await video_rpc(params)

    service.llm = SimpleNamespace(
        video_rpc=AsyncMock(side_effect=rpc),
        usage_capture=service.llm.usage_capture,
    )
    job = await service.create("owner@test", create_input())
    await step(service, job["job_id"])
    await step(service, job["job_id"])
    if runtime == "executor":
        with pytest.raises(ValueError, match="^Video transfer failed$"):
            await step(service, job["job_id"])
    elif action == "revoke":
        with pytest.raises(PermissionError):
            await step(service, job["job_id"])
    else:
        await step(service, job["job_id"])
    row = await service.store.get(job["job_id"])
    assert not row.outputs
    assert row.cost_status == "unknown" and row.actual_microusd is None
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_invalid_inline_delivery_preserves_known_charge_and_never_resubmits(
    service, monkeypatch
):
    configure_inline_runtime(service, "controller")
    calls = []
    body = {
        "id": "charged-inline-render",
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "video", "mime_type": "video/mp4", "data": "!!!!"}],
            }
        ],
    }

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            service,
            "adapter",
            lambda connection, _client, binding, row=None: VideoHTTPAdapter(connection, client),
        )
        job = await service.create("owner@test", create_input())
        await step(service, job["job_id"])
        row = await step(service, job["job_id"])
        remote = await service.store.result(row.attempt_id)
        await service.store.record_result(
            row.attempt_id, remote.model_copy(update={"cost_usd": Decimal("1.5")})
        )
        for _ in range(2):
            with pytest.raises(VideoProviderError, match="^invalid_inline_video$"):
                await step(service, job["job_id"])
    row = await service.store.get(job["job_id"])
    assert row.cost_status == "reported" and row.actual_microusd == 1_500_000
    assert not row.outputs
    assert calls == ["POST", "GET", "GET"]


@pytest_asyncio.fixture
async def private_service(service):
    from cognis.store.models import Agent, Conversation, User

    async with service.sessions() as db:
        db.add_all([User(email="owner@test"), User(email="other@test")])
        await db.flush()
        db.add(Agent(agent_id="video-agent", owner_email="owner@test", name="video"))
        await db.flush()
        db.add(
            Conversation(
                conversation_id="video-conversation",
                agent_id="video-agent",
                user_email="owner@test",
                context_type="web",
                visibility="private",
            )
        )
        await db.commit()
    service.context = VideoService.context.__get__(service)
    connection, binding = service.connection.return_value
    service.connection.return_value = (
        connection.model_copy(update={"adapter": "omni", "model": "gemini-omni-1.1-flash"}),
        {
            **binding,
            "adapter": "omni",
            "model": "gemini-omni-1.1-flash",
            "provider_owner": "owner@test",
        },
    )
    service.policy.return_value.model_max_cost_usd = {"google/gemini-omni-1.1-flash": Decimal("2")}
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in", [False, True])
async def test_interactions_message_requires_opt_in_and_explicit_private_detail(
    private_service, monkeypatch, caplog, opt_in
):
    import json

    from cognis.tools.builtin.video import handle_video_tool

    service = private_service
    message = "Unknown parameter 'response_format.delivery'."
    calls = []

    def handler(request):
        calls.append(request.method)
        assert "inspect_provider_error" not in json.loads(request.content)
        return httpx.Response(
            400, json={"error": {"code": "parameter_unknown", "message": message}}
        )

    request = create_input(inspect_provider_error=opt_in)
    job = await service.create("owner@test", request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            service,
            "adapter",
            lambda connection, _client, binding, row: VideoHTTPAdapter(connection, client),
        )
        await step(service, job["job_id"])
        row = await step(service, job["job_id"])
        await step(service, job["job_id"])
    assert calls == ["POST"] and row.status == "failed"
    assert row.error_code == "provider_http_400:code=parameter_unknown"
    assert row.provider_error_message == (message if opt_in else None)
    assert row.actual_microusd == 0 and row.cost_status == "not_submitted"
    assert await service.store.result(row.attempt_id) is None
    assert (await service.get("owner@test", row.job_id))["provider_error_message"] is None
    assert (await service.list("owner@test", row.conversation_id))[0][
        "provider_error_message"
    ] is None
    assert (await service.create("owner@test", request))["provider_error_message"] is None
    assert job_view(row).provider_error_message is None
    result = await handle_video_tool(
        "video_job_get",
        {"job_id": row.job_id, "inspect_provider_error": True},
        service=service,
        owner="owner@test",
        conversation_id=row.conversation_id,
    )
    assert not result.is_error
    assert json.loads(result.output)["provider_error_message"] == (message if opt_in else None)
    for owner, conversation in [
        ("other@test", row.conversation_id),
        ("owner@test", "different-conversation"),
        ("owner@test", None),
    ]:
        with pytest.raises(PermissionError):
            await service.get(
                owner,
                row.job_id,
                conversation_id=conversation,
                inspect_provider_error=True,
            )
    service.scheduler = SimpleNamespace(submit_turn=AsyncMock(return_value=None))
    await service.notify()
    assert message not in str(service.scheduler.submit_turn.call_args)
    assert message not in caplog.text
    async with service.sessions() as db:
        budgets = list(await db.scalars(select(VideoBudgetRow)))
    assert sum(b.exposure_microusd for b in budgets) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,field,value",
    [
        ("conversation", "visibility", "project"),
        ("conversation", "project_id", "private-project"),
        ("conversation", "status", "deleted"),
        ("conversation", "user_email", "other@test"),
        ("agent", "owner_email", "other@test"),
        ("binding", "provider_owner", "other@test"),
        ("binding", "runtime", "executor"),
        ("binding", "adapter", "veo"),
    ],
)
async def test_diagnostic_admission_fails_closed_before_dispatch(
    private_service, target, field, value
):
    from cognis.store.models import Agent, Conversation

    service = private_service
    if target == "binding":
        service.connection.return_value[1][field] = value
    else:
        async with service.sessions() as db:
            row = await db.get(
                Conversation if target == "conversation" else Agent,
                "video-conversation" if target == "conversation" else "video-agent",
            )
            setattr(row, field, value)
            await db.commit()
    with pytest.raises(PermissionError):
        await service.create("owner@test", create_input(inspect_provider_error=True))
    assert not await service.store.list("owner@test", "video-conversation", None)


@pytest.mark.asyncio
async def test_privacy_change_during_response_discards_message(private_service, monkeypatch):
    from cognis.store.models import Conversation

    service = private_service

    async def handler(request):
        async with service.sessions() as db:
            conversation = await db.get(Conversation, "video-conversation")
            conversation.project_id = "new-project"
            await db.commit()
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "permission_denied",
                    "message": "This model requires billing to be enabled.",
                }
            },
        )

    job = await service.create("owner@test", create_input(inspect_provider_error=True))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            service,
            "adapter",
            lambda connection, _client, binding, row: VideoHTTPAdapter(connection, client),
        )
        await step(service, job["job_id"])
        row = await step(service, job["job_id"])
    assert row.provider_error_message is None and row.cost_status == "not_submitted"
    with pytest.raises(PermissionError):
        await service.get(
            "owner@test",
            row.job_id,
            conversation_id=row.conversation_id,
            inspect_provider_error=True,
        )
    assert (await service.get("owner@test", row.job_id))["provider_error_message"] is None


@pytest.mark.asyncio
async def test_old_request_hash_and_new_diagnostic_opt_in_are_idempotent(service):
    import hashlib
    import json

    request = create_input()
    job = await service.create("owner@test", request)
    row = await service.store.get(job["job_id"])
    legacy_hash = hashlib.sha256(
        request.request.model_dump_json(exclude={"inspect_provider_error"}).encode()
    ).hexdigest()
    assert row.request_hash == legacy_hash
    pinned, _ = await service.artifacts.async_load(
        "video_requests", row.request_artifact_id, "request.json"
    )
    assert "inspect_provider_error" not in json.loads(pinned)
    assert (await service.create("owner@test", request))["job_id"] == row.job_id
    with pytest.raises(ValueError, match="different video input"):
        await service.create(
            "owner@test",
            request.model_copy(
                update={
                    "request": request.request.model_copy(update={"inspect_provider_error": True})
                }
            ),
        )
