"""System task bindings, distinct from custom named inference routes."""

from __future__ import annotations

import hashlib
from typing import Any

from cognis import json_codec as json
from cognis.api.common import api_exception
from cognis.api.models import ModelRoutingEntry, ModelRoutingResponse, ModelRoutingUpdateRequest
from cognis.core.management_access import ManagementActor
from cognis.models.config import normalize_reasoning_level
from cognis.ownership import SYSTEM_USER_EMAIL
from cognis.providers.llm.hosted import is_chat_model
from cognis.providers.llm.routing import (
    COMPANION_DEFAULTS,
    COMPANION_ROUTES,
    EMBEDDING_ROUTES,
    INHERIT_COMPANION_MODEL,
    ROUTING_TASK_TYPES,
    TEXT_ROUTING_TASK_TYPES,
)
from cognis.store.local_models import lock_local_model_dispatch_guard
from cognis.store.models import LLMProvider
from cognis.store.queries import (
    delete_model_routing,
    get_setting_value,
    list_model_routing,
    upsert_model_routing,
    upsert_setting,
)

SAME_SESSION_MODEL_SENTINEL = "__same_session_model__"
_REVISION_KEY = "internal.system_routing_revision"


def route_model_is_eligible(task_type: str, *, model_id: str, model_info: Any) -> bool:
    """Apply the same modality constraints for bindings and custom-route candidates."""
    mode = getattr(model_info, "model_mode", None)
    if mode == "systemone":
        return task_type in {"intaris_evaluation", "classifier"}
    chat = is_chat_model(
        {"model_mode": mode, "supports_embedding": getattr(model_info, "supports_embedding", False)}
    )
    names = [model_id, getattr(model_info, "display_name", "") or ""]
    names = [name.strip().lower().replace("_", "-") for name in names]
    if task_type == "speech_to_text":
        if getattr(model_info, "supports_transcription", False):
            return True
        if mode:
            return bool(mode == "audio_transcription")
        return any(
            token in name for name in names for token in ("transcribe", "whisper", "speech-to-text")
        )
    if task_type == "text_to_speech":
        if getattr(model_info, "supports_speech_synthesis", False):
            return True
        if mode:
            return bool(mode == "audio_speech")
        return any(
            token in name
            for name in names
            for token in ("tts", "text-to-speech", "speech-1", "eleven", "elevenlabs", "piper")
        )
    if task_type == "image_generation":
        return bool(getattr(model_info, "supports_image_generation", False))
    if task_type in {"video_generation", "video_editing"}:
        return mode == "video" or bool(getattr(model_info, "supports_video_generation", False))
    if task_type == "video_analysis":
        return chat and bool(getattr(model_info, "supports_video_input", False))
    if task_type == "attachment_analysis":
        return chat and any(
            getattr(model_info, key, False)
            for key in (
                "supports_vision",
                "supports_pdf_input",
                "supports_audio_input",
                "supports_file_input",
            )
        )
    if task_type in EMBEDDING_ROUTES:
        return bool(getattr(model_info, "supports_embedding", False))
    return chat


def routing_entry_from_row(task_type: str, row: Any | None) -> dict[str, str | None]:
    """Preserve qualified model references and companion effort inheritance."""
    if row is None:
        return {"model": None, "reasoning_effort": None}
    effort = None
    if task_type in TEXT_ROUTING_TASK_TYPES and isinstance(getattr(row, "config", None), dict):
        effort = normalize_reasoning_level(row.config.get("reasoning_effort"))
        if effort == "default" and task_type not in COMPANION_ROUTES:
            effort = None
    model = getattr(row, "model", None)
    if model == INHERIT_COMPANION_MODEL and task_type in COMPANION_ROUTES:
        model = None
    provider_id = getattr(row, "provider_id", None)
    if model and provider_id and model != SAME_SESSION_MODEL_SENTINEL:
        model = f"{provider_id}/{model}"
    return {"model": model, "reasoning_effort": effort}


