"""Helpers for summarising attachment metadata in text contexts."""

from __future__ import annotations

from typing import Any


def attachment_note(attachments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for attachment in attachments:
        filename = str(attachment.get("filename") or attachment.get("artifact_id") or "attachment")
        kind = str(attachment.get("kind") or "file")
        parts.append(f"{filename} ({kind})")
    return "Attachments: " + ", ".join(parts)


def merge_content_and_attachment_note(content: str, attachments: list[dict[str, Any]]) -> str:
    if not attachments:
        return content
    note = attachment_note([a for a in attachments if isinstance(a, dict)])
    if not content.strip():
        return note
    return f"{content}\n\n{note}"
