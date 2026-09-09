"""Canonical authoring and source resolution for write_deliverable."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from cognis.channels.rich_markdown import render_rich_markdown
from cognis.core.artifact_access import artifact_authorized_for_conversation
from cognis.models.artifact import ArtifactStatus
from cognis.models.deliverable import normalize_required_rich_payload
from cognis.store.queries import get_artifact_record

MAX_RICH_PAYLOAD_BYTES = 256 * 1024
_PUBLISHED_ARTIFACT_ID_RE = re.compile(r"^art_[0-9a-f]{32}$")
_JSON_MIME_TYPES = frozenset({"application/json"})
_RICH_ACTIONS = {
    "rich": None,
    "rich:dashboard": "dashboard",
    "rich:pulse": "pulse",
}
_FORBIDDEN_RICH_ARGUMENTS = frozenset({"content", "format", "rich", "title", "target", "outputs"})


class DeliverableAuthoringError(ValueError):
    """A caller-correctable authoring error with a JSON path."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message


@dataclass(frozen=True, slots=True)
class ResolvedDeliverableAuthoring:
    """Canonical values that can be persisted without an artifact dependency."""

    content: str
    format: str
    title: str | None
    outputs: dict[str, Any]
    rich: dict[str, Any] | None
    source_artifact_id: str | None = None
    source_digest: str | None = None


def is_rich_authoring_action(action: object) -> bool:
    """Return whether an action selects a registered Rich authoring contract."""

    return isinstance(action, str) and action in _RICH_ACTIONS


def rich_action_for_presentation(presentation: object) -> str:
    """Return the canonical authoring action for persisted presentation metadata."""

    if presentation is None:
        return "rich"
    action = f"rich:{presentation}"
    if action not in _RICH_ACTIONS:
        raise DeliverableAuthoringError(
            "unsupported_presentation",
            "$.payload.metadata.presentation",
            "The persisted Rich presentation does not have an authoring action.",
        )
    return action


def artifact_source(arguments: dict[str, Any]) -> tuple[str, str] | None:
    """Return the artifact ID and its argument path when one is selected."""

    source = arguments.get("payload_artifact")
    if not isinstance(source, dict):
        return None
    artifact_id = source.get("artifact_id")
    if not isinstance(artifact_id, str):
        return None
    return artifact_id, "$.payload_artifact.artifact_id"


async def resolve_deliverable_authoring(
    arguments: dict[str, Any],
    *,
    session_factory: Any,
    artifact_store: Any,
    owner_email: str,
    conversation_id: str,
    agent_id: str | None,
    expected_artifact_id: str | None = None,
    expected_digest: str | None = None,
) -> ResolvedDeliverableAuthoring:
    """Resolve and normalize one authoring call under the current access scope."""

    action = arguments.get("action")
    if action not in _RICH_ACTIONS:
        return _resolve_text(arguments)

    forbidden = sorted(_FORBIDDEN_RICH_ARGUMENTS.intersection(arguments))
    if forbidden:
        key = forbidden[0]
        raise DeliverableAuthoringError(
            "invalid_rich_argument",
            f"$.{key}",
            f"{key} is not accepted for Rich authoring.",
        )
    has_payload = "payload" in arguments
    has_artifact = "payload_artifact" in arguments
    if has_payload == has_artifact:
        raise DeliverableAuthoringError(
            "invalid_payload_source",
            "$",
            "Rich authoring requires exactly one of payload or payload_artifact.",
        )

    artifact_id: str | None = None
    digest: str | None = None
    payload: Any
    if has_artifact:
        payload, artifact_id, digest = await _load_payload_artifact(
            arguments.get("payload_artifact"),
            session_factory=session_factory,
            artifact_store=artifact_store,
            owner_email=owner_email,
            conversation_id=conversation_id,
            agent_id=agent_id,
        )
    else:
        payload = arguments.get("payload")
        if not isinstance(payload, dict):
            raise DeliverableAuthoringError(
                "invalid_payload", "$.payload", "Rich payload must be a JSON object."
            )

    if expected_artifact_id is not None and artifact_id != expected_artifact_id:
        raise DeliverableAuthoringError(
            "artifact_source_changed",
            "$.payload_artifact.artifact_id",
            "The Rich payload artifact changed after validation.",
        )
    if expected_digest is not None and digest != expected_digest:
        raise DeliverableAuthoringError(
            "artifact_digest_changed",
            "$.payload_artifact.artifact_id",
            "The Rich payload artifact content changed after validation.",
        )

    presentation = _RICH_ACTIONS[str(action)]
    authored_metadata = payload.get("metadata")
    if authored_metadata is not None and not isinstance(authored_metadata, dict):
        raise DeliverableAuthoringError(
            "invalid_metadata",
            "$.payload.metadata",
            "Rich payload metadata must be a JSON object.",
        )
    if isinstance(authored_metadata, dict) and "presentation" in authored_metadata:
        raise DeliverableAuthoringError(
            "author_selected_presentation",
            "$.payload.metadata.presentation",
            "The Rich action owns metadata.presentation. Omit this field.",
        )
    payload = dict(payload)
    payload["metadata"] = dict(authored_metadata) if isinstance(authored_metadata, dict) else {}
    if presentation is not None:
        payload["metadata"]["presentation"] = presentation
    normalized, _warnings = normalize_required_rich_payload(payload)
    title = _required_title(normalized)
    content = render_rich_markdown(
        normalized,
        title=title,
        full_view_link=None,
        deliverable_id="",
        fallback_text="",
    )
    content = content.removesuffix("\n\n_Open the full version in Cognis ()._")
    outputs = normalized.get("outputs")
    return ResolvedDeliverableAuthoring(
        content=content,
        format="rich",
        title=title,
        outputs=dict(outputs) if isinstance(outputs, dict) else {},
        rich=normalized,
        source_artifact_id=artifact_id,
        source_digest=digest,
    )


