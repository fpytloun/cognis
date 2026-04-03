"""Artifact and attachment domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ArtifactKind(StrEnum):
    IMAGE = "image"
    AUDIO = "audio"
    PDF = "pdf"
    FILE = "file"
    VIDEO = "video"


class ArtifactStatus(StrEnum):
    TEMPORARY = "temporary"
    ATTACHED = "attached"
    DELETED = "deleted"


class AttachmentRef(BaseModel):
    artifact_id: str
    kind: ArtifactKind
    mime_type: str
    filename: str
    size_bytes: int
    url: str | None = None


class ArtifactRecord(BaseModel):
    artifact_id: str
    namespace: str
    object_id: str
    filename: str
    owner_email: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    message_role: str | None = None
    purpose: str = "chat_input"
    kind: ArtifactKind = ArtifactKind.FILE
    mime_type: str = "application/octet-stream"
    size_bytes: int = 0
    status: ArtifactStatus = ArtifactStatus.TEMPORARY
    created_at: datetime | None = None
    expires_at: datetime | None = None
    deleted_at: datetime | None = None


class SignedArtifactURL(BaseModel):
    artifact_id: str
    url: str
    expires_at: datetime | None = None
