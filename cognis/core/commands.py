"""Transport-agnostic slash command dispatch.

CommandDispatcher handles all slash commands (``/compact``, ``/new``,
``/model``, ``/thinking``, ``/context``, ``/info``, ``/lsp``, ``/help``,
``/approve``, ``/deny``, ``/retry``, ``/continue``) without any dependency on
WebSocket or other
transport layers.

Each command returns a ``CommandResult`` that the transport layer renders
into its native format (WS JSON, REST response, CLI output, etc.).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from cognis.core.agent_loop import PauseResolution
from cognis.core.notifications import NotificationType
from cognis.logging import get_logger
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel, SessionModel
from cognis.providers.llm.reasoning import (
    normalize_reasoning_effort,
    reasoning_efforts_for_model,
    remap_reasoning_effort_to_available,
)

logger = get_logger(__name__)


def _find_gate_revise_action(pause: Any) -> str | None:
    """Return the first revise(...) action available on a pending gate."""

    for option in pause.options or []:
        if not isinstance(option, dict):
            continue
        action = option.get("action")
        if isinstance(action, str) and action.startswith("revise(") and action.endswith(")"):
            return action
    return None


def _gate_offers_action(pause: Any, action: str) -> bool:
    """Return whether a pending gate explicitly offers an action."""

    for option in pause.options or []:
        if not isinstance(option, dict):
            continue
        option_action = option.get("action")
        if option_action == action:
            return True
    return False


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
        has_busy_turn: bool | None = None,
    ) -> CommandResult | None:
        """Dispatch a slash command. Returns None if not a command."""
        stripped = command.strip()
        if not stripped.startswith("/"):
            return None
        stripped = f"/{stripped[1:].lstrip()}"
        has_busy_turn = has_active_turn if has_busy_turn is None else has_busy_turn

        # /compact or /summarize
        if stripped in ("/compact", "/summarize"):
            if has_busy_turn:
                return CommandResult(
                    type="error",
                    text="Cannot compact while a turn is active. Wait for it to finish or cancel it.",
                    data={"code": "turn_active"},
                )
            return await self._handle_compact(conversation, session)

        # /new, /reset, /clear
        if stripped in ("/new", "/reset", "/clear"):
            if has_busy_turn:
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
            return await self._handle_info(
                session,
                has_active_turn=has_active_turn,
                has_busy_turn=has_busy_turn,
            )

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
            return await self._handle_lsp(user_email=user_email)

        # /help
        if stripped == "/help":
            return self._handle_help()

        # /approve [note] or /deny [note]
        if stripped.startswith("/approve") or stripped.startswith("/deny"):
            is_approve = stripped.startswith("/approve")
            cmd_word = "/approve" if is_approve else "/deny"
            note = stripped[len(cmd_word) :].strip() or None
            return await self._handle_approve_deny(conversation, is_approve, note, user_email)

        # /retry [note]
        if stripped == "/retry" or stripped.startswith("/retry "):
            note = stripped[len("/retry") :].strip() or None
            return await self._handle_gate_resolution(conversation, "retry", note, user_email)

        # /continue [note]
        if stripped == "/continue" or stripped.startswith("/continue "):
            note = stripped[len("/continue") :].strip() or None
            return await self._handle_gate_resolution(conversation, "continue", note, user_email)

        # /stop or /cancel [note]
        if stripped == "/stop" or stripped.startswith("/stop "):
            return await self._handle_stop(conversation)
        if stripped == "/cancel" or stripped.startswith("/cancel "):
            note = stripped[len("/cancel") :].strip() or None
            gate_result = await self._handle_gate_resolution(
                conversation, "cancel", note, user_email, allow_missing=True
            )
            if gate_result is not None:
                return gate_result
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
        model_info = await self._get_context_usage_model_info(usage)
        if model_info is not None:
            lines.append(f"Model context window: {model_info.context_window:,} tokens")
        self._append_context_usage_lines(lines, usage)
        return CommandResult(type="system_message", text="\n".join(lines))

    async def _handle_info(
        self,
        session: SessionModel,
        *,
        has_active_turn: bool = False,
        has_busy_turn: bool | None = None,
    ) -> CommandResult:
        """Handle /info — display session details and statistics."""
        from cognis.core.session import _to_session_model
        from cognis.store.queries import get_session_row, list_child_sessions

        current_session = session
        child_sessions: list[SessionModel] = []
        if self._session_factory is not None:
            async with self._session_factory() as db_session:
                session_row = await get_session_row(db_session, session.session_id)
                if session_row is not None:
                    current_session = _to_session_model(session_row)
                child_rows = await list_child_sessions(db_session, session.session_id)
                child_sessions = [_to_session_model(row) for row in child_rows]

        lines: list[str] = []

        if has_active_turn:
            display_status = "running"
        elif has_busy_turn:
            display_status = "settling"
        else:
            display_status = current_session.status
        lines.append(f"Session: {current_session.session_id}")
        lines.append(f"Agent: {current_session.agent_id}")
        lines.append(f"Status: {display_status}")
        lines.append(f"Session lifecycle: {current_session.status}")
        self._append_session_metadata(lines, current_session)

        # Context usage + model + thinking effort
        usage = self._session_cache.get_context_usage(current_session.session_id)
        if usage:
            lines.append(f"Model: {usage['model']}")
            model_info = await self._get_context_usage_model_info(usage)
            if model_info is not None:
                lines.append(f"Model context window: {model_info.context_window:,} tokens")
            provider_id = usage.get("provider_id")
            llm_provider = getattr(self._providers, "llm", None)
            has_drift = getattr(llm_provider, "has_hosted_instruction_drift", None)
            drift_reason_getter = getattr(llm_provider, "hosted_instruction_drift_reason", None)
            if (
                isinstance(provider_id, str)
                and provider_id
                and callable(has_drift)
                and has_drift(provider_id, usage["model"])
            ):
                drift_reason = (
                    drift_reason_getter(provider_id, usage["model"])
                    if callable(drift_reason_getter)
                    else None
                )
                detail = (
                    f" ({drift_reason})" if isinstance(drift_reason, str) and drift_reason else ""
                )
                lines.append(f"LLM diagnostics: provider reported hosted instruction drift{detail}")
            self._append_context_usage_lines(lines, usage)
        reasoning = self._session_cache.get_reasoning_effort_override(current_session.session_id)
        if reasoning:
            lines.append(f"Thinking effort: {reasoning}")
        tool_runtime = self._session_cache.get_tool_runtime_info(current_session.session_id)
        if tool_runtime:
            strategy = tool_runtime.get("strategy")
            if isinstance(strategy, str) and strategy:
                lines.append(f"Tool exposure mode: {strategy}")
            llm_api = tool_runtime.get("llm_api")
            if isinstance(llm_api, str) and llm_api:
                lines.append(f"LLM API: {llm_api}")
            discovery_mode = tool_runtime.get("discovery_mode")
            if isinstance(discovery_mode, str) and discovery_mode:
                lines.append(f"Tool discovery: {discovery_mode}")
            step_profile_id = tool_runtime.get("step_profile_id")
            step_profile_mode = tool_runtime.get("step_profile_mode")
            if isinstance(step_profile_id, str) and step_profile_id:
                if isinstance(step_profile_mode, str) and step_profile_mode:
                    lines.append(f"Step profile: {step_profile_id} ({step_profile_mode})")
                else:
                    lines.append(f"Step profile: {step_profile_id}")
            elif isinstance(step_profile_mode, str) and step_profile_mode:
                lines.append(f"Step profile mode: {step_profile_mode}")
            allow_tool_search = tool_runtime.get("allow_tool_search")
            if isinstance(allow_tool_search, bool):
                lines.append(f"Tool search: {'enabled' if allow_tool_search else 'disabled'}")
            inventory_tool_count = tool_runtime.get("inventory_tool_count")
            visible_tool_count = tool_runtime.get("visible_tool_count")
            hidden_searchable_count = tool_runtime.get("hidden_searchable_count")
            promoted_count = tool_runtime.get("promoted_count")
            if isinstance(visible_tool_count, int) and isinstance(inventory_tool_count, int):
                tool_summary = [f"{visible_tool_count} visible", f"{inventory_tool_count} eligible"]
                if isinstance(hidden_searchable_count, int):
                    tool_summary.append(f"{hidden_searchable_count} hidden")
                if isinstance(promoted_count, int) and promoted_count > 0:
                    tool_summary.append(f"{promoted_count} promoted")
                lines.append(f"Tools: {', '.join(tool_summary)}")

        # Intaris session stats
        intaris_sid = current_session.intaris_session_id or current_session.session_id
        lines.append(f"Intaris session: {intaris_sid}")
        try:
            intaris_session = await self._providers.guardrails.get_session(intaris_sid)
            lines.append(f"Intaris status: {intaris_session.status}")
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
            lines.append("Intaris status: unavailable")
            lines.append("Intaris stats: unavailable")

        if child_sessions:
            lines.append(f"Sub-sessions: {len(child_sessions)}")
            for index, child_session in enumerate(child_sessions, start=1):
                lines.append(
                    f"  [{index}] {child_session.session_id} ({child_session.status}, agent={child_session.agent_id})"
                )
                self._append_session_metadata(lines, child_session, indent="  ")

        if current_session.started_at:
            lines.append(f"Started: {current_session.started_at}")

        return CommandResult(type="system_message", text="\n".join(lines))

    async def _get_context_usage_model_info(self, usage: dict[str, Any]) -> Any | None:
        """Resolve model metadata for cached context usage."""

        if self._providers is None or getattr(self._providers, "llm", None) is None:
            return None
        provider_id = usage.get("provider_id")
        try:
            if provider_id:
                try:
                    return await self._providers.llm.get_model_info(
                        usage["model"], provider_id=provider_id
                    )
                except TypeError:
                    pass
            return await self._providers.llm.get_model_info(usage["model"])
        except Exception:
            return None

    def _append_context_usage_lines(self, lines: list[str], usage: dict[str, Any]) -> None:
        """Append cached context-usage diagnostics to a command response."""

        lines.append(f"Effective context window: {usage['max_context_tokens']:,} tokens")
        lines.append(
            f"Current usage: {usage['prompt_tokens']:,} tokens ({usage['percentage']}% of model window)"
        )

        reserve_output_tokens = usage.get("reserve_output_tokens")
        effective_reserve_output_tokens = usage.get(
            "effective_reserve_output_tokens", reserve_output_tokens
        )
        if isinstance(reserve_output_tokens, int):
            if (
                isinstance(effective_reserve_output_tokens, int)
                and effective_reserve_output_tokens != reserve_output_tokens
            ):
                lines.append(
                    "Requested output tokens: "
                    f"{reserve_output_tokens:,} (controller reserve: "
                    f"{effective_reserve_output_tokens:,} for prompt budgeting)"
                )
            else:
                lines.append(f"Requested output tokens: {reserve_output_tokens:,}")

        effective_prompt_budget = usage.get("effective_prompt_budget")
        if isinstance(effective_prompt_budget, int):
            lines.append(f"Effective prompt budget: {effective_prompt_budget:,} tokens")

        loop_pressure_threshold = usage.get("loop_pressure_threshold")
        if isinstance(loop_pressure_threshold, int):
            lines.append(f"Loop pressure threshold: {loop_pressure_threshold:,} tokens")

        compaction_threshold = getattr(self._compaction_strategy, "compaction_threshold", None)
        if isinstance(compaction_threshold, int | float):
            lines.append(f"Compaction threshold: {int(compaction_threshold * 100)}%")

        self._append_last_llm_usage_lines(lines, usage.get("last_llm_usage"))

    def _append_last_llm_usage_lines(self, lines: list[str], usage: dict[str, Any] | None) -> None:
        """Append provider-reported last-call token usage when available."""

        if not isinstance(usage, dict) or not usage:
            return

        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if all(
            isinstance(value, int) for value in (prompt_tokens, completion_tokens, total_tokens)
        ):
            lines.append(
                "Last LLM call tokens: "
                f"{prompt_tokens:,} prompt, {completion_tokens:,} completion, {total_tokens:,} total"
            )

        cache_lines_added = False
        cached_tokens = usage.get("cached_tokens")
        if isinstance(cached_tokens, int):
            lines.append(f"Last LLM call cached tokens: {cached_tokens:,}")
            cache_lines_added = True

        cache_read_tokens = usage.get("cache_read_input_tokens")
        if isinstance(cache_read_tokens, int):
            lines.append(f"Last LLM call cache read tokens: {cache_read_tokens:,}")
            cache_lines_added = True

        cache_creation_tokens = usage.get("cache_creation_input_tokens")
        if isinstance(cache_creation_tokens, int):
            lines.append(f"Last LLM call cache write tokens: {cache_creation_tokens:,}")
            cache_lines_added = True

        if not cache_lines_added:
            lines.append("Last LLM call cache details: not reported by provider")

    def _append_session_metadata(
        self,
        lines: list[str],
        session: SessionModel,
        *,
        indent: str = "",
    ) -> None:
        """Append operator-facing session metadata when it exists."""

        if session.parent_session_id:
            lines.append(f"{indent}Parent session: {session.parent_session_id}")
        if session.previous_session_id:
            lines.append(f"{indent}Previous session: {session.previous_session_id}")
        if session.delegation_mode:
            lines.append(f"{indent}Delegation mode: {session.delegation_mode}")
        if session.delegation_task:
            lines.append(f"{indent}Task summary: {session.delegation_task}")
        if session.result_summary:
            lines.append(f"{indent}Result summary: {session.result_summary}")
        if session.completion_reason:
            lines.append(f"{indent}Completion reason: {session.completion_reason}")

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
                lines.append(f"Current thinking effort: {current_effort}")
            else:
                lines.append("Thinking effort: default (not set)")
            if available:
                lines.append(f"Available levels: {', '.join(available)}")
            else:
                lines.append("No thinking effort levels available for current model.")
            lines.append("Usage: /thinking <level>  (use 'off' to reset to default)")
            return CommandResult(type="system_message", text="\n".join(lines))

        normalized_arg = normalize_reasoning_effort(arg)
        if normalized_arg is None:
            return CommandResult(
                type="system_message",
                text=(
                    f"Unsupported level: {arg}\nAvailable: {', '.join(available)}"
                    if available
                    else "Unsupported thinking effort."
                ),
            )

        # Reset
        if normalized_arg == "default":
            self._session_cache.set_reasoning_effort_override(session_id, None)
            return CommandResult(
                type="system_message",
                text="Thinking effort reset to default.",
            )

        resolved_arg = (
            remap_reasoning_effort_to_available(normalized_arg, available_efforts=available)
            if available
            else normalized_arg
        )
        if resolved_arg is None:
            return CommandResult(
                type="system_message",
                text=(
                    f"Unsupported level: {normalized_arg}\nAvailable: {', '.join(available)}"
                    if available
                    else "Unsupported thinking effort."
                ),
            )

        # Validate
        if available and resolved_arg not in available:
            return CommandResult(
                type="system_message",
                text=f"Unsupported level: {normalized_arg}\nAvailable: {', '.join(available)}",
            )

        self._session_cache.set_reasoning_effort_override(session_id, resolved_arg)
        mapped_note = f" (mapped from {normalized_arg})" if resolved_arg != normalized_arg else ""
        return CommandResult(
            type="system_message",
            text=f"Thinking effort set to: {resolved_arg}{mapped_note}\nTakes effect on next message.",
        )

    async def _handle_lsp(self, *, user_email: str | None = None) -> CommandResult:
        """Handle /lsp — display LSP diagnostics subsystem status."""
        lines: list[str] = []

        executor = self._providers.executor
        statuses = (
            await executor.get_lsp_statuses(owner_email=user_email)
            if hasattr(executor, "get_lsp_statuses")
            else []
        )

        if not statuses:
            lines.append("LSP Diagnostics")
            lines.append("  Status: no executor LSP status available")
            return CommandResult(type="system_message", text="\n".join(lines))

        lines.append("LSP Diagnostics")
        for status in statuses:
            cfg = status.config
            totals = status.totals
            lines.append("")
            lines.append(
                f"{status.executor_id or 'unknown'} ({status.executor_type or 'unknown'}) - {status.state}"
            )
            lines.append(f"  Enabled: {'yes' if status.enabled else 'no'}")
            lines.append(f"  Auto-install: {'yes' if cfg.auto_install else 'no'}")
            lines.append(f"  Timeout: {cfg.diagnostics_timeout_ms}ms")
            lines.append(f"  Max servers: {cfg.max_concurrent_servers}")
            if status.warnings:
                for warning in status.warnings:
                    lines.append(f"  Warning: {warning}")
            if status.state != "ready":
                continue

            active = status.active_servers
            if active:
                lines.append(
                    f"  Active servers ({totals.active_server_count}/{cfg.max_concurrent_servers}):"
                )
                for srv in active:
                    alive_str = "" if srv.alive else " [dead]"
                    lines.append(f"    {srv.server_name}{alive_str}")
                    lines.append(
                        f"      Files: {srv.file_count}, diagnostics: {srv.error_count} errors, {srv.warning_count} warnings"
                    )
                    idle = srv.idle_seconds
                    if idle >= 60:
                        lines.append(f"      Idle: {idle // 60}m {idle % 60}s")
                    else:
                        lines.append(f"      Idle: {idle}s")
            else:
                lines.append("  No active servers")

            broken = status.broken_servers
            if broken:
                lines.append(f"  Broken servers ({len(broken)}):")
                for brk in broken:
                    retry = brk.retry_in_seconds
                    retry_str = f"{retry // 60}m {retry % 60}s" if retry >= 60 else f"{retry}s"
                    lines.append(f"    {brk.client_key} (retry in {retry_str})")

            if status.spawning_count > 0:
                lines.append(f"  Spawning: {status.spawning_count} server(s)")

            lines.append(
                f"  Totals: {totals.files_tracked} files tracked, {totals.total_errors} errors, {totals.total_warnings} warnings"
            )

            if status.available_servers:
                lines.append("  Available servers:")
                for srv in status.available_servers:
                    if srv.active:
                        status_str = "active"
                    elif srv.available:
                        status_str = "installed"
                    elif srv.has_auto_install:
                        status_str = "not found (auto-install available)"
                    else:
                        status_str = "not found"
                    lines.append(f"    {srv.server_id} ({srv.extensions}) - {status_str}")

        return CommandResult(
            type="system_message", text="\n".join(line for line in lines if line is not None)
        )

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

    async def _handle_gate_resolution(
        self,
        conversation: ConversationModel,
        action: str,
        note: str | None,
        user_email: str,
        *,
        allow_missing: bool = False,
    ) -> CommandResult | None:
        """Resolve a pending workflow gate for the current conversation."""

        pending = await self._find_latest_gate_pause(conversation, user_email)
        if pending is None:
            if allow_missing:
                return None
            return CommandResult(
                type="system_message",
                text="No pending workflow gate to resolve.",
            )

        decision = action
        if action == "retry":
            decision = _find_gate_revise_action(pending) or ""
            if not decision:
                return CommandResult(
                    type="error",
                    text="This paused workflow gate does not offer a retry action.",
                    data={"code": "gate_retry_unavailable", "pause_id": pending.pause_id},
                )
        elif action in {"continue", "cancel"} and not _gate_offers_action(pending, action):
            return CommandResult(
                type="error",
                text=f"This paused workflow gate does not offer a {action} action.",
                data={"code": "gate_action_unavailable", "pause_id": pending.pause_id},
            )

        resolution_data = {"note": note or ""}
        if self._notification_service is not None:
            resolved = await self._notification_service.resolve(
                pending.pause_id,
                decision,
                resolution_data,
                user_email=user_email,
            )
            if not resolved:
                return CommandResult(
                    type="error",
                    text="Could not resolve the pending workflow gate. It may already be resolved.",
                    data={"code": "gate_resolve_failed", "pause_id": pending.pause_id},
                )
        else:
            ok = self._pause_waiter.resolve(
                pending.pause_id,
                PauseResolution(decision=decision, data=resolution_data),
            )
            if not ok:
                return CommandResult(
                    type="error",
                    text="Could not resolve the pending workflow gate. It may already be resolved.",
                    data={"code": "gate_resolve_failed", "pause_id": pending.pause_id},
                )

        note_suffix = f": {note}" if note else ""
        if action == "retry":
            return CommandResult(
                type="system_message",
                text=f"Retrying the paused workflow step{note_suffix}",
            )
        if action == "continue":
            return CommandResult(
                type="system_message",
                text=f"Continuing the paused workflow{note_suffix}",
            )
        return CommandResult(
            type="system_message",
            text=f"Cancelled the paused workflow{note_suffix}",
        )

    async def _find_latest_gate_pause(
        self,
        conversation: ConversationModel,
        user_email: str,
    ) -> Any | None:
        """Return the latest persisted pending gate pause for this conversation."""

        if self._notification_service is not None and hasattr(
            self._notification_service, "list_pending"
        ):
            notifications = await self._notification_service.list_pending(
                user_email,
                conversation_id=conversation.conversation_id,
            )
            for notification in notifications:
                if notification.notification_type != NotificationType.GATE:
                    continue
                pending = self._pause_waiter.get(notification.notification_id)
                if pending is None or pending.resolved:
                    return None
                return pending
            return None

        pending_gates = self._pause_waiter.list_pending(
            pause_type="gate",
            conversation_id=conversation.conversation_id,
        )
        if not pending_gates:
            return None
        return pending_gates[-1]

    async def _handle_stop(self, conversation: ConversationModel) -> CommandResult:
        """Handle /stop or /cancel by aborting active work immediately."""
        conversation_id = conversation.conversation_id
        stopped_anything = False

        if self._turn_scheduler is not None:
            stopped_anything = await self._turn_scheduler.cancel_turn(conversation_id)

        pending_pauses = self._pause_waiter.list_pending(conversation_id=conversation_id)
        for pause in pending_pauses:
            if pause.pause_type == "step_question" and pause.task_id is None:
                resolved = False
                if self._notification_service is not None:
                    resolved = await self._notification_service.resolve(
                        pause.pause_id,
                        "cancel",
                        {"reason": "user_stop"},
                    )
                if not resolved and self._notification_service is not None:
                    with contextlib.suppress(Exception):
                        await self._notification_service.mark_orphaned(
                            pause.pause_id,
                            reason="user_stop_recovery",
                        )
                if resolved or self._pause_waiter.resolve(
                    pause.pause_id,
                    PauseResolution(decision="cancel", data={"reason": "user_stop"}),
                ):
                    stopped_anything = True
            elif pause.pause_type == "escalation":
                resolved = False
                if self._notification_service is not None:
                    resolved = await self._notification_service.resolve(
                        pause.pause_id,
                        "deny",
                        {"note": "Stopped by user"},
                    )
                if not resolved and self._notification_service is not None:
                    with contextlib.suppress(Exception):
                        await self._notification_service.mark_orphaned(
                            pause.pause_id,
                            reason="user_stop_recovery",
                        )
                if resolved or self._pause_waiter.resolve(
                    pause.pause_id,
                    PauseResolution(decision="deny", data={"note": "Stopped by user"}),
                ):
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
  /thinking [level]  Show or set reasoning effort
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
  /deny [note]       Deny pending tool escalation
  /retry [note]      Retry paused workflow gate using its revise action
  /continue [note]   Continue paused workflow gate
  /cancel [note]     Cancel paused workflow gate, or stop active work"""


def _infer_reasoning_efforts(model: str) -> list[str]:
    """Best-effort reasoning effort levels for a model."""
    return reasoning_efforts_for_model(model, supports_reasoning=True)
