"""Explicit composition boundary for feedback application collaborators."""

from __future__ import annotations

from sqlalchemy import select

from cognis.core.feedback.agents import FeedbackAgents
from cognis.core.feedback.continuity import Continuity
from cognis.core.feedback.primitives import Actor, Payloads
from cognis.core.feedback.public import Captures, PublicFeedback
from cognis.core.feedback.sources import Sources
from cognis.core.feedback.submissions import Submissions
from cognis.core.feedback.threads import Threads
from cognis.core.task_queue import TaskQueue
from cognis.core.turn_scheduler import TurnScheduler
from cognis.models import feedback as dto
from cognis.store.feedback_models import FeedbackSubmissionRow


class FeedbackService:
    """Shared application owner for HTTP, canonical admission and controller tools."""

    def __init__(
        self,
        sources: Sources,
        payloads: Payloads,
        agents: FeedbackAgents,
        scheduler: TurnScheduler,
        task_queue: TaskQueue,
    ) -> None:
        self.sources, self.payloads, self.agents = sources, payloads, agents
        self.threads = Threads(sources, payloads, sources.tokens)
        agents.bind_threads(self.threads)
        self.public = PublicFeedback(sources, payloads, sources.tokens)
        self.captures = Captures(sources, payloads, self.public, self.threads)
        self.submissions = Submissions(sources, payloads, self.threads, scheduler, task_queue)
        self.continuity = Continuity(sources, payloads, self.threads, self.submissions)

    async def staging(self, actor: Actor, conversation_id: str) -> dto.StagingSnapshot:
        """Recover composer state with its latest durable admission receipts."""
        snapshot = await self.threads.staging(actor, conversation_id)
        async with self.sources.factory() as db:
            ids = (
                await db.scalars(
                    select(FeedbackSubmissionRow.submission_id)
                    .where(
                        FeedbackSubmissionRow.sender_email == actor.identity,
                        FeedbackSubmissionRow.origin_conversation_id == conversation_id,
                    )
                    .order_by(FeedbackSubmissionRow.created_at.desc())
                    .limit(100)
                )
            ).all()
        receipts = [await self.submissions.receipt(actor, submission_id) for submission_id in ids]
        return snapshot.model_copy(update={"receipts": receipts})
