"""Object-store backing for first-class deliverable payloads."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from cognis.rendering.rich_visuals import rich_payload_has_noncanonical_chart

DELIVERABLE_STORAGE_NAMESPACE = "deliverables"
DELIVERABLE_CONTENT_FILENAME = "content.md"
DELIVERABLE_LEGACY_RICH_FILENAME = "rich.json"
DELIVERABLE_CHART_V1_RICH_KEY_PREFIX = "rich.chart-v1"
DELIVERABLE_RICH_FILENAME = f"{DELIVERABLE_CHART_V1_RICH_KEY_PREFIX}.json"
DELIVERABLE_OUTPUTS_FILENAME = "outputs.json"


def deliverable_content_mime_type(format_name: str | None) -> str:
    return {
        "markdown": "text/markdown",
        "plain": "text/plain",
        "html": "text/html",
        "rich": "text/markdown",
    }.get(str(format_name or "").lower(), "text/plain")


@dataclass(frozen=True)
class StoredDeliverableFile:
    key: str | None
    mime: str | None
    size: int | None
    hash: str | None


@dataclass(frozen=True)
class StoredDeliverablePayload:
    namespace: str
    object_id: str
    content: StoredDeliverableFile
    rich: StoredDeliverableFile
    outputs: StoredDeliverableFile


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _file_meta(key: str | None, mime: str | None, content: bytes | None) -> StoredDeliverableFile:
    if key is None or mime is None or content is None:
        return StoredDeliverableFile(key=None, mime=None, size=None, hash=None)
    return StoredDeliverableFile(
        key=key,
        mime=mime,
        size=len(content),
        hash=hashlib.sha256(content).hexdigest(),
    )


async def store_deliverable_payload(
    artifact_store: Any,
    *,
    deliverable_id: str,
    content: str,
    format: str,
    rich_payload: dict[str, Any] | None,
    outputs: dict[str, Any] | None,
    owner_email: str | None = None,
) -> StoredDeliverablePayload:
    """Store one deliverable's payload files and return DB metadata."""

    if artifact_store is None:
        raise ValueError("deliverable storage requires an artifact store")

    namespace = DELIVERABLE_STORAGE_NAMESPACE
    object_id = deliverable_id
    saved = False
    try:
        content_bytes = content.encode("utf-8")
        content_mime = deliverable_content_mime_type(format)
        await artifact_store.async_save(
            namespace,
            object_id,
            DELIVERABLE_CONTENT_FILENAME,
            content_bytes,
            content_mime,
            owner_email,
        )
        saved = True

        rich_bytes: bytes | None = None
        if rich_payload is not None:
            rich_bytes = _json_bytes(rich_payload)
            await artifact_store.async_save(
                namespace,
                object_id,
                DELIVERABLE_RICH_FILENAME,
                rich_bytes,
                "application/json",
                owner_email,
            )

        outputs_bytes: bytes | None = None
        if outputs:
            outputs_bytes = _json_bytes(outputs)
            await artifact_store.async_save(
                namespace,
                object_id,
                DELIVERABLE_OUTPUTS_FILENAME,
                outputs_bytes,
                "application/json",
                owner_email,
            )
        return StoredDeliverablePayload(
            namespace=namespace,
            object_id=object_id,
            content=_file_meta(DELIVERABLE_CONTENT_FILENAME, content_mime, content_bytes),
            rich=_file_meta(
                DELIVERABLE_RICH_FILENAME if rich_payload is not None else None,
                "application/json" if rich_payload is not None else None,
                rich_bytes,
            ),
            outputs=_file_meta(
                DELIVERABLE_OUTPUTS_FILENAME if outputs else None,
                "application/json" if outputs else None,
                outputs_bytes,
            ),
        )
    except Exception:
        if saved:
            await artifact_store.async_delete_object(namespace, object_id)
        raise


async def delete_deliverable_payload(artifact_store: Any, row: Any) -> None:
    namespace = getattr(row, "storage_namespace", None) or DELIVERABLE_STORAGE_NAMESPACE
    object_id = getattr(row, "storage_object_id", None) or getattr(row, "deliverable_id", None)
    if artifact_store is not None and isinstance(object_id, str) and object_id:
        await artifact_store.async_delete_object(namespace, object_id)


def attach_deliverable_payload(
    row: Any,
    *,
    content: str,
    rich_payload: dict[str, Any] | None,
    outputs: dict[str, Any] | None,
) -> Any:
    """Attach hydrated payload fields to an ORM row without mapping DB columns."""

    row.content = content
    row.rich_payload = rich_payload
    row.outputs = outputs or {}
    return row


async def hydrate_deliverable_payload(row: Any, artifact_store: Any) -> Any:
    """Load payload files from the object store into row transient attributes."""

    if row is None:
        return row
    if artifact_store is None:
        raise ValueError("deliverable hydration requires an artifact store")
    namespace = getattr(row, "storage_namespace", None) or DELIVERABLE_STORAGE_NAMESPACE
    object_id = getattr(row, "storage_object_id", None) or getattr(row, "deliverable_id", None)
    if not isinstance(object_id, str) or not object_id:
        raise FileNotFoundError("deliverable storage object id missing")

    content_key = getattr(row, "content_key", None) or DELIVERABLE_CONTENT_FILENAME
    content_bytes, _content_type = await artifact_store.async_load(
        namespace, object_id, content_key
    )
    content = content_bytes.decode("utf-8")

    rich_payload: dict[str, Any] | None = None
    rich_key = getattr(row, "rich_key", None)
    if isinstance(rich_key, str) and rich_key:
        rich_bytes, _rich_type = await artifact_store.async_load(namespace, object_id, rich_key)
        raw_rich = json.loads(rich_bytes.decode("utf-8"))
        rich_payload = raw_rich if isinstance(raw_rich, dict) else None
        if (
            rich_payload is not None
            and not rich_key.startswith(f"{DELIVERABLE_CHART_V1_RICH_KEY_PREFIX}.")
            and rich_payload_has_noncanonical_chart(rich_payload)
        ):
            rich_payload = None

    outputs: dict[str, Any] | None = {}
    outputs_key = getattr(row, "outputs_key", None)
    if isinstance(outputs_key, str) and outputs_key:
        outputs_bytes, _outputs_type = await artifact_store.async_load(
            namespace, object_id, outputs_key
        )
        raw_outputs = json.loads(outputs_bytes.decode("utf-8"))
        outputs = raw_outputs if isinstance(raw_outputs, dict) else {}

    return attach_deliverable_payload(
        row, content=content, rich_payload=rich_payload, outputs=outputs
    )


def attach_deliverable_projection(row: Any) -> Any:
    """Attach a payload-free projection for list/detail surfaces."""

    row.content = ""
    row.rich_payload = None
    row.outputs = {}
    return row
