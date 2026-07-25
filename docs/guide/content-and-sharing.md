# Content and Temporary Sharing

Cognis keeps content and the URL used to share it as separate concerns. An
artifact can remain available to its authorized owner after a temporary sharing
link has expired.

> Cognis temporary sharing is not web hosting. A signed URL is a bearer link:
> anyone who has it can use it until it expires.

## Content lifecycle

| Use case | Stored as | Retention | Sharing behavior |
|---|---|---|---|
| A user uploads an image, PDF, or file for an agent | Temporary artifact | Upload records expire after 24 hours | Use it in the conversation while it is retained; do not rely on it as durable storage |
| An agent generates an image | Attached artifact | Retained until deletion or an applicable storage cleanup policy | Rendered in web chat; a new temporary URL can be generated when needed |
| An agent generates a PDF or other document | Local executor output, then a published artifact when delivered | Published artifacts are retained until deletion or cleanup policy | Share the published artifact with a signed download URL |
| An agent generates a self-contained HTML page | `text/html` artifact | Retained until deletion or cleanup policy | Share an inline-view signed URL; this is a one-shot page, not a hosted site |
| An agent writes a rich deliverable | Durable deliverable record with fallback content | Managed as a deliverable, separately from file artifacts | Rendered in Cognis; a standalone temporary link can be generated from the Share action |

Retention is not a promise of permanent storage. Administrators may apply
storage cleanup policies and users can delete their content.

## Artifacts

Artifacts are the binary or file-like objects Cognis persists: uploads,
generated images, PDFs, data files, and published HTML. Artifact IDs can be
used with artifact-aware tools and references.

### Temporary signed links

`artifact_get_url` mints a signed URL for an existing artifact:

- Minimum TTL: 60 seconds.
- Maximum TTL: 7 days.
- Default TTL: 1 hour.
- `mode="download"` prompts a download.
- `mode="view"` serves supported content inline. HTML viewing requires
  `text/html`.

The link expiry does **not** delete the artifact. An owner or another
authorized Cognis user can mint a replacement link while the artifact still
exists. A recipient who only has the old bearer URL cannot refresh it.

The returned `public_url` or `signed_url` is a temporary capability URL, not a
permanent public URL.

### Self-contained HTML

Publishing a `text/html` artifact is useful for a disposable, single-page
report or micro-experience. Inline viewing deliberately uses a restrictive
content-security policy: make the page self-contained, using inline
JavaScript/CSS and `data:` or `blob:` assets. Do not depend on external network
requests.

This feature is intentionally not a replacement for application hosting. A
future micro-app/public-hosting capability will have separate publication,
routing, versioning, and revocation semantics.

## Documents and PDFs

`document_generate` turns Markdown or HTML into a PDF on an executor. The
generated local file becomes a shareable chat attachment only after it is
published as a Cognis artifact.

Use documents when the desired output is a conventional portable file: a
proposal, invoice, report, or print-ready document. Use a rich deliverable
when the primary experience is an interactive Cognis-native page.

## Planned expired-link recovery

Today, an expired temporary link stops working for every visitor. Cognis is
adding an authenticated recovery view so expiration revokes anonymous bearer
access without preventing an authorized Cognis user from recovering their own
content.

The planned behavior is:

1. An anonymous visitor sees a neutral expired-link page without content
   metadata.
2. An authenticated authorized user sees the normal artifact preview or rich
   deliverable view.
3. That recovered view shows a dismissible red expiry banner and an explicit
   **Generate new share link** action.
4. An authenticated but unauthorized user gets no preview and no metadata.

This preserves the difference between possession of a temporary URL and normal
Cognis authorization. It also lets an owner recover a link delivered to a
channel after its TTL has elapsed.

## Related guides

- [Rich Deliverables](rich-deliverables.md)
- [Rich Deliverable Composition](rich-deliverable-composition.md)
- [Tools and Skills](tools-and-skills.md)
