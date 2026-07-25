# Rich Deliverables v2

`write_deliverable` accepts `format: "rich"` in addition to `markdown`, `plain`, and `html`.
`content` remains required and is the canonical fallback for model-visible summaries,
channels, compaction, notifications, accessibility, copy/export seams, and clients that
do not support rich rendering.

Rich payloads use a renderer-neutral, block-composed shape:

```json
{
  "blocks": [{ "type": "section", "title": "Summary", "blocks": [] }],
  "assets": [],
  "sources": [],
  "datasets": [],
  "exports": [],
  "metadata": {}
}
```

There is no primary `kind` or `template_hint`. Canonical writes reject unsupported block
types and invalid child containers before persistence. The required fallback `content`
remains the compatibility surface for clients that cannot interpret a valid rich payload.

Supported v2 block types:

- layout: `section`, `stack`, `columns`, `grid`, `tabs`, `accordion`, `modal`
- content: `markdown`, `callout`, `card`, `card_grid`
- media: `figure`, `gallery`
- data: `table`, `comparison_matrix`, `chart`, `day_agenda`
- diagrams/web: `mermaid`, `link`, `link_preview`, `source_list`

Charts use the canonical `cognis.chart.v1` block contract described below. Raw
chart-library configurations, callbacks, agent-supplied JavaScript, and executable
markup are not part of the payload. Mermaid source remains controlled source/fallback
so malformed diagrams never break the deliverable.

Persistence supports exactly one owner scope per deliverable:

- workflow/task step scope via `step_run_id`
- direct-chat scope via `conversation_id`, `session_id`, and `turn_id`

## v2 renderer boundaries

Svelte is the only interactive web renderer. The in-app deliverable tree and the
standalone view both mount the same Svelte rich-deliverable renderer; Chart.js is loaded
by that renderer only when a canonical chart can render to a canvas. Python does not
render an interactive web view. It renders sanitized static HTML and deterministic SVG
for PDF generation and the no-JavaScript fallback.

The standalone view is a client-side mount, not SSR and not hydration. The controller
emits an empty `#cognis-deliverable-root`, an escaped inert JSON
`<template id="cognis-deliverable-payload">`, and a controller-served external
same-origin module script from the hashed standalone asset manifest. The template is
data, not executable source; its JSON is HTML-escaped before insertion. The module
parses that template and calls Svelte `mount`.

Controller-built, same-origin renderer JavaScript is permitted for that shell. Payloads
may never supply JavaScript, HTML event handlers, inline CSS, raw Chart.js
configuration/callbacks, or a renderer asset URL. The shell has no inline script, and
the controller does not add `'unsafe-inline'` to `script-src`.

When JavaScript is unavailable, the standalone document exposes a Python-rendered,
semantic `<noscript>` fallback. The same Python path supplies PDF input and static SVG
charts. The shell also contains controller-derived title, description, and Open Graph
`article`, title, description, and URL metadata, so previews do not require JavaScript.
If the UI is disabled, the standalone manifest is unavailable, rendering is not `rich`,
or shell construction fails, the route falls back to the static Python HTML renderer.

### Standalone security and cache contract

Token-backed Svelte-shell HTML uses `Cache-Control: no-store`,
`Referrer-Policy: no-referrer`, and `X-Content-Type-Options: nosniff`. Its exact CSP is:

```text
sandbox allow-scripts allow-same-origin allow-downloads; default-src 'none';
script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self';
img-src 'self' data:; font-src 'self' data:; media-src 'self' data:
```

`'unsafe-inline'` is limited to styles for the Svelte/runtime styling contract; it is
not a script permission. Hashed standalone assets are controller-served before
authentication, confined to the standalone asset manifest, same-origin, `nosniff`, and
cacheable as `public, max-age=31536000, immutable`. They are not agent-provided assets.

The static Python fallback is a distinct response with `STATIC_CSP`, including
`script-src 'unsafe-inline'` and `style-src 'unsafe-inline'`; that renderer currently
uses controller-generated inline theme/interaction scripts. It is therefore not covered
by the shell's no-inline-script guarantee.

## Canonical charts: `cognis.chart.v1`

A submitted chart block is canonical only when it contains `type: "chart"`,
`spec_version: "cognis.chart.v1"`, a supported `chart_type`, and at least one series
with points. Submission validation rejects unknown or legacy chart fields and invalid
recognized values. Renderer normalization additionally discards unusable series or
points. Static/PDF rendering then emits unavailable-chart fallback text; the Svelte
renderer retains its empty interactive chart container and omits its data table when no
usable series remains.

