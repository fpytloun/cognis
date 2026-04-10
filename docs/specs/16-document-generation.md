# Cognis: Document Generation and Report Delivery

## Overview

Cognis needs a first-class way for agents to produce rich user-facing documents
such as design docs, research reports, incident summaries, proposals, and audit
packages. The output must be a durable artifact that can be delivered back into
 conversations and external channels as a real file, not just as pasted text.

This spec defines a `document_generate` tool that renders Markdown or HTML/CSS
into PDF, supports inline assets, preserves source files for revision, and
delivers the final document through the existing artifact and channel systems.

The design goal is agent usability, not just renderer correctness. The normal
workflow must be one tool call for a simple report, while still supporting
advanced reports with generated images, local executor files, Excalidraw-based
diagrams rendered to files, and remote web assets.

## Status

Implemented today:

1. executor-native `document_generate`,
2. executor-native `artifact_publish`,
3. Markdown and HTML/CSS PDF generation via WeasyPrint,
4. assets from artifact id, path, and URL,
5. optional local `output_path`,
6. optional `append_pdf_assets` support,
7. public Cognis artifact URLs for user-facing delivery,
8. direct-turn and background-task attachment delivery,
9. attachment-only assistant messages across persistence, replay, web UI, and
   channel delivery.

Still deferred:

1. full persisted document bundle sidecars (`source.md` / `source.html`, CSS,
   `manifest.json`) under one document object,
2. richer regeneration workflow based on persisted document manifests,
3. broader integration/end-to-end tests beyond the current unit coverage,
4. extra output formats such as DOCX.

## Goals

1. Let an agent generate a polished PDF from Markdown or HTML/CSS in a single
   tool call.
2. Support inline assets from Cognis artifacts, executor-local files, and remote
   URLs.
3. Preserve source content and render metadata so the document can be revised
   later.
4. Deliver the generated PDF back to the user as a real channel attachment when
   the channel supports files, with a public Cognis URL fallback when it does
   not.
5. Make the feature work for direct turns and for background task results.
6. Use only public Cognis artifact URLs for user-facing or channel-facing
   delivery.

## Non-Goals

1. Native Excalidraw rendering in the document tool. Excalidraw remains a
   separate skill and can be used by rendering to a local file or Cognis
   artifact that is then embedded as an asset.
2. DOCX generation in v1.
3. Unrestricted generic "upload any file to artifact store" as an LLM-facing
   tool.
4. Arbitrary authenticated remote asset fetching in v1.

## Why A High-Level Tool

The LLM should not need to compose a report by writing temp files with `bash`
and then uploading them manually. That is brittle, difficult to validate, and
too low-level for normal report generation.

Instead, Cognis should expose a high-level `document_generate` tool that:

1. accepts source content or a source reference,
2. resolves inline assets,
3. renders a PDF,
4. stores the result in the artifact store,
5. returns attachment metadata for channel delivery.

A constrained local-file publish escape hatch may exist separately, but it is
not the primary report-generation path.

## User Stories

### Simple Report

The agent researches a topic, writes Markdown, calls `document_generate`, and
returns a PDF design document to the user in chat or Signal.

### Rich Design Doc

The agent generates an architecture image with `image_generate`, references the
returned artifact in the report, includes rendered Excalidraw or other local
diagram assets, and produces a polished PDF with custom CSS.

### Local Exploration Report

The agent uses executor tools (`bash`, browser automation, screenshots,
filesystem tools) and then builds a report from local files on the executor.

### Background Task Delivery

A delegated worker completes a long-running report in the background and the PDF
is delivered back into the originating conversation or preferred channel.

## Execution Model

`document_generate` MUST be an **executor-native** tool, not a controller-local
builtin handler.

Reasons:

1. local file support requires executor filesystem access,
2. WeasyPrint and related rendering dependencies are better isolated to the
   executor environment,
3. agents often create supporting files during research on the executor.

This is a deliberate exception from current image-tool behavior. Image generation
can remain controller-side because it delegates to a provider and does not need
local file access. Document generation is fundamentally a local rendering task.

## Tool Contract

### Tool Name

`document_generate`

### Input Schema

The schema MUST stay compact to reduce optional-field overfilling by models.

Required semantics:

1. `input_format`: `markdown` or `html`
2. exactly one source:
   - `content`
   - `source_path`
   - `source_artifact_id`

Optional fields:

1. `title`
2. `filename`
3. `output_path`
4. `css`
5. `template`
6. `page_size`
7. `orientation`
8. `assets`
9. `append_pdf_assets`

### Recommended Shape

