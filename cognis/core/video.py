"""Durable paid video orchestration. Provider calls never run inside DB transactions."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.engine import CursorResult

from cognis.core.artifact_access import readable_artifact_ids
from cognis.core.project_access import resolve_agent_use_grant
from cognis.models.usage import (
    UsageAttribution,
    UsageCorrection,
    UsageIdentity,
    UsageNumericExtra,
    UsageProviderCost,
    UsageResourceBinding,
)
from cognis.models.video import (
    TERMINAL_VIDEO_STATES,
    VideoAnalysis,
    VideoJobCreate,
    VideoMutation,
    VideoRecovery,
    VideoRemoteResult,
    VideoRequest,
    VideoSpendPolicy,
)
from cognis.providers.llm.usage_binding import UsageBinding
from cognis.providers.llm.usage_scope import current_usage_execution
from cognis.providers.video import VideoConnection, VideoHTTPAdapter, VideoInput, VideoProviderError
from cognis.store.models import ArtifactRecordRow, VideoAttemptRow, VideoJobRow
from cognis.store.queries import (
    create_artifact_record,
    get_agent,
    get_conversation,
    get_setting_value,
)
from cognis.store.usage.codec import terminal_event
from cognis.store.usage.models import UsageAttemptRow
from cognis.store.video_jobs import VideoJobStore, job_view, microusd

if TYPE_CHECKING:
    from cognis.core.turn_scheduler import TurnScheduler
    from cognis.core.usage.maintenance import UsageMaintenanceService
    from cognis.core.video_transfer import ExecutorVideoAdapter


def _connection_binding(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in binding.items() if key not in {"usage_origin", "usage_settled"}
    }


class VideoService:
    """Own admission, pinned provider identity, reconciliation and publication."""

    def __init__(
        self,
        *,
        session_factory: Any,
        artifacts: Any,
        llm: Any,
        usage_maintenance: UsageMaintenanceService | None = None,
    ):
        self.sessions = session_factory
        self.artifacts = artifacts
        self.llm = llm
        self.store = VideoJobStore(session_factory)
        self.scheduler: TurnScheduler | None = None
        self.usage_maintenance = usage_maintenance

    def adapter(
        self,
        connection: VideoConnection,
        client: httpx.AsyncClient,
        binding: dict[str, Any],
        row: VideoJobRow | None = None,
    ) -> VideoHTTPAdapter | ExecutorVideoAdapter:
        if binding["runtime"] == "executor":
            from cognis.core.video_transfer import ExecutorVideoAdapter

            return ExecutorVideoAdapter(self, connection, binding, row)
        return VideoHTTPAdapter(
            connection,
            client,
            usage_binding=self.capture_binding(connection, binding, row),
        )

    def capture_binding(
        self,
        connection: VideoConnection,
        binding: dict[str, Any],
        row: VideoJobRow | None,
    ) -> UsageBinding | None:
        """Bind a verified video job without inventing a conversation session."""
        if row is None:
            return None
        capture = self.llm.usage_capture
        if capture is None:
            return None
        try:
            request = capture.request(
                request_id=row.attempt_id or row.job_id,
                attribution=UsageAttribution(
                    identity=UsageIdentity(
                        principal_type="user",
                        principal_subject=row.owner_email,
                        usage_subject_kind="user",
                        usage_subject_id=row.owner_email,
                        resource_owner=row.owner_email,
                        initiating_actor=row.owner_email,
                        provider_payer=binding["provider_owner"],
                    ),
                    purpose="video",
                    resources=UsageResourceBinding.model_validate(row.binding["usage_origin"])
                    if "usage_origin" in row.binding
                    else UsageResourceBinding(
                        provenance="controller",
                        conversation_id=row.conversation_id,
                        project_id=row.project_id,
                        agent_id=row.agent_id,
                    ),
                ),
            )
            return UsageBinding(
                request,
                binding["provider_id"],
                connection.model,
                media_operation="video_extend" if row.operation == "extend" else "video_generate",
            )
        except Exception:
            capture.failed_events += 1
            return None

    async def context(self, owner: str, conversation_id: str) -> Any:
        async with self.sessions() as session:
            conversation = await get_conversation(session, conversation_id)
            if (
                conversation is None
                or conversation.user_email != owner
                or conversation.status == "deleted"
            ):
                raise PermissionError("Video conversation not found")
            agent = await get_agent(session, conversation.agent_id)
            if agent is None:
                raise PermissionError("Video agent not found")
            if agent.owner_email != owner and not await resolve_agent_use_grant(
                session,
                agent_id=agent.agent_id,
                user_email=owner,
                project_id=conversation.project_id,
            ):
                raise PermissionError("Video agent access revoked")
            return conversation

    async def policy(self, owner: str) -> VideoSpendPolicy:
        async with self.sessions() as session:
            value = await get_setting_value(session, f"video.policy.{owner}", {})
        return VideoSpendPolicy.model_validate(value)

    async def connection(
        self, request: VideoRequest, owner: str
    ) -> tuple[VideoConnection, dict[str, Any]]:
        result = await self.llm.resolve_video_connection(
            model=request.model,
            provider_id=request.provider_id,
            owner=owner,
            role="video_generation" if request.operation == "generate" else "video_editing",
        )
        return cast(tuple[VideoConnection, dict[str, Any]], result)

    @staticmethod
    def diagnostic_provider(owner: str, binding: dict[str, Any]) -> bool:
        """The runtime/provider boundary shared by admission and capability reads."""
        return (
            binding.get("runtime") == "controller"
            and binding.get("adapter") == "omni"
            and binding.get("provider_owner") == owner
        )

    async def diagnostic_context(
        self, owner: str, conversation_id: str, binding: dict[str, Any]
    ) -> bool:
        """Fail closed outside an owner-owned provider/agent and private origin."""
        if not self.diagnostic_provider(owner, binding):
            return False
        async with self.sessions() as session:
            conversation = await get_conversation(session, conversation_id)
            if (
                conversation is None
                or conversation.user_email != owner
                or conversation.status == "deleted"
                or conversation.visibility != "private"
                or conversation.project_id is not None
            ):
                return False
            agent = await get_agent(session, conversation.agent_id)
            return agent is not None and agent.owner_email == owner

    @staticmethod
    def cost_bound(
        policy: VideoSpendPolicy, request: VideoRequest, binding: dict[str, Any]
    ) -> Decimal | None:
        """Optional budgets fail closed; ordinary video does not require a price table."""
        bound = policy.model_max_cost_usd.get(f"{binding['provider_id']}/{binding['model']}")
        if bound is not None and (not bound.is_finite() or bound <= 0):
            raise ValueError("Model cost bound must be a finite positive amount")
        limits = [request.max_cost_usd] if request.max_cost_usd is not None else []
        if policy.enabled:
            limits.append(policy.per_job_usd)
        if limits:
            if bound is None:
                raise ValueError(
                    "A model cost estimate is required when spending limits are enabled"
                )
            if bound > min(limits):
                raise ValueError("Video cost estimate exceeds the authorized per-job limit")
        return bound

    async def models(
        self, owner: str, model: str | None, provider_id: str | None
    ) -> dict[str, Any]:
        connection, binding = await self.llm.resolve_video_connection(
            model=model, provider_id=provider_id, owner=owner, role="video_generation"
        )
        async with httpx.AsyncClient() as client:
            capabilities = await self.adapter(connection, client, binding).capabilities()
        capabilities.provider_error_messages &= self.diagnostic_provider(owner, binding)
        return {"binding": binding, "capabilities": capabilities.model_dump(mode="json")}

    async def analyze(
        self, owner: str, conversation_id: str, request: VideoAnalysis
    ) -> dict[str, Any]:
        """Reuse normal inference billing/runtime; never extract frames locally."""
        conversation = await self.context(owner, conversation_id)
        async with self.sessions() as session:
            readable = await readable_artifact_ids(
                session,
                [request.source_artifact_id],
                owner_email=owner,
                agent_id=conversation.agent_id,
                conversation_id=conversation_id,
            )
            source = await session.get(ArtifactRecordRow, request.source_artifact_id)
        if source is None or source.artifact_id not in readable:
            raise PermissionError("Video artifact not found")
        if source.mime_type not in {"video/mp4", "video/webm"}:
            raise ValueError("Native analysis requires an MP4 or WebM artifact")
        model, provider_id = await self.llm.resolve_video_analysis(
            model=request.model,
            provider_id=request.provider_id,
            owner=owner,
        )
        url = self.artifacts.get_public_url(
            source.namespace,
            source.object_id,
            source.filename,
            ttl_seconds=1800,
            expires_at=source.expires_at,
        )
        result = await self.llm.generate(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": request.question
                            + "\nReport time-coded observations where possible. State coverage limitations; do not claim frame-exact review.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": url, "format": source.mime_type},
                        },
                    ],
                }
            ],
            model=model,
            provider_id=provider_id,
            task_type="video_analysis",
            acting_user_email=owner,
            max_tokens=4096,
        )
        await self.context(owner, conversation_id)
        return {
            "source_artifact_id": source.artifact_id,
            "model": model,
            "coverage": "Native provider video input; provider sampling, not frame-exact coverage.",
            "result": result.get("content", result.get("text", "")),
        }

    async def inputs(
        self, request: VideoRequest, owner: str, conversation: Any, binding: dict[str, Any]
    ) -> list[VideoInput]:
        refs: list[tuple[str, str]] = [(ref.artifact_id, ref.role) for ref in request.references]
        if request.source_artifact_id:
            refs.append((request.source_artifact_id, "source"))
        if not refs:
            return []
        async with self.sessions() as session:
            allowed = await readable_artifact_ids(
                session,
                [ref for ref, _ in refs],
                owner_email=owner,
                agent_id=conversation.agent_id,
                conversation_id=conversation.conversation_id,
            )
            if any(ref not in allowed for ref, _ in refs):
                raise PermissionError("Video input not found or not authorized")
            result = []
            for artifact_id, role in refs:
                record = await session.get(ArtifactRecordRow, artifact_id)
                if record is None:
                    raise PermissionError("Video input not found")
                expected = "video/" if role == "source" else "image/"
                if not record.mime_type.startswith(expected):
                    raise ValueError(f"Video {role} must have MIME type {expected}")
                locator = interaction = None
                if role == "source":
                    # Output identity comes from the service, never from caller-supplied URLs.
                    jobs = await session.scalars(
                        select(VideoJobRow).where(
                            VideoJobRow.owner_email == owner,
                            VideoJobRow.conversation_id == conversation.conversation_id,
                            VideoJobRow.provider_id == binding["provider_id"],
                            VideoJobRow.model == binding["model"],
                            VideoJobRow.status == "succeeded",
                        )
                    )
                    parent = next(
                        (
                            job
                            for job in jobs
                            if any(output["artifact_id"] == artifact_id for output in job.outputs)
                        ),
                        None,
                    )
                    if parent is None or _connection_binding(parent.binding) != binding:
                        raise ValueError("Source requires a retained same-provider/model output")
                    remote = await self.store.result(parent.attempt_id)
                    index = next(
                        i
                        for i, out in enumerate(parent.outputs)
                        if out["artifact_id"] == artifact_id
                    )
                    if remote is None or index >= len(remote.outputs):
                        raise ValueError("Source provider continuation is unavailable")
                    locator, interaction = remote.outputs[index].locator, remote.handle
                url = self.artifacts.get_public_url(
                    record.namespace,
                    record.object_id,
                    record.filename,
                    ttl_seconds=3600,
                    expires_at=record.expires_at,
                )
                result.append(
                    VideoInput(
                        role=role,
                        url=url,
                        mime_type=record.mime_type,
                        size_bytes=record.size_bytes,
                        provider_locator=locator,
                        interaction_id=interaction,
                    )
                )
            return result

    async def create(self, owner: str, create: VideoJobCreate) -> dict[str, Any]:
        conversation = await self.context(owner, create.conversation_id)
        request = create.request
        # Existing persisted hashes predate this default-off option.
        excluded = {"inspect_provider_error"} if not request.inspect_provider_error else set()
        request_hash = hashlib.sha256(
            request.model_dump_json(exclude=excluded).encode()
        ).hexdigest()
        existing = await self.store.existing(owner, create.conversation_id, create.idempotency_key)
        if existing:
            if existing.request_hash != request_hash:
                raise ValueError("Idempotency key already used with different video input")
            return job_view(existing).model_dump(mode="json")
        policy = await self.policy(owner)
        connection, binding = await self.connection(request, owner)
        if request.inspect_provider_error and not await self.diagnostic_context(
            owner, create.conversation_id, binding
        ):
            raise PermissionError(
                "Provider diagnostics require an owner-owned Omni controller provider/agent "
                "and a private non-project conversation"
            )
        async with httpx.AsyncClient() as client:
            capabilities = await self.adapter(connection, client, binding).capabilities()
        capabilities.provider_error_messages &= self.diagnostic_provider(owner, binding)
        capabilities.validate_request(request)
        from cognis.providers.video import validate_video_combination

        validate_video_combination(binding["adapter"], request)
        await self.inputs(request, owner, conversation, binding)
        estimate = self.cost_bound(policy, request, binding)
        if create.dry_run:
            return {
                "dry_run": True,
                "binding": binding,
                "estimated_cost_usd": str(estimate) if estimate is not None else None,
                "capabilities": capabilities.model_dump(mode="json"),
                "cost_note": (
                    "Owner-configured reservation, not a provider price cap"
                    if estimate is not None
                    else "Price unknown; provider charges apply"
                ),
                "spending_limits_enabled": policy.enabled,
            }
        origin = UsageResourceBinding(
            provenance="controller",
            conversation_id=create.conversation_id,
            agent_id=conversation.agent_id,
            project_id=getattr(conversation, "project_id", None),
        )
        execution = current_usage_execution.get()
        if (
            execution is not None
            and execution.actor == owner
            and execution.conversation_id == create.conversation_id
        ):
            origin = UsageResourceBinding(provenance="controller", **execution.request_values())
        binding = {**binding, "usage_origin": origin.model_dump(mode="json", exclude_none=True)}
        job_id = "vjob_" + uuid.uuid4().hex
        request_id = "art_" + uuid.uuid4().hex
        pinned = request.model_copy(
            update={
                "model": binding["model"],
                "provider_id": binding["provider_id"],
            }
        )
        await self.artifacts.async_save(
            "video_requests",
            request_id,
            "request.json",
            pinned.model_dump_json(exclude=excluded).encode(),
            "application/json",
            owner,
        )
        try:
            row = await self.store.create(
                {
                    "job_id": job_id,
                    "owner_email": owner,
                    "agent_id": conversation.agent_id,
                    "conversation_id": create.conversation_id,
                    "project_id": getattr(conversation, "project_id", None),
                    "idempotency_key": create.idempotency_key,
                    "request_hash": request_hash,
                    "request_artifact_id": request_id,
                    "operation": request.operation,
                    "provider_id": binding["provider_id"],
                    "model": binding["model"],
                    "binding": binding,
                    "attempt_id": "vattempt_" + uuid.uuid4().hex,
                    "estimated_microusd": microusd(estimate) if estimate is not None else 0,
                    "cost_status": "reserved" if estimate is not None else "unknown",
                    "budget_day": datetime.now(UTC).date().isoformat(),
                },
                daily_limit=microusd(policy.daily_usd) if policy.enabled else None,
                active_limit=policy.max_active_jobs,
            )
        except BaseException:
            # A commit may have succeeded despite transport cancellation: retain its request.
            committed = await self.store.existing(
                owner, create.conversation_id, create.idempotency_key
            )
            if committed is None or committed.request_artifact_id != request_id:
                await self.artifacts.async_delete("video_requests", request_id, "request.json")
            raise
        if row.request_artifact_id != request_id:
            await self.artifacts.async_delete("video_requests", request_id, "request.json")
        return await self.get(owner, row.job_id, wait_seconds=create.wait_seconds)

    async def authorized(
        self, owner: str, job_id: str, conversation_id: str | None = None
    ) -> VideoJobRow:
        row = await self.store.get(job_id)
        if row is None or row.owner_email != owner:
            raise PermissionError("Video job not found")
        if conversation_id is not None and row.conversation_id != conversation_id:
            raise PermissionError("Video job not found")
        await self.context(owner, row.conversation_id)
        return row

    async def get(
        self,
        owner: str,
        job_id: str,
        *,
        conversation_id: str | None = None,
        wait_seconds: int = 0,
        inspect_provider_error: bool = False,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + min(60, max(0, wait_seconds))
        while True:
            row = await self.authorized(owner, job_id, conversation_id)
            if row.status in TERMINAL_VIDEO_STATES or asyncio.get_running_loop().time() >= deadline:
                view = job_view(row)
                if inspect_provider_error:
                    if conversation_id != row.conversation_id or not await self.diagnostic_context(
                        owner, row.conversation_id, row.binding
                    ):
                        raise PermissionError("Provider diagnostics require the private origin")
                    view.provider_error_message = row.provider_error_message
                return view.model_dump(mode="json")
            await asyncio.sleep(min(1, max(0, deadline - asyncio.get_running_loop().time())))

    async def list(
        self, owner: str, conversation_id: str, before: str | None = None
    ) -> list[dict[str, Any]]:
        await self.context(owner, conversation_id)
        return [
            job_view(row).model_dump(mode="json")
            for row in await self.store.list(owner, conversation_id, before)
        ]

    async def cancel(
        self, owner: str, job_id: str, mutation: VideoMutation, conversation_id: str | None = None
    ) -> dict[str, Any]:
        row = await self.authorized(owner, job_id, conversation_id)
        if await self.store.mutation_seen(
            job_id, mutation.idempotency_key, "cancel", mutation.expected_version
        ):
            return job_view(row).model_dump(mode="json")
        if row.cancel_requested or row.status in TERMINAL_VIDEO_STATES:
            return job_view(row).model_dump(mode="json")
        if row.version != mutation.expected_version:
            raise ValueError("Video job version changed; read it before retrying")
        await self.store.mutate(row, mutation, "cancel", "cancelled", cancel_requested=True)
        return await self.get(owner, job_id)

    async def recover(
        self, owner: str, job_id: str, mutation: VideoRecovery, conversation_id: str | None = None
    ) -> dict[str, Any]:
        row = await self.authorized(owner, job_id, conversation_id)
        if await self.store.mutation_seen(
            job_id, mutation.idempotency_key, mutation.action, mutation.expected_version
        ):
            return job_view(row).model_dump(mode="json")
        if row.version != mutation.expected_version:
            raise ValueError("Video job version changed; read it before retrying")
        if mutation.action not in job_view(row).recovery_actions:
            raise ValueError("Recovery action is not available")
        remote = await self.store.result(row.attempt_id)
        if mutation.action == "abandon":
            status = "abandoned"
        elif mutation.action == "resume":
            status = "running" if remote else "queued"
            # Never resume a possibly dispatched request as a new submission.
            async with self.sessions() as session:
                from cognis.store.models import VideoAttemptRow

                attempt = await session.get(VideoAttemptRow, row.attempt_id)
                if attempt is None:
                    raise ValueError("Video attempt is unavailable")
                if attempt.dispatched and not remote:
                    status = "needs_reconciliation"
        else:
            status = (
                "ingesting" if mutation.action == "retry_collection" else "needs_reconciliation"
            )
        await self.store.mutate(
            row, mutation, mutation.action, status, error_code=None, next_poll_at=datetime.now(UTC)
        )
        return await self.get(owner, job_id)

    async def tick(self) -> None:
        """Run under the controller's renewed maintenance lease."""
        for row in await self.store.due():
            try:
                await self.advance(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                current = await self.store.get(row.job_id)
                if current and current.status not in TERMINAL_VIDEO_STATES:
                    await self.store.postpone(current, 60, "video_reconciliation_failed")
        await self.notify()
        await self.reconcile_usage()
        from cognis.core.video_transfer import VideoTransfers

        await VideoTransfers(self).cleanup()

    async def reconcile_usage(self, limit: int = 32) -> None:
        """Settle late provider facts without replaying submission or counting polls.

        The job owns render evidence. Usage owns fact-version CAS and projection;
        the metadata marker commits only after correction, so a restart can retry.
        No database transaction spans another service operation or provider I/O.
        """
        if self.usage_maintenance is None:
            return
        from sqlalchemy import or_

        capture_ready = (
            select(UsageAttemptRow.row_id)
            .where(
                UsageAttemptRow.request_id == VideoJobRow.attempt_id,
                UsageAttemptRow.provider_id == VideoJobRow.provider_id,
                UsageAttemptRow.terminal_payload.is_not(None),
            )
            .exists()
        )
        has_evidence = (
            select(VideoAttemptRow.attempt_id)
            .where(
                VideoAttemptRow.attempt_id == VideoJobRow.attempt_id,
                or_(
                    VideoAttemptRow.result["cost_usd"].as_string().is_not(None),
                    VideoAttemptRow.result["output_duration_ms"].as_string().is_not(None),
                ),
            )
            .exists()
        )
        async with self.sessions() as session:
            jobs = list(
                await session.scalars(
                    select(VideoJobRow)
                    .where(
                        VideoJobRow.status.in_(TERMINAL_VIDEO_STATES),
                        VideoJobRow.binding["usage_origin"].as_string().is_not(None),
                        VideoJobRow.binding["usage_settled"].as_boolean().is_not(True),
                        or_(capture_ready, ~has_evidence),
                    )
                    .order_by(VideoJobRow.updated_at)
                    .limit(limit)
                )
            )
        for job in jobs:
            async with self.sessions() as session:
                evidence = await session.get(VideoAttemptRow, job.attempt_id)
                evidence_version = evidence.version if evidence is not None else None
                remote = (
                    VideoRemoteResult.model_validate(evidence.result)
                    if evidence is not None and evidence.result
                    else None
                )
            if remote is not None and (
                remote.cost_usd is not None or remote.output_duration_ms is not None
            ):
                async with self.sessions() as session:
                    facts = list(
                        await session.scalars(
                            select(UsageAttemptRow)
                            .where(
                                UsageAttemptRow.request_id == job.attempt_id,
                                UsageAttemptRow.terminal_payload.is_not(None),
                                UsageAttemptRow.provider_id == job.provider_id,
                            )
                            .order_by(UsageAttemptRow.attempt_ordinal.desc())
                        )
                    )
                if not facts:
                    # Capture is asynchronous; the durable job remains pending.
                    continue
                fact = facts[0]
                final = terminal_event(fact)
                extras = list(final.usage.extras)
                if remote.output_duration_ms is not None:
                    extras = [x for x in extras if x.name != "output_video_duration_ms"]
                    extras.append(
                        UsageNumericExtra(
                            name="output_video_duration_ms",
                            value=remote.output_duration_ms,
                            unit="milliseconds",
                            source="provider",
                        )
                    )
                usage = final.usage.model_copy(
                    update={
                        "provider_cost": UsageProviderCost(amount=remote.cost_usd)
                        if remote.cost_usd is not None
                        else final.usage.provider_cost,
                        "extras": tuple(extras),
                    }
                )
                if usage != final.usage:
                    await self.usage_maintenance.correct(
                        UsageCorrection(
                            correction_id="video:"
                            + job.job_id
                            + ":"
                            + usage.observation_digest()[:32],
                            expected_fact_version=fact.fact_version,
                            reason="late_provider_usage",
                            replacement=final.model_copy(update={"usage": usage}),
                        )
                    )
            async with self.sessions() as session:
                current = await session.get(VideoJobRow, job.job_id, with_for_update=True)
                latest = await session.get(VideoAttemptRow, job.attempt_id)
                if (
                    current is not None
                    and current.attempt_id == job.attempt_id
                    and latest is not None
                    and latest.version == evidence_version
                ):
                    current.binding = {**current.binding, "usage_settled": True}
                    await session.commit()

    async def notify(self) -> None:
        """Durable follow-up admission, deduplicated by the existing turn scheduler."""
        if self.scheduler is None:
            return
        from sqlalchemy import update

        from cognis.core.followups import (
            FollowUpMode,
            FollowUpOriginKind,
            FollowUpRelevanceHint,
            FollowUpRequiredAction,
            FollowUpStatus,
            OtherFollowUp,
        )

        async with self.sessions() as session:
            rows = list(
                await session.scalars(
                    select(VideoJobRow)
                    .where(
                        VideoJobRow.status.in_(TERMINAL_VIDEO_STATES),
                        VideoJobRow.notified.is_(False),
                    )
                    .limit(20)
                )
            )
        for row in rows:
            try:
                await self.context(row.owner_email, row.conversation_id)
            except PermissionError:
                # Suppression is terminal; revocation must not starve later notifications.
                async with self.sessions() as session:
                    await session.execute(
                        update(VideoJobRow)
                        .where(VideoJobRow.job_id == row.job_id)
                        .values(notified=True)
                    )
                    await session.commit()
                continue
            view = job_view(row)
            text = (
                f"Video job {row.job_id} is {row.status}. "
                f"Output artifact IDs: {', '.join(view.output_artifact_ids) or 'none'}. "
                "Use video_job_get for current status and cost; use existing artifact tools "
                "to inspect or deliver completed outputs. Do not automatically start another render."
            )
            error = await self.scheduler.submit_turn(
                row.conversation_id,
                text,
                user_email=row.owner_email,
                system_initiated=True,
                allow_queue=True,
                follow_up=OtherFollowUp(
                    follow_up_id=row.job_id,
                    mode=FollowUpMode.INTEGRATE,
                    origin_kind=FollowUpOriginKind.OTHER,
                    relevance_hint=FollowUpRelevanceHint.SAME_THREAD,
                    required_action=FollowUpRequiredAction.INTEGRATE_RESULT,
                    status=FollowUpStatus.COMPLETED
                    if row.status == "succeeded"
                    else FollowUpStatus.FAILED,
                    title="Video provider job",
                    summary=text[:600],
                    metadata={"video_job_id": row.job_id, "artifact_ids": view.output_artifact_ids},
                ),
                idempotency_scope="video_job",
                idempotency_key=row.job_id,
            )
            if error is None:
                async with self.sessions() as session:
                    await session.execute(
                        update(VideoJobRow)
                        .where(VideoJobRow.job_id == row.job_id)
                        .values(notified=True)
                    )
                    await session.commit()

    async def advance(self, row: VideoJobRow) -> None:
        if row.status in TERMINAL_VIDEO_STATES:
            return
        if row.status == "submitting":
            # A new worker cannot distinguish a successful lost response from rejection.
            await self.store.transition(row, "needs_reconciliation")
            return
        remote = await self.store.result(row.attempt_id)
        if remote and remote.cost_usd is not None:
            await self.store.settle(row.job_id, microusd(remote.cost_usd))
        if row.cancel_requested and row.status in {"queued", "preparing"}:
            if await self.store.transition(row, "cancelled"):
                await self.store.settle(row.job_id, 0, not_submitted=True)
            return
        if row.status == "needs_reconciliation" and remote is None:
            await self.store.postpone(row, 300, "submission_outcome_unknown")
            return
        try:
            conversation = await self.context(row.owner_email, row.conversation_id)
            data, _ = await self.artifacts.async_load(
                "video_requests", row.request_artifact_id, "request.json"
            )
            request = VideoRequest.model_validate_json(data)
            connection, binding = await self.connection(request, row.owner_email)
            if binding != _connection_binding(row.binding):
                raise ValueError("Provider binding changed")
            policy = await self.policy(row.owner_email)
            inputs = await self.inputs(request, row.owner_email, conversation, binding)
            if request.inspect_provider_error and not await self.diagnostic_context(
                row.owner_email, row.conversation_id, binding
            ):
                # Diagnostic opt-in never broadens normal render authority.
                request = request.model_copy(update={"inspect_provider_error": False})
        except (ValueError, PermissionError, FileNotFoundError):
            await self.store.transition(
                row, "blocked", error_code="video_authority_or_binding_unavailable"
            )
            return
        async with httpx.AsyncClient() as client:
            adapter = self.adapter(connection, client, binding, row)
            if row.status in {"queued", "preparing"}:
                if row.status == "queued":
                    await self.store.transition(row, "preparing")
                    return
                capabilities = await adapter.capabilities()
                capabilities.validate_request(request)
                try:
                    bound = self.cost_bound(policy, request, binding)
                    if bound is not None and microusd(bound) > row.estimated_microusd:
                        raise ValueError("Reservation no longer covers the configured bound")
                except ValueError:
                    await self.store.transition(
                        row, "blocked", error_code="spending_policy_changed"
                    )
                    return
                if not await self.store.dispatch(row):
                    return
                try:
                    remote = await adapter.submit(request, inputs)
                    await self.store.record_result(row.attempt_id, remote)
                except VideoProviderError as exc:
                    current = await self.store.get(row.job_id)
                    if exc.rejected:
                        await self.store.settle(row.job_id, 0, not_submitted=True)
                    if current is None or current.status in TERMINAL_VIDEO_STATES:
                        return
                    message = None
                    if request.inspect_provider_error and await self.diagnostic_context(
                        row.owner_email, row.conversation_id, binding
                    ):
                        message = exc.provider_error_message
                    await self.store.transition(
                        current,
                        "failed" if exc.rejected else "needs_reconciliation",
                        error_code=exc.code,
                        provider_error_message=message,
                    )
                    return
                except (httpx.HTTPError, ValueError):
                    current = await self.store.get(row.job_id)
                    if current is None or current.status in TERMINAL_VIDEO_STATES:
                        return
                    await self.store.transition(
                        current, "needs_reconciliation", error_code="submission_outcome_unknown"
                    )
                    return
                current = await self.store.get(row.job_id)
                if current is None:
                    return
                row = current
                if row.status in TERMINAL_VIDEO_STATES or row.status == "blocked":
                    # Late result is retained as evidence, never authority to revive work.
                    if remote.cost_usd is not None:
                        await self.store.settle(row.job_id, microusd(remote.cost_usd))
                    return
            elif remote and remote.state == "running":
                try:
                    remote = await adapter.poll(remote.handle)
                    await self.store.record_result(row.attempt_id, remote)
                except VideoProviderError as exc:
                    await self.store.postpone(row, exc.retry_after, exc.code)
                    return
            if remote is None:
                await self.store.transition(row, "needs_reconciliation")
                return
            if remote.cost_usd is not None:
                await self.store.settle(row.job_id, microusd(remote.cost_usd))
            if remote.state == "running":
                await self.store.transition(
                    row,
                    "running",
                    next_poll_at=datetime.now(UTC) + timedelta(seconds=remote.retry_after),
                )
                return
            if row.cancel_requested or remote.state in {"failed", "cancelled"}:
                await self.store.transition(
                    row,
                    "cancelled" if row.cancel_requested else remote.state,
                    error_code=remote.error_code,
                )
                return
            if row.status != "ingesting":
                await self.store.transition(row, "ingesting")
                return
            await self.publish(row, remote, adapter, policy)

    async def publish(
        self,
        row: VideoJobRow,
        remote: VideoRemoteResult,
        adapter: VideoHTTPAdapter | ExecutorVideoAdapter,
        policy: VideoSpendPolicy,
    ) -> None:
        """Stable output keys and atomic DB registration make collection retryable."""
        from cognis.providers.video_media import mp4_stream

        outputs: list[dict[str, Any]] = []
        for index, output in enumerate(remote.outputs):
            artifact_id = "art_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{row.job_id}/{index}").hex
            filename = f"video-{index + 1}.mp4"
            if row.binding["runtime"] == "executor":
                from cognis.core.video_transfer import ExecutorVideoAdapter, VideoTransfers

                if not isinstance(adapter, ExecutorVideoAdapter):
                    raise ValueError("Executor video adapter required")
                staged_id = await adapter.stage_output(output, output_index=index)
                metadata = await VideoTransfers(self).staged(row, staged_id)
                content = self.artifacts.async_read_range(
                    "video_staging",
                    staged_id,
                    "video-1.mp4",
                    0,
                    metadata.size - 1,
                )
            else:
                if not isinstance(adapter, VideoHTTPAdapter):
                    raise ValueError("Controller video adapter required")
                content = adapter.output_stream(output, policy.max_output_bytes, output_index=index)
            size, digest = await self.artifacts.async_save_stream(
                "videos",
                artifact_id,
                filename,
                mp4_stream(content),
                output.mime_type,
                row.owner_email,
                max_bytes=policy.max_output_bytes,
            )
            outputs.append(
                {
                    "artifact_id": artifact_id,
                    "filename": filename,
                    "size_bytes": size,
                    "sha256": digest,
                    "mime_type": output.mime_type,
                }
            )
        if not outputs:
            raise ValueError("Provider returned no video")
        # Authorize again after a potentially long download; never expose revoked output.
        await self.context(row.owner_email, row.conversation_id)
        async with self.sessions() as session:
            from sqlalchemy import update

            updated = await session.execute(
                update(VideoJobRow)
                .where(
                    VideoJobRow.job_id == row.job_id,
                    VideoJobRow.version == row.version,
                    VideoJobRow.cancel_requested.is_(False),
                )
                .values(
                    status="succeeded",
                    outputs=outputs,
                    version=row.version + 1,
                    error_code=None,
                    updated_at=datetime.now(UTC),
                )
            )
            if cast(CursorResult[Any], updated).rowcount != 1:
                return
            for item in outputs:
                await create_artifact_record(
                    session,
                    artifact_id=item["artifact_id"],
                    namespace="videos",
                    object_id=item["artifact_id"],
                    filename=item["filename"],
                    mime_type=item["mime_type"],
                    size_bytes=item["size_bytes"],
                    owner_email=row.owner_email,
                    owner_agent_id=row.agent_id,
                    conversation_id=row.conversation_id,
                    purpose=f"Video output for {row.job_id}",
                    kind="file",
                    status="saved",
                    content_hash=item["sha256"],
                    project_id=row.project_id,
                )
            await session.commit()
