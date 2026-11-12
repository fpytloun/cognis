"""Recipient policy for successful project task channel notifications."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cognis.core.channel_delivery_targets import preferred_channel_target
from cognis.core.followups import TaskResultFollowUp
from cognis.core.project_access import (
    agent_usable_in_project,
    can_read_task,
    resolve_project_role_by_id,
    role_satisfies,
)
from cognis.core.task_outcomes import task_outcome_key
from cognis.models.task import TaskModel
from cognis.store.models import ChannelDeliveryOutboxRow, Task
from cognis.store.queries import (
    create_conversation,
    get_agent,
    get_channel_delivery_outbox,
    get_conversation,
    get_conversation_channel_route,
    get_preferred_channel_account_for_agent,
    get_project,
    get_task,
    list_project_grants,
)

logger = logging.getLogger(__name__)


def project_result_is_shareable(task: Task) -> bool:
    """Exclude private, silent, unsuccessful, and non-final task notifications."""
    return bool(
        task.project_id
        and task.visibility == "project"
        and task.status == "completed"
        and task.delivery_mode == "preferred_channel"
        and task.applied_completion_mode != "silent"
    )


async def project_result_recipient_allowed(
    session: AsyncSession, task_id: str, user_email: str
) -> bool:
    """Recheck both task readability and agent access, including on outbox retries."""
    row = await get_task(session, task_id)
    if row is None:
        return False
    if not project_result_is_shareable(row) or row.created_by == user_email:
        return False
    if not await can_read_task(session, row, user_email):
        return False
    project, access = await resolve_project_role_by_id(session, row.project_id, user_email)
    if project is None or project.status != "active" or not role_satisfies(access.role, "use"):
        return False
    agent = await get_agent(session, row.agent_id)
    if agent is None or agent.status != "active":
        return False
    return await agent_usable_in_project(
        session, agent=agent, user_email=user_email, project_id=row.project_id
    )


async def project_result_recipients(session: AsyncSession, task: TaskModel) -> list[str]:
    """Resolve current project readers who may also run the task's agent."""
    row = await get_task(session, task.task_id)
    if row is None or not row.project_id or not project_result_is_shareable(row):
        return []
    project = await get_project(session, row.project_id)
    if project is None or project.status == "deleted":
        return []
    recipients = {project.owner_email}
    recipients.update(
        grant.grantee_user_email
        for grant in await list_project_grants(session, project.project_id)
        if grant.revoked_at is None
        and grant.grantee_type == "user"
        and grant.grantee_user_email is not None
    )
    recipients.discard(task.created_by)
    allowed: list[str] = []
    for user_email in sorted(recipients):
        if await project_result_recipient_allowed(session, task.task_id, user_email):
            allowed.append(user_email)
    return allowed


async def project_result_execution_target(
    session: AsyncSession, *, task: TaskModel, conversation_id: str
) -> str | None:
    """Keep project-derived execution in a private recipient-owned project chat.

    The deterministic conversation identity is metadata-only and converges through
    its primary key. It never reassigns a main chat or expands an agent grant.
    """
    conversation = await get_conversation(session, conversation_id)
    if conversation is None or not await project_result_recipient_allowed(
        session, task.task_id, conversation.user_email
    ):
        return None
    agent = await get_agent(session, task.agent_id)
    if agent is None or conversation.agent_id != task.agent_id:
        return None
    if await agent_usable_in_project(
        session,
        agent=agent,
        user_email=conversation.user_email,
        project_id=conversation.project_id,
    ):
        return conversation_id
    identity = f"project_task_results:{task.project_id}:{conversation_id}"
    target_id = f"conv_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}"
    target = await get_conversation(session, target_id)
    if target is None:
        try:
            async with session.begin_nested():
                target = await create_conversation(
                    session,
                    conversation_id=target_id,
                    user_email=conversation.user_email,
                    agent_id=task.agent_id,
                    context_type="web" if conversation.context_type == "web" else "chat",
                    context_ref=identity,
                    context_data={
                        **(conversation.context_data or {}),
                        "kind": "project_task_results",
                        "source_conversation_id": conversation_id,
                    },
                    project_id=task.project_id,
                    visibility="private",
                    title="Project task results",
                )
        except IntegrityError:
            target = await get_conversation(session, target_id)
    if (
        target is None
        or target.status != "active"
        or target.user_email != conversation.user_email
        or target.agent_id != task.agent_id
        or target.project_id != task.project_id
    ):
        return None
    return target.conversation_id


async def project_result_target_allowed(
    session: AsyncSession,
    task_id: str,
    conversation_id: str,
    *,
    require_execution: bool = False,
) -> bool:
    """Recheck success, visibility, recipient permission, and preferred destination."""
    task = await get_task(session, task_id)
    conversation = await get_conversation(session, conversation_id)
    if (
        task is None
        or conversation is None
        or conversation.status != "active"
        or conversation.agent_id != task.agent_id
        or not await project_result_recipient_allowed(session, task_id, conversation.user_email)
    ):
        return False
    preferred = await preferred_channel_target(
        session, user_email=conversation.user_email, agent_id=task.agent_id
    )
    if preferred != conversation_id:
        context = conversation.context_data or {}
        if (
            preferred is None
            or context.get("kind") != "project_task_results"
            or context.get("source_conversation_id") != preferred
            or conversation.project_id != task.project_id
        ):
            return False
        if await get_conversation_channel_route(
            session, preferred
        ) != await get_conversation_channel_route(session, conversation_id):
            return False
    if require_execution:
        agent = await get_agent(session, task.agent_id)
        if agent is None or not await agent_usable_in_project(
            session,
            agent=agent,
            user_email=conversation.user_email,
            project_id=conversation.project_id,
        ):
            return False
    return True


