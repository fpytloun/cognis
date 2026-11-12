# Cognis UI vocabulary

Read this before changing a screen. Compose from `src/lib/components/ui`;
fix inconsistency in touched code without restyling unrelated screens.

## Components

- `Button`: actions, with existing variants and sizes. `variant="link"` with
  `size="inline"` opens detail from a row, legend or list item. `Input`: labelled input.
- `AccessibleTabs`: page views, keyboard navigation and sticky navigation.
  `SegmentedControl`: related choices within one view.
- `Card`: grouped surface. `Panel`: titled data section built on Card, with
  accessible region name and an optional actions snippet.
- `Stat`: headline value, one baseline, optional click target and semantic tone.
- `Badge` / `StatusBadge`: compact state, not decoration.
- `DataTable`: lookup, numeric comparison, sorting and row actions.
- `ConfirmDialog`, `BlockingDialog`, `Sheet`, `Popover`, `Tooltip`:
  confirmation, blocking decisions, detail and contextual help.

Keep Usage's Forecast, Work ranking, grouped limits, request log, client
grouping and service drill-down. Extend their current owners, not older local
screen variants. Select, textarea and reusable loading/empty primitives are not
currently in this vocabulary. If a primitive is missing, add it here once,
with keyboard, focus, disabled, light/dark and narrow-screen states.

## Scales and tokens

`src/lib/theme/tokens.css`, `src/app.css` and `tailwind.config.ts` own the
scales. Use semantic aliases, not raw colours or arbitrary pixel font sizes.

- Type: `text-xs` (0.75rem), `text-sm` (0.875rem), `text-base` (1rem),
  `text-lg` (1.125rem), `text-xl` (1.25rem), `text-2xl` (1.5rem).
  Use tabular figures for quantities; monospace for identifiers.
- Spacing: quarter-rem steps; prefer 1, 2, 3, 4, 5, 6 and 8 (0.25–2rem).
  Reflow rather than reducing type on narrow screens.
- Radius: keep the primitive's radius. Card, Panel and Stat share the Card
  surface; controls use their shared control radius; Badge is full.
  Do not add a local border, radius, shadow or card/pill recipe.
- Colour: `foreground`, `secondary`, `muted`, `canvas`, `surface`, `raised`,
  `inset`, `separator`, `control-border`, `accent` and semantic `success`,
  `warning`, `danger`, `info` and their subtle/border pairs.
  Use chart-series tokens for categories, not status colours.

Document TOCs share `src/lib/theme/document-toc.css`: indentation belongs to
the label, while hover, current-location and focus span the full row. Keep
coarse-pointer rows at least 44px tall. Paper uses quieter neutral row fills;
Classic retains compact app controls. Rich readers map their own palette into
the shared row tokens.

## One status vocabulary

`src/lib/status.ts` owns presentation; `StatusBadge` composes Badge.
Failed/error is red; degraded/partial/at-risk is amber; verified ready/completed
is green; running is blue. Unknown, stale, disabled and cancelled are neutral.
Observed start means **Outcome unknown**, not verified running or success.
Pair colour with text/icon. Keep data quality separate from freshness:
partial data remains visible even when stale. Missing evidence is an em dash
or Unknown, never zero or healthy. Numerical thresholds belong to the metric
owner; do not create another local status-to-colour dictionary.

## Copy and errors

State facts and offer actions. Say one fact once. Mark money with one short
Estimated label; show in-progress work separately from closed totals.
Keep useful forecasts and scope controls. Remove paragraphs that explain the
capture pipeline, defend the system, give negative guarantees, or repeat labels.
Put definitions in tooltips or documentation, not explanatory paragraphs.

Errors name the failed operation and safe reason, show a correlation/reference
ID when available, and offer a recovery action. Never display or log arbitrary
upstream bodies, SQL parameters, prompts, message/tool content or secrets.
Log bounded protocol metadata and normalized reasons under AGENTS.md rules.

`npm run lint:design` checks added production lines against the merge base
with `origin/main`; existing debt stays out. `DESIGN_LINT_BASE` selects an
explicit comparison base. Tests and synthetic fixtures are excluded.
The guard is deliberately a small heuristic, not a UI parser.
