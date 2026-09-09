"""Authoritative runtime selection for an interactive session."""

from __future__ import annotations

from copy import copy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

from cognis.core.agent_profiles import resolve_conversation_agent_profile
from cognis.models.agent import AgentDefinition
from cognis.models.session import ConversationModel, SessionModel


@dataclass(frozen=True, slots=True)
class RuntimeSelection:
    """Effective inference selection and its persisted revision."""

    revision: int
    profile_id: str
    profile_source: str
    model: str | None
    provider_id: str | None
    model_source: str
    reasoning_effort: str | None
    reasoning_effort_source: str
    fast_mode: bool | None
    fast_mode_source: str

    def as_dict(self) -> dict[str, str | int | bool | None]:
        """Return an API-safe representation."""

        return asdict(self)


def resolve_runtime_selection(
    agent: AgentDefinition,
    session: SessionModel,
    conversation: ConversationModel | None = None,
) -> RuntimeSelection:
    """Resolve persisted session overrides before profile and agent defaults."""

    profile = resolve_conversation_agent_profile(agent, session, conversation)
    llm_config = agent.llm_config

    model: str | None
    provider_id: str | None
    if session.model_override:
        model = session.model_override
        provider_id = session.model_override_provider_id
        model_source = "session_override"
    else:
        model = profile.model or (llm_config.model if llm_config else None)
        provider_id = profile.provider_id or (llm_config.provider_id if llm_config else None)
        model_source = (
            "agent_profile"
            if profile.model is not None
            else ("agent_config" if model or provider_id else "provider_default")
        )

    reasoning_effort: str | None
    if session.reasoning_effort_override is not None:
        reasoning_effort = session.reasoning_effort_override
        reasoning_source = "session_override"
    elif profile.reasoning_effort is not None:
        reasoning_effort = profile.reasoning_effort
        reasoning_source = "agent_profile"
    else:
        reasoning_effort = llm_config.reasoning_effort if llm_config else None
        reasoning_source = "agent_config" if reasoning_effort is not None else "provider_default"

    fast_mode: bool | None
    if session.fast_mode_override is not None:
        fast_mode = session.fast_mode_override
        fast_mode_source = "session_override"
    elif profile.fast_mode is not None:
        fast_mode = profile.fast_mode
        fast_mode_source = "agent_profile"
    else:
        fast_mode = llm_config.fast_mode if llm_config else None
        fast_mode_source = "agent_config" if fast_mode is not None else "provider_default"

    return RuntimeSelection(
        revision=session.runtime_override_revision,
        profile_id=profile.profile_id,
        profile_source=profile.source,
        model=model,
        provider_id=provider_id,
        model_source=model_source,
        reasoning_effort=reasoning_effort,
        reasoning_effort_source=reasoning_source,
        fast_mode=fast_mode,
        fast_mode_source=fast_mode_source,
    )


_UNSET = object()


class _RuntimeChange(TypedDict):
    profile_id: str | object
    model_override: str | None | object
    model_override_provider_id: str | None | object
    reasoning_effort_override: str | None | object
    fast_mode_override: bool | None | object
    clear_overrides: bool


@dataclass
class RuntimeSelectionPlan:
    """One validated change, staged without database or cache side effects."""

    change: _RuntimeChange | None = None
    persist_conversation_profile: bool = False
    require_active_session: bool = True

    async def apply(
        self, db_session: Any, conversation: ConversationModel, session: SessionModel
    ) -> None:
        """Apply the plan in the caller's transaction without publishing a cache."""
        if self.change is None:
            return
        from cognis.store.queries import get_conversation_for_update, get_session_for_update

        conversation_row = await get_conversation_for_update(
            db_session, conversation.conversation_id
        )
        if conversation_row is None:
            raise RuntimeError("Conversation disappeared during runtime selection update")
        if self.require_active_session and conversation_row.active_session_id != session.session_id:
            raise RuntimeError("Active session changed during runtime selection update")
        session_row = await get_session_for_update(db_session, session.session_id)
        if session_row is None or session_row.conversation_id != conversation.conversation_id:
            raise RuntimeError("Session disappeared during runtime selection update")
        _apply_runtime_change(session_row, **self.change)
        if self.persist_conversation_profile and self.change["profile_id"] is not _UNSET:
            conversation_row.agent_profile_id = str(self.change["profile_id"])
            conversation_row.updated_at = datetime.now(UTC)
        session_row.updated_at = datetime.now(UTC)
        await db_session.flush()
        for key in (
            "agent_profile_id",
            "model_override",
            "model_override_provider_id",
            "reasoning_effort_override",
            "fast_mode_override",
            "runtime_override_revision",
        ):
            setattr(session, key, getattr(session_row, key))
        if self.persist_conversation_profile:
            conversation.agent_profile_id = conversation_row.agent_profile_id

    def publish(self, session_cache: Any, session: SessionModel) -> None:
        """Publish committed state to reconstructable process-local caches."""
        if self.change is None:
            return
        _mirror_runtime_selection(session_cache, session)
        if self.change["clear_overrides"]:
            update = getattr(session_cache, "update_tool_runtime_info", None)
            if callable(update):
                update(session.session_id, None)