def _resolve_text(arguments: dict[str, Any]) -> ResolvedDeliverableAuthoring:
    if "action" in arguments:
        raise DeliverableAuthoringError(
            "invalid_action", "$.action", "Unknown write_deliverable action."
        )
    content = arguments.get("content")
    if not isinstance(content, str) or not content.strip():
        raise DeliverableAuthoringError(
            "empty_content", "$.content", "write_deliverable requires non-empty content."
        )
    format_name = arguments.get("format", "markdown")
    if format_name not in {"markdown", "plain", "html"}:
        raise DeliverableAuthoringError(
            "invalid_format",
            "$.format",
            "Text deliverable format must be markdown, plain, or html.",
        )
    title = arguments.get("title")
    outputs = arguments.get("outputs")
    return ResolvedDeliverableAuthoring(
        content=content,
        format=str(format_name),
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        outputs=dict(outputs) if isinstance(outputs, dict) else {},
        rich=None,
    )


async def _load_payload_artifact(
    source: object,
    *,
    session_factory: Any,
    artifact_store: Any,
    owner_email: str,
    conversation_id: str,
    agent_id: str | None,
) -> tuple[dict[str, Any], str, str]:
    path = "$.payload_artifact"
    if not isinstance(source, dict) or set(source) != {"artifact_id"}:
        raise DeliverableAuthoringError(
            "invalid_payload_artifact",
            path,
            "payload_artifact must contain only artifact_id.",
        )
    artifact_id = source.get("artifact_id")
    if not isinstance(artifact_id, str) or not _PUBLISHED_ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise DeliverableAuthoringError(
            "invalid_artifact_id",
            f"{path}.artifact_id",
            "payload_artifact requires a real art_ Cognis artifact ID.",
        )
    if session_factory is None or artifact_store is None:
        raise DeliverableAuthoringError(
            "artifact_support_unavailable", path, "Artifact-backed Rich authoring is unavailable."
        )

    async with session_factory() as session:
        record = await get_artifact_record(session, artifact_id)
        authorized = await artifact_authorized_for_conversation(
            session,
            artifact=record,
            owner_email=owner_email,
            conversation_id=conversation_id,
            agent_id=agent_id,
        )
        if (
            record is None
            or not authorized
            or record.owner_email != owner_email
            or record.status != ArtifactStatus.ATTACHED
            or record.deleted_at is not None
            or record.expires_at is not None
            or record.conversation_id is not None
            or record.purpose != "artifact_publish"
        ):
            raise DeliverableAuthoringError(
                "artifact_unavailable",
                f"{path}.artifact_id",
                "The published artifact is unavailable in the current scope.",
            )
        mime_type = str(record.mime_type).split(";", 1)[0].strip().lower()
        if mime_type not in _JSON_MIME_TYPES:
            raise DeliverableAuthoringError(
                "invalid_artifact_mime",
                f"{path}.artifact_id",
                "The Rich payload artifact must use application/json.",
            )
        if record.size_bytes > MAX_RICH_PAYLOAD_BYTES:
            raise DeliverableAuthoringError(
                "payload_too_large",
                f"{path}.artifact_id",
                f"The Rich payload artifact exceeds {MAX_RICH_PAYLOAD_BYTES} bytes.",
            )
        namespace, object_id, filename = record.namespace, record.object_id, record.filename
        recorded_size = record.size_bytes
        recorded_hash = getattr(record, "content_hash", None)

    try:
        raw, stored_mime = await artifact_store.async_load(namespace, object_id, filename)
    except (FileNotFoundError, OSError) as exc:
        raise DeliverableAuthoringError(
            "artifact_unavailable",
            f"{path}.artifact_id",
            "The Rich payload artifact content is unavailable.",
        ) from exc
    if len(raw) > MAX_RICH_PAYLOAD_BYTES:
        raise DeliverableAuthoringError(
            "payload_too_large",
            f"{path}.artifact_id",
            f"The Rich payload artifact exceeds {MAX_RICH_PAYLOAD_BYTES} bytes.",
        )
    if len(raw) != recorded_size:
        raise DeliverableAuthoringError(
            "artifact_integrity_error",
            f"{path}.artifact_id",
            "The Rich payload artifact size does not match its immutable metadata.",
        )
    digest = hashlib.sha256(raw).hexdigest()
    if not isinstance(recorded_hash, str) or recorded_hash != digest:
        raise DeliverableAuthoringError(
            "artifact_integrity_error",
            f"{path}.artifact_id",
            "The Rich payload artifact digest does not match its immutable metadata.",
        )
    if str(stored_mime).split(";", 1)[0].strip().lower() not in _JSON_MIME_TYPES:
        raise DeliverableAuthoringError(
            "invalid_artifact_mime",
            f"{path}.artifact_id",
            "The stored Rich payload must use application/json.",
        )
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DeliverableAuthoringError(
            "invalid_utf8", f"{path}.artifact_id", "The Rich payload is not valid UTF-8."
        ) from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise DeliverableAuthoringError(
            "invalid_json", f"{path}.artifact_id", f"The Rich payload is invalid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DeliverableAuthoringError(
            "invalid_payload", f"{path}.artifact_id", "The Rich payload must be a JSON object."
        )
    return payload, artifact_id, digest


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key}")
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> None:
    raise ValueError(f"non-finite number is not permitted: {value}")


def _required_title(payload: dict[str, Any]) -> str:
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise DeliverableAuthoringError(
            "missing_title", "$.payload.title", "Rich payload title is required."
        )
    return title.strip()
