"""Canonical versioned Work Feedback wire contracts.

Browser observations are locators, not authentication or execution authority.
"""

from __future__ import annotations

from datetime import datetime
from types import GenericAlias
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

Id = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Version = Annotated[int, Field(ge=0, strict=True)]
Positive = Annotated[int, Field(ge=1, strict=True)]


def bounded_draft_body(value: str) -> str:
    """Bound UTF-8 storage while preserving unfinished draft text."""
    if len(value.encode("utf-8")) > 16384:
        raise ValueError("body must fit 16 KiB")
    return value


def nonblank_entry_body(value: str) -> str:
    """Require text for every persisted discussion entry."""
    bounded_draft_body(value)
    if not value.strip():
        raise ValueError("body must contain text")
    return value


EntryBody = Annotated[str, AfterValidator(nonblank_entry_body)]
Body = Annotated[EntryBody, Field(min_length=1, max_length=16384)]
DraftBody = Annotated[str, Field(max_length=16384), AfterValidator(bounded_draft_body)]


class FeedbackModel(BaseModel):
    """Reject unknown properties and non-finite geometry on every boundary."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)


class EventRef(FeedbackModel):
    store: Id
    session_id: Id
    seq: Version
    event_id: Id | None = None
    event_type: Id


class DocumentSourceRef(FeedbackModel):
    kind: Literal["document"]
    content_ref: Id


class TranscriptSourceRef(FeedbackModel):
    kind: Literal["transcript"]
    conversation_id: Id
    source_event: EventRef
    source_item_id: Id
    field_path: list[str] = Field(max_length=16)
    representation: Literal["chat-item-as-of-event-v1"] | None = None

    @model_serializer(mode="wrap")
    def serialize_source(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Keep persisted legacy source keys, token bindings and manifest bytes unchanged."""
        result: dict[str, Any] = handler(self)
        if self.representation is None:
            result.pop("representation", None)
        return result

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """Omitting an optional key must not erase strict response properties."""
        structural = dict(core_schema)
        structural.pop("serialization", None)
        return handler(structural)


class WorkDiffSourceRef(FeedbackModel):
    kind: Literal["work_diff"]
    activity_scope_id: Id
    source_event: EventRef
    source_item_id: Id
    file_ordinal: Annotated[int, Field(ge=0, le=1000, strict=True)]
    root_id: Id | None = None
    path: str = Field(min_length=1, max_length=4096)
    old_path: str | None = Field(default=None, max_length=4096)


FeedbackSourceRef = Annotated[
    DocumentSourceRef | TranscriptSourceRef | WorkDiffSourceRef, Field(discriminator="kind")
]


class ChatFeedbackContext(FeedbackModel):
    kind: Literal["chat"]
    conversation_id: Id


class StandaloneFeedbackContext(FeedbackModel):
    kind: Literal["standalone"]


FeedbackContext = Annotated[
    ChatFeedbackContext | StandaloneFeedbackContext, Field(discriminator="kind")
]


class TextRange(FeedbackModel):
    representation: Literal["feedback-text-v1"]
    text_digest: Digest
    start: Version
    end: Version
    exact: str = Field(max_length=2048)
    prefix: str = Field(default="", max_length=512)
    suffix: str = Field(default="", max_length=512)
    start_line: Positive | None = None
    start_column: Positive | None = None
    end_line: Positive | None = None
    end_column: Positive | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start or len(self.exact.encode("utf-8")) > 2048:
            raise ValueError("invalid text range")
        return self


class Point(FeedbackModel):
    x: float = Field(ge=0, le=100000)
    y: float = Field(ge=0, le=100000)


class Pin(FeedbackModel):
    kind: Literal["pin"]
    point: Point


class Area(FeedbackModel):
    kind: Literal["rectangle", "ellipse"]
    x: float = Field(ge=0, le=100000)
    y: float = Field(ge=0, le=100000)
    width: float = Field(gt=0, le=100000)
    height: float = Field(gt=0, le=100000)