```ts
type ChartType =
  | 'line' | 'area' | 'bar' | 'grouped_bar' | 'stacked_bar'
  | 'sparkline' | 'progress' | 'range' | 'donut';
type AxisType = 'time' | 'category' | 'linear';
type LegendPosition = 'top' | 'right' | 'bottom' | 'none';
type PaletteToken = 'default' | 'cool' | 'warm' | 'categorical';

interface ChartAxis {
  type?: AxisType;       // default: category for x, linear for y
  label?: string | null;
  unit?: string | null;
  min?: number | null;
  max?: number | null;
}

interface ChartPoint {
  x: string | number;
  y: number | [number, number]; // exactly two finite bounds only for chart_type: 'range'
  label?: string | null;
}

interface ChartSeries {
  id?: string | null;   // defaults to series-N during normalization
  label?: string | null; // defaults to id during normalization
  points: ChartPoint[];
  stack?: string | null; // named stack group; separate from block-level stack
}

interface CanonicalChart {
  type: 'chart';
  spec_version: 'cognis.chart.v1';
  chart_type: ChartType;
  series: ChartSeries[];
  title?: string;
  x_axis?: ChartAxis | null;
  y_axis?: ChartAxis | null;
  stack?: boolean;      // default false
  legend_position?: LegendPosition; // default bottom
  palette_token?: PaletteToken;     // default default
  source_ids?: string[]; // each value is a non-empty string
  source?: string | null;
  source_url?: string | null;
  observed_at?: string | null;
  description?: string | null; // defaults to "Chart"
}
```

For a `range` chart, `y` is exactly `[low, high]`; normalizers order reversed
bounds as `[low, high]`. Other chart types use one finite numeric `y`. Linear x-values
are finite numbers; category x-values are non-empty labels or finite numbers; and time
x-values must parse as dates/timestamps. `source_ids`, `source`, `source_url`, and
`observed_at` carry
the chart's provenance/freshness metadata; they are data, not a request to fetch or
execute anything.

Point ordering is deterministic. Duplicate x-values within one series retain the last
point. Category points retain author order; linear points sort numerically; time points
sort chronologically. Labels are derived from the ordered union of series points, not
authored as a parallel labels array. The persisted canonical chart schema does not
include `range_selector` or `ranges`; range controls seen by the Svelte renderer are not
an authoring extension to this persisted contract.

One canonical chart is the source for every surface:

| Surface | Renderer/output | Chart behavior |
| --- | --- | --- |
| In-app web | Svelte + lazily loaded Chart.js | interactive canvas, default range controls, series visibility, and an accessible table when normalized data exists |
| Standalone web | same Svelte client mount + Chart.js | same canonical payload and behavior; Python `<noscript>` fallback remains available |
| Static HTML / PDF | Python | deterministic SVG plus accessible data table; no browser chart runtime |
| External channel | channel projector | concise deterministic trend text from the same normalized series |

No surface accepts a separate Chart.js configuration, chart callback, or independently
authored trend summary as the chart definition.

## Media URL resolution

Artifact-backed rich media keep media as manifest keys, not URLs. A renderer receives
`mediaUrlFor(mediaKey) -> string`, supplied by the controller for the authorized
deliverable/share scope. It may resolve only a `media_<24 lowercase hex>` key that is
present in `media_manifest`; an empty result leaves the media unresolved. The in-app
private/share resolver and standalone resolver construct controller routes from that
key, URL-encoding the key path segment. Signed artifact URLs and artifact IDs as browser
URLs are never persisted for artifact-backed media. Ordinary figure `src`/`url` fields
remain separately URL-sanitized renderer inputs and are not resolved through
`mediaUrlFor`.

## Object-backed payloads and chart backfill

Deliverable content, rich payload, and optional outputs are object-backed under the
deliverable namespace/object ID. Database rows retain object keys, MIME metadata, sizes,
and SHA-256 hashes; hydration loads those objects into transient fields. Once content has
been saved, a later payload-write failure triggers best-effort deletion of that
deliverable object. A successfully loaded legacy rich payload with a noncanonical chart
is withheld from rich rendering, leaving the required `content` fallback. Object-store
load, UTF-8 decode, and JSON parse failures are retrieval errors; this storage layer does
not convert them to fallback content.

