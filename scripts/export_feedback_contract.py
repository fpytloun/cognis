"""Export canonical feedback DTO schema and executable input fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from cognis.models.feedback import (
    FeedbackPreflightRequest,
    GrantRequest,
    PublicExchangeRequest,
    ResolveSourceRequest,
    ScopedTextRequest,
    ShareRequest,
    feedback_json_schema,
)
from cognis.models.feedback_routes import PRIVATE_ROUTES, PUBLIC_ROUTES, route_contract

HANDOFF = """# Work Feedback final HTTP contract

Canonical DTOs: `cognis/models/feedback.py`; routes/headers:
`cognis/models/feedback_routes.py`; generated schema, requests and route fixture:
`tests/fixtures/feedback/{schema,inputs,routes}.json`. This document is generated.
These are the final wire semantics for parallel shell/annotation work. Runtime
qualification is reported separately; a DTO commit is not a runtime completion.

## Authentication and envelopes

Authenticated routes use normal Cognis authentication/ownership. Public routes
do **not** use owner Authorization: except `/exchange`, every public operation
requires `X-Cognis-Feedback-Capability: <capability>` from exchange. Posting,
replying, editing, and capturing also require `X-Cognis-Feedback-Guest:
<guest_capability>`. Both are explicit headers, never cookies or iframe messages.
Public Origin `null` is rejected. Exchange takes a JSON `share_token`, optional
`create_guest` (default false), and optional `display_name` (max 80). With
`create_guest:true`, its response includes `guest_session`; otherwise that field
is null and the caller can POST `/guest-sessions`. The exchange response contains
`capability,share_id,expires_at,source,allow_comments,discussion,
permitted_locator_kinds,guest_session,viewer_path`. Public sessions remain
unverified guests even if the browser has a separate logged-in owner session.

JSON responses are exactly the named DTO, not `{data:...}`. Collections have
`{items:[...]}` and, only where defined, `next_cursor`. Errors are
`{detail:{code,message}}` with HTTP 403/404/409/410/413/422/429/503 as appropriate.
No error response echoes supplied comment bodies, tokens, or private metadata.
Cursor/limit defaults are 30/max 100. Mutation version conflicts are HTTP 409.
DELETE staging/draft has an empty HTTP 204 response. No replace-all grant API.

## Private authenticated routes

Prefix: `/api/v1/feedback`. Query input is named in the table when no DTO exists.
All JSON field names are in the generated schema. `expected_version` is zero
only when creating draft/staging/grant/preference rows; subsequent writes use
the returned version. `GrantRequest.principal_email` identifies one existing
source reader; PUT is a singular upsert and GET returns `GrantListResponse`.

"""

DETAILS = """
## Sharing, source pins and the public viewer

`ShareRequest` requires **both** `source:DocumentSourceRef` and
`expected_content_digest`, plus `expires_at`, optional `allow_comments` (default
false), and `discussion` (default own_only). The source must equal the reviewed
exact document revision and digest resolved under the subject. No source change
or latest-following update is supported. `ShareResponse.share_token` is returned
only on creation; lists/updates return it as null. Grant list is
`{items:GrantResponse[]}`; share list is `{items:ShareResponse[]}`. DELETE share
requires query `expected_version` and returns the revoked `ShareResponse`.
Every source read, pixel read and mutation rechecks live revocation/expiry.

Public `/source` returns `PublicContentResponse` with `format,mime_type,filename,
content,rich_payload,bytes_path,theme_family`. Only the owner's theme family is
public; appearance remains the reader's choice. Text/rich documents have text/payload there;
binary media uses `/source/bytes` with the same capability header. Raw bytes
are attachment/nosniff/sandbox responses, never credentialed interactive HTML.
`/viewer` returns the trusted standalone shell with the bounded, HTML-escaped
minted share capability on `#cognis-deliverable-root` as
`data-feedback-share-token`. **This attribute is already a header capability;
do not send it through `/exchange` again.** It contains no owner identity,
private destination, task metadata, or agent authority. Artifact HTML remains
in the existing opaque-origin sandbox; it cannot set trusted bootstrap data.
The initial share URL consumer exchanges its raw share token in viewer chrome.