async def persist_runtime_selection(
    *,
    session_factory: Any,
    session_cache: Any,
    conversation: ConversationModel,
    session: SessionModel,
    profile_id: str | object = _UNSET,
    model_override: str | None | object = _UNSET,
    model_override_provider_id: str | None | object = _UNSET,
    reasoning_effort_override: str | None | object = _UNSET,
    fast_mode_override: bool | None | object = _UNSET,
    clear_overrides: bool = False,
    persist_conversation_profile: bool = False,
    require_active_session: bool = True,
    runtime_plan: RuntimeSelectionPlan | None = None,
) -> None:
    """Persist one runtime change under the conversation and session row locks."""

    change: _RuntimeChange = {
        "profile_id": profile_id,
        "model_override": model_override,
        "model_override_provider_id": model_override_provider_id,
        "reasoning_effort_override": reasoning_effort_override,
        "fast_mode_override": fast_mode_override,
        "clear_overrides": clear_overrides,
    }
    if runtime_plan is not None:
        if runtime_plan.change is not None:
            raise RuntimeError("A runtime command can stage only one change")
        runtime_plan.change = change
        runtime_plan.persist_conversation_profile = persist_conversation_profile
        runtime_plan.require_active_session = require_active_session
        _apply_runtime_change(session, **change)
        if persist_conversation_profile and profile_id is not _UNSET:
            conversation.agent_profile_id = str(profile_id)
        return

    if session_factory is None:
        _apply_runtime_change(
            session,
            profile_id=profile_id,
            model_override=model_override,
            model_override_provider_id=model_override_provider_id,
            reasoning_effort_override=reasoning_effort_override,
            fast_mode_override=fast_mode_override,
            clear_overrides=clear_overrides,
        )
        if persist_conversation_profile and profile_id is not _UNSET:
            conversation.agent_profile_id = str(profile_id)
        _mirror_runtime_selection(session_cache, session)
        return

    plan = RuntimeSelectionPlan(change, persist_conversation_profile, require_active_session)
    persisted_session = copy(session)
    persisted_conversation = copy(conversation)
    async with session_factory() as db_session:
        try:
            await plan.apply(db_session, persisted_conversation, persisted_session)
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    for key in (
        "agent_profile_id",
        "model_override",
        "model_override_provider_id",
        "reasoning_effort_override",
        "fast_mode_override",
        "runtime_override_revision",
    ):
        setattr(session, key, getattr(persisted_session, key))
    if persist_conversation_profile and profile_id is not _UNSET:
        conversation.agent_profile_id = str(profile_id)
    _mirror_runtime_selection(session_cache, session)


def _apply_runtime_change(
    target: Any,
    *,
    profile_id: str | object,
    model_override: str | None | object,
    model_override_provider_id: str | None | object,
    reasoning_effort_override: str | None | object,
    fast_mode_override: bool | None | object,
    clear_overrides: bool,
) -> None:
    if profile_id is not _UNSET:
        target.agent_profile_id = str(profile_id)
    if clear_overrides:
        target.model_override = None
        target.model_override_provider_id = None
        target.reasoning_effort_override = None
        target.fast_mode_override = None
    if model_override is not _UNSET:
        target.model_override = model_override
        target.model_override_provider_id = (
            None if model_override is None else model_override_provider_id
        )
    if reasoning_effort_override is not _UNSET:
        target.reasoning_effort_override = reasoning_effort_override
    if fast_mode_override is not _UNSET:
        target.fast_mode_override = fast_mode_override
    target.runtime_override_revision = int(getattr(target, "runtime_override_revision", 0) or 0) + 1


def _mirror_runtime_selection(session_cache: Any, session: SessionModel) -> None:
    """Keep legacy process-local readers coherent during rolling deployment."""

    session_cache.set_model_override(
        session.session_id,
        session.model_override,
        provider_id=session.model_override_provider_id,
    )
    session_cache.set_reasoning_effort_override(
        session.session_id, session.reasoning_effort_override
    )
    set_fast_mode_override = getattr(session_cache, "set_fast_mode_override", None)
    if callable(set_fast_mode_override):
        set_fast_mode_override(session.session_id, session.fast_mode_override)