The chart-v1 backfill is a one-shot, failure-isolated background pass over eligible
rich-deliverable database rows in bounded, stable keyset batches; it does not scan object
storage. It verifies the current object byte length and SHA-256 before parsing, rejects
oversized/corrupt/unsupported payloads, writes a chart-v1-keyed JSON object, rereads and
hash-verifies that staged object, then atomically promotes it with a compare-and-swap on
deliverable ID, prior rich key, prior rich hash, and non-superseded status. A CAS miss
does not overwrite a concurrent change. The old object is deleted only after promotion;
cleanup failure is recorded without invalidating the promoted payload. Failed,
unmigratable, missing, or integrity-mismatched rows are skipped. Migration uses the same
normalizer as rendering: a payload with a usable canonical chart can be promoted even
when some series/points are later discarded by a renderer. Legacy object keys containing
a noncanonical chart are not hydrated as rich data before migration.

### Generic visual fields and artifact-backed media

Every supported block, including `card`, `metric`, `status`, and `action`, may use
renderer-neutral `variant`, `dek`, `summary`, `href`, `source_ids`/`citations`,
scalar `icon`, and `tone` fields. Unknown icon names are data, not executable
markup; renderers must fall back to text or omit them. Arbitrary block HTML, SVG,
CSS, and inline style are rejected.

Authored block media uses a saved Cognis artifact-compatible image ref:

```json
{
  "media": {
    "ref": "att_...",
    "alt": "Accessible description",
    "credit": "Source",
    "source_url": "https://example.test/source",
    "role": "hero",
    "aspect_ratio": "16:9",
    "focal_point": { "x": 0.5, "y": 0.4 }
  }
}
```

Before artifact bytes are loaded, write normalization checks owner and conversation
access, including managed-descendant ancestry while denying sibling branches. It then
checks active lifecycle state, raster MIME (`PNG`, `JPEG`, `GIF`, or `WebP`), stored
size/hash, decoder validity, dimensions, and pixel limits. SVG and HTML are not accepted
as inline artifact-backed media.

Persistence does not copy media bytes and never stores a signed URL. The block ref
becomes `{ "key": "media_<digest>", ...presentation fields }`; a controller-owned
top-level `media_manifest` maps that local key to immutable artifact provenance,
MIME, filename, dimensions, size, and SHA-256 metadata. The source artifact uses the
existing `attached` lifecycle (`expires_at = null`) as the retention hook. It remains
owned by its original conversation and may be reused by multiple deliverables; therefore
superseding a deliverable does not delete it. Explicit artifact deletion remains
authoritative and media access then degrades to `404` without invalidating the
deliverable fallback.

Authenticated clients resolve
`/api/v1/deliverables/{deliverable_id}/media/{media_key}`. Public clients use
`/api/v1/deliverables/share/{same_token}/media/{media_key}`; the proxy accepts only a
manifest member of the token's deliverable and never accepts a raw artifact ID. Internal
and PDF integrations can call `resolve_deliverable_media` directly after deliverable
authorization. Embedded and standalone Svelte renderers receive scoped `mediaUrlFor`
resolvers that convert only manifest-listed media keys to controller-owned routes;
arbitrary external URLs and raw artifact IDs are never synthesized.

Completed task retrieval includes fallback `content`, `rich_payload`, validation warnings,
render metadata, and export metadata. Direct-chat rich deliverables are returned inline via
the `write_deliverable` tool result and replay through existing tool-call timeline events.
Lightweight projections may omit oversized rich block payloads and return projection
metadata indicating that full payload retrieval is required; full task/deliverable retrieval
surfaces keep the complete payload when it is within the storage guard.

## Progressive publication semantics

The same v2 payload can opt into publication behavior without introducing a parallel
document schema:

```json
{
  "metadata": {
    "toc": { "enabled": true, "depth": 3 },
    "publication": {
      "number_figures": true,
      "number_tables": true
    }
  }
}
```

- TOCs appear automatically only when a document has at least four titled H2-level
  blocks. `metadata.toc` may be a boolean or `{ "enabled": boolean, "depth": 2|3 }`.
  A depth-only object does not override `metadata.show_toc`; `show_toc` remains the
  compatibility boolean. Depth is `2` by default. Markdown blocks use their first
  level-1–3 Markdown heading when they do not have an explicit block title.