Public source-resolution/text bodies use the same DTO as authenticated calls,
but must specify the exact shared document and standalone context. Source
responses use `subject_id=share_id`, null private lineage/revision/latest
metadata, and read/comment-only capabilities. Public thread/anchor responses
similarly use the share-local subject ID and audience `public`; internal threads,
deliveries, receipts and source provenance are never returned. Public outcomes
are `{items:PublicOutcome[]}` explicitly published by the owner via the private
`/public-shares/{share_id}/outcomes` endpoint. These contain only the public entry,
thread, disposition and owner-authored sanitized reason, not private result URLs.

## Capture acquisition/adoption

Only `method:observed_pixels` exists: native user-mediated capture or explicitly
confirmed source-bound raster paste/file. All pixels are untrusted user input.
POST `/captures` returns `CaptureResponse` with status `awaiting_pixels`.
PUT `/captures/{capture_id}/pixels` has raw PNG/WebP bytes, Content-Type
`image/png` or `image/webp`, and required query `expected_version,width,height`.
It is not multipart and accepts no URL/artifact reference. Input/output is max
8 MiB, max 16 million pixels; dimensions are positive/max 16384 each. Decode,
dimension check, single-frame validation, metadata removal and PNG re-encoding
precede immutable private adoption. Success returns status `ready`, incremented
version, actual dimensions and `trust:untrusted_user_provided`. GET that pixel
path returns private PNG with capability checks; no signed/public artifact URL.
The public capture paths have identical shapes but need both capability headers.

## Text and exact Work evidence

POST `/sources/text` takes `{source,context,scope,start,limit}` and returns
`{scope,content_digest,descriptor,text_page}`. Default limit 16384/max 65536;
offsets are UTF-16, line endings LF, no Unicode normalization.
`scope.kind` is `document`, `rich_field`, or `pdf_page`. Rich scope fields:
`block_id,identity,item_key,field_path`; PDF scope has one-based `page`.
Semantic block IDs must be unique authored IDs; revision-local block IDs are
JSON pointers such as `/blocks/0`. Item keys match unique authored `items[].key`.
Field paths are relative to that exact block/item. Backend CommonMark extraction
is used for prose body/content/markdown fields; other strings use stored text.
Code is not pretty-printed. Descriptor digests are scope-specific and anchor
validation repeats the same extraction. No client-authored canonical traversal.
Unknown/ambiguous matches fail closed; geometric/page/whole feedback remains.

`FileDiffRef.feedback_source` is nullable `WorkDiffSourceRef` from exact retained
event/file evidence, never synthetic aggregate identity. It preserves original
event/path/file ordinal even if display paths are normalized. Unavailable,
binary or truncated source evidence has null.

## Submission and normal-send atomicity

Preflight freezes the exact selected entry versions plus marked context, source
revisions/digests, update mode and destination; it sends no turn. Standalone
POST `/submissions` uses `{creation_key,preflight_id,manifest_hash}` plus optional
`replaces_rejected_submission_id`. The canonical receipt returns separately
`delivery` and `work`, exact admission identity, recovery actions and version.
Reading the receipt or POST `/submissions/{submission_id}/check-delivery` never
resubmits or expands the reviewed packet.
`SubmissionDetail.children` lists explicit child links. Each `ChildWorkResponse`
also returns optional `turn_id` and `work`, derived from that exact linked
turn/attempt, not current conversation activity or a newer task attempt.
Unknown work is `unavailable`. The composer staging snapshot includes its latest
100 durable receipts; older submissions remain in source history.

Child responses also have optional `session_id` for immutable secondary work.
`ChildWorkRequest` accepts `child_session_id` and optional `child_turn_id`.
POST children confirms already-persisted admission proof; it never grants packet
access from generic ancestry. If multiple turns of one child conversation are
linked, `child_turn_id` is required to disambiguate rather than following latest.
The native child-creation tools accept optional
`feedback_submissions:[{submission_id,manifest_hash}]`; canonical creation/admission
commits these explicit grants before the child can execute or be exposed.
Omitting the argument does not inherit feedback access.