def binding_revision(rows: list[Any], generation: int = 0) -> str:
    """An aggregate precondition including explicit and absent binding state."""
    snapshot = sorted(
        [(row.task_type, row.model, row.provider_id, row.config) for row in rows],
        key=lambda entry: entry[0],
    )
    return hashlib.sha256(json.dumps([generation, snapshot], sort_keys=True).encode()).hexdigest()


async def get_bindings(session_factory: Any) -> tuple[ModelRoutingResponse, str]:
    """Read the complete system binding document and its opaque revision."""
    async with session_factory() as session:
        rows = await list_model_routing(session, owner_email=SYSTEM_USER_EMAIL)
        generation = await get_setting_value(session, _REVISION_KEY, 0)
        assert isinstance(generation, int)
    by_task = {row.task_type: row for row in rows}
    return ModelRoutingResponse(
        **{
            role: ModelRoutingEntry(**routing_entry_from_row(role, by_task.get(role)))
            for role in ROUTING_TASK_TYPES
        }
    ), binding_revision(rows, generation)


async def put_bindings(
    session_factory: Any,
    llm: Any,
    actor: ManagementActor,
    payload: ModelRoutingUpdateRequest,
    *,
    expected_revision: str | None = None,
    preview: bool = False,
) -> ModelRoutingResponse | dict[str, Any]:
    """Validate outside transactions, then replace bindings under a database write guard."""
    actor.require_admin()
    updates = {role: getattr(payload, role) for role in ROUTING_TASK_TYPES}
    prepared: dict[str, tuple[str | None, str | None, dict[str, str] | None]] = {}
    for task_type, entry in updates.items():
        inherits = not entry.model or not entry.model.strip()
        parent = COMPANION_ROUTES.get(task_type)
        if inherits and not (
            parent
            and task_type in TEXT_ROUTING_TASK_TYPES
            and entry.reasoning_effort not in {None, ""}
        ):
            if entry.reasoning_effort not in {None, "", "default"}:
                raise api_exception(
                    422,
                    "validation_error",
                    f"{task_type} reasoning_effort requires an explicit model",
                )
            prepared[task_type] = (None, None, None)
            continue
        effective = updates[parent].model if inherits and parent else entry.model
        if not effective:
            raise api_exception(
                422, "validation_error", f"{task_type} requires a configured parent model"
            )
        reference = effective.strip()
        if reference == SAME_SESSION_MODEL_SENTINEL:
            if task_type != "compaction" or entry.reasoning_effort not in {None, "", "default"}:
                raise api_exception(
                    422,
                    "validation_error",
                    f"{task_type} cannot use the same-session model sentinel with reasoning_effort",
                )
            prepared[task_type] = (reference, None, None)
            continue
        try:
            if hasattr(llm, "resolve_model_reference"):
                model, provider_id = await llm.resolve_model_reference(
                    reference, acting_user_email=SYSTEM_USER_EMAIL
                )
            else:
                model = reference
                provider_id = await llm.find_provider_for_model(
                    model, acting_user_email=SYSTEM_USER_EMAIL
                )
        except ValueError as exc:
            raise api_exception(422, "validation_error", f"{task_type} {exc}") from exc
        if model.startswith("cognis/"):
            plan = await llm.inference_plan(model=model, acting_user_email=SYSTEM_USER_EMAIL)
            if task_type in EMBEDDING_ROUTES and len(plan.candidates) != 1:
                raise api_exception(422, "validation_error", "Embedding routes need one target")
            for candidate in plan.candidates:
                info = await llm.get_model_info(
                    candidate.target.model,
                    provider_id=candidate.target.provider_id,
                    acting_user_email=SYSTEM_USER_EMAIL,
                )
                if not route_model_is_eligible(
                    task_type, model_id=candidate.target.model, model_info=info
                ):
                    raise api_exception(422, "validation_error", "Route has an ineligible target")
            prepared[task_type] = (
                model,
                None,
                {"reasoning_effort": entry.reasoning_effort} if entry.reasoning_effort else None,
            )
            continue
        if provider_id is None:
            raise api_exception(
                422,
                "validation_error",
                f"{task_type} model {model!r} is not present in configured providers",
            )
        if task_type in {*COMPANION_DEFAULTS, *COMPANION_ROUTES}:
            async with session_factory() as session:
                provider = await session.get(LLMProvider, provider_id)
            if (
                provider is None
                or provider.owner_email != SYSTEM_USER_EMAIL
                or provider.status != "active"
            ):
                raise api_exception(
                    422, "validation_error", f"{task_type} requires an enabled global provider"
                )
        info = await llm.get_model_info(
            model, provider_id=provider_id, acting_user_email=SYSTEM_USER_EMAIL
        )
        if not route_model_is_eligible(task_type, model_id=model, model_info=info):
            raise api_exception(
                422,
                "validation_error",
                f"{task_type} model {model!r} is not eligible for that route",
            )
        effort = None
        if isinstance(entry.reasoning_effort, str) and entry.reasoning_effort.strip():
            effort = normalize_reasoning_level(entry.reasoning_effort)
            if effort is None:
                raise api_exception(
                    422,
                    "validation_error",
                    f"{task_type} reasoning_effort {entry.reasoning_effort!r} is invalid",
                )
        provider_default = effort == "default" and task_type in COMPANION_ROUTES
        if effort == "default":
            effort = None
        inherited = (
            normalize_reasoning_level(updates[parent].reasoning_effort)
            if parent and entry.reasoning_effort in {None, ""}
            else None
        )
        effective_effort = effort or inherited
        if getattr(info, "model_mode", None) == "systemone":
            if effort is not None:
                raise api_exception(
                    422, "validation_error", "System One does not support reasoning_effort"
                )
            effective_effort = None
        if effective_effort == "default":
            effective_effort = None
        if effective_effort is not None:
            if task_type not in TEXT_ROUTING_TASK_TYPES:
                raise api_exception(
                    422, "validation_error", f"{task_type} does not support reasoning_effort"
                )
            if effective_effort not in info.reasoning_efforts:
                raise api_exception(
                    422,
                    "validation_error",
                    f"{task_type} thinking effort {effective_effort!r} is not supported by model {model!r}",
                )
        config = (
            {"reasoning_effort": effort}
            if effort is not None
            else ({"reasoning_effort": "default"} if provider_default else None)
        )
        prepared[task_type] = (
            INHERIT_COMPANION_MODEL if inherits else model,
            provider_id if reference != model and not inherits else None,
            config,
        )

    async with session_factory() as session:
        await lock_local_model_dispatch_guard(session)
        rows = await list_model_routing(session, owner_email=SYSTEM_USER_EMAIL)
        generation = await get_setting_value(session, _REVISION_KEY, 0)
        assert isinstance(generation, int)
        if (
            expected_revision is not None
            and binding_revision(rows, generation) != expected_revision
        ):
            raise api_exception(
                409, "routing_conflict", "Routing bindings changed; read them again"
            )
        if preview:
            return {"preview": True, "bindings": payload.model_dump(mode="json")}
        for row in rows:
            if row.task_type not in ROUTING_TASK_TYPES:
                await delete_model_routing(session, row.task_type, owner_email=SYSTEM_USER_EMAIL)
        for task_type, (model, provider_id, config) in prepared.items():
            if model is None:
                await delete_model_routing(session, task_type, owner_email=SYSTEM_USER_EMAIL)
            else:
                await upsert_model_routing(
                    session,
                    task_type=task_type,
                    provider_id=provider_id,
                    model=model,
                    owner_email=SYSTEM_USER_EMAIL,
                    config=config,
                )
        await upsert_setting(
            session,
            key=_REVISION_KEY,
            value=generation + 1,
            category="internal",
            updated_by=actor.email,
        )
        await session.commit()
    response, _ = await get_bindings(session_factory)
    return response
