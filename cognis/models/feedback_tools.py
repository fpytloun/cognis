"""Strict arguments for submission-scoped controller tools."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from cognis.models.feedback import Digest, FeedbackModel, Id


class FeedbackReadArguments(FeedbackModel):
    submission_id: Id
    manifest_hash: Digest
    view: Literal["manifest", "source", "capture"] = "manifest"
    source_id: Id | None = None
    capture_id: Id | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=32768, ge=1, le=65536)
    prompt: str | None = Field(default=None, max_length=2000)


class CommentReplyArguments(FeedbackModel):
    """Answer one sent comment: reply, resolve, or both."""

    comment_id: Id = Field(description="The comment id shown in the comments context (fbt_…).")
    reply: str | None = Field(
        default=None,
        min_length=1,
        max_length=8192,
        description="Short reply to the comment author: what you did or why not.",
    )
    resolve: bool = Field(
        default=False, description="Mark the comment resolved once it has been handled."
    )

    @model_validator(mode="after")
    def reply_or_resolve(self) -> Self:
        if not self.reply and not self.resolve:
            raise ValueError("Provide a reply, set resolve, or both")
        return self


class FeedbackRecordArguments(FeedbackModel):
    submission_id: Id
    manifest_hash: Digest
    action: Literal["disposition", "result", "child"]
    entry_version_id: Id | None = None
    disposition: Literal["addressed", "needs_clarification", "not_changed"] | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=8192)
    result_ref: Id | None = None
    relation: Literal["revises_source", "separate_result", "answer_only"] | None = None
    child_conversation_id: Id | None = None
    child_task_id: Id | None = None
    child_session_id: Id | None = None
    child_turn_id: Id | None = None
    child_attempt_number: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def action_fields(self) -> Self:
        if self.action == "disposition" and not (
            self.entry_version_id and self.disposition and self.reason
        ):
            raise ValueError("disposition requires an exact selected entry, disposition and reason")
        if self.action == "result" and not (self.result_ref and self.relation):
            raise ValueError("result requires result_ref and relation")
        if self.action == "child":
            if (
                sum(
                    bool(ref)
                    for ref in (
                        self.child_conversation_id,
                        self.child_task_id,
                        self.child_session_id,
                    )
                )
                != 1
            ):
                raise ValueError(
                    "child requires exactly one concrete conversation, task or session"
                )
            if self.child_task_id and self.child_attempt_number is None:
                raise ValueError("child task requires an exact attempt")
        return self
