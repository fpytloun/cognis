# Rich Deliverable Composition

A rich deliverable is a renderer-neutral payload. Cognis validates and stores
the payload; each target renderer chooses an appropriate representation.

## Payload shape

```text
{
  blocks: [...],
  assets: [...],
  sources: [...],
  datasets: [...],
  exports: [...],
  metadata: {...}
}
```

`blocks` are the authored reading experience. The remaining collections are
document-level context that blocks can reference by ID.

## Composition rules

1. Start with the reader's decision or question, not a catalogue of widgets.
2. Use a single focal element: a hero, executive summary, decisive comparison,
   or evidence-backed conclusion.
3. Use charts only for real multi-point quantitative series.
4. Prefer a Markdown or section block for ordinary explanatory prose.
5. Attach sources to claims, reports, cards, or document-level references;
   avoid decorative citations.
6. Keep the Markdown fallback complete enough for channels and accessibility.
7. Do not nest cards inside cards without a clear information hierarchy.

## Block families

| Family | Typical blocks |
|---|---|
| Layout and narrative | `hero`, `section`, `stack`, `columns`, `grid`, `tabs`, `accordion`, `markdown`, `callout`, `quote`, `timeline`, `steps` |
| Status and action | `dashboard`, `status`, `status_grid`, `metric`, `action`, `checklist`, `incident_checklist` |
| Evidence and analysis | `research_answer`, `evidence_report`, `claim_cards`, `comparison_matrix`, `decision_matrix`, `source_list`, `chart` |
| Media and utilities | `figure`, `gallery`, `table`, `code`, `mermaid`, `link`, `link_preview`, `modal` |

Aliases remain supported for compatibility: `kv` and `key_value`, for
example, render the same key-value concept. Prefer the canonical spelling in
new authored payloads.

## Reference screenshots

The block guides use screenshots generated from the same deterministic fixture
used by browser visual QA. Regenerate them with:

```bash
cd ui
npx playwright test e2e/rich-deliverable-doc-assets.spec.ts --project=chromium
```

The test verifies fixture coverage for every supported block type and writes
the documentation images only when `UPDATE_RICH_DOC_ASSETS=1` is set.

- [Layout and narrative blocks](rich-deliverable-blocks-layout.md)
- [Data, evidence, and utility blocks](rich-deliverable-blocks-data.md)