async def project_result_route_allowed(
    session: AsyncSession, row: ChannelDeliveryOutboxRow
) -> bool:
    """Fence delayed delivery against revoked access and changed channel routes."""
    if row.source_type == "project_task_result_reply":
        parent_id, separator, turn_id = (row.source_id or "").partition(":")
        if not separator or not turn_id:
            return False
        parent = await get_channel_delivery_outbox(session, parent_id)
        return (
            parent is not None
            and parent.source_type == "project_task_result_follow_up"
            and row.conversation_id == parent.conversation_id
            and row.user_email == parent.user_email
            and (row.channel_type, row.account_id, row.chat_id, row.thread_id)
            == (parent.channel_type, parent.account_id, parent.chat_id, parent.thread_id)
            and await project_result_route_allowed(session, parent)
        )
    if not row.source_id or not await project_result_target_allowed(
        session, row.source_id, row.conversation_id
    ):
        return False
    task = await get_task(session, row.source_id)
    conversation = await get_conversation(session, row.conversation_id)
    if (
        task is None
        or conversation is None
        or conversation.status != "active"
        or conversation.user_email != row.user_email
        or conversation.agent_id != task.agent_id
    ):
        return False
    if row.source_type == "project_task_result":
        identities = (task_outcome_key(task), task.task_id)
        allowed_ids = {
            "cdel_"
            + hashlib.sha256(
                f"project_task_result:{occurrence}:{row.user_email}".encode()
            ).hexdigest()[:20]
            for occurrence in identities
        }
        # Already-persisted informational deliveries from before outcome keys
        # retain their original identity until their durable retry completes.
        if row.delivery_id not in allowed_ids:
            return False
    elif row.source_type == "project_task_result_follow_up":
        identity = f"task_follow_up:{task_outcome_key(task)}:{row.conversation_id}"
        if row.delivery_id != f"cdel_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}":
            return False
    account = await get_preferred_channel_account_for_agent(
        session, user_email=row.user_email, agent_id=task.agent_id
    )
    route = await get_conversation_channel_route(session, row.conversation_id)
    return (
        account is not None
        and account.account_id == row.account_id
        and route == (row.channel_type, row.account_id, row.chat_id, row.thread_id, row.user_email)
    )


async def project_result_follow_up_allowed(
    session: AsyncSession, follow_up: TaskResultFollowUp, conversation_id: str
) -> bool:
    """Re-authorize a durable recipient turn, including its terminal occurrence."""
    task = await get_task(session, follow_up.task_id)
    return (
        task is not None
        and follow_up.outcome_key == task_outcome_key(task)
        and await project_result_target_allowed(
            session, follow_up.task_id, conversation_id, require_execution=True
        )
    )


async def project_result_direct_turn_allowed(
    session: AsyncSession, row: ChannelDeliveryOutboxRow
) -> bool:
    """Keep an adopted fenced terminal under its originating recipient grant."""
    if not row.direct_turn_request_id:
        return True
    from cognis.store.models import DirectTurnRequestRow

    request = await session.scalar(
        select(DirectTurnRequestRow).where(
            DirectTurnRequestRow.request_id == row.direct_turn_request_id
        )
    )
    if request is None:
        return False
    metadata = (request.payload or {}).get("metadata", {})
    payload = metadata.get("follow_up") if isinstance(metadata, dict) else None
    if not isinstance(payload, dict) or not payload.get("project_result"):
        return True
    follow_up = TaskResultFollowUp.model_validate(payload)
    route = await get_conversation_channel_route(session, row.conversation_id)
    return (
        row.user_email == request.user_id
        and row.conversation_id == request.conversation_id
        and route is not None
        and route[:4] == (row.channel_type, row.account_id, row.chat_id, row.thread_id)
        and await project_result_follow_up_allowed(session, follow_up, row.conversation_id)
    )


async def fan_out_project_result(
    task: TaskModel,
    session_factory: async_sessionmaker[AsyncSession],
    deliver_result: Callable[[str], Awaitable[None]],
) -> None:
    """Isolate each eligible recipient's outcome from the owner's delivery."""
    if (
        not task.project_id
        or task.status != "completed"
        or task.delivery.mode != "preferred_channel"
        or task.applied_completion_mode == "silent"
    ):
        return
    try:
        async with session_factory() as session:
            recipients = await project_result_recipients(session, task)
    except Exception:
        logger.warning(
            "Project result recipient resolution failed",
            extra={"extra_data": {"task_id": task.task_id}},
        )
        return
    for user_email in recipients:
        try:
            await deliver_result(user_email)
        except Exception:
            # A recipient's unavailable route must not break the owner's delivery.
            logger.warning(
                "Project result recipient delivery failed",
                extra={"extra_data": {"task_id": task.task_id}},
            )
