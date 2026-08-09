"""Model-only envelopes for user messages."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape, quoteattr


def utc_timestamp(value: datetime | str | None = None) -> str:
    """Return a stable UTC ISO-8601 timestamp."""
    if value is None:
        parsed = datetime.now(UTC)
    elif isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def message_metadata(
    *,
    ts: datetime | str | None = None,
    channel: str | None = None,
    sender: str | None = None,
    untrusted: bool = False,
) -> dict[str, Any]:
    """Build compact persisted metadata with only applicable fields."""
    result: dict[str, Any] = {"ts": utc_timestamp(ts)}
    if channel:
        result["channel"] = channel.strip().lower()
    if sender:
        result["sender"] = sender.strip()
    if untrusted:
        result["untrusted"] = True
    return result


def normalize_message_metadata(
    value: Any,
    *,
    fallback_ts: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Normalize persisted metadata without inventing provenance."""
    raw = value if isinstance(value, dict) else {}
    raw_ts = raw.get("ts") or fallback_ts
    if raw_ts is None:
        return None
    provenance = {
        "channel": raw.get("channel") if isinstance(raw.get("channel"), str) else None,
        "sender": raw.get("sender") if isinstance(raw.get("sender"), str) else None,
        "untrusted": raw.get("untrusted") is True,
    }
    try:
        result = message_metadata(ts=raw_ts, **provenance)
    except (TypeError, ValueError):
        if fallback_ts is None or raw_ts == fallback_ts:
            return None
        try:
            result = message_metadata(ts=fallback_ts, **provenance)
        except (TypeError, ValueError):
            return None
    return result


def render_message_envelope(content: str, metadata: dict[str, Any]) -> str:
    """Render one escaped model envelope."""
    attributes: list[str] = []
    for key in ("ts", "channel", "sender"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            attributes.append(f"{key}={quoteattr(value, {'"': '&quot;'})}")
    if metadata.get("untrusted") is True:
        attributes.append('untrusted="true"')
    suffix = f" {' '.join(attributes)}" if attributes else ""
    return f"<message{suffix}>{escape(content)}</message>"


def render_user_message(
    content: str,
    metadata: Any,
    contextual_messages: Any = None,
    *,
    fallback_ts: datetime | str | None = None,
    max_content_chars: int | None = None,
) -> str:
    """Render contextual messages in supplied order and the primary message last."""
    primary_metadata = normalize_message_metadata(metadata, fallback_ts=fallback_ts)
    if primary_metadata is None:
        return content

    rendered: list[str] = []
    if isinstance(contextual_messages, list):
        for item in contextual_messages:
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                continue
            item_metadata = normalize_message_metadata(item.get("message_metadata"))
            if item_metadata is None:
                continue
            item_content = _truncate_content(item["content"], max_content_chars)
            rendered.append(render_message_envelope(item_content, item_metadata))
    rendered.append(
        render_message_envelope(_truncate_content(content, max_content_chars), primary_metadata)
    )
    return "\n".join(rendered)


def render_user_event_content(
    event: Any,
    *,
    content_override: str | None = None,
    max_content_chars: int | None = None,
) -> str:
    """Render a cached or dictionary user event for model or compaction input."""
    if isinstance(event, dict):
        data = event.get("data", {})
        fallback_ts = event.get("ts")
    else:
        data = getattr(event, "data", {})
        fallback_ts = getattr(event, "ts", None)
    if not isinstance(data, dict):
        return ""
    content = content_override if content_override is not None else data.get("content")
    if not isinstance(content, str):
        return ""
    return render_user_message(
        content,
        data.get("message_metadata"),
        data.get("context_messages"),
        fallback_ts=fallback_ts,
        max_content_chars=max_content_chars,
    )


def _truncate_content(content: str, max_chars: int | None) -> str:
    if max_chars is None or len(content) <= max_chars:
        return content
    marker = "\n...[truncated]...\n"
    if max_chars <= len(marker):
        return content[:max_chars]
    remaining = max_chars - len(marker)
    head = remaining // 2
    return content[:head] + marker + content[-(remaining - head) :]
