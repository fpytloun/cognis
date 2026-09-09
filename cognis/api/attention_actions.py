"""Safe list and detail projections for dashboard attention actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cognis.api.models import (
    AttentionActionAvailability,
    AttentionActionChoice,
    AttentionActionDetail,
    AttentionActionDisplay,
    AttentionActionKind,
    AttentionActionSource,
    AttentionActionSummary,
    AttentionQuestion,
)
from cognis.core.notifications import NotificationType, safe_display_arguments
from cognis.core.question_sets import normalize_questions
from cognis.store.models import NotificationRow

_VISIBLE_STATUSES = ("pending", "resolving")
_SUPPORTED_TYPES = {
    NotificationType.ESCALATION,
    NotificationType.GATE,
    NotificationType.STEP_QUESTION,
    NotificationType.CREDENTIAL_REQUEST,
    NotificationType.AUTH_CHALLENGE,
}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _payload_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def attention_action_expires_at(row: NotificationRow) -> datetime | None:
    """Return the persisted deadline, with legacy payload compatibility."""

    persisted = _aware(row.expires_at)
    if persisted is not None:
        return persisted
    payload = row.payload if isinstance(row.payload, dict) else {}
    payload_expiry = _payload_datetime(payload.get("expires_at"))
    metadata = payload.get("metadata")
    if payload_expiry is None and isinstance(metadata, dict):
        payload_expiry = _payload_datetime(metadata.get("expires_at"))
    if payload_expiry is not None:
        return payload_expiry
    if row.notification_type != NotificationType.ESCALATION:
        return None
    timeout = payload.get("timeout_seconds", 300)
    try:
        timeout_seconds = max(0.0, float(timeout))
    except (TypeError, ValueError):
        timeout_seconds = 300.0
    created_at = _aware(row.created_at)
    return created_at + timedelta(seconds=timeout_seconds) if created_at else None


def _kind(row: NotificationRow) -> AttentionActionKind:
    payload = row.payload if isinstance(row.payload, dict) else {}
    if (
        row.notification_type == NotificationType.AUTH_CHALLENGE
        and payload.get("kind") == "oauth_authorization"
    ):
        return "oauth_authorization"
    if row.notification_type in _SUPPORTED_TYPES:
        return cast(AttentionActionKind, row.notification_type)
    return "unsupported"


def _title(kind: str) -> str:
    return {
        "escalation": "Tool approval required",
        "gate": "Task decision required",
        "step_question": "Response required",
        "credential_request": "Credential required",
        "auth_challenge": "Authentication required",
        "oauth_authorization": "Authorization required",
        "unsupported": "Action unavailable",
    }[kind]


def _source(row: NotificationRow) -> AttentionActionSource:
    payload = row.payload if isinstance(row.payload, dict) else {}
    managed_origin = payload.get("managed_origin_conversation_id")
    return AttentionActionSource(
        notification_id=row.notification_id,
        conversation_id=row.conversation_id,
        managed_origin_conversation_id=(
            managed_origin
            if isinstance(managed_origin, str) and managed_origin != row.conversation_id
            else None
        ),
        task_id=row.task_id,
        step_name=row.step_name,
        step_run_id=row.step_run_id,
        session_id=row.session_id,
    )


def project_attention_summary(
    row: NotificationRow,
    *,
    can_mutate: bool,
    now: datetime | None = None,
) -> AttentionActionSummary:
    """Project a payload-free card summary from one owned notification row."""

    current = now or datetime.now(UTC)
    expires_at = attention_action_expires_at(row)
    kind = _kind(row)
    expired = row.status in _VISIBLE_STATUSES and expires_at is not None and current >= expires_at
    has_action_form = _has_action_form(kind, row.payload if isinstance(row.payload, dict) else {})
    if row.status == "resolved":
        availability: AttentionActionAvailability = "resolved"
    elif expired:
        availability = "expired"
    elif row.status == "resolving":
        availability = "resolving"
    elif row.status != "pending":
        availability = "recovery_required"
    elif kind == "unsupported":
        availability = "unsupported"
    elif row.status == "resolving":
        availability = "resolving"
    elif not can_mutate:
        availability = "read_only"
    elif not has_action_form:
        availability = "recovery_required"
    else:
        availability = "actionable"
    can_resolve = availability == "actionable" and has_action_form
    revision = int(row.revision or 1)
    return AttentionActionSummary(
        action_id=row.notification_id,
        kind=kind,
        status="expired" if expired else row.status,
        availability=availability,
        title=_title(kind),
        source=_source(row),
        can_resolve=can_resolve,
        has_action_form=has_action_form,
        expires_at=expires_at,
        revision=revision,
        convergence_id=f"{row.notification_id}:{revision}",
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
    )


def _questions(value: object) -> list[AttentionQuestion]:
    try:
        normalized = normalize_questions(value)
    except ValueError:
        return []
    questions: list[AttentionQuestion] = []
    for item in normalized:
        if bool(item["required"]) and not item["options"] and not bool(item["allow_custom"]):
            return []
        questions.append(AttentionQuestion.model_validate(item))
    return questions


def _http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strict_string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    normalized: list[str] = []
    for item in value:
        if not _nonempty_string(item):
            return None
        field = item.strip()
        if field in normalized:
            return None
        normalized.append(field)
    return normalized


def _has_action_form(kind: AttentionActionKind, payload: dict[str, object]) -> bool:
    if kind == "escalation":
        return _nonempty_string(payload.get("tool_name"))
    if kind == "gate":
        options = payload.get("options")
        return isinstance(options, list) and any(
            isinstance(option, dict) and _nonempty_string(option.get("action"))
            for option in options
        )
    if kind == "step_question":
        return bool(_questions(payload.get("questions")))
    if kind == "credential_request":
        required_fields = _strict_string_list(payload.get("required_fields"))
        return (
            _nonempty_string(payload.get("credential_id"))
            and _nonempty_string(payload.get("kind"))
            and bool(required_fields)
        )
    if kind == "auth_challenge":
        required_fields = _strict_string_list(payload.get("required_fields", []))
        return _nonempty_string(payload.get("kind")) and required_fields is not None
    if kind == "oauth_authorization":
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            return False
        return any(
            _http_url(metadata.get(key)) is not None
            for key in ("verification_uri_complete", "verification_uri", "authorization_url")
        )
    return False


def project_attention_detail(
    row: NotificationRow,
    *,
    can_mutate: bool,
    now: datetime | None = None,
) -> AttentionActionDetail:
    """Project an owner-authorized, allowlisted action detail."""

    summary = project_attention_summary(row, can_mutate=can_mutate, now=now)
    payload = row.payload if isinstance(row.payload, dict) else {}
    kind = summary.kind
    display = AttentionActionDisplay()
    actions: list[AttentionActionChoice] = []

    if kind == "escalation":
        arguments = payload.get("arguments_display")
        display = AttentionActionDisplay(
            message="Review this tool request before it runs.",
            tool_name=payload.get("tool_name")
            if isinstance(payload.get("tool_name"), str)
            else None,
            arguments_display=(
                safe_display_arguments(arguments) if isinstance(arguments, dict) else None
            ),
            reasoning=payload.get("reasoning")
            if isinstance(payload.get("reasoning"), str)
            else None,
            risk=payload.get("risk") if isinstance(payload.get("risk"), str) else None,
        )
        actions = [
            AttentionActionChoice(
                action="approve", label="Approve", intent="primary", input="note"
            ),
            AttentionActionChoice(action="deny", label="Deny", intent="danger", input="note"),
        ]
    elif kind == "gate":
        raw_options = payload.get("options")
        options = raw_options if isinstance(raw_options, list) else []
        for index, option in enumerate(options):
            if not isinstance(option, dict):
                continue
            action = option.get("action")
            if not isinstance(action, str) or not action.strip():
                continue
            label = option.get("label")
            actions.append(
                AttentionActionChoice(
                    action=action,
                    label=label if isinstance(label, str) and label.strip() else action,
                    intent="primary" if index == 0 else "secondary",
                    input="feedback",
                )
            )
        display = AttentionActionDisplay(
            message=next(
                (
                    value
                    for value in (payload.get("message"), payload.get("question"))
                    if isinstance(value, str) and value.strip()
                ),
                None,
            )
        )
    elif kind == "step_question":
        questions = _questions(payload.get("questions"))
        display = AttentionActionDisplay(questions=questions)
        if questions:
            actions = [
                AttentionActionChoice(
                    action="continue",
                    label="Send response",
                    intent="primary",
                    input="structured",
                ),
                AttentionActionChoice(
                    action="cancel",
                    label="Cancel request",
                    intent="danger",
                    input="none",
                ),
            ]
    elif kind == "credential_request":
        required_fields = _strict_string_list(payload.get("required_fields")) or []
        display = AttentionActionDisplay(
            message=next(
                (
                    value
                    for value in (payload.get("message"), payload.get("description"))
                    if isinstance(value, str) and value.strip()
                ),
                "This credential is required to continue.",
            ),
            required_fields=required_fields,
            credential_id=(
                payload.get("credential_id")
                if isinstance(payload.get("credential_id"), str)
                else None
            ),
            credential_kind=payload.get("kind") if isinstance(payload.get("kind"), str) else None,
            credential_label=(
                payload.get("label") if isinstance(payload.get("label"), str) else None
            ),
            credential_scope=(
                payload.get("scope") if isinstance(payload.get("scope"), str) else "user"
            ),
        )
        if display.credential_id and display.credential_kind and required_fields:
            actions = [
                AttentionActionChoice(
                    action="approve",
                    label="Save and resume",
                    intent="primary",
                    input="credential",
                ),
                AttentionActionChoice(
                    action="cancel",
                    label="Cancel request",
                    intent="secondary",
                    input="none",
                ),
                AttentionActionChoice(
                    action="deny",
                    label="Deny",
                    intent="danger",
                    input="none",
                ),
            ]
    elif kind == "auth_challenge":
        required_fields = _strict_string_list(payload.get("required_fields", [])) or []
        display = AttentionActionDisplay(
            message=payload.get("message") if isinstance(payload.get("message"), str) else None,
            required_fields=required_fields,
        )
        actions = [
            AttentionActionChoice(
                action="continue" if required_fields else "completed",
                label="Continue" if required_fields else "I completed this step",
                intent="primary",
                input="auth_fields" if required_fields else "none",
            ),
            AttentionActionChoice(
                action="cancel",
                label="Cancel request",
                intent="secondary",
                input="none",
            ),
            AttentionActionChoice(action="deny", label="Deny", intent="danger", input="none"),
        ]
    elif kind == "oauth_authorization":
        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        authorization_url = next(
            (
                url
                for value in (
                    metadata.get("verification_uri_complete"),
                    metadata.get("verification_uri"),
                    metadata.get("authorization_url"),
                )
                if (url := _http_url(value)) is not None
            ),
            None,
        )
        display = AttentionActionDisplay(
            message=payload.get("message") if isinstance(payload.get("message"), str) else None,
            authorization_url=authorization_url,
            user_code=(
                metadata.get("user_code") if isinstance(metadata.get("user_code"), str) else None
            ),
            callback_mode=(
                metadata.get("callback_mode")
                if isinstance(metadata.get("callback_mode"), str)
                else None
            ),
            executor_name=(
                metadata.get("oauth_executor_name")
                if isinstance(metadata.get("oauth_executor_name"), str)
                else None
            ),
        )
        if authorization_url:
            actions = [
                AttentionActionChoice(
                    action="cancel",
                    label="Cancel authorization",
                    intent="danger",
                    input="none",
                )
            ]

    if not actions and summary.availability == "actionable":
        summary.availability = "recovery_required"
        summary.can_resolve = False
    if not summary.can_resolve:
        actions = []
    return AttentionActionDetail(
        **summary.model_dump(),
        display=display,
        allowed_actions=actions,
    )


async def list_attention_rows_by_conversation(
    session: AsyncSession,
    *,
    owner_email: str,
    conversation_ids: list[str],
) -> dict[str, list[NotificationRow]]:
    """Load visible action rows for many owned conversations in one query."""

    ids = sorted({item for item in conversation_ids if item})
    if not ids:
        return {}
    result = await session.execute(
        select(NotificationRow)
        .where(NotificationRow.user_email == owner_email)
        .where(NotificationRow.conversation_id.in_(ids))
        .where(NotificationRow.status.in_(_VISIBLE_STATUSES))
        .order_by(NotificationRow.created_at.asc(), NotificationRow.notification_id.asc())
    )
    grouped: dict[str, list[NotificationRow]] = {}
    for row in result.scalars():
        grouped.setdefault(row.conversation_id, []).append(row)
    return grouped


async def list_attention_rows_by_task(
    session: AsyncSession,
    *,
    owner_email: str,
    task_ids: list[str],
) -> dict[str, list[NotificationRow]]:
    """Load visible action rows for many owned tasks in one query."""

    ids = sorted({item for item in task_ids if item})
    if not ids:
        return {}
    result = await session.execute(
        select(NotificationRow)
        .where(NotificationRow.user_email == owner_email)
        .where(NotificationRow.task_id.in_(ids))
        .where(NotificationRow.status.in_(_VISIBLE_STATUSES))
        .order_by(
            NotificationRow.task_id.asc(),
            NotificationRow.created_at.asc(),
            NotificationRow.notification_id.asc(),
        )
    )
    grouped: dict[str, list[NotificationRow]] = {}
    for row in result.scalars():
        if row.task_id:
            grouped.setdefault(row.task_id, []).append(row)
    return grouped


async def get_attention_row(
    session: AsyncSession,
    *,
    owner_email: str,
    action_id: str,
) -> NotificationRow | None:
    """Load one action by owner without revealing cross-owner existence."""

    result = await session.execute(
        select(NotificationRow).where(
            NotificationRow.notification_id == action_id,
            NotificationRow.user_email == owner_email,
        )
    )
    return result.scalar_one_or_none()