- Heading anchors are stable ASCII slugs with deterministic `-2`, `-3` suffixes.
  Explicit block `id` or `anchor` values seed the slug. Ordinary Markdown fragment
  links such as `[see evaluation](#evaluation)` are the conservative cross-reference
  syntax; no renderer-specific expression language is added. Browser rendering keeps
  the former top-level `rich-section-{index}` fragments as zero-height aliases. Only
  the currently visible embedded or full-view tree is mounted, so canonical anchors,
  legacy aliases, citation dialogs, and ARIA relationships remain unique.
- All destinations share one document-wide allocator. User-derived IDs that occupy
  generated namespaces (`rich-section-*`, `cite-*`, `citation-*`,
  `rich-citation-*`, `reference-*`, `references-heading`, `toc*`, `figure-*`,
  or `table-*`) receive a deterministic `section-` prefix; subsequent collisions
  receive numeric suffixes. TOC, bibliography, citation, and backreference links
  always use the allocated result rather than the untrusted requested ID.
- Browser output adds a per-mounted-deliverable namespace to every canonical anchor,
  legacy alias, citation dialog/control relationship, and Mermaid render ID. Durable
  deliverable IDs seed the namespace; repeated mounts receive deterministic numeric
  instance suffixes, while anonymous fixtures receive process-local monotonic
  namespaces. This keeps multiple reports collision-free in one conversation. Raw
  internal fragment links are rewritten to the current report's allocated target.
  Unscoped legacy fragments are retained only in standalone/PDF output, where a
  single deliverable owns the document and doing so cannot create duplicates.
- A Markdown block with an explicit title renders that title as its anchored H2/H3
  publication heading. Its Markdown H1–H3 content is normalized to subordinate H3
  headings and joins the TOC only at depth 3. Without an explicit title, the first
  Markdown heading owns the block anchor and later headings are subordinate. This
  avoids attaching a title-derived target to unrelated content or duplicating titles.
- Navigation indexes top-level blocks and explicit canonical `blocks`/`children`
  descendants. Item-backed `tabs`, `accordion`, and `gallery` entries remain
  component-local presentation and are deliberately excluded from the TOC on both
  browser and PDF surfaces; this prevents links to synthetic or collapsed content
  without stable cross-renderer targets. The titled container itself remains a valid
  navigable section.
- Figure/table numbering is off by default. Enable it with `publication: true`,
  the granular publication keys above, or `metadata.number_figures` /
  `metadata.number_tables`. Granular booleans override the `publication: true`
  default independently in both renderers.
- Research/evidence blocks continue to cite sources by `id`, `key`, `citation_id`,
  title, or URL. Standalone/PDF rendering deduplicates sources by explicit identity,
  DOI, URL, then bibliographic metadata; numbers them in first-use order; and emits
  a compact IEEE-style bibliography. Source metadata may include `authors`/`author`,
  `title`, `publication`/`publisher`, `year`/`date`, `accessed`, `doi`, and `url`.
  Chat keeps source previews and expandable snippets. Citation numbers are
  document-wide across research and evidence blocks, and each citation group removes
  repeated references before rendering. Scalar, array, and inline source-object
  reference forms have the same meaning in browser and PDF output. DOI prefixes
  (`doi:` and `https://doi.org/`) are normalized case-insensitively.
- PDF citations remain inline at the end of the sentence or claim. Bibliography
  return links render as a compact inline `↩ 1, 2` suffix. Short bibliographies flow
  with the preceding content; `metadata.references.dedicated_page` can force or
  suppress a dedicated page, while long bibliographies select one automatically.
- Figure sources accept normal HTTP(S) images, inert URI-encoded SVG, and base64
  PNG/JPEG/GIF/WebP data images. Inline SVG containing scripts, event handlers,
  external references, embedded images, or CSS URLs is rejected.

PDF and no-JavaScript standalone fallback use the Python sanitized static renderer.
The JavaScript-enabled standalone view instead client-mounts the Svelte renderer as
specified above. WeasyPrint provides internal links, target page counters in the TOC,
repeated table headers, and semantic heading bookmarks/outlines. External resource
loading remains disabled.