In-chat submission is **only** the normal
`PUT /api/v1/chat/v2/conversations/{conversation_id}/messages/{client_txn_id}`:
add optional `feedback:{creation_key,preflight_id,manifest_hash}`. Feedback-only
input is valid. Its response gains optional `feedback_receipt` alongside the
existing transaction response. Destination stays origin_chat. One queued turn
contains ordinary text, attachments, chat mode and feedback reference; feedback
is included in request identity and durable payload. Receipt binding and exact
staging consumption commit with admission; later edits/new prepared entries
survive. No second send and no absorption into active work.

No authentication-header aliases, alternate grant replacement semantics, or
latest-following comment shares are supported.
"""


def export_handoff(root: Path) -> None:
    """Publish the exact table from the same declarations as the route fixture."""
    parts = [HANDOFF]
    for prefix, routes in (
        ("/api/v1/feedback", PRIVATE_ROUTES),
        ("/api/v1/public/feedback", PUBLIC_ROUTES),
    ):
        parts.append(f"\n### `{prefix}`\n\n")
        parts.append("| Method | Path | Input | Response | HTTP |\n|---|---|---|---|---|\n")
        for method, path, input_, response, status in routes:
            parts.append(
                f"| {method} | `{path}` | {input_ or '—'} | {response or 'empty'} | {status} |\n"
            )
    parts.append(DETAILS)
    (root / "docs" / "development" / "work-feedback-api.md").write_text(
        "".join(parts), encoding="utf-8"
    )


def main() -> None:
    """Write deterministic contracts consumed by backend and frontend tests."""
    root = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "feedback"
    root.mkdir(parents=True, exist_ok=True)
    fixtures = {
        "resolve_document": {
            "source": {"kind": "document", "content_ref": "art_" + "1" * 32},
            "context": {"kind": "chat", "conversation_id": "conv_" + "2" * 32},
        },
        "rich_field_text": {
            "source": {"kind": "document", "content_ref": "dlv_" + "5" * 32},
            "context": {"kind": "standalone"},
            "scope": {
                "kind": "rich_field",
                "block_id": "/blocks/0",
                "identity": "revision_local",
                "item_key": None,
                "field_path": ["body"],
            },
            "start": 0,
            "limit": 16384,
        },
        "pdf_page_text": {
            "source": {"kind": "document", "content_ref": "art_" + "6" * 32},
            "context": {"kind": "standalone"},
            "scope": {"kind": "pdf_page", "page": 1},
            "start": 0,
            "limit": 16384,
        },
        "preflight": {
            "context": {"kind": "standalone"},
            "selection": [{"entry_version_id": "fbv_" + "3" * 32, "expected_entry_version": 1}],
            "reviewed_guest_entry_version_ids": [],
            "overall_message": "Use the selected feedback.",
            "destination": {
                "kind": "new_followup",
                "agent_id": "agent_" + "4" * 32,
                "project_id": None,
                "title": None,
            },
            "update_mode": "answer_only",
        },
        "share": {
            "source": {"kind": "document", "content_ref": "dlv_" + "5" * 32},
            "expected_content_digest": "a" * 64,
            "allow_comments": True,
            "discussion": "own_only",
            "expires_at": "2030-01-01T00:00:00Z",
        },
        "grant": {
            "expected_version": 0,
            "principal_email": "reviewer@cognis.test",
            "capabilities": {"read": True, "comment": True},
        },
        "public_exchange": {
            "share_token": "bounded-share-token-from-viewer",
            "create_guest": True,
            "display_name": "Guest reviewer",
        },
    }
    ResolveSourceRequest.model_validate(fixtures["resolve_document"])
    FeedbackPreflightRequest.model_validate(fixtures["preflight"])
    ScopedTextRequest.model_validate(fixtures["rich_field_text"])
    ScopedTextRequest.model_validate(fixtures["pdf_page_text"])
    ShareRequest.model_validate(fixtures["share"])
    GrantRequest.model_validate(fixtures["grant"])
    PublicExchangeRequest.model_validate(fixtures["public_exchange"])
    for name, value in (
        ("schema", feedback_json_schema()),
        ("inputs", fixtures),
        ("routes", route_contract()),
    ):
        (root / f"{name}.json").write_text(
            json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    export_handoff(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
