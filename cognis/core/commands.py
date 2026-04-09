"""Transport-agnostic slash command dispatch.

CommandDispatcher handles all slash commands (``/compact``, ``/new``,
``/model``, ``/thinking``, ``/context``, ``/info``, ``/lsp``, ``/help``,
``/approve``, ``/deny``) without any dependency on WebSocket or other
transport layers.

Each command returns a ``CommandResult`` that the transport layer renders
into its native format (WS JSON, REST response, CLI output, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cognis.core.agent_loop import PauseResolution
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel, SessionModel

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CommandResult:
    """Result of a slash command execution.

    The ``type`` field indicates how the transport layer should render
    the result:
    - ``system_message``: display ``text`` as a system message
    - ``session_compacted``: session was compacted, ``data`` has details
    - ``conversation_created``: new conversation created, ``data`` has IDs
    - ``session_reset``: session was reset, ``data`` has IDs
    - ``error``: command failed, ``text`` has the error message
    - ``queued``: message queued (escalation pending), ``data`` has reason
    """

    type: str
    text: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# CommandDispatcher
# ---------------------------------------------------------------------------


class CommandDispatcher:
    """Transport-agnostic slash command handler.

    Call ``dispatch()`` with the raw message content. If the content is
    a recognized slash command, the command is executed and a
    ``CommandResult`` is returned. If not a command, returns ``None``.
    """

    def __init__(
        self,
        *,
        session_factory: Any,
        session_manager: Any,
        session_cache: Any,
        compaction_strategy: Any,
        providers: Any,
        pause_waiter: Any,
        notification_service: Any,
        turn_scheduler: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._session_manager = session_manager
        self._session_cache = session_cache
        self._compaction_strategy = compaction_strategy
        self._providers = providers
        self._pause_waiter = pause_waiter
        self._notification_service = notification_service
        self._turn_scheduler = turn_scheduler

    async def dispatch(
        self,
        command: str,
        *,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        user_email: str,
        has_active_turn: bool = False,
    ) -> CommandResult | None:
        """Dispatch a slash command. Returns None if not a command."""
        stripped = command.strip()
        if not stripped.startswith("/"):
            return None

        # /compact or /summarize
        if stripped in ("/compact", "/summarize"):
            if has_active_turn:
                return CommandResult(
                    type="error",
                    text="Cannot compact while a turn is active. Wait for it to finish or cancel it.",
                    data={"code": "turn_active"},
                )
            return await self._handle_compact(conversation, session)

        # /new, /reset, /clear
        if stripped in ("/new", "/reset", "/clear"):
            if has_active_turn:
                return CommandResult(
                    type="error",
                    text="Cannot reset while a turn is active. Wait for it to finish or cancel it.",
                    data={"code": "turn_active"},
                )
            return await self._handle_new(conversation, session, agent, user_email)

        # /context
        if stripped == "/context":
            return await self._handle_context(session)

        # /info
        if stripped == "/info":
            return await self._handle_info(session)

        # /model [name]
        if stripped == "/model" or stripped.startswith("/model "):
            arg = stripped[6:].strip() if len(stripped) > 6 else ""
            return await self._handle_model(session, arg)

        # /thinking [level]
        if stripped == "/thinking" or stripped.startswith("/thinking "):
            arg = stripped[9:].strip() if len(stripped) > 9 else ""
            return await self._handle_thinking(session, arg)

        # /lsp
        if stripped == "/lsp":
            return await self._handle_lsp()

        # /help
        if stripped == "/help":
            return self._handle_help()

        # /approve [note] or /deny [note]
        if stripped.startswith("/approve") or stripped.startswith("/deny"):
            is_approve = stripped.startswith("/approve")
            cmd_word = "/approve" if is_approve else "/deny"
            note = stripped[len(cmd_word) :].strip() or None
            return await self._handle_approve_deny(conversation, is_approve, note, user_email)

        # /stop or /cancel
        if stripped in ("/stop", "/cancel"):
            return await self._handle_stop(conversation)

        # Not a recognized command
        return None

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _handle_compact(
        self, conversation: ConversationModel, session: SessionModel
    ) -> CommandResult:
        """Handle /compact or /summarize."""
        conversation_id = conversation.conversation_id

        try:
            compaction_result = await self._compaction_strategy.compact(session, trigger="manual")
        except Exception:
            logger.exception(
                "Command /compact failed",
                extra={"extra_data": {"session_id": session.session_id}},
            )
            return CommandResult(
                type="error",
                text="Compaction failed. Try again or continue chatting.",
                data={"code": "compaction_failed"},
            )

        if not compaction_result.compacted:
            return CommandResult(
                type="system_message",
                text="Not enough conversation history to compact.",
            )

        # Mark current session as completed (deferred creation)
        await self._session_manager.mark_completed(
            session.session_id,
            result_summary=f"Compacted ({compaction_result.method})",
            completion_reason="compacted",
        )

        summary_preview = (compaction_result.summary or "")[:500]
        return CommandResult(
            type="session_compacted",
            text="Conversation history compacted.",
            data={
                "conversation_id": conversation_id,
                "session_id": session.session_id,
                "previous_session_id": session.session_id,
                "summary_preview": summary_preview,
                "method": compaction_result.method,
                "turns_compacted": compaction_result.turns_compacted,
            },
        )

    async def _handle_new(
        self,
        conversation: ConversationModel,
        session: SessionModel,
        agent: AgentDefinition,
        user_email: str,
    ) -> CommandResult:
        """Handle /new, /reset, or /clear."""
        conversation_id = conversation.conversation_id

        if conversation.context.type == "web":
            # Web context: create a new conversation entirely
            try:
                (
                    new_conversation,
                    new_session,
                ) = await self._session_manager.create_conversation_with_root_session(
                    user_email=user_email,
                    agent_id=agent.agent_id,
                    context=conversation.context,
                    intention=f"New conversation with {agent.name}",
                )
            except Exception:
                logger.exception("Command /new failed to create conversation")
                return CommandResult(
                    type="error",
                    text="Could not create a new conversation.",
                    data={"code": "creation_failed"},
                )

            # Mark old session completed
            await self._session_manager.mark_completed(
                session.session_id,
                result_summary="User started new conversation",
                completion_reason="user_reset",
            )

            return CommandResult(
                type="conversation_created",
                data={
                    "conversation_id": new_conversation.conversation_id,
                    "old_conversation_id": conversation_id,
                },
            )
        else:
            # Channel-bound: create new root session within same conversation
            try:
                new_session = await self._session_manager.rotate_session(
                    conversation_id=conversation_id,
                    current_session=session,
                    intention=f"Conversation with {agent.name}",
                    completion_reason="user_reset",
                )
            except Exception:
                logger.exception("Command /new failed to rotate session")
                return CommandResult(
                    type="error",
                    text="Could not create a new session.",
                    data={"code": "creation_failed"},
                )

            return CommandResult(
                type="session_reset",
                data={
                    "conversation_id": conversation_id,
                    "session_id": new_session.session_id,
                    "previous_session_id": session.session_id,
                },
            )

    async def _handle_context(self, session: SessionModel) -> CommandResult:
        """Handle /context — display context window usage."""
        usage = self._session_cache.get_context_usage(session.session_id)

        if usage is None:
            return CommandResult(
                type="system_message",
                text="Context usage: no data yet (send a message first).",
            )

        lines = [f"Model: {usage['model']}"]
        try:
            model_info = await self._providers.llm.get_model_info(usage["model"])
            lines.append(f"Model context window: {model_info.context_window:,} tokens")
        except Exception:
            pass
        lines.extend(
            [
                f"Session configured cap: {usage['max_context_tokens']:,} tokens",
                f"Current usage: {usage['prompt_tokens']:,} tokens ({usage['percentage']}% of session cap)",
                f"Compaction threshold: {int(self._compaction_strategy.compaction_threshold * 100)}%",
            ]
        )
        return CommandResult(type="system_message", text="\n".join(lines))

    async def _handle_info(self, session: SessionModel) -> CommandResult:
        """Handle /info — display session details and statistics."""
        lines: list[str] = []

        # Session metadata
        lines.append(f"Session: {session.session_id}")
        lines.append(f"Agent: {session.agent_id}")
        lines.append(f"Status: {session.status}")

        # Context usage + model + reasoning effort
        usage = self._session_cache.get_context_usage(session.session_id)
        if usage:
            lines.append(f"Model: {usage['model']}")
            try:
                model_info = await self._providers.llm.get_model_info(usage["model"])
                lines.append(f"Model context window: {model_info.context_window:,} tokens")
            except Exception:
                pass
            lines.append(f"Session configured cap: {usage['max_context_tokens']:,} tokens")
            lines.append(
                f"Current usage: {usage['prompt_tokens']:,} tokens ({usage['percentage']}% of session cap)"
            )
        reasoning = self._session_cache.get_reasoning_effort_override(session.session_id)
        if reasoning:
            lines.append(f"Reasoning effort: {reasoning}")

        # Intaris session stats
        intaris_sid = session.intaris_session_id or session.session_id
        try:
            intaris_session = await self._providers.guardrails.get_session(intaris_sid)
            if intaris_session.intention:
                lines.append(f"Intention: {intaris_session.intention}")
            stats_parts = [f"{intaris_session.total_calls} total"]
            if intaris_session.approved_count:
                stats_parts.append(f"{intaris_session.approved_count} approved")
            if intaris_session.denied_count:
                stats_parts.append(f"{intaris_session.denied_count} denied")
            if intaris_session.escalated_count:
                stats_parts.append(f"{intaris_session.escalated_count} escalated")
            lines.append(f"Tool calls: {', '.join(stats_parts)}")
        except Exception:
            lines.append("Intaris stats: unavailable")

        if session.started_at:
            lines.append(f"Started: {session.started_at}")

        return CommandResult(type="system_message", text="\n".join(lines))

    async def _handle_model(self, session: SessionModel, arg: str) -> CommandResult:
        """Handle /model [name] — list or switch LLM model."""
        session_id = session.session_id

        if not arg:
            # List available models
            try:
                model_ids = await self._providers.llm.list_model_ids()
            except Exception:
                model_ids = []

            current = self._session_cache.get_model_override(session_id)
            if not current:
                usage = self._session_cache.get_context_usage(session_id)
                current = usage["model"] if usage else None

            if not model_ids:
                return CommandResult(
                    type="system_message",
                    text="No models configured. Add LLM providers in Settings → Providers.",
                )

            lines = ["Available models:"]
            for mid in model_ids:
                marker = " *" if mid == current else ""
                lines.append(f"  {mid}{marker}")
            lines.append(f"\nCurrent: {current or 'system default'}")
            lines.append("Usage: /model <model_name>")
            return CommandResult(type="system_message", text="\n".join(lines))

        # Switch model
        try:
            model_ids = await self._providers.llm.list_model_ids()
        except Exception:
            model_ids = []

        if model_ids and arg not in model_ids:
            return CommandResult(
                type="system_message",
                text=f"Unknown model: {arg}\nAvailable: {', '.join(model_ids)}",
            )

        self._session_cache.set_model_override(session_id, arg)
        return CommandResult(
            type="system_message",
            text=f"Model switched to: {arg}\nTakes effect on next message.",
        )

    async def _handle_thinking(self, session: SessionModel, arg: str) -> CommandResult:
        """Handle /thinking [level] — list or switch reasoning effort."""
        session_id = session.session_id

        # Determine current model for effort level inference
        current_model = self._session_cache.get_model_override(session_id)
        if not current_model:
            usage = self._session_cache.get_context_usage(session_id)
            current_model = usage["model"] if usage else ""

        # Get supported effort levels
        try:
            if current_model:
                model_info = await self._providers.llm.get_model_info(current_model)
                available = model_info.reasoning_efforts if model_info.reasoning_efforts else []
            else:
                available = []
        except Exception:
            available = []

        if not available and current_model:
            available = _infer_reasoning_efforts(current_model)

        current_effort = self._session_cache.get_reasoning_effort_override(session_id)

        if not arg:
            lines = []
            if current_effort:
                lines.append(f"Current reasoning effort: {current_effort}")
            else:
                lines.append("Reasoning effort: default (not set)")
            if available:
                lines.append(f"Available levels: {', '.join(available)}")
            else:
                lines.append("No reasoning effort levels available for current model.")
            lines.append("Usage: /thinking <level>  (use 'off' to reset to default)")
            return CommandResult(type="system_message", text="\n".join(lines))

        # Reset
        if arg in ("off", "default", "reset", "none"):
            self._session_cache.set_reasoning_effort_override(session_id, None)
            return CommandResult(
                type="system_message",
                text="Reasoning effort reset to default.",
            )

        # Validate
        if available and arg not in available:
            return CommandResult(
                type="system_message",
                text=f"Unsupported level: {arg}\nAvailable: {', '.join(available)}",
            )

        self._session_cache.set_reasoning_effort_override(session_id, arg)
        return CommandResult(
            type="system_message",
            text=f"Reasoning effort set to: {arg}\nTakes effect on next message.",
        )

    async def _handle_lsp(self) -> CommandResult:
        """Handle /lsp — display LSP diagnostics subsystem status."""
        lines: list[str] = []

        executor = self._providers.executor
        lsp_managers = executor.get_lsp_managers() if hasattr(executor, "get_lsp_managers") else []

        if not lsp_managers:
            lines.append("LSP Diagnostics")
            lines.append("  Status: no active LSP managers")
            return CommandResult(type="system_message", text="\n".join(lines))

        for lsp_mgr in lsp_managers:
            status = lsp_mgr.status()
            cfg = status["config"]
            totals = status["totals"]

            lines.append("LSP Diagnostics")
            lines.append(f"  Status: {'enabled' if cfg['enabled'] else 'disabled'}")
            lines.append(f"  Auto-install: {'enabled' if cfg['auto_install'] else 'disabled'}")
            lines.append(f"  Timeout: {cfg['diagnostics_timeout_ms']}ms")
            lines.append(f"  Max servers: {cfg['max_concurrent_servers']}")

            active = status["active_servers"]
            if active:
                lines.append(
                    f"\nActive servers ({totals['active_server_count']}/{cfg['max_concurrent_servers']}):"
                )
                for srv in active:
                    pid_str = f"PID {srv['pid']}" if srv["pid"] else "no PID"
                    alive_str = "" if srv["alive"] else " [dead]"
                    lines.append(f"  {srv['server_name']} ({pid_str}{alive_str})")
                    lines.append(f"    Root: {srv['root_path']}")
                    lines.append(
                        f"    Files: {srv['file_count']}, "
                        f"diagnostics: {srv['error_count']} errors, "
                        f"{srv['warning_count']} warnings"
                    )
                    idle = srv["idle_seconds"]
                    if idle >= 60:
                        lines.append(f"    Idle: {idle // 60}m {idle % 60}s")
                    else:
                        lines.append(f"    Idle: {idle}s")
            else:
                lines.append("\nNo active servers")

            broken = status["broken_servers"]
            if broken:
                lines.append(f"\nBroken servers ({len(broken)}):")
                for brk in broken:
                    retry = brk["retry_in_seconds"]
                    retry_str = f"{retry // 60}m {retry % 60}s" if retry >= 60 else f"{retry}s"
                    lines.append(f"  {brk['client_key']} (retry in {retry_str})")

            if status["spawning_count"] > 0:
                lines.append(f"\nSpawning: {status['spawning_count']} server(s)")

            lines.append(
                f"\nTotals: {totals['files_tracked']} files tracked, "
                f"{totals['total_errors']} errors, {totals['total_warnings']} warnings"
            )

            # Available servers
            try:
                avail = await lsp_mgr.available_servers()
                if avail:
                    lines.append("\nAvailable servers:")
                    for srv in avail:
                        if srv["active"]:
                            status_str = "active"
                        elif srv["available"]:
                            status_str = srv["path"]
                        elif srv["has_auto_install"]:
                            status_str = "not found (auto-install available)"
                        else:
                            status_str = "not found"
                        lines.append(f"  {srv['server_id']} ({srv['extensions']}) — {status_str}")
            except Exception:
                pass  # Best-effort

        return CommandResult(type="system_message", text="\n".join(lines))

    def _handle_help(self) -> CommandResult:
        """Handle /help — show available slash commands."""
        return CommandResult(type="system_message", text=_HELP_TEXT)

    async def _handle_approve_deny(
        self,
        conversation: ConversationModel,
        is_approve: bool,
        note: str | None,
        user_email: str,
    ) -> CommandResult:
        """Handle /approve [note] or /deny [note]."""
        conversation_id = conversation.conversation_id
        esc_decision = "approve" if is_approve else "deny"

        pending = self._pause_waiter.find_pending(
            pause_type="escalation",
            conversation_id=conversation_id,
        )
        if pending is None:
            return CommandResult(
                type="system_message",
                text="No pending escalation to resolve.",
            )

        tool_name = (pending.context or {}).get("tool_name", "tool call")

        # Use the unified notification service
        if self._notification_service is not None:
            resolved = await self._notification_service.resolve(
                pending.pause_id,
                esc_decision,
                {"note": note or ""},
                user_email=user_email,
            )
            if not resolved:
                return CommandResult(
                    type="error",
                    text="Could not resolve the pending escalation. It may already be resolved or Intaris rejected the approval update.",
                    data={"code": "escalation_resolve_failed", "call_id": pending.pause_id},
                )
        else:
            # Legacy fallback
            self._pause_waiter.resolve(
                pending.pause_id,
                PauseResolution(
                    decision=esc_decision,
                    data={"note": note or ""},
                ),
            )
            import contextlib

            with contextlib.suppress(Exception):
                intaris_call_id = (pending.context or {}).get("call_id", pending.pause_id)
                await self._providers.guardrails.submit_decision(
                    intaris_call_id, esc_decision, note
                )

        verb = "approved" if is_approve else "denied"
        note_suffix = f": {note}" if note else ""
        return CommandResult(
            type="system_message",
            text=f"User {verb} {tool_name}{note_suffix}",
        )

    async def _handle_stop(self, conversation: ConversationModel) -> CommandResult:
        """Handle /stop or /cancel by aborting active work immediately."""
        conversation_id = conversation.conversation_id
        stopped_anything = False

        if self._turn_scheduler is not None:
            stopped_anything = await self._turn_scheduler.cancel_turn(conversation_id)

        pending_pauses = self._pause_waiter.list_pending(conversation_id=conversation_id)
        for pause in pending_pauses:
            if pause.pause_type == "step_question" and pause.task_id is None:
                if self._notification_service is not None:
                    await self._notification_service.resolve(
                        pause.pause_id,
                        "cancel",
                        {"reason": "user_stop"},
                    )
                else:
                    self._pause_waiter.resolve(
                        pause.pause_id,
                        PauseResolution(decision="cancel", data={"reason": "user_stop"}),
                    )
                stopped_anything = True
            elif pause.pause_type == "escalation":
                if self._notification_service is not None:
                    await self._notification_service.resolve(
                        pause.pause_id,
                        "deny",
                        {"note": "Stopped by user"},
                    )
                else:
                    self._pause_waiter.resolve(
                        pause.pause_id,
                        PauseResolution(decision="deny", data={"note": "Stopped by user"}),
                    )
                stopped_anything = True

        if not stopped_anything:
            return CommandResult(
                type="system_message",
                text="No active work to stop.",
            )

        return CommandResult(
            type="system_message",
            text="Stopped the current work and cleared any live clarification wait.",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
Available commands:
  /help              Show this help message
  /lsp               Show LSP diagnostics status
  /model [name]      List available models or switch model
  /thinking [level]  Show or set reasoning effort (low/medium/high)
  /context           Show context window usage
  /info              Show session details and statistics
  /compact           Compact conversation history
  /summarize         Alias for /compact
  /new               Start a new conversation
  /reset             Alias for /new
  /clear             Alias for /new
  /stop              Stop the current work immediately
  /cancel            Alias for /stop
  /approve [note]    Approve pending tool escalation
  /deny [note]       Deny pending tool escalation"""


_DEFAULT_REASONING_EFFORTS: dict[str, list[str]] = {
    "anthropic": ["low", "medium", "high"],
    "openai": ["low", "medium", "high"],
}


def _infer_reasoning_efforts(model: str) -> list[str]:
    """Best-effort reasoning effort levels for a model."""
    m = model.lower()
    if "opus" in m:
        return ["low", "medium", "high", "max"]
    if any(p in m for p in ("claude", "anthropic")):
        return ["low", "medium", "high"]
    if any(p in m for p in ("o1", "o3", "o4")):
        return ["low", "medium", "high"]
    if "gpt-5" in m:
        return ["none", "low", "medium", "high"]
    return ["low", "medium", "high"]
