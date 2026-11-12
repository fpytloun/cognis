# Rich Deliverable Composition

A rich deliverable is a renderer-neutral payload. Cognis validates and stores
the payload; each target renderer chooses an appropriate representation.

In direct chat, each `write_deliverable` call creates an independent durable
output. All outputs are presented after the final assistant response, in write
order. A later call does not replace or delete an earlier output. In workflow
steps, repeated writes still revise the canonical step deliverable.

If an older deliverable's stored payload is missing, artifact tools reject the
reference without failing the turn. Browser exports return a structured
not-found or payload-unavailable response rather than an internal server error.

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

Consider useful visuals during research, not only during composition. Verified
subject-specific media such as film posters or product photos can aid
recognition and comparison; place them with the relevant item and provide
specific alt text and provenance. Diagrams can clarify relationships,
architecture, or processes. Omit visuals that add no value or cannot be
verified; do not invent URLs or use unrelated decorative imagery.

### Web presentation and spacing

Standalone and full-view documents use an edge-to-edge canvas, at least as
tall as the viewport. Reading gutters are inside the document layout, not an
outer contrasting frame. The viewer toolbar is in normal flow and scrolls
away with the document; the desktop contents sidebar remains sticky.
Embedded chat documents retain their host boundary.

The web renderer uses flat, token-based surfaces across block families.
Charts do not add a second decorative frame inside their containing block.
Callout backgrounds, borders, and indicators use the same authored semantic
tone in both themes.

Publication block lists own the 24px sibling gap (16px in compact mode); blocks own their internal
padding and heading-to-content spacing. Do not add spacer blocks to compensate
for missing renderer spacing. Sections add a larger ruled transition.
Default, Pulse, and dashboard presentations use the same publication stylesheet:
none bypasses typography, theme, reading gutters, or table styling. Compact
mode changes spacing and heading scale, not the visual language. Simple metric
tiles have no minimum height and use two columns on phones.

Use `hero.subtitle` for the summary. Existing payloads using `hero.dek` remain
readable in web, static/PDF, and Markdown renderers; `subtitle` takes precedence.

The `every-block-reference` gallery scenario and
`ui/e2e/rich-element-rhythm.spec.ts` exercise composed blocks together, including
measured gaps. Isolated element previews alone cannot detect touching siblings.
Phone publication views use 16px side gutters (or larger device safe-area
insets). The responsive rule has the same specificity as the desktop shell,
so its 40px padding cannot override the phone layout.

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

## Portable semantic refinements

- Set `metadata.viewer_identity` only when the deliverable supplies a specific
  opening-header identity. It accepts plain `label` and/or a registered
  `icon: {name, alt?}`. Do not infer it. An icon-only identity requires `alt`.
- Hero `badges` and `tags` accept legacy text or `{label, tone?}`. Use a tone
  only to communicate meaning; the label remains required and visible.
- Use `key_value` with `variant: "summary"` for a compact ordered fact strip.
  Item `tone` is semantic. Do not author columns, widths, colors, or styles.
- Standalone and full-view readers provide a flat viewer shell with
  copy, download, and table-of-contents controls. Only external standalone
  readers offer theme controls; in-app views follow account appearance. The viewer shell is
  renderer-owned; do not model it with authored action blocks.

## Reference screenshots

The QA gallery uses one compact toolbar for the scenario, theme, width, and
surface. Each selector preserves the other URL parameters and browser history.
Use `capture=viewer` to hide QA controls in deliverable captures.

![Compact Rich gallery toolbar](../assets/screenshots/rich-deliverables/gallery-toolbar.png)

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
| `design-document-legacy` | Long Markdown-only design document used to compare baseline renderer treatment |
| `design-document-composed` | The same fictional design document with viewer identity, typed badges, summary facts, TOC, table, and code |

Use the stable scenario URL to inspect one example:

```text
/rich-deliverable-fixture?scenario=illustrated-recipe&theme=light&width=1280&surface=embedded
```

- [Layout and narrative blocks](rich-deliverable-blocks-layout.md)
- [Data, evidence, and utility blocks](rich-deliverable-blocks-data.md)
