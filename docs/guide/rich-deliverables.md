# Rich Deliverables

Rich deliverables are durable, structured Cognis outputs. They let an agent
author a deliberate presentation instead of flattening everything into a
Markdown response, while retaining an accessible Markdown fallback.

Use them for decision briefs, dashboards, research reports, comparisons,
incident summaries, timelines, and other outputs where hierarchy, evidence, or
visual structure improves understanding.

## How they differ from artifacts and documents

| Capability | Best for | Primary representation |
|---|---|---|
| Artifact | A file, upload, generated image, PDF, or self-contained HTML page | Stored blob |
| Document tools | A portable PDF produced from Markdown/HTML | Generated PDF artifact |
| Rich deliverable | A structured, Cognis-native experience | Durable block payload plus Markdown fallback |

A rich deliverable is artifact-compatible as a `dlv_*` content reference, but
the downloadable virtual artifact is its fallback content. It is not the rich
payload JSON.

## Rendering targets

- **Web chat** renders the interactive rich layout directly in the
  conversation.
- **Open** uses the authenticated Cognis experience.
- **Share** mints a standalone temporary bearer URL when the user requests it.
  It is not a permanent public URL.
- **Channels** receive the accessible Markdown fallback rather than an
  interactive block tree.
- **PDF export** uses Cognis server-side rendering and caching. It is distinct
  from executor-side `document_generate`.

The standalone renderer scopes media access to the shared deliverable and
applies its own security policy. It is not a static HTML artifact and should
not be described as hosting.

## Writing a rich deliverable

Agents use the `write_deliverable` `rich` action. The action accepts one
canonical payload source. Cognis normalizes that payload and derives the
channel-safe Markdown fallback.

```text
write_deliverable(
  action="rich",
  payload={
    title: "Weekly delivery review",
    blocks: [...],
    sources: [...],
    metadata: {...}
  }
)
```

For large payloads, publish an `application/json` file with
`artifact_publish`. Then pass its immutable `art_*` ID:

```text
write_deliverable(
  action="rich",
  payload_artifact={ artifact_id: "art_<32 lowercase hex characters>" }
)
```

Do not combine `payload` and `payload_artifact`. Rich calls do not accept
`content`, `format`, `rich`, top-level `title`, `target`, or top-level
`outputs`. Put the title and optional outputs in the payload.

The **Cognis Rich Deliverable** system skill guides agents toward appropriate
composition, hierarchy, evidence, and block selection. It is a writing and
presentation guide; it does not replace schema validation.

### Dashboard presentation

Use `action: "rich:dashboard"` for dense, technical operational views. The
action owns the presentation metadata and applies a wide canvas with compact
spacing. Do not set `metadata.presentation` in an authored payload.

Embedded chat hosts keep control of their width. Full and standalone views use
the wider bounded dashboard canvas.

The payload can include a top-level `title`. The stored deliverable title from
the host takes precedence when both values exist.

## Choose the smallest useful format

- Use ordinary Markdown for prose that does not benefit from visual hierarchy.
- Use an artifact for a file the reader should download or retain.
- Use `document_generate` for a print-ready PDF.
- Use a rich deliverable when the reader benefits from structured navigation,
  comparison, evidence, metrics, or an interactive standalone view.

## Continue

- [Content, Artifacts, and Temporary Sharing](content-and-sharing.md)
- [Rich Deliverable Composition](rich-deliverable-composition.md)
- [Layout and narrative blocks](rich-deliverable-blocks-layout.md)
- [Data, evidence, and utility blocks](rich-deliverable-blocks-data.md)
