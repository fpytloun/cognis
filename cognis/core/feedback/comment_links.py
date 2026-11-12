"""Comment-enabled document links shared by agent publication tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from cognis.core.feedback.primitives import Actor, FeedbackError, aware
from cognis.core.feedback.public import PublicFeedback
from cognis.models import feedback as dto
from cognis.store.models import ArtifactRecordRow, DeliverableRow
from cognis.store.queries import get_artifact_record

COMMENT_LINK_FIELDS = (
    "comment_url",
    "comment_share_id",
    "comment_link_expires_at",
    "comment_link_status",
    "comment_link_error",
    "allow_comments",
)


async def create_comment_link(
    sharing: PublicFeedback | None,
    *,
    content_ref: str,
    user_email: str | None,
    agent_id: str | None,
    ttl_seconds: int = 604800,
    published_by_call: bool = False,
) -> dict[str, Any]:
    """Mint a pinned, revocable link without converting read grants into disclosure."""
    if sharing is None or not user_email or not agent_id:
        raise FeedbackError(503, "comment_sharing_unavailable", "Comment sharing is unavailable")
    base_url = sharing.sources.store.base_url.rstrip("/")
    if not base_url:
        raise FeedbackError(503, "comment_sharing_unavailable", "Public origin is not configured")
    expires_at = datetime.now(UTC) + timedelta(seconds=max(1, min(ttl_seconds, 604800)))
    row: ArtifactRecordRow | DeliverableRow | None
    async with sharing.factory() as db:
        if content_ref.startswith("dlv_"):
            row = await db.get(DeliverableRow, content_ref)
        else:
            row = await get_artifact_record(db, content_ref)
            if row is not None and row.expires_at is not None:
                expires_at = min(expires_at, aware(row.expires_at))
        if row is None or (not published_by_call and row.owner_agent_id != agent_id):
            raise FeedbackError(404, "not_found", "Comment sharing is unavailable for this agent")
    actor = Actor.user(user_email)
    resolved = await sharing.sources.resolve(
        actor,
        dto.ResolveSourceRequest(
            source=dto.DocumentSourceRef(kind="document", content_ref=content_ref),
            context=dto.StandaloneFeedbackContext(kind="standalone"),
        ),
    )
    share = await sharing.create_share(
        actor,
        resolved.subject_id,
        dto.ShareRequest(
            source=resolved.source,
            expected_content_digest=resolved.content_digest,
            allow_comments=True,
            discussion="own_only",
            expires_at=expires_at,
        ),
    )
    if not share.share_token:
        raise FeedbackError(503, "comment_link_unconfirmed", "Check existing links before retrying")
    return {
        "comment_url": f"{base_url}/feedback#{quote(share.share_token, safe='')}",
        "comment_share_id": share.share_id,
        "comment_link_expires_at": aware(share.expires_at).isoformat(),
        "comment_link_status": "ready",
        "allow_comments": True,
    }


async def publication_comment_link(
    sharing: PublicFeedback | None,
    *,
    requested: bool,
    content_ref: str,
    user_email: str | None,
    agent_id: str | None,
) -> dict[str, Any]:
    """Keep committed content on link failure; never retry publication or grant creation."""
    if not requested:
        return {}
    try:
        return await create_comment_link(
            sharing,
            content_ref=content_ref,
            user_email=user_email,
            agent_id=agent_id,
            published_by_call=True,
        )
    except Exception as exc:
        return {
            "allow_comments": None,
            "comment_link_status": "unconfirmed",
            "comment_link_error": {
                "code": exc.code if isinstance(exc, FeedbackError) else "comment_link_unconfirmed",
                "message": (
                    "Content was published. Do not republish it. Check existing sharing links "
                    "before requesting another comment link."
                ),
            },
        }