```json
{
  "type": "object",
  "properties": {
    "input_format": {"type": "string", "enum": ["markdown", "html"]},
    "content": {"type": "string"},
    "source_path": {"type": "string"},
    "source_artifact_id": {"type": "string"},
    "title": {"type": "string"},
    "filename": {"type": "string"},
    "output_path": {"type": "string"},
    "css": {"type": "string"},
    "template": {"type": "string", "enum": ["default", "design_spec", "research_report", "incident_report", "proposal"]},
    "page_size": {"type": "string", "enum": ["A4", "Letter"]},
    "orientation": {"type": "string", "enum": ["portrait", "landscape"]},
    "append_pdf_assets": {"type": "boolean"},
    "assets": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "name": {"type": "string"},
          "artifact_id": {"type": "string"},
          "path": {"type": "string"},
          "url": {"type": "string"},
          "alt": {"type": "string"},
          "caption": {"type": "string"},
          "mime_type": {"type": "string"}
        },
        "required": ["name"]
      }
    }
  },
  "required": ["input_format"]
}
```

Validation rules:

1. exactly one of `content`, `source_path`, `source_artifact_id` must be set,
2. each asset must specify exactly one of `artifact_id`, `path`, or `url`,
3. empty optional strings and collections must be stripped before execution,
4. output format is fixed to PDF in v1.

## Supported Authoring Modes

### Markdown

Markdown is the default authoring mode for agents. It is easier for models to
write, debug, and revise. Markdown mode should support at least:

1. headings,
2. paragraphs,
3. ordered and unordered lists,
4. tables,
5. fenced code blocks,
6. blockquotes,
7. inline images via asset references,
8. inline asset references for rendered diagrams and screenshots.

### HTML/CSS

HTML is the advanced mode for high-fidelity layouts. The tool must support raw
HTML input plus optional CSS overrides. This mode is necessary for premium
reports, proposals, and branded deliverables.

## Asset Model

Assets are named inputs that the source document can reference.

### Supported Asset Sources In V1

1. `artifact_id` — preferred and safest.
2. `path` — executor-local file path.
3. `url` — unauthenticated remote HTTP(S) asset.

### Reference Syntax

Markdown:

```md
![System architecture](asset:architecture)
```

HTML:

```html
<img src="asset:architecture" alt="System architecture">
```

The renderer resolves `asset:<name>` using the `assets` array.

### Relative Local Files

If `source_path` is used, relative file references in the source document MUST
resolve relative to the source file directory. This makes standard Markdown
authoring work naturally when the agent writes a local `.md` file first.

### Companion Assets

Some assets cannot or should not be inlined into the PDF, such as ZIP files,
CSVs, and non-renderable binaries. The tool may return them as companion
attachments alongside the final PDF.

## Excalidraw Handling

Native Excalidraw parsing is out of scope for v1.

Recommended workflow:

1. use the Excalidraw skill to render SVG or PNG locally,
2. include the rendered file via `path`,
3. or publish it as an artifact and include it via `artifact_id`.

This keeps `document_generate` focused while still supporting Excalidraw-based
reports in practice.

## Rendering Pipeline

The executor-side pipeline is:

1. normalize arguments,
2. load source from `content`, `source_path`, or `source_artifact_id`,
3. resolve all assets,
4. convert Markdown to HTML if needed,
5. apply template CSS and optional custom CSS,
6. render HTML to PDF with WeasyPrint,
7. save output bundle to Cognis artifact storage,
8. return attachment metadata and a structured textual result.

## Internal Artifact Publish Path

Because `document_generate` is executor-native, the controller must be able to
materialize generated outputs from executor tool results into the artifact store.

This is an internal runtime capability, not an LLM-facing tool.

### Requirement

The minimal implementation may reuse inline tool attachments from the executor
and let the controller persist them into artifact storage before delivery.

The tool MUST NOT return raw PDF bytes through normal tool output.

## Document Bundle Storage

Longer term, each generated document should be stored as a full bundle under a
dedicated namespace such as `documents`.

Recommended layout:

1. `documents/doc_<id>/document.pdf`
2. `documents/doc_<id>/source.md` or `source.html`
3. `documents/doc_<id>/style.css` when used
4. `documents/doc_<id>/manifest.json`

This bundle format preserves enough information for future revision or
regeneration.

Current implementation note:

1. the final generated PDF is persisted as the primary artifact,
2. companion attachments are persisted and delivered as separate artifacts,
3. full source/CSS/manifest sidecars are not yet persisted as a unified bundle.

### Manifest Fields

At minimum:

1. title,
2. input format,
3. template,
4. source provenance,
5. resolved assets,
6. render warnings,
7. generation timestamp,
8. page count if available.

## Public Artifact URLs

All user-facing and channel-facing attachment URLs MUST use Cognis public signed
URLs, never backend-native MinIO or S3 presigned URLs.

This requirement applies to:

1. `document_generate` output,
2. task result attachments,
3. conversation message attachment serialization,
4. channel delivery payloads.

The controller-facing artifact URL is the only URL shape that works reliably
across controller-hosted adapters, executor-hosted adapters, and mixed network
topologies.

## Output Contract

The tool returns:

1. a structured textual summary in `ToolResult.output`,
2. one primary attachment for the generated PDF,
3. optional companion attachments for non-inline assets when requested.
4. after controller-side artifact persistence, visible tool output is enriched
   with final artifact metadata such as `artifact_id`, `url`, `mime_type`, and
   `size_bytes`.

Example summary payload before controller-side enrichment:

