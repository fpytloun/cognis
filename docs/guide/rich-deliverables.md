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

Agents use `write_deliverable` with `format="rich"`. The call always includes
a channel-safe fallback in `content`; the structured payload is carried in
`rich`.

```text
write_deliverable(
  action="write_deliverable",
  format="rich",
  title="Weekly delivery review",
  content="Accessible Markdown fallback…",
  rich={ blocks: [...], sources: [...], metadata: {...} }
)
```

The **Cognis Rich Deliverable** system skill guides agents toward appropriate
composition, hierarchy, evidence, and block selection. It is a writing and
presentation guide; it does not replace schema validation.

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
