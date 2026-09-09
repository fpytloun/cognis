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

Canonical scenarios live in `ui/src/lib/rich-scenarios/scenarios/`. The web
fixture, Python projection tests, static renderer tests, and documentation
previews read these files. Add one JSON file to extend the gallery. Do not edit
a central scenario index.

Use stable scenario links for visual review:

```text
/rich-deliverable-fixture?scenario=research-answer&theme=dark&width=1280&surface=embedded
```

Canonical fixtures represent stored renderer input. Thus,
`metadata.presentation` can occur in a fixture. Rich authoring actions still
select the presentation and do not accept a separately authored presentation.

Generate all scenario screenshots or one group with:

```bash
cd ui
npm run screenshot:rich-gallery -- --group=dashboard --themes=light,dark --widths=390,1280
```

The command writes screenshots, a manifest, and contact sheets under
`ui/review-artifacts/rich-scenario-gallery/` by default. Playwright does not
clear this ignored review directory between test runs.

## Cross-domain scenario portfolio

The scenario gallery includes complete examples that use the same generic
blocks as authored deliverables. Maps are not part of this portfolio.

| Scenario | Composition focus |
|---|---|
| `weekend-schedule` | Two-day agenda, constraints, reservations, costs, and fallback plans |
| `fridge-comparison` | Hard fit gates, product media, decision criteria, and installation cost |
| `solar-infographic` | Image-led science narrative, comparisons, chart, timeline, and sources |
| `educational-cheatsheet` | Dense visual reference, examples, common mistakes, and glossary |
| `book-spoilers` | Editorial summary with explicit interactive and static spoiler handling |
| `wearable-recovery` | Synthetic wearable metrics, trends, personal baselines, and data coverage |
| `menstrual-cycle` | Synthetic cycle patterns, estimated ranges, uncertainty, and safety context |
| `data-projections` | Observed baseline, forecast range, scenarios, assumptions, and decision |
| `illustrated-recipe` | Step-associated media, ingredients, timing, failures, and food safety |
| `weekly-meal-plan` | Seven-day schedule, recipe media, batch preparation, reuse, and shopping |

Use the stable scenario URL to inspect one example:

```text
/rich-deliverable-fixture?scenario=illustrated-recipe&theme=light&width=1280&surface=embedded
```

- [Layout and narrative blocks](rich-deliverable-blocks-layout.md)
- [Data, evidence, and utility blocks](rich-deliverable-blocks-data.md)