```json
{
  "document_id": "doc_a1b2c3",
  "pdf_artifact_id": "docpdf_a1b2c3",
  "filename": "cognis-design-spec.pdf",
  "url": "https://cognis.example.com/api/v1/artifacts/content/documents/doc_a1b2c3/document.pdf?...",
  "source_artifact_id": "docsrc_a1b2c3",
  "append_pdf_assets": false,
  "appended_pdfs": [],
  "output_path": null,
  "warnings": [],
  "assets_used": ["architecture", "timeline"]
}
```

Example enriched output after artifact persistence:

```json
{
  "document_title": "Cognis Design Spec",
  "filename": "cognis-design-spec.pdf",
  "artifact_id": "doc_abc123",
  "url": "https://cognis.example.com/api/v1/artifacts/content/documents/doc_abc123/cognis-design-spec.pdf?...",
  "mime_type": "application/pdf",
  "size_bytes": 48213
}
```

Attachment metadata MUST include:

1. artifact id,
2. public URL,
3. MIME type,
4. filename,
5. size.

## Channel Delivery

The existing channel delivery path should be reused.

### Channels That Support Files

These adapters already have generic file/media support and should send the PDF
as a file attachment when given a valid public URL and MIME type:

1. Signal,
2. Telegram,
3. Slack,
4. Discord,
5. WhatsApp,
6. Matrix,
7. BlueBubbles.

### Channels With Link Fallback

1. Google Chat should send a link fallback for PDFs.
2. IRC should send a filename/link note.

### Combined Delivery

When a channel supports both text and attachments, the default behavior should
be to send the report text and attachment together in one outbound message when
the adapter can do so.

## Background Task Delivery

This feature is not complete unless background task results can carry document
attachments.

### Implemented Design

Generated attachments are persisted in `task.result_data`, then propagated through:

1. workflow step completion,
2. task completion persistence,
3. task result event publication,
4. follow-up conversation injection,
5. channel follow-up delivery.

This avoids a DB migration in v1 while making delegated-worker reports useful.

## Security and Safety

### Remote URL Assets

Remote URL fetching MUST enforce:

1. `http` or `https` only,
2. timeout,
3. maximum bytes,
4. redirect limit,
5. MIME allowlist,
6. no authenticated fetches in v1.

### Local Path Assets

Local paths are resolved on the executor and inherit the same trust boundary as
other executor filesystem tools.

### HTML/CSS Safety

WeasyPrint does not execute JavaScript, but the renderer should still ignore or
strip active script content and control how external assets are resolved.

### Limits

Enforce explicit limits for:

1. source length,
2. number of assets,
3. total embedded asset bytes,
4. final PDF size.

## Dependencies

V1 requires:

1. WeasyPrint,
2. a Markdown parser suitable for tables and fenced blocks.

These dependencies belong on the executor environment used for document
generation.

## API and Model Changes

### Tooling

1. Add a new executor-native tool definition for `document_generate`.
2. Register its handler in executor runtime construction.

### Artifact Support

1. Use controller-side materialization of inline tool attachments into artifact
   storage.
2. Reuse public signed URLs for all user-facing references.
3. Provide `artifact_publish` as a constrained executor-native escape hatch for
   locally generated files.

### Task Results

1. Store attachment metadata in `task.result_data`.
2. Extend task follow-up delivery to carry attachments.

## Testing Requirements

### Unit Tests

1. Markdown content -> PDF.
2. HTML content + CSS -> PDF.
3. `source_path` resolution.
4. `source_artifact_id` resolution.
5. asset resolution from `artifact_id`.
6. asset resolution from `path`.
7. asset resolution from `url`.
8. public Cognis URL generation.
9. attachment metadata returned.
10. task result attachment persistence.

### Integration Tests

1. direct chat generates PDF and Signal receives it,
2. delegated worker generates PDF and delivery returns to the originating
   conversation,
3. Telegram sends the PDF as a document,
4. Google Chat falls back to a link,
5. remote asset timeout and size rejection work as expected.

## Rollout Plan

### Completed

1. executor-native `document_generate`,
2. executor-native `artifact_publish`,
3. Markdown and HTML input,
4. WeasyPrint rendering,
5. assets from artifact id, path, and URL,
6. public artifact URLs,
7. direct-turn attachment delivery,
8. background task attachment delivery,
9. richer built-in templates,
10. attachment-aware assistant message persistence.

### Deferred

1. persisted document source/CSS/manifest bundles,
2. source artifact regeneration flow,
3. advanced diagram workflows,
4. extra output formats if needed.

## Recommendation

Implement `document_generate` as an executor-native high-level tool with
artifact, path, and URL asset support in v1. Treat Excalidraw and other
diagram systems as separate render-to-file workflows that feed into the
document tool through local files or Cognis artifacts.

The core user-facing feature is complete once the final PDF or generated file
travels through the same direct-turn and background-task delivery paths that
users rely on in real conversations and channels. Remaining work should focus
on richer bundle persistence and regeneration, not on the basic delivery path.
