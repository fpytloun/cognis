"""Authorized, revision-pinned text sources and author speech policy."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from cognis.artifacts.preview import classify_artifact_preview
from cognis.core.artifact_revisions import ContentActor
from cognis.core.content_refs import authorize_deliverable_access
from cognis.core.project_access import can_project_read_artifact
from cognis.core.speech import SpeechBinding, SpeechService, narration_text
from cognis.store.deliverable_storage import hydrate_deliverable_payload
from cognis.store.models import Agent, ArtifactRecordRow
from cognis.store.queries import get_deliverable

MAX_AUDIO_SOURCE_BYTES = 16 * 1024 * 1024


@dataclass
class AudioSource:
    """The exact source revision, never a mutable lineage head."""

    ref: str
    owner_email: str | None
    agent_id: str | None
    format: str
    content: str = ""
    rich_payload: dict[str, Any] | None = None
    supported: bool = True

    def spoken_text(self) -> str:
        """Extract readable source text without scripts, styling, or hidden markup."""
        content = self.content
        if self.format == "html":
            soup = BeautifulSoup(content, "html.parser")
            for element in soup(["script", "style", "head", "template"]):
                element.decompose()
            content = soup.get_text(" ", strip=True)
        return content if self.format in {"plain", "html"} else narration_text(content)


async def load_audio_source(
    speech: SpeechService, owner: str, ref: str, *, load: bool = False
) -> AudioSource:
    """Use the same human content authorization as ordinary document readers."""
    if ref.startswith("dlv_"):
        async with speech.sessions() as session:
            row = await get_deliverable(session, ref)
            if (
                row is None
                or row.status == "deleted"
                or not await authorize_deliverable_access(session, row, owner)
            ):
                raise PermissionError("Document not found")
        if load:
            await hydrate_deliverable_payload(row, speech.artifacts)
        return AudioSource(
            ref,
            row.owner_email,
            row.owner_agent_id,
            row.format,
            row.content or "",
            row.rich_payload,
        )
    if not ref.startswith(("art_", "att_", "doc_", "img_")):
        raise PermissionError("Document not found")
    async with speech.sessions() as session:
        artifact = await session.get(ArtifactRecordRow, ref)
        if artifact is not None and not (
            artifact.owner_email == owner
            or await can_project_read_artifact(session, artifact, owner)
        ):
            raise PermissionError("Document not found")
    if (
        artifact is None
        or artifact.status == "deleted"
        or artifact.deleted_at
        or (artifact.expires_at and artifact.expires_at.replace(tzinfo=UTC) <= datetime.now(UTC))
    ):
        raise PermissionError("Document not found")
    kind = classify_artifact_preview(artifact.filename, artifact.mime_type)
    supported = kind in {"text", "markdown", "html", "csv", "mermaid"}
    source = AudioSource(
        ref,
        artifact.owner_email,
        artifact.owner_agent_id,
        "html" if kind == "html" else "markdown" if kind == "markdown" else "plain",
        supported=supported,
    )
    if load:
        if not supported:
            raise ValueError("This artifact has no readable text for narration or a podcast.")
        if artifact.size_bytes > MAX_AUDIO_SOURCE_BYTES:
            raise ValueError("This document is too large for audio generation.")
        content, _ = await speech.artifacts.async_load(
            artifact.namespace, artifact.object_id, artifact.filename
        )
        if len(content) > MAX_AUDIO_SOURCE_BYTES:
            raise ValueError("This document is too large for audio generation.")
        if b"\0" in content:
            raise ValueError("This artifact has no readable text for audio generation.")
        source.content = content.decode("utf-8")
    return source


async def source_speech_binding(
    speech: SpeechService,
    owner: str,
    source: AudioSource,
    voice: str | None,
    *,
    validate_voice: bool = True,
) -> SpeechBinding:
    """Prefer the author's accessible TTS settings; never borrow another user's billing."""
    actor = ContentActor(owner, None, None, None, human=True)
    if source.agent_id and source.owner_email == owner:
        async with speech.sessions() as session:
            author = await session.get(Agent, source.agent_id)
        if (
            author is not None
            and author.status == "active"
            and (author.is_system or author.owner_email == owner)
        ):
            try:
                return await speech.binding(
                    replace(actor, agent_id=source.agent_id),
                    voice,
                    source.agent_id,
                    validate_voice=validate_voice,
                )
            except (ValueError, PermissionError):
                fallback = await speech.binding(actor, voice, validate_voice=validate_voice)
                return replace(
                    fallback,
                    warnings=(
                        *fallback.warnings,
                        "The author's TTS configuration is unavailable. Using the default instead.",
                    ),
                )
    return await speech.binding(actor, voice, validate_voice=validate_voice)