PDF cache misses use an in-process, content-scoped single-flight key covering the
deliverable identity, storage object, and render cache version. PDF output is invariant
across already-authorized user/share callers, so concurrent requests within one
controller process safely share one render and receive the same result. Failed flights
are removed so later requests can retry. This does not coordinate across multiple
controller processes. Multi-process deployments still use the versioned artifact cache,
but the first cache miss may render once per process.

Deliberate non-goals: agent-supplied executable code; SSR or hydration of standalone
deliverables; inline scripts or `'unsafe-inline'` script CSP in the Svelte shell;
arbitrary HTML, SVG, CSS, or Chart.js configuration; remote resource fetching by
static/PDF rendering; artifact-backed payload signed URLs; and a TeX/MathJax dependency.
Equations should currently use accessible Unicode or escaped preformatted text. Mermaid
remains an interactive-browser diagram with a readable source fallback in PDF.

## Pulse/newsroom presentation

News briefs and personal intelligence updates opt into the reusable mobile-first
newsroom presentation with `metadata.presentation: "pulse"`. Pulse is a presentation
preset over the v2 schema. This revision also adds the backward-compatible canonical
`day_agenda` block. Older schema-driven clients that do not recognize it must render
the required top-level fallback `content` (or their existing unknown-block fallback)
rather than partially interpreting agenda fields.

Pulse guarantees:

- no TOC, even when `metadata.toc` is truthy or the brief crosses the automatic
  substance threshold;
- no publication-style figure or table numbering, while captions, source labels,
  timestamps, alt text, and source links remain;
- one document H1, owned by the first `hero` block when present;
- compact editorial typography and spacing in chat, full view, standalone HTML,
  and PDF;
- responsive single-column composition on narrow screens and restrained newsroom
  columns on desktop/PDF.

The recommended daily-brief payload is:

```json
{
  "metadata": {
    "presentation": "pulse",
    "eyebrow": "Neděle · Lovosice",
    "subtitle": "12. července 2026 · data 07:10 CEST"
  },
  "blocks": [
    {
      "type": "hero",
      "eyebrow": "Osobní intelligence · 07:10",
      "title": "Ranní pulse",
      "subtitle": "Místní datum, časové pásmo a nejstarší čas dat"
    },
    {
      "type": "grid",
      "blocks": [
        { "type": "metric", "label": "Agenda", "value": "3", "delta": "Další 09:30" },
        { "type": "metric", "label": "Lovosice", "value": "18 °C", "delta": "25 °C · déšť 20 %" },
        { "type": "metric", "label": "Trhy", "value": "↗ mírně", "delta": "USD/CZK i BTC výše" },
        { "type": "metric", "label": "Dominantní signál", "value": "Okno 9–12", "delta": "Jedna věta" }
      ]
    },
    {
      "type": "columns",
      "blocks": [
        {
          "type": "section",
          "eyebrow": "Hlavní zpráva",
          "title": "Silný konkrétní titulek",
          "content": "Dvě až tři věty briefingu.\\n\\n**Pro Filipa:** konkrétní dopad.",
          "blocks": [{
            "type": "figure",
            "src": "https://verified.example/news-image.jpg",
            "alt": "Věcný popis významu obrázku",
            "caption": "Co obrázek dokládá.",
            "source": "Vydavatel",
            "source_url": "https://verified.example/article",
            "timestamp": "07:02 CEST"
          }]
        },
        {
          "type": "stack",
          "title": "Dnes udělat",
          "blocks": [{ "type": "card", "eyebrow": "1 · Priorita", "title": "Akce", "content": "Důvod." }]
        }
      ]
    },
    {
      "type": "day_agenda",
      "title": "Neděle 12. července",
      "now": "2026-07-12T07:10:00+02:00",
      "timezone": "Europe/Prague",
      "freshness": "07:08 CEST",
      "items": [
        { "all_day": true, "title": "Celodenní položka ze zdroje" },
        { "start": "2026-07-12T07:10:00+02:00", "end": "2026-07-12T09:15:00+02:00", "title": "Volné okno", "kind": "free" },
        { "start": "2026-07-12T09:30:00+02:00", "end": "2026-07-12T10:00:00+02:00", "title": "Další schůzka", "next": true }
      ],
      "tasks": [{ "title": "Existující úkol z Todoist" }]
    },
    {
      "type": "section",
      "title": "Vědět",
      "blocks": [{
        "type": "card_grid",
        "blocks": [{ "type": "card", "eyebrow": "Česko · 06:55", "title": "Titulek", "content": "**Dopad:** jedna věta. [Zdroj](https://verified.example/article)." }]
      }]
    },
    {
      "type": "section",
      "title": "Sledovat",
      "blocks": [{
        "type": "chart",
        "title": "Tržní směr · 5 dní",
        "description": "Hodnoty a časový horizont.",
        "spec_version": "cognis.chart.v1",
        "chart_type": "line",
        "x_axis": { "type": "category", "label": "Den" },
        "y_axis": { "type": "linear", "unit": "CZK" },
        "series": [{
          "id": "usd-czk",
          "label": "USD/CZK",
          "points": [{ "x": "Po", "y": 100.0 }]
        }],
        "source": "Poskytovatel dat",
        "source_url": "https://verified.example/data",
        "observed_at": "2026-07-10T22:00:00+02:00"
      }]
    },
    { "type": "callout", "title": "Dnešní kurz", "content": "Jeden rozhodovací odstavec." },
    { "type": "source_list", "title": "Zdroje a časy dat" }
  ],
  "assets": [],
  "sources": [],
  "datasets": [],
  "exports": []
}
```