class Arrow(FeedbackModel):
    kind: Literal["arrow"]
    from_: Point = Field(alias="from")
    to: Point


class Freehand(FeedbackModel):
    kind: Literal["freehand"]
    points: list[Point] = Field(min_length=2, max_length=2048)


Shape = Annotated[Pin | Area | Arrow | Freehand, Field(discriminator="kind")]


class Viewport(FeedbackModel):
    width_css_px: float = Field(gt=0, le=16384)
    height_css_px: float = Field(gt=0, le=16384)
    scroll_x_css_px: float = Field(ge=0, le=100000)
    scroll_y_css_px: float = Field(ge=0, le=100000)
    device_pixel_ratio: float = Field(gt=0, le=8)


class WholeLocator(FeedbackModel):
    kind: Literal["whole"]


class TextLocator(FeedbackModel):
    kind: Literal["text"]
    range: TextRange


class RichTableRange(FeedbackModel):
    """Exact authored row membership and a half-open authored column interval."""

    representation: Literal["rich-table-v1"]
    scope: Literal["cells", "rows", "columns"]
    rows: list[Annotated[int, Field(ge=0, lt=1000, strict=True)]] = Field(
        min_length=1, max_length=1000
    )
    column_start: Annotated[int, Field(ge=0, lt=128, strict=True)]
    column_end: Annotated[int, Field(ge=1, le=128, strict=True)]

    @model_validator(mode="after")
    def bounded_membership(self) -> Self:
        width = self.column_end - self.column_start
        if width <= 0 or any(a >= b for a, b in zip(self.rows, self.rows[1:], strict=False)):
            raise ValueError("table range requires ordered unique rows and columns")
        if len(self.rows) * width > 4096:
            raise ValueError("table range exceeds the 4096 cell budget")
        return self


