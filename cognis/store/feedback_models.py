"""Feedback metadata only; bodies and immutable packets live in private object storage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from cognis.store.models import Base


def now() -> datetime:
    return datetime.now(UTC)


class FeedbackSubjectRow(Base):
    __tablename__ = "feedback_subjects"
    subject_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_key: Mapped[str] = mapped_column(String, unique=True)
    owner_email: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str | None] = mapped_column(String)
    source: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_digest: Mapped[str] = mapped_column(String)
    lineage_id: Mapped[str | None] = mapped_column(String, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)


class FeedbackAnchorRow(Base):
    __tablename__ = "feedback_anchors"
    anchor_id: Mapped[str] = mapped_column(String, primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("feedback_subjects.subject_id"), index=True)
    payload_id: Mapped[str] = mapped_column(String)
    content_digest: Mapped[str] = mapped_column(String)
    created_by: Mapped[str] = mapped_column(String)
    capture_id: Mapped[str | None] = mapped_column(String, index=True)


class FeedbackAnchorMappingRow(Base):
    __tablename__ = "feedback_anchor_mappings"
    original_anchor_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_subject_id: Mapped[str] = mapped_column(String, primary_key=True)
    mapped_anchor_id: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    payload_id: Mapped[str] = mapped_column(String)


class FeedbackThreadRow(Base):
    __tablename__ = "feedback_threads"
    thread_id: Mapped[str] = mapped_column(String, primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("feedback_subjects.subject_id"), index=True)
    anchor_id: Mapped[str] = mapped_column(ForeignKey("feedback_anchors.anchor_id"))
    author_id: Mapped[str] = mapped_column(String)
    creation_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    audience: Mapped[str] = mapped_column(String, default="internal")
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String, default="open")
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("author_id", "creation_key", name="uq_feedback_thread_creation"),
    )


class FeedbackEntryRow(Base):
    __tablename__ = "feedback_entries"
    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("feedback_threads.thread_id"), index=True)
    author_id: Mapped[str] = mapped_column(String)
    author_kind: Mapped[str] = mapped_column(String)
    author_label: Mapped[str] = mapped_column(String)
    audience: Mapped[str] = mapped_column(String)
    reply_to_entry_id: Mapped[str | None] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    head_version_id: Mapped[str] = mapped_column(String)
    creation_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("author_id", "creation_key", name="uq_feedback_entry_creation"),
    )


class FeedbackEntryVersionRow(Base):
    __tablename__ = "feedback_entry_versions"
    entry_version_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(ForeignKey("feedback_entries.entry_id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    payload_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("entry_id", "version", name="uq_feedback_entry_version"),)


class FeedbackPayloadRow(Base):
    __tablename__ = "feedback_payloads"
    payload_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String)
    digest: Mapped[str] = mapped_column(String)
    mime_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    adopted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now, index=True)


class FeedbackDraftRow(Base):
    __tablename__ = "feedback_drafts"
    owner_email: Mapped[str] = mapped_column(String, primary_key=True)
    draft_key: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    payload_id: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)


class FeedbackStagingScopeRow(Base):
    __tablename__ = "feedback_staging_scopes"
    owner_email: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)


class FeedbackStagingItemRow(Base):
    __tablename__ = "feedback_staging_items"
    owner_email: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_id: Mapped[str] = mapped_column(String, primary_key=True)
    entry_version_id: Mapped[str] = mapped_column(String)
    inclusion: Mapped[str] = mapped_column(String, default="prepared")
    version: Mapped[int] = mapped_column(Integer, default=1)
    subject_id: Mapped[str] = mapped_column(String)
    anchor_id: Mapped[str] = mapped_column(String)


class FeedbackPreflightRow(Base):
    __tablename__ = "feedback_preflights"
    preflight_id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_email: Mapped[str] = mapped_column(String, index=True)
    manifest_hash: Mapped[str] = mapped_column(String)
    payload_id: Mapped[str] = mapped_column(String)
    selection: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    safety_digest: Mapped[str | None] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))


class FeedbackSubmissionRow(Base):
    __tablename__ = "feedback_submissions"
    submission_id: Mapped[str] = mapped_column(String, primary_key=True)
    sender_email: Mapped[str] = mapped_column(String, index=True)
    creation_key: Mapped[str] = mapped_column(String)
    command_hash: Mapped[str] = mapped_column(String)
    preflight_id: Mapped[str] = mapped_column(ForeignKey("feedback_preflights.preflight_id"))
    manifest_hash: Mapped[str] = mapped_column(String)
    payload_id: Mapped[str] = mapped_column(String)
    command_payload_id: Mapped[str | None] = mapped_column(String)
    origin_conversation_id: Mapped[str | None] = mapped_column(String, index=True)
    destination_conversation_id: Mapped[str | None] = mapped_column(String)
    destination_agent_id: Mapped[str | None] = mapped_column(String)
    destination_task_id: Mapped[str | None] = mapped_column(String)
    delivery: Mapped[str] = mapped_column(String, default="pending", index=True)
    rejection_code: Mapped[str | None] = mapped_column(String)
    request_id: Mapped[str | None] = mapped_column(String, unique=True)
    turn_id: Mapped[str | None] = mapped_column(String)
    attempt_number: Mapped[int | None] = mapped_column(Integer)
    claim_epoch: Mapped[int] = mapped_column(Integer, default=0)
    claim_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("sender_email", "creation_key", name="uq_feedback_submission_creation"),
    )


class FeedbackSubmissionItemRow(Base):
    __tablename__ = "feedback_submission_items"
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_submissions.submission_id"), primary_key=True
    )
    entry_version_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_entry_versions.entry_version_id"), primary_key=True
    )
    subject_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String)


class FeedbackAuditRow(Base):
    __tablename__ = "feedback_audit"
    audit_id: Mapped[str] = mapped_column(String, primary_key=True)
    subject_id: Mapped[str] = mapped_column(String, index=True)
    thread_id: Mapped[str | None] = mapped_column(String)
    actor_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    payload_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)


class TaskRevisionReceiptRow(Base):
    __tablename__ = "task_revision_receipts"
    revision_request_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id"), index=True)
    actor_email: Mapped[str] = mapped_column(String)
    idempotency_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    attempt_number: Mapped[int] = mapped_column(Integer)
    submission_id: Mapped[str | None] = mapped_column(String, unique=True)
    state: Mapped[str] = mapped_column(String, default="pending")
    target_step: Mapped[str] = mapped_column(String)
    superseded_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("actor_email", "idempotency_key", name="uq_task_revision_receipt_key"),
    )


class FeedbackWorkLinkRow(Base):
    __tablename__ = "feedback_work_links"
    link_id: Mapped[str] = mapped_column(String, primary_key=True)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_submissions.submission_id"), index=True
    )
    kind: Mapped[str] = mapped_column(String)
    reference_id: Mapped[str] = mapped_column(String)
    parent_reference_id: Mapped[str | None] = mapped_column(String)
    turn_id: Mapped[str | None] = mapped_column(String)
    attempt_number: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)
    # The non-null primary key hashes the complete execution identity, including
    # turn/attempt; a conversation or task alone is not a unique child execution.


class FeedbackDispositionRow(Base):
    __tablename__ = "feedback_dispositions"
    disposition_id: Mapped[str] = mapped_column(String, primary_key=True)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_submissions.submission_id"), index=True
    )
    entry_version_id: Mapped[str] = mapped_column(String)
    disposition: Mapped[str] = mapped_column(String)
    result_ref: Mapped[str | None] = mapped_column(String)
    actor_session_id: Mapped[str] = mapped_column(String)
    payload_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)


class FeedbackGrantRow(Base):
    __tablename__ = "feedback_grants"
    subject_id: Mapped[str] = mapped_column(
        ForeignKey("feedback_subjects.subject_id"), primary_key=True
    )
    principal_email: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON)


class FeedbackShareRow(Base):
    __tablename__ = "feedback_shares"
    share_id: Mapped[str] = mapped_column(String, primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("feedback_subjects.subject_id"), index=True)
    owner_email: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    link_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="view", server_default="view"
    )
    token_format: Mapped[str] = mapped_column(
        String, nullable=False, default="random_v1", server_default="random_v1"
    )
    source: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_digest: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    allow_comments: Mapped[bool] = mapped_column(Boolean, default=False)
    discussion: Mapped[str] = mapped_column(String, default="own_only")
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (Index("ix_feedback_share_expiry", "expires_at"),)


class FeedbackGuestSessionRow(Base):
    __tablename__ = "feedback_guest_sessions"
    guest_session_id: Mapped[str] = mapped_column(String, primary_key=True)
    share_id: Mapped[str] = mapped_column(ForeignKey("feedback_shares.share_id"), index=True)
    token_hash: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class FeedbackCaptureRow(Base):
    __tablename__ = "feedback_captures"
    capture_id: Mapped[str] = mapped_column(String, primary_key=True)
    subject_id: Mapped[str] = mapped_column(ForeignKey("feedback_subjects.subject_id"), index=True)
    actor_id: Mapped[str] = mapped_column(String)
    creation_key: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    share_id: Mapped[str | None] = mapped_column(String)
    viewport: Mapped[dict[str, Any]] = mapped_column(JSON)
    render_state_digest: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="awaiting_pixels")
    payload_id: Mapped[str | None] = mapped_column(String)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("actor_id", "creation_key", name="uq_feedback_capture_creation"),
    )


class FeedbackPreferenceRow(Base):
    __tablename__ = "feedback_preferences"
    owner_email: Mapped[str] = mapped_column(String, primary_key=True)
    subject_id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)


class FeedbackRateBucketRow(Base):
    __tablename__ = "feedback_rate_buckets"
    bucket_key: Mapped[str] = mapped_column(String, primary_key=True)
    window_start: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer)


class FeedbackPublicOutcomeRow(Base):
    __tablename__ = "feedback_public_outcomes"
    outcome_id: Mapped[str] = mapped_column(String, primary_key=True)
    share_id: Mapped[str] = mapped_column(String, index=True)
    entry_version_id: Mapped[str] = mapped_column(String)
    disposition: Mapped[str] = mapped_column(String)
    payload_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), default=now)


FOUNDATION_TABLES = (
    FeedbackSubjectRow,
    FeedbackPayloadRow,
    FeedbackAnchorRow,
    FeedbackAnchorMappingRow,
    FeedbackThreadRow,
    FeedbackEntryRow,
    FeedbackEntryVersionRow,
    FeedbackDraftRow,
    FeedbackStagingScopeRow,
    FeedbackStagingItemRow,
    FeedbackPreflightRow,
    FeedbackSubmissionRow,
    FeedbackSubmissionItemRow,
    FeedbackAuditRow,
)
RECEIPT_TABLES = (TaskRevisionReceiptRow, FeedbackWorkLinkRow, FeedbackDispositionRow)
CAPABILITY_TABLES = (
    FeedbackGrantRow,
    FeedbackShareRow,
    FeedbackGuestSessionRow,
    FeedbackCaptureRow,
    FeedbackPreferenceRow,
    FeedbackRateBucketRow,
    FeedbackPublicOutcomeRow,
)