Authoring rules:

- Use verified, story-relevant HTTP(S) images only. Browser loading continues through
  the existing URL sanitizer; standalone/PDF external resource loading remains
  disabled and the figure is omitted gracefully. Never substitute generic stock art.
- Every chart uses the canonical series/points model, provides a description plus
  provenance/freshness metadata when known, and leaves the renderer to derive the
  accessible table fallback. Static/PDF rendering emits unavailable-chart text for
  invalid data; the Svelte surface retains an empty chart container without a data table.
- `day_agenda` canonical fields are `title`, optional local calendar `date`
  (`YYYY-MM-DD`), IANA `timezone`, ISO-8601 `now`, `items`, `tasks`, and `source`.
  `source` is `{id?, label?, url?, refreshed_at?}`. `items` contain `{title,
  all_day?, start?, end?, location?, description?, kind?: "event"|"free",
  source_id?}`. `now`, timed `start`/`end`, task `due`, and source `refreshed_at`
  accept only full ISO datetimes with `T`, a valid clock time, and an explicit `Z` or
  numeric offset (for example `2026-07-12T07:10Z` or
  `2026-07-12T07:10:00+02:00`). Date-only, space-separated, and naive datetimes are
  rejected. All-day entries belong to `date` and do not require times. `tasks` contain
  `{title, due?, priority?, source_id?}` and remain a sourced task summary, never
  inferred calendar events.
- Compatibility aliases accepted during normalization are `events` for `items`,
  `now_iso` for `now`,
  `label` for event title, `content` for task title, `allDay`, `start_iso` /
  `start_time`, `end_iso` / `end_time`, `due_at`, `refreshed_at_iso`, and scalar source
  identifiers. Canonical values win when both forms are present, even when the
  canonical value is invalid. Persisted normalized payloads contain only canonical
  names.
- Validation drops null/scalar array entries, untitled entries, invalid timestamps,
  and timed events without a valid start. Invalid end-before-start is omitted.
  Unknown timezone names are discarded and renderers deterministically use UTC.
  Renderers convert all instants to the valid agenda IANA timezone, compare absolute
  instants, sort timed entries by start, end, then title, keep all-day entries
  separate, and derive past/current/future plus the next event from `now`;
  author-supplied `next` flags are not authoritative. An event without `end` is a
  zero-duration event: it is future only while `start > now`, past when
  `start <= now`, and never current.
- Missing or malformed arrays normalize to empty arrays. Invalid `now` is omitted,
  so no entry is labelled past/current/next and no current-time marker is rendered.
  An agenda without timed
  events renders a quiet empty state while retaining all-day items, tasks, freshness,
  and the required deliverable fallback. Unknown clients safely downgrade to the
  top-level `content`.
- Keep secondary stories to a headline plus one impact sentence and relevant source.
  Do not repeat them as citations and full source cards in the same local section.
- The canonical order is executive grid, lead story, `Dnes udělat`, `Vědět`,
  `Sledovat`, `Dnešní kurz`, then a compact source list.

The full static Czech fixture, including local SVG and chart datasets, lives in
`ui/src/lib/components/rich/daily-pulse.fixture.ts`. Its values are test-only and
must never be copied into production briefs as current claims.
