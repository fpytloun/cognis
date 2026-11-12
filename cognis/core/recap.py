"""Quiet, read-only conversation recaps persisted outside agent context."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cognis.api.models import UserPreferencesResponse
from cognis.core.diff_stats import count_diff_lines
from cognis.core.events import Event, EventType
from cognis.core.json_utils import extract_json_object, extract_visible_text_from_response
from cognis.core.maintenance_lease import run_periodic_maintenance
from cognis.logging import get_logger
from cognis.models.session import ConversationModel, SessionEvent, SessionModel
from cognis.runtime_context import scoped_runtime_context
from cognis.store.coordination import DatabaseLeaseStore, Lease
from cognis.store.queries import (
    AUTO_RECAP_ENABLED_STATE_KEY,
    get_agent,
    get_conversation,
    get_session_row,
    get_user_ui_state,
    list_active_delegation_sessions,
    list_active_tasks_by_source_conversation,
    list_open_managed_conversation_links,
    list_pending_notification_types_by_conversation,
    list_recent_web_recap_candidates,
)

logger = get_logger(__name__)
IDLE_SECONDS = 300
AUTO_COOLDOWN = timedelta(minutes=30)
RECOVERY_HORIZON = timedelta(hours=2)
RECAP_POLICY_VERSION = 4
_SOURCE_TYPES = frozenset(
    {
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "artifact",
        "file_diff",
        "lifecycle",
        "task_result",
    }
)
_RECENT_MESSAGE_WINDOW = 30


def _recap_marker(event: dict[str, Any]) -> bool:
    data = event.get("data")
    return (
        event.get("type") == "lifecycle"
        and isinstance(data, dict)
        and data.get("event") in {"conversation_recap", "conversation_recap_skipped"}
    )


def _published_recap_marker(event: dict[str, Any]) -> bool:
    data = event.get("data")
    return (
        event.get("type") == "lifecycle"
        and isinstance(data, dict)
        and data.get("event") == "conversation_recap"
    )


def _source_since_marker(
    flattened: list[tuple[str, dict[str, Any]]],
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any] | None]:
    """Resume from the recorded source watermark, not the later recap append.

    A turn may be appended while the classifier runs. Those events can precede
    the recap marker and must still be included in the next recap.
    """
    marker = next(
        (event for _session_id, event in reversed(flattened) if _published_recap_marker(event)),
        None,
    )
    after = 0
    if marker is not None:
        data = marker.get("data")
        source_session = data.get("source_session_id") if isinstance(data, dict) else None
        source_seq = data.get("source_seq") if isinstance(data, dict) else None
        for index, (session_id, event) in enumerate(flattened):
            if session_id == source_session and event.get("seq") == source_seq:
                after = index + 1
                break
        else:
            # Retention or bounded history removed the source: never replay
            # the whole conversation as though it were new.
            after = next(
                (index + 1 for index, (_sid, event) in enumerate(flattened) if event is marker),
                0,
            )
    return [
        (session_id, event)
        for session_id, event in flattened[after:]
        if event.get("type") in _SOURCE_TYPES and not _recap_marker(event)
    ], marker


def _conversation_message_positions(
    events: list[tuple[str, dict[str, Any]]],
) -> list[int]:
    """Keep requests and the last assistant text per turn, not progress chatter."""
    user_positions: list[int] = []
    assistant_positions: dict[str, int] = {}
    current_turn: str | None = None
    for index, (session_id, event) in enumerate(events):
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        kind = event.get("type")
        if kind == "user_message":
            current_turn = str(data.get("turn_id") or f"{session_id}:{event.get('seq')}")
            user_positions.append(index)
        elif kind == "assistant_message":
            turn = str(data.get("turn_id") or current_turn or f"{session_id}:{event.get('seq')}")
            assistant_positions[turn] = index
    return sorted([*user_positions, *assistant_positions.values()])


def _recent_window(
    flattened: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Return one evidence window for both the recap prose and its work stats."""
    message_positions = _conversation_message_positions(flattened)
    if not message_positions:
        return []
    start = message_positions[max(0, len(message_positions) - _RECENT_MESSAGE_WINDOW)]
    return [
        (session_id, event)
        for session_id, event in flattened[start:]
        if event.get("type") in _SOURCE_TYPES and not _recap_marker(event)
    ]


