"""Frozen feedback HTTP semantics, verified against registered routes in tests."""

from __future__ import annotations

PRIVATE_PREFIX = "/api/v1/feedback"
PUBLIC_PREFIX = "/api/v1/public/feedback"
CAPABILITY_HEADER = "X-Cognis-Feedback-Capability"
GUEST_HEADER = "X-Cognis-Feedback-Guest"

# method, relative path, input DTO (or transport), response DTO, success status
PRIVATE_ROUTES = (
    ("POST", "/sources/resolve", "ResolveSourceRequest", "ResolvedFeedbackSource", 200),
    ("POST", "/sources/text", "ScopedTextRequest", "ScopedCanonicalFeedbackText", 200),
    ("GET", "/subjects/{subject_id}/threads", "cursor,limit", "ThreadPage", 200),
    ("POST", "/threads", "ThreadCreateRequest", "FeedbackThread", 201),
    ("GET", "/threads/{thread_id}", None, "FeedbackThread", 200),
    ("POST", "/threads/{thread_id}/entries", "EntryCreateRequest", "EntryVersion", 201),
    ("PUT", "/entries/{entry_id}", "EntryEditRequest", "EntryVersion", 200),
    ("POST", "/threads/{thread_id}/resolution", "ResolutionRequest", "FeedbackThread", 200),
    ("POST", "/threads/{thread_id}/moderation", "ModerationRequest", "FeedbackThread", 200),
    ("POST", "/threads/{thread_id}/reattach", "ReattachRequest", "FeedbackThread", 200),
    ("GET", "/subjects/{subject_id}/summary", None, "SubjectSummary", 200),
    ("GET", "/subjects/{subject_id}/history", "cursor,limit", "HistoryPage", 200),
    ("GET", "/anchors/{anchor_id}/mapping", "target_subject_id", "AnchorMapping", 200),
    (
        "PUT",
        "/subjects/{subject_id}/preferred-followup",
        "FollowupPreferenceRequest",
        "SubjectSummary",
        200,
    ),
    ("GET", "/drafts/{draft_key}", None, "DraftResponse", 200),
    ("PUT", "/drafts/{draft_key}", "DraftRequest", "DraftResponse", 200),
    ("DELETE", "/drafts/{draft_key}", "expected_version", None, 204),
    ("GET", "/conversations/{conversation_id}/staging", None, "StagingSnapshot", 200),
    (
        "PUT",
        "/conversations/{conversation_id}/staging/{entry_id}",
        "StagingUpdateRequest",
        "StagedItem",
        200,
    ),
    (
        "DELETE",
        "/conversations/{conversation_id}/staging/{entry_id}",
        "expected_version",
        None,
        204,
    ),
    ("POST", "/destinations", "DestinationRequest", "DestinationResponse", 200),
    ("POST", "/preflights", "FeedbackPreflightRequest", "FeedbackPreflight", 200),
    ("POST", "/submissions", "SubmissionCreateRequest", "SubmissionReceipt", 202),
    ("GET", "/submissions/by-creation-key/{creation_key}/receipt", None, "SubmissionReceipt", 200),
    ("GET", "/submissions/{submission_id}", None, "SubmissionDetail", 200),
    ("POST", "/submissions/{submission_id}/check-delivery", None, "SubmissionReceipt", 200),
    ("GET", "/submissions/{submission_id}/results", None, "SubmissionResultsResponse", 200),
    ("POST", "/submissions/{submission_id}/children", "ChildWorkRequest", "ChildWorkResponse", 201),
    ("POST", "/captures", "CaptureRequest", "CaptureResponse", 201),
    ("GET", "/captures/{capture_id}", None, "CaptureResponse", 200),
    (
        "PUT",
        "/captures/{capture_id}/pixels",
        "PNG/WebP;expected_version,width,height",
        "CaptureResponse",
        200,
    ),
    ("GET", "/captures/{capture_id}/pixels", None, "image/png", 200),
    ("GET", "/subjects/{subject_id}/grants", None, "GrantListResponse", 200),
    ("PUT", "/subjects/{subject_id}/grants", "GrantRequest", "GrantResponse", 200),
    ("GET", "/subjects/{subject_id}/public-shares", None, "ShareListResponse", 200),
    ("POST", "/subjects/{subject_id}/public-shares", "ShareRequest", "ShareResponse", 201),
    ("PUT", "/public-shares/{share_id}", "ShareUpdateRequest", "ShareResponse", 200),
    ("DELETE", "/public-shares/{share_id}", "expected_version", "ShareResponse", 200),
    ("POST", "/public-shares/{share_id}/outcomes", "PublicOutcomeRequest", "PublicOutcome", 201),
)
PUBLIC_ROUTES = (
    ("POST", "/exchange", "PublicExchangeRequest", "PublicExchangeResponse", 200),
    ("POST", "/guest-sessions", "GuestSessionRequest", "GuestSessionResponse", 201),
    ("GET", "/source", None, "PublicContentResponse", 200),
    ("GET", "/source/bytes", None, "source MIME", 200),
    ("GET", "/viewer", None, "text/html", 200),
    ("POST", "/sources/resolve", "ResolveSourceRequest", "ResolvedFeedbackSource", 200),
    ("POST", "/sources/text", "ScopedTextRequest", "ScopedCanonicalFeedbackText", 200),
    ("GET", "/threads", "cursor,limit", "ThreadPage", 200),
    ("GET", "/threads/{thread_id}", None, "FeedbackThread", 200),
    ("POST", "/threads", "PublicThreadCreateRequest", "FeedbackThread", 201),
    ("POST", "/threads/{thread_id}/entries", "PublicEntryCreateRequest", "EntryVersion", 201),
    ("PUT", "/entries/{entry_id}", "PublicEntryEditRequest", "EntryVersion", 200),
    ("GET", "/outcomes", None, "PublicOutcomePage", 200),
    ("POST", "/captures", "CaptureRequest", "CaptureResponse", 201),
    ("GET", "/captures/{capture_id}", None, "CaptureResponse", 200),
    (
        "PUT",
        "/captures/{capture_id}/pixels",
        "PNG/WebP;expected_version,width,height",
        "CaptureResponse",
        200,
    ),
    ("GET", "/captures/{capture_id}/pixels", None, "image/png", 200),
)


def route_contract() -> dict[str, object]:
    """Export exact routes/headers without requiring an application runtime."""
    return {
        "private_prefix": PRIVATE_PREFIX,
        "public_prefix": PUBLIC_PREFIX,
        "capability_header": CAPABILITY_HEADER,
        "guest_header": GUEST_HEADER,
        "private": PRIVATE_ROUTES,
        "public": PUBLIC_ROUTES,
        "public_bootstrap": {
            "root": "#cognis-deliverable-root",
            "attribute": "data-feedback-share-token",
            "value": "bounded minted share capability; use as capability header, not owner Authorization",
        },
    }