class RichLocator(FeedbackModel):
    kind: Literal["rich"]
    block_id: Id
    identity: Literal["semantic", "revision_local"]
    item_key: str | None = Field(default=None, max_length=512)
    field_path: list[str] = Field(max_length=16)
    range: TextRange | None = None
    table_range: RichTableRange | None = None

    @model_validator(mode="after")
    def structural_range(self) -> Self:
        if self.table_range is not None and (
            self.field_path or self.item_key is not None or self.range is not None
        ):
            raise ValueError("table ranges cannot mix field, item or text targets")
        return self

    @model_serializer(mode="wrap")
    def serialize_locator(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Preserve persisted v1 request/manifest hashes for legacy rich locators."""
        result: dict[str, Any] = handler(self)
        if self.table_range is None:
            result.pop("table_range", None)
        return result


class ShapesLocator(FeedbackModel):
    shapes: list[Shape] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def point_budget(self) -> Self:
        count = sum(len(s.points) if isinstance(s, Freehand) else 4 for s in self.shapes)
        if count > 8192:
            raise ValueError("annotation exceeds point budget")
        return self


class HtmlLocator(ShapesLocator):
    kind: Literal["html"]
    element_key: str | None = Field(default=None, max_length=512)
    selector_path: str | None = Field(default=None, max_length=2048)
    range: TextRange | None = None
    viewport: Viewport
    render_state_digest: Digest
    capture_id: Id | None = None

    @model_validator(mode="after")
    def captured_geometry(self) -> Self:
        if self.shapes and not self.capture_id:
            raise ValueError("HTML geometry requires confirmed observed capture")
        return self


class ImageLocator(ShapesLocator):
    kind: Literal["image"]
    intrinsic_width: Annotated[int, Field(ge=1, le=100000, strict=True)]
    intrinsic_height: Annotated[int, Field(ge=1, le=100000, strict=True)]
    orientation: Annotated[int, Field(ge=1, le=8, strict=True)]


class PdfLocator(ShapesLocator):
    kind: Literal["pdf"]
    page: Positive
    page_width_pt: float = Field(gt=0, le=100000)
    page_height_pt: float = Field(gt=0, le=100000)
    rotation: Literal[0, 90, 180, 270]
    range: TextRange | None = None


class DiffLocator(FeedbackModel):
    kind: Literal["diff"]
    scope: Literal["file", "lines"]
    side: Literal["old", "new"] | None = None
    start_line: Positive | None = None
    end_line: Positive | None = None
    hunk_digest: Digest | None = None
    quote: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def valid_lines(self) -> Self:
        if self.scope == "lines" and (
            self.side is None
            or self.start_line is None
            or self.end_line is None
            or self.end_line < self.start_line
            or self.hunk_digest is None
        ):
            raise ValueError("line feedback requires side, ordered lines and exact hunk digest")
        if self.scope == "file" and any(
            item is not None for item in (self.side, self.start_line, self.end_line)
        ):
            raise ValueError("file feedback cannot claim line evidence")
        return self


FeedbackLocator = Annotated[
    WholeLocator
    | TextLocator
    | RichLocator
    | HtmlLocator
    | ImageLocator
    | PdfLocator
    | DiffLocator,
    Field(discriminator="kind"),
]


class AnchorInput(FeedbackModel):
    schema_version: Literal[1] = 1
    source_token: str = Field(min_length=1, max_length=16384)
    content_digest: Digest
    locator: FeedbackLocator


class FeedbackAnchor(FeedbackModel):
    anchor_id: Id
    source: FeedbackSourceRef
    content_digest: Digest
    locator: FeedbackLocator
    schema_version: Literal[1] = 1


class FeedbackCapabilities(FeedbackModel):
    read: bool = False
    comment: bool = False
    moderate: bool = False
    dispatch: bool = False
    disclose: bool = False


class CanonicalText(FeedbackModel):
    representation: Literal["feedback-text-v1"] = "feedback-text-v1"
    text_digest: Digest
    length: Version
    available_ranges: list[tuple[int, int]]


class ResolveSourceRequest(FeedbackModel):
    source: FeedbackSourceRef
    context: FeedbackContext


class ResolvedFeedbackSource(FeedbackModel):
    subject_id: Id
    source: FeedbackSourceRef
    source_token: str
    context: FeedbackContext
    content_digest: Digest
    lineage_id: Id | None = None
    revision: Positive | None = None
    latest_content_ref: Id | None = None
    format: str
    title: str | None = None
    availability: Literal["available", "partial", "unavailable"]
    permitted_locator_kinds: list[str]
    capabilities: FeedbackCapabilities
    canonical_text: CanonicalText | None = None


class SelectedVersion(FeedbackModel):
    entry_version_id: Id
    expected_entry_version: Positive
    staging_version: Positive | None = None


class OriginChatDestination(FeedbackModel):
    kind: Literal["origin_chat"]
    conversation_id: Id


class ConversationDestination(FeedbackModel):
    kind: Literal["producer_conversation", "existing_followup"]
    conversation_id: Id


class NewFollowupDestination(FeedbackModel):
    kind: Literal["new_followup"]
    agent_id: Id
    project_id: Id | None = None
    title: str | None = Field(default=None, max_length=200)


class TaskRevisionDestination(FeedbackModel):
    kind: Literal["task_revision"]
    task_id: Id
    expected_attempt: Positive
    target_step: Id


Destination = Annotated[
    OriginChatDestination
    | ConversationDestination
    | NewFollowupDestination
    | TaskRevisionDestination,
    Field(discriminator="kind"),
]
UpdateMode = Literal["follow_request", "revise_source", "answer_only", "separate_result"]


class FeedbackPreflightRequest(FeedbackModel):
    context: FeedbackContext
    selection: list[SelectedVersion] = Field(min_length=1, max_length=100)
    # Ignored: selecting a guest comment is its review. Accepted for already frozen reviews and
    # installed PWA clients that still send it; remove once neither can carry the field.
    reviewed_guest_entry_version_ids: list[Id] = Field(default_factory=list, max_length=100)
    overall_message: str | None = Field(default=None, max_length=100000)
    destination: Destination
    update_mode: UpdateMode = "follow_request"

    @model_validator(mode="after")
    def explicit_selection(self) -> Self:
        if len({v.entry_version_id for v in self.selection}) != len(self.selection):
            raise ValueError("duplicate selected version")
        if self.context.kind == "chat" and (
            self.destination.kind != "origin_chat"
            or self.destination.conversation_id != self.context.conversation_id
            or self.overall_message is not None
        ):
            raise ValueError("in-chat feedback stays in its originating composer")
        if self.context.kind == "standalone" and self.destination.kind == "origin_chat":
            raise ValueError("standalone feedback has no originating chat")
        return self


class IncludedFeedback(FeedbackModel):
    creation_key: Id
    preflight_id: Id
    manifest_hash: Digest


class SubmissionCreateRequest(IncludedFeedback):
    replaces_rejected_submission_id: Id | None = None


class ConversationAdmissionLink(FeedbackModel):
    kind: Literal["conversation_turn"]
    request_id: Id
    conversation_id: Id
    turn_id: Id


class TaskAdmissionLink(FeedbackModel):
    kind: Literal["task_attempt"]
    revision_request_id: Id
    task_id: Id
    attempt_number: Positive


AdmissionLink = Annotated[
    ConversationAdmissionLink | TaskAdmissionLink, Field(discriminator="kind")
]


class Rejection(FeedbackModel):
    code: str
    recoverable: bool


FeedbackDeliveryState = Literal["pending", "accepted", "rejected", "uncertain"]
FeedbackWorkState = Literal[
    "queued", "working", "waiting_for_input", "completed", "failed", "cancelled", "unavailable"
]


class SubmissionReceipt(FeedbackModel):
    submission_id: Id
    creation_key: Id
    manifest_hash: Digest
    delivery: FeedbackDeliveryState
    work: FeedbackWorkState
    reconciling: bool
    version: Positive
    admission: AdmissionLink | None = None
    rejection: Rejection | None = None
    check_delivery_available: bool
    review_again_available: bool


class FeedbackReference(FeedbackModel):
    submission_id: Id
    manifest_hash: Digest


class EntryDelivery(FeedbackReference):
    delivery: Literal["pending", "accepted", "rejected", "uncertain"]


class Attribution(FeedbackModel):
    kind: Literal["verified_user", "guest_unverified", "agent"]
    label: str
    is_self: bool = False


class EntryVersion(FeedbackModel):
    entry_id: Id
    entry_version_id: Id
    version: Positive
    thread_id: Id
    body: EntryBody
    author: Attribution
    audience: str
    reply_to_entry_id: Id | None = None
    created_at: datetime
    delivery: list[EntryDelivery] = Field(default_factory=list)


class FeedbackThread(FeedbackModel):
    thread_id: Id
    subject_id: Id
    version: Positive
    state: Literal["open", "resolved"]
    hidden: bool
    audience: str
    anchor: FeedbackAnchor
    entries: list[EntryVersion]


class ThreadPage(FeedbackModel):
    items: list[FeedbackThread]
    next_cursor: str | None = None


class ThreadCreateRequest(FeedbackModel):
    creation_key: Id
    context: FeedbackContext
    anchor: AnchorInput
    body: Body
    audience: Literal["internal"] = "internal"


class EntryCreateRequest(FeedbackModel):
    creation_key: Id
    expected_thread_version: Positive
    reply_to_entry_id: Id | None = None
    body: Body
    context: FeedbackContext


class PublicEntryEditRequest(FeedbackModel):
    expected_version: Positive
    body: Body


class EntryEditRequest(PublicEntryEditRequest):
    context: FeedbackContext | None = None


class ResolutionRequest(FeedbackModel):
    expected_version: Positive
    state: Literal["open", "resolved"]
    reason: str = Field(max_length=2048)


class ReattachRequest(FeedbackModel):
    expected_version: Positive
    anchor: AnchorInput
    reason: str = Field(min_length=1, max_length=2048)


class ModerationRequest(FeedbackModel):
    expected_version: Positive
    action: Literal["hide", "restore"]
    reason: str = Field(min_length=1, max_length=2048)


class StagingUpdateRequest(FeedbackModel):
    expected_version: Version
    entry_version_id: Id
    inclusion: Literal["prepared", "included"]


class StagedItem(FeedbackModel):
    entry_id: Id
    entry_version_id: Id
    staging_version: Positive
    inclusion: Literal["prepared", "included"]
    subject_id: Id
    anchor_id: Id


class StagingSnapshot(FeedbackModel):
    conversation_id: Id
    version: Version
    # Only comments still ready to include; resolved or removed ones are omitted unless included.
    items: list[StagedItem]
    receipts: list[SubmissionReceipt]
    # Every subject commented on from this conversation, including resolved or removed comments.
    subject_ids: list[Id] = Field(default_factory=list)


class DraftRequest(FeedbackModel):
    expected_version: Version
    context: FeedbackContext
    source: FeedbackSourceRef | None = None
    anchor: AnchorInput | None = None
    body: DraftBody = ""
    selection: list[SelectedVersion] = Field(default_factory=list, max_length=100)
    overall_message: str | None = Field(default=None, max_length=100000)


class DraftResponse(FeedbackModel):
    draft_key: Id
    version: Positive
    draft: DraftRequest


class TaskConsequences(FeedbackModel):
    safety_token: str
    expected_attempt: Positive
    workflow_definition_digest: Digest
    target_step: str
    downstream_steps: list[str]
    side_effect_warning: str
    pending_interactions: list[PendingInteraction] = Field(default_factory=list)


class PendingInteraction(FeedbackModel):
    notification_id: Id
    revision: Positive
    notification_type: str
    status: str


class ManifestEntry(FeedbackModel):
    entry: EntryVersion
    anchor: FeedbackAnchor
    role: Literal["instruction", "context"]


class FeedbackPreflight(FeedbackModel):
    preflight_id: Id
    manifest_hash: Digest
    expires_at: datetime
    selection: list[SelectedVersion]
    context_items: list[ManifestEntry]
    source_summaries: list[ResolvedFeedbackSource]
    destination: Destination
    update_mode: UpdateMode
    task_consequences: TaskConsequences | None = None
    warnings: list[str] = Field(default_factory=list)


class SubmissionManifest(FeedbackModel):
    schema_version: Literal[1] = 1
    context: FeedbackContext
    entries: list[ManifestEntry]
    sources: list[ManifestSource]
    destination: Destination
    update_mode: UpdateMode
    overall_message: str | None = None
    task_consequences: TaskConsequences | None = None


class ManifestSource(FeedbackModel):
    subject_id: Id
    source: FeedbackSourceRef
    content_digest: Digest
    lineage_id: Id | None = None
    revision: Positive | None = None
    format: str
    title: str | None = None
    availability: Literal["available", "partial", "unavailable"]
    canonical_text: CanonicalText | None = None


class SubmissionDetail(FeedbackModel):
    receipt: SubmissionReceipt
    manifest: SubmissionManifest
    children: list[ChildWorkResponse] = Field(default_factory=list)


class DestinationRequest(FeedbackModel):
    context: FeedbackContext
    subject_ids: list[Id] = Field(min_length=1, max_length=20)
    # Where the document is being viewed. It only chooses the recommended option; every
    # destination is still validated on its own.
    surface: Literal["task", "library", "other"] | None = None


class DestinationOption(FeedbackModel):
    destination: Destination | None = None
    available: bool
    reason: str | None = None
    label: str
    description: str | None = None
    recommended: bool = False


class DestinationResponse(FeedbackModel):
    options: list[DestinationOption]


class CaptureRequest(FeedbackModel):
    creation_key: Id
    source_token: str = Field(max_length=16384)
    content_digest: Digest
    viewport: Viewport
    render_state_digest: Digest
    method: Literal["observed_pixels"] = "observed_pixels"


class CaptureResponse(FeedbackModel):
    capture_id: Id
    status: Literal["awaiting_pixels", "ready", "failed"]
    fidelity: Literal["observed"] = "observed"
    version: Positive
    upload_limit_bytes: int = 8 * 1024 * 1024
    error_code: str | None = None
    width: int | None = None
    height: int | None = None
    trust: Literal["untrusted_user_provided"] = "untrusted_user_provided"


class GrantRequest(FeedbackModel):
    expected_version: Version
    principal_email: str = Field(max_length=320)
    capabilities: FeedbackCapabilities


class GrantResponse(GrantRequest):
    version: Positive


class GrantListResponse(FeedbackModel):
    items: list[GrantResponse]


class SubjectSummary(FeedbackModel):
    subject_id: Id
    capabilities: FeedbackCapabilities
    open_threads: Version
    resolved_threads: Version
    preferred_followup: str | None = None
    version: Positive


class FollowupPreferenceRequest(FeedbackModel):
    expected_version: Version
    conversation_id: Id | None = None


class AnchorMapping(FeedbackModel):
    original_anchor_id: Id
    mapped_anchor_id: Id | None = None
    category: Literal["exact", "remapped", "not_located"]
    target_content_ref: str
    reason: str
    version: Positive


class CanonicalTextPage(FeedbackModel):
    representation: Literal["feedback-text-v1"] = "feedback-text-v1"
    text_digest: Digest
    start: Version
    end: Version
    total_length: Version
    text: str


class DocumentTextScope(FeedbackModel):
    kind: Literal["document"]


class RichFieldTextScope(FeedbackModel):
    kind: Literal["rich_field"]
    block_id: Id
    identity: Literal["semantic", "revision_local"]
    item_key: str | None = Field(default=None, max_length=512)
    field_path: list[str] = Field(max_length=16)


class PdfPageTextScope(FeedbackModel):
    kind: Literal["pdf_page"]
    page: Positive


FeedbackTextScope = Annotated[
    DocumentTextScope | RichFieldTextScope | PdfPageTextScope, Field(discriminator="kind")
]


class ScopedTextRequest(FeedbackModel):
    source: FeedbackSourceRef
    context: FeedbackContext
    scope: FeedbackTextScope
    start: Version = 0
    limit: Annotated[int, Field(ge=1, le=65536, strict=True)] = 16384


class ScopedCanonicalFeedbackText(FeedbackModel):
    scope: FeedbackTextScope
    content_digest: Digest
    descriptor: CanonicalText
    text_page: CanonicalTextPage


class HistoryItem(FeedbackModel):
    kind: Literal["submission", "resolution", "reattach", "moderation", "disposition", "result"]
    subject_id: Id
    created_at: datetime
    receipt: SubmissionReceipt | None = None
    entry_version_id: Id | None = None
    state: str | None = None
    reason: str | None = None
    result_ref: str | None = None


class HistoryPage(FeedbackModel):
    items: list[HistoryItem]
    next_cursor: str | None = None


class ShareRequest(FeedbackModel):
    link_mode: Literal["view", "download"] = "view"
    source: DocumentSourceRef
    expected_content_digest: Digest
    allow_comments: bool = False
    discussion: Literal["own_only", "shared"] = "own_only"
    expires_at: datetime


class ShareResponse(FeedbackModel):
    link_mode: Literal["view", "download"] = "view"
    can_copy: bool = False
    share_id: Id
    share_token: str | None = None
    version: Positive
    allow_comments: bool
    discussion: Literal["own_only", "shared"]
    expires_at: datetime
    revoked: bool


class ShareListResponse(FeedbackModel):
    items: list[ShareResponse]


class ShareUpdateRequest(FeedbackModel):
    expected_version: Positive
    allow_comments: bool | None = None
    discussion: Literal["own_only", "shared"] | None = None
    expires_at: datetime | None = None
    revoked: bool | None = None


class PublicExchangeRequest(FeedbackModel):
    share_token: str = Field(max_length=1024)
    create_guest: bool = False
    display_name: str | None = Field(default=None, max_length=80)


class PublicSource(FeedbackModel):
    source_token: str
    content_digest: Digest
    content_ref: Id
    format: str
    title: str | None = None
    canonical_text: CanonicalText | None = None


class PublicExchangeResponse(FeedbackModel):
    link_mode: Literal["view", "download"] = "view"
    share_id: Id
    capability: str
    expires_at: datetime
    source: PublicSource
    allow_comments: bool
    discussion: Literal["own_only", "shared"]
    permitted_locator_kinds: list[str]
    guest_session: GuestSessionResponse | None = None
    viewer_path: Literal["/api/v1/public/feedback/viewer"] = "/api/v1/public/feedback/viewer"


class GuestSessionRequest(FeedbackModel):
    display_name: str | None = Field(default=None, max_length=80)


class GuestSessionResponse(FeedbackModel):
    guest_session_id: Id
    guest_capability: str
    expires_at: datetime


class PublicThreadCreateRequest(FeedbackModel):
    creation_key: Id
    anchor: AnchorInput
    body: Body


class PublicEntryCreateRequest(FeedbackModel):
    creation_key: Id
    expected_thread_version: Positive
    reply_to_entry_id: Id | None = None
    body: Body


class PublicContentResponse(FeedbackModel):
    format: str
    mime_type: str
    filename: str | None = None
    content: str | None = None
    rich_payload: dict[str, object] | None = None
    theme_family: str = "classic"
    bytes_path: Literal["/api/v1/public/feedback/source/bytes"] = (
        "/api/v1/public/feedback/source/bytes"
    )


class PublicOutcome(FeedbackModel):
    entry_version_id: Id
    thread_id: Id
    disposition: Literal["addressed", "needs_clarification", "not_changed"]
    reason: str = Field(min_length=1, max_length=2048)


class PublicOutcomeRequest(FeedbackModel):
    entry_version_id: Id
    disposition: Literal["addressed", "needs_clarification", "not_changed"]
    reason: str = Field(min_length=1, max_length=2048)


class PublicOutcomePage(FeedbackModel):
    items: list[PublicOutcome]


class FeedbackPacketReference(FeedbackModel):
    submission_id: Id
    manifest_hash: Digest


class ChildWorkRequest(FeedbackModel):
    manifest_hash: Digest
    child_conversation_id: Id | None = None
    child_task_id: Id | None = None
    child_session_id: Id | None = None
    child_turn_id: Id | None = None
    child_attempt_number: Positive | None = None

    @model_validator(mode="after")
    def one_child(self) -> Self:
        if (
            sum(
                bool(ref)
                for ref in (self.child_conversation_id, self.child_task_id, self.child_session_id)
            )
            != 1
        ):
            raise ValueError("select exactly one child work reference")
        if self.child_task_id and self.child_attempt_number is None:
            raise ValueError("a child task requires its exact attempt")
        return self


class ChildWorkResponse(FeedbackModel):
    work_link_id: Id
    submission_id: Id
    conversation_id: Id | None = None
    task_id: Id | None = None
    session_id: Id | None = None
    attempt_number: Positive | None = None
    turn_id: Id | None = None
    work: FeedbackWorkState = "unavailable"


class SubmissionResult(FeedbackModel):
    content_ref: Id
    relation: Literal["revises_source", "separate_result", "answer_only"]


class SubmissionResultsResponse(FeedbackModel):
    items: list[SubmissionResult]


class DispositionRequest(FeedbackModel):
    submission_id: Id
    manifest_hash: Digest
    entry_version_id: Id
    disposition: Literal["addressed", "needs_clarification", "not_changed"]
    reason: str = Field(min_length=1, max_length=4096)
    result_ref: Id | None = None


def feedback_json_schema() -> dict[str, object]:
    """Export every public DTO, including unions, without frontend code generation."""
    models = [
        value
        for value in globals().values()
        if isinstance(value, type) and issubclass(value, FeedbackModel)
    ]
    for model in models:
        model.model_rebuild()
    adapter: TypeAdapter[object] = TypeAdapter(GenericAlias(tuple, tuple(models)))
    return adapter.json_schema()