def _evidence(
    events: list[tuple[str, dict[str, Any]]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    messages: list[dict[str, str]] = []
    selected_messages = set(_conversation_message_positions(events))
    deliverables: dict[str, dict[str, str]] = {}
    artifacts: dict[str, dict[str, Any]] = {}
    files: dict[str, dict[str, Any]] = {}
    file_diffs_omitted = False
    user_turns: set[str] = set()
    tool_results = 0
    for index, (session_id, event) in enumerate(events):
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        kind = str(event.get("type") or "")
        subtype = str(data.get("event") or "")
        if kind == "user_message":
            user_turns.add(str(data.get("turn_id") or f"{session_id}:{event.get('seq')}"))
        if kind == "tool_result" and data.get("result"):
            tool_results += 1
        if index in selected_messages:
            content = str(
                (data.get("user_visible_content") or data.get("content"))
                if kind == "user_message"
                else data.get("content") or ""
            )
            if content:
                role = "User" if kind == "user_message" else "Assistant"
                messages.append({"role": role, "content": content[:1200]})
        if kind == "lifecycle" and subtype == "assistant_deliverable":
            key = str(data.get("deliverable_id") or "")
            if key:
                deliverables[key] = {"id": key, "title": str(data.get("title") or "Deliverable")}
        if kind == "artifact" or (kind == "lifecycle" and subtype == "artifact"):
            key = str(data.get("artifact_id") or "")
            if key:
                artifacts[key] = {
                    "id": key,
                    "title": str(data.get("filename") or "Artifact"),
                    "mime_type": str(data.get("mime_type") or "application/octet-stream"),
                    "size_bytes": data.get("size_bytes")
                    if isinstance(data.get("size_bytes"), int)
                    else 0,
                }
        if kind == "tool_result":
            for attachment in data.get("attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                key = str(attachment.get("artifact_id") or "")
                if key:
                    artifacts[key] = {
                        "id": key,
                        "title": str(attachment.get("filename") or "Artifact"),
                        "mime_type": str(attachment.get("mime_type") or "application/octet-stream"),
                        "size_bytes": attachment.get("size_bytes")
                        if isinstance(attachment.get("size_bytes"), int)
                        else 0,
                    }
        if kind in {"file_diff", "tool_result"}:
            for item in data.get("file_diffs") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("omitted_count"):
                    file_diffs_omitted = True
                if not isinstance(item.get("path"), str) or not item["path"]:
                    continue
                path = item["path"]
                diff = item.get("diff")
                complete_diff = (
                    isinstance(diff, str)
                    and bool(diff)
                    and not (
                        item.get("truncated")
                        or item.get("content_truncated")
                        or item.get("preview_omitted")
                    )
                )
                counted = (
                    count_diff_lines(diff)
                    if complete_diff and isinstance(diff, str)
                    else (None, None)
                )
                additions = item.get("additions")
                deletions = item.get("deletions")
                additions = additions if type(additions) is int and additions >= 0 else counted[0]
                deletions = deletions if type(deletions) is int and deletions >= 0 else counted[1]
                previous = files.get(path)
                if previous is None:
                    files[path] = {"path": path, "additions": additions, "deletions": deletions}
                else:
                    for field, count in (("additions", additions), ("deletions", deletions)):
                        previous[field] = (
                            previous[field] + count
                            if previous[field] is not None and count is not None
                            else None
                        )
    recent = messages[-30:]
    first_request = next((message for message in messages if message["role"] == "User"), None)
    if first_request is not None and first_request not in recent:
        recent = [first_request, *recent]
    return recent, {
        "deliverables": list(deliverables.values())[:30],
        "artifacts": list(artifacts.values())[:30],
        "files": list(files.values())[:100],
        "file_diffs_omitted": file_diffs_omitted or len(files) > 100,
        "user_turns": len(user_turns),
        "tool_results": tool_results,
    }


def _work_focus(
    events: list[tuple[str, dict[str, Any]]],
) -> tuple[str, str] | None:
    """Locate the latest actual change and its surrounding user/assistant outcome."""
    selected = set(_conversation_message_positions(events))
    for index in range(len(events) - 1, -1, -1):
        event = events[index][1]
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        changed_files = (
            event.get("type") == "tool_result"
            and not data.get("is_error")
            and any(
                isinstance(diff, dict) and bool(diff.get("path"))
                for diff in data.get("file_diffs") or []
            )
        )
        delivered = (
            event.get("type") == "lifecycle" and data.get("event") == "assistant_deliverable"
        )
        if not changed_files and not delivered:
            continue
        user_index = next(
            (pos for pos in range(index, -1, -1) if events[pos][1].get("type") == "user_message"),
            None,
        )
        if user_index is None:
            continue
        request_data = events[user_index][1].get("data") or {}
        request = str(
            request_data.get("user_visible_content") or request_data.get("content") or ""
        ).strip()
        assistant = next(
            (
                str((events[pos][1].get("data") or {}).get("content") or "").strip()
                for pos in sorted(selected, reverse=True)
                if pos > index
                and events[pos][1].get("type") == "assistant_message"
                and not any(
                    later[1].get("type") == "user_message" for later in events[index + 1 : pos]
                )
            ),
            "",
        )
        if request and assistant:
            return request[:1200], assistant[:1200]
    return None


class RecapService:
    """Create recaps without entering the agent loop or publishing turn events."""

    def __init__(
        self,
        *,
        session_factory: Any,
        providers: Any,
        scheduler: Any,
        session_cache: Any,
        event_bus: Any,
        lease_store: DatabaseLeaseStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._providers = providers
        self._scheduler = scheduler
        self._session_cache = session_cache
        self._event_bus = event_bus
        self._lease_store = lease_store
        self._lease_owner = uuid.uuid4().hex
        self._sweep_stop = asyncio.Event()
        self._sweep_task: asyncio.Task[None] | None = None
        self._scanned_activity: dict[str, datetime] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        event_bus.subscribe(EventType.TURN_COMPLETED, self._on_turn_completed)

    def start(self) -> None:
        """Recover recently due auto-recaps under one cluster-wide lease."""
        if self._lease_store is not None and self._sweep_task is None:
            self._sweep_task = asyncio.create_task(
                run_periodic_maintenance(
                    self._scan_recent,
                    stop=self._sweep_stop,
                    interval_seconds=30,
                    resource_key="conversation-recap-scan",
                    lease_store=self._lease_store,
                ),
                name="conversation-recap-scan",
            )

    async def stop(self) -> None:
        self._event_bus.unsubscribe(EventType.TURN_COMPLETED, self._on_turn_completed)
        self._sweep_stop.set()
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            await asyncio.gather(self._sweep_task, return_exceptions=True)
            self._sweep_task = None
        tasks = list(self._jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()

    async def _scan_recent(self) -> None:
        now = datetime.now(UTC)
        offset = 0
        while not self._sweep_stop.is_set():
            async with self._session_factory() as db:
                candidates = await list_recent_web_recap_candidates(
                    db,
                    since=now - RECOVERY_HORIZON,
                    until=now - timedelta(seconds=IDLE_SECONDS),
                    offset=offset,
                )
            if not candidates:
                break
            offset += len(candidates)
            for conversation_id, activity_at, enabled_at in candidates:
                if self._sweep_stop.is_set():
                    return
                activity = (
                    activity_at.replace(tzinfo=UTC) if activity_at.tzinfo is None else activity_at
                )
                enabled = (
                    enabled_at.replace(tzinfo=UTC)
                    if enabled_at is not None and enabled_at.tzinfo is None
                    else enabled_at
                )
                if (enabled is not None and enabled > activity) or (
                    self._scanned_activity.get(conversation_id) == activity
                ):
                    continue
                try:
                    outcome = await self._auto_if_idle(conversation_id, expected_activity=activity)
                    if outcome not in {
                        "Conversation is busy.",
                        "Recap is already being generated.",
                        "Recap ownership changed before publication.",
                        "Conversation resumed before the recap was published.",
                        "Recap cooldown is active.",
                    }:
                        self._scanned_activity[conversation_id] = activity
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Automatic recap recovery failed",
                        extra={"conversation_id": conversation_id},
                    )
            if len(candidates) < 200:
                break
        if len(self._scanned_activity) > 2000:
            self._scanned_activity = {
                conversation_id: activity
                for conversation_id, activity in self._scanned_activity.items()
                if activity >= now - RECOVERY_HORIZON
            }

    async def _on_turn_completed(self, event: Event) -> None:
        data = event.data
        if (
            data.get("task_id")
            or data.get("system_initiated")
            or data.get("delegated")
            or data.get("delivery_id")
            or data.get("managed_continuation_pending")
            or data.get("partial")
            or data.get("queued_count")
        ):
            return
        conversation_id = str(data.get("conversation_id") or "")
        if not conversation_id:
            return
        existing = self._jobs.pop(conversation_id, None)
        if existing:
            existing.cancel()
        task = asyncio.create_task(self._auto_after_idle(conversation_id))
        self._jobs[conversation_id] = task
        task.add_done_callback(
            lambda finished: (
                self._jobs.pop(conversation_id, None)
                if self._jobs.get(conversation_id) is finished
                else None
            )
        )

    async def _auto_after_idle(self, conversation_id: str) -> None:
        try:
            await asyncio.sleep(IDLE_SECONDS)
            await self._auto_if_idle(conversation_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Automatic conversation recap failed", extra={"conversation_id": conversation_id}
            )

    async def _auto_if_idle(
        self, conversation_id: str, *, expected_activity: datetime | None = None
    ) -> str:
        async with self._session_factory() as db:
            row = await get_conversation(db, conversation_id)
            if row is None or row.context_type != "web" or row.status != "active":
                return "Conversation is ineligible."
            if (row.context_ref or "").startswith("web:agent_direct:") or (
                isinstance(row.context_data, dict)
                and row.context_data.get("kind") == "agent_direct"
            ):
                return "Conversation is ineligible."
            last_message_at = row.last_message_at
            if last_message_at is None:
                return "Conversation has no recent message."
            last_message = (
                last_message_at.replace(tzinfo=UTC)
                if last_message_at.tzinfo is None
                else last_message_at
            )
            if expected_activity is not None and last_message != expected_activity:
                return "Conversation resumed."
            if datetime.now(UTC) - last_message < timedelta(seconds=IDLE_SECONDS):
                return "Conversation resumed."
            pref = await get_user_ui_state(db, row.user_email, "ui.preferences")
            if not UserPreferencesResponse.model_validate(
                pref.value if pref else {}
            ).chat.auto_recap:
                return "Automatic recap is disabled."
            if expected_activity is not None:
                enabled_state = await get_user_ui_state(
                    db, row.user_email, AUTO_RECAP_ENABLED_STATE_KEY
                )
                if enabled_state is not None:
                    enabled_time = enabled_state.updated_at
                    enabled_time = (
                        enabled_time.replace(tzinfo=UTC)
                        if enabled_time.tzinfo is None
                        else enabled_time
                    )
                    if enabled_time > last_message:
                        return "Automatic recap was enabled after the last message."
            if await list_pending_notification_types_by_conversation(
                db, row.user_email, [conversation_id]
            ):
                return "Conversation is busy."
            from cognis.core.session import _to_conversation_model, _to_session_model

            session_row = (
                await get_session_row(db, row.active_session_id) if row.active_session_id else None
            )
            agent_row = await get_agent(db, row.agent_id)
            if session_row is None or agent_row is None:
                return "Conversation is ineligible."
            conversation = _to_conversation_model(row)
            session = _to_session_model(session_row)
            agent_owner_email = agent_row.owner_email
        if await self._scheduler.durable_running_turn_state(
            conversation_id
        ) is not None or await self._scheduler.get_queued_messages(conversation_id):
            return "Conversation is busy."
        return await self.generate(
            conversation, session, auto=True, agent_owner_email=agent_owner_email
        )

    async def generate(
        self,
        conversation: ConversationModel,
        session: SessionModel,
        *,
        agent_owner_email: str,
        auto: bool = False,
    ) -> str:
        if conversation.context.type not in {
            "web",
            "signal",
            "slack",
            "telegram",
            "discord",
            "matrix",
        }:
            return "Recap is only available in chat conversations."
        if (
            session.delegation_mode
            or conversation.context.platform_data.get("task_id")
            or (
                conversation.context.ref
                and conversation.context.ref.startswith("web:agent_direct:")
            )
            or conversation.context.platform_data.get("kind") == "agent_direct"
        ):
            return "Recap is not available in task or delegated sessions."
        if auto and conversation.context.type != "web":
            return "Automatic recaps are only available in web chat."
        lease_holder: list[Lease] = []
        lost_lease = asyncio.Event()
        renew_task: asyncio.Task[None] | None = None
        if self._lease_store is not None:
            lease = await self._lease_store.acquire(
                f"conversation-recap:{conversation.conversation_id}",
                f"{self._lease_owner}:{uuid.uuid4().hex}",
                ttl_seconds=120,
            )
            if lease is None:
                return "Recap is already being generated."
            lease_holder.append(lease)
            renew_task = asyncio.create_task(
                self._renew_recap_lease(lease_holder, lost_lease),
                name="conversation-recap-lease",
            )
        # The slash-command REST path and idle callback do not run inside an
        # agent turn. Intaris read_events derives its authorization identity
        # from these context variables, not from record_events' user_email.
        try:
            with scoped_runtime_context(
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
                agent_owner_email=agent_owner_email,
            ):
                return await self._generate_in_context(
                    conversation,
                    session,
                    auto=auto,
                    lease_holder=lease_holder,
                    lost_lease=lost_lease,
                )
        finally:
            if renew_task is not None:
                renew_task.cancel()
                await asyncio.gather(renew_task, return_exceptions=True)
            if lease_holder and self._lease_store is not None:
                await asyncio.shield(self._lease_store.release(lease_holder[0]))

    async def _renew_recap_lease(
        self, lease_holder: list[Lease], lost_lease: asyncio.Event
    ) -> None:
        if self._lease_store is None:
            return
        try:
            while True:
                await asyncio.sleep(30)
                renewed = await self._lease_store.renew(lease_holder[0], ttl_seconds=120)
                if renewed is None:
                    lost_lease.set()
                    return
                lease_holder[0] = renewed
        except asyncio.CancelledError:
            raise
        except Exception:
            lost_lease.set()
            logger.exception("Recap lease renewal failed")

    async def _auto_still_idle(
        self, conversation: ConversationModel, session: SessionModel
    ) -> bool:
        conversation_id = conversation.conversation_id
        if await self._scheduler.durable_running_turn_state(
            conversation_id
        ) is not None or await self._scheduler.get_queued_messages(conversation_id):
            return False
        async with self._session_factory() as db:
            current = await get_conversation(db, conversation_id)
            latest_message = current.last_message_at if current else None
            snapshot_message = conversation.last_message_at
            if latest_message is not None and latest_message.tzinfo is None:
                latest_message = latest_message.replace(tzinfo=UTC)
            if snapshot_message is not None and snapshot_message.tzinfo is None:
                snapshot_message = snapshot_message.replace(tzinfo=UTC)
            idle = (
                current is not None
                and current.status == "active"
                and current.active_session_id == session.session_id
                and latest_message == snapshot_message
            )
        return idle and not await self._has_ongoing_work(conversation)

    async def _has_ongoing_work(self, conversation: ConversationModel) -> bool:
        """Use the ongoing-work lifecycle sources, not just the parent turn."""
        conversation_id = conversation.conversation_id
        async with self._session_factory() as db:
            if await list_active_delegation_sessions(
                db,
                user_email=conversation.user_email,
                controller_conversation_id=conversation_id,
                limit=1,
            ) or await list_active_tasks_by_source_conversation(
                db,
                created_by=conversation.user_email,
                controller_conversation_id=conversation_id,
                limit=1,
            ):
                return True
            links = await list_open_managed_conversation_links(
                db,
                user_email=conversation.user_email,
                controller_conversation_id=conversation_id,
                limit=501,
            )
        # A full bounded page cannot prove that the remainder is idle.
        if len(links) == 501:
            return True
        for link in links:
            if (
                link.turn_state in {"queued", "running"}
                or await self._scheduler.durable_running_turn_state(link.target_conversation_id)
                is not None
                or await self._scheduler.get_queued_messages(link.target_conversation_id)
            ):
                return True
        # Shell jobs can survive their turn and controller session. Query all
        # connected executors, and filter by owner plus conversation, not session.
        try:
            handles = await self._providers.executor.list_active()

            async def has_shell(handle: Any) -> bool:
                connection = await self._providers.executor.get_executor(handle)
                payload = await asyncio.wait_for(
                    connection.background_shell_status(include_completed=False), timeout=2.0
                )
                if not isinstance(payload, dict) or not isinstance(payload.get("shells"), list):
                    return True
                return any(
                    isinstance(shell, dict)
                    and shell.get("user_email") == conversation.user_email
                    and shell.get("conversation_id") == conversation_id
                    and shell.get("status", "running") in {"running", "queued"}
                    for shell in payload["shells"]
                )

            return any(await asyncio.gather(*(has_shell(handle) for handle in handles)))
        except Exception:
            # Unknown background state must not publish a premature automatic recap.
            logger.debug("Automatic recap deferred: background status unavailable", exc_info=True)
            return True

    async def _generate_in_context(
        self,
        conversation: ConversationModel,
        session: SessionModel,
        *,
        auto: bool,
        lease_holder: list[Lease],
        lost_lease: asyncio.Event,
    ) -> str:
        lock = self._locks.setdefault(conversation.conversation_id, asyncio.Lock())
        async with lock:
            if auto and not await self._auto_still_idle(conversation, session):
                return "Conversation is busy."
            if self._scheduler.has_active_turn(conversation.conversation_id):
                return "Wait for the current turn to finish before recapping."
            streams: list[tuple[str, list[dict[str, Any]]]] = []
            current = session
            for _ in range(6):
                streams.append(
                    (current.session_id, await self._read_recent_events(current.session_id))
                )
                if not current.previous_session_id:
                    break
                async with self._session_factory() as db:
                    previous = await get_session_row(db, current.previous_session_id)
                if previous is None or previous.conversation_id != conversation.conversation_id:
                    break
                from cognis.core.session import _to_session_model

                current = _to_session_model(previous)
            streams.reverse()
            flattened = [(sid, event) for sid, events in streams for event in events]
            source, last_marker = _source_since_marker(flattened)
            if not source:
                return "Nothing new to recap."
            if auto:
                last_skip = next(
                    (
                        event
                        for _sid, event in reversed(flattened)
                        if _recap_marker(event) and not _published_recap_marker(event)
                    ),
                    None,
                )
                if isinstance(last_skip, dict):
                    skipped_data = last_skip.get("data")
                    if (
                        isinstance(skipped_data, dict)
                        and skipped_data.get("policy_version") == RECAP_POLICY_VERSION
                        and source[-1][0] == skipped_data.get("source_session_id")
                        and source[-1][1].get("seq") == skipped_data.get("source_seq")
                    ):
                        return "No new activity since the last recap check."
            if auto and last_marker:
                timestamp = last_marker.get("timestamp") or last_marker.get("created_at")
                try:
                    if (
                        timestamp
                        and datetime.now(UTC)
                        - datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                        < AUTO_COOLDOWN
                    ):
                        return "Recap cooldown is active."
                except ValueError:
                    pass
            new_messages, new_stats = _evidence(source)
            substantive_reply = any(
                message["role"] == "Assistant" and len(message["content"]) >= 500
                for message in new_messages
            )
            if (
                auto
                and new_stats["user_turns"] < 3
                and not (
                    new_stats["deliverables"]
                    or new_stats["artifacts"]
                    or new_stats["files"]
                    or substantive_reply
                )
            ):
                return "Not enough new activity to recap."
            window = _recent_window(flattened)
            messages, stats = _evidence(window)
            if not messages:
                return "Nothing new to recap."
            if not any(message["role"] == "User" for message in messages):
                # Bounded Intaris paging can still lose the originating
                # request in exceptionally long tool-heavy turns. Do not
                # publish a recap of an assistant answer without its task.
                return "Recent user request is outside the recap history window."
            work_focus = _work_focus(window)
            completed_change = auto and work_focus is not None and work_focus == _work_focus(source)
            source_session, last_source = source[-1]
            source_seq = last_source.get("seq")
            if not isinstance(source_seq, int) or source_seq <= 0:
                return "Conversation history is not ready for a recap."
            # Claude Code uses session memory plus recent messages. Cognis
            # already has a compaction summary for rotated/compacted sessions;
            # a previous recap is presentation output, not session memory.
            memory = ""
            for _sid, past_event in reversed(flattened):
                if past_event.get("type") == "compaction_summary":
                    past_data = past_event.get("data")
                    if isinstance(past_data, dict):
                        memory = str(past_data.get("summary") or "")[:3000]
                    break
            evidence = "\n".join(f"{item['role']}: {item['content']}" for item in messages)
            focus = (
                "Most recent concrete work (from user and assistant messages, not tool output):\n"
                f"User request: {work_focus[0]}\n"
                f"Assistant outcome: {work_focus[1]}\n\n"
                if work_focus
                else ""
            )
            context = (
                f"Existing session compaction summary for background only "
                f"(not new completed work): {memory}\n\n"
                if memory
                else ""
            )
            new_evidence = "\n".join(f"{item['role']}: {item['content']}" for item in new_messages)
            if auto:
                instruction = (
                    "Decide conservatively whether NEW activity after the previous published "
                    "recap contains COMPLETED, meaningful work that the user would benefit "
                    "from remembering a week later. This is not a turn summary. A short answer "
                    "to a question, conversation about how a feature might "
                    "work, a status update, or a proposed next step without a completed outcome "
                    "does NOT qualify, regardless of the number of turns or tools. Completed "
                    "research with substantial findings, an implemented change, a finished "
                    "document, or a settled consequential decision can qualify even with no "
                    "mutations. A final merge or push status alone is not the work: when earlier "
                    "assistant messages report implemented fixes in this window, name those "
                    "fixes first. If uncertain, do not publish. Never paraphrase just the last "
                    "assistant message or repeat an earlier recap. Reply ONLY with a JSON "
                    'object: {"publish": false, "summary": ""} for a non-qualifying interval, '
                    'or {"publish": true, "summary": "1-3 concise sentences about the '
                    'completed result and current next step"} when it qualifies. Base the '
                    "summary on the recent conversation window, not just its last reply. "
                    "Do not treat session background as new work. Do not invent outcomes. "
                    "Use the conversation's language.\n\n"
                )
            else:
                instruction = (
                    "The user requested a mid-term recap of the recent conversation window. "
                    "Use session background only for orientation; describe current work and its "
                    "state rather than claiming old work happened again. Name the actual "
                    "technical changes before mentioning a merge, push, or deployment status. "
                    "In 1–3 short sentences identify what was completed or decided and the current "
                    "next step or blocker. Distinguish completed "
                    "work from attempted or pending work. Do not invent outcomes; the interface "
                    "presents files, artifacts, and deliverables separately. If there is nothing "
                    "worth remembering, respond ONLY with NO_RECAP. Use the conversation's "
                    "language.\n\n"
                )
            prompt = (
                instruction
                + context
                + focus
                + "Recent conversation (scope for prose and activity):\n"
                + evidence
                + (
                    "\n\nNew activity to assess for automatic recap:\n" + new_evidence
                    if auto
                    else ""
                )
            )
            response = await self._providers.llm.generate(
                [{"role": "user", "content": prompt}],
                task_type="auxiliary_generation",
                temperature=0,
                max_tokens=250,
                max_retries=1,
                acting_user_email=conversation.user_email,
                cognis_conversation_id=conversation.conversation_id,
                cognis_session_id=session.session_id,
                cognis_agent_id=conversation.agent_id,
                cognis_project_id=conversation.project_id,
            )
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            if message.get("tool_calls") or message.get("function_call"):
                raise RuntimeError("Recap model proposed a tool call; execution denied")
            output = extract_visible_text_from_response(response).strip()
            if not output:
                raise RuntimeError("Recap model returned no visible text")
            if auto:
                try:
                    decision = extract_json_object(output, label="auto_recap")
                except ValueError as exc:
                    raise RuntimeError("Automatic recap model returned no valid decision") from exc
                if type(decision.get("publish")) is not bool:
                    raise RuntimeError("Automatic recap model returned an invalid decision")
                skipped = decision["publish"] is False
                summary = decision.get("summary")
                if skipped and completed_change:
                    # The recent user request and assistant outcome bracket a
                    # recorded, successful change. Ask for an outcome summary
                    # rather than letting a conservative classifier bury it.
                    recovery_response = await self._providers.llm.generate(
                        [
                            {
                                "role": "user",
                                "content": (
                                    "Summarize the concrete technical work reported below in "
                                    "1-3 concise sentences. Lead with what changed; a merge or "
                                    "push is secondary. Use only user/assistant evidence. If the "
                                    "assistant explicitly says all changes failed or were undone, "
                                    "respond ONLY NO_RECAP. Never invent outcomes. Use the "
                                    "conversation's language.\n\n" + focus + evidence
                                ),
                            }
                        ],
                        task_type="auxiliary_generation",
                        temperature=0,
                        max_tokens=250,
                        max_retries=1,
                        acting_user_email=conversation.user_email,
                        cognis_conversation_id=conversation.conversation_id,
                        cognis_session_id=session.session_id,
                        cognis_agent_id=conversation.agent_id,
                        cognis_project_id=conversation.project_id,
                    )
                    recovery_message = (
                        ((recovery_response.get("choices") or [{}])[0]).get("message") or {}
                    )
                    if recovery_message.get("tool_calls") or recovery_message.get("function_call"):
                        raise RuntimeError("Recap model proposed a tool call; execution denied")
                    recovered = extract_visible_text_from_response(recovery_response).strip()
                    if recovered and recovered != "NO_RECAP":
                        skipped = False
                        summary = recovered
                if auto:
                    logger.info(
                        "Automatic recap decision",
                        extra={
                            "conversation_id": conversation.conversation_id,
                            "publish": not skipped,
                            "completed_change": completed_change,
                            "changed_files": len(new_stats["files"]),
                        },
                    )
                if not skipped and (not isinstance(summary, str) or not summary.strip()):
                    raise RuntimeError("Automatic recap model returned no summary")
                text = summary.strip()[:650] if not skipped and isinstance(summary, str) else ""
            else:
                text = output[:650]
                skipped = text == "NO_RECAP"
            if auto and not await self._auto_still_idle(conversation, session):
                return "Conversation resumed before the recap was published."
            if (
                lease_holder
                and self._lease_store is not None
                and (lost_lease.is_set() or not await self._lease_store.is_current(lease_holder[0]))
            ):
                return "Recap ownership changed before publication."
            # A declined automatic recap and a requested recap are different
            # events even if they cover the same source watermark.
            digest = hashlib.sha256(
                f"recap:{conversation.conversation_id}:{source_session}:{source_seq}".encode()
            ).hexdigest()
            key = (
                f"{session.session_id}:recap:{digest}:"
                f"{'skip:v' + str(RECAP_POLICY_VERSION) if skipped else 'publish'}"
            )
            event = SessionEvent(
                type="lifecycle",
                data={
                    "event": "conversation_recap_skipped" if skipped else "conversation_recap",
                    "text": "" if skipped else text,
                    "source_session_id": source_session,
                    "source_seq": source_seq,
                    "stats": stats if not skipped else {},
                    "stats_version": 3,
                    "scope": "recent_window",
                    "window_message_limit": _RECENT_MESSAGE_WINDOW,
                    "policy_version": RECAP_POLICY_VERSION,
                    "auto": auto,
                },
            )
            result = await self._providers.guardrails.record_events(
                session.session_id,
                [event],
                source="cognis_recap",
                idempotency_key=key,
                user_email=conversation.user_email,
                agent_id=conversation.agent_id,
            )
            if not result.ok:
                raise RuntimeError("Could not persist conversation recap")
            await self._session_cache.append_recorded_events(session, [event], result)
            signals = getattr(self._scheduler, "cluster_signals", None)
            if signals is not None:
                await signals.publish_chat_change(
                    conversation.conversation_id,
                    session_id=session.session_id,
                    revision=result.last_seq,
                )
            return "Nothing worth recapping yet." if skipped else text

    async def _read_recent_events(self, session_id: str) -> list[dict[str, Any]]:
        """Find the request behind a long tool-heavy turn without unbounded reads."""
        page = await self._providers.guardrails.read_events(
            session_id, last_n=350, allow_missing_stream=True
        )
        events: list[dict[str, Any]] = list(page.events)
        for _ in range(3):
            if (
                not getattr(page, "has_more", False)
                or len(_conversation_message_positions([(session_id, event) for event in events]))
                >= _RECENT_MESSAGE_WINDOW
            ):
                break
            first_seq = events[0].get("seq") if events else None
            if not isinstance(first_seq, int) or first_seq <= 1:
                break
            page = await self._providers.guardrails.read_events(
                session_id, before_seq=first_seq, limit=350, allow_missing_stream=True
            )
            if not page.events:
                break
            events = [*page.events, *events]
        return events
