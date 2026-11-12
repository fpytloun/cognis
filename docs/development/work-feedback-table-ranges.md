# Exact authored rich table ranges

`RichLocator.table_range` is an optional additive JSON field:

```typescript
interface RichTableRange {
  representation: 'rich-table-v1';
  scope: 'cells' | 'rows' | 'columns';
  rows: number[];          // sorted unique zero-based authored row ordinals
  column_start: number;    // zero-based authored column ordinal, inclusive
  column_end: number;      // exclusive
}
```

A structural locator requires `field_path: []`, `item_key: null` and text
`range: null`. It selects the Cartesian product of explicit row membership and
the column interval. Row scope requires every data column; column scope requires
all canonical rendered-data rows. Sorting can produce non-adjacent authored
memberships: display neighbors never imply every row between their minimum and
maximum.

## Authority and compatibility

`Sources.validate_anchor` remains the canonical save boundary.
`rich_tables.py` performs bounded unique block resolution, checks authored block
positions rendered by rich container owners, and derives membership matching the
existing `tableRows`/`tableColumns` normalization. Tables and comparison/decision
matrices support positional, `values`, keyed-object and typed-cell rows, with
explicit or inferred columns. Duplicate/coerced column identities and missing
selected cells are rejected. Table-shaped metadata or assets are not targets.

Budgets are 1,000 rows, 128 columns and 4,096 selected cells. DTO validation checks
integer ordering and the product budget before expansion. Existing source-token,
actor, context, content-digest, grant, guest, disclosure and manifest fences remain
unchanged; DOM provenance is never server authority.

Absent/null `table_range` is omitted by the rich-locator serializer. Legacy
anchor, request and manifest JSON bytes/hashes remain unchanged. Existing
persisted locators continue to decode. Anchor schema version remains 1; no
database schema, backfill or parallel persistence is added. New ranges require
matching controller/UI deployment; older strict controllers reject the new field.

Ranges replay only against their exact source revision. Existing continuity
returns `not_located` across revisions, including semantic block IDs. Captured
HTML observed-element semantics and sandbox rules are unchanged.

## Renderer and shared presentation boundary

Renderers register normalized table/cell provenance through WeakMaps. The lazy
`table-range.ts` adapter owns temporary grip interaction, per-cell preview and
geometry only. It publishes an ordinary `onTargetChange` candidate with multiple
client rects. It does not own drafts, send, activation or shared presentation.

Library and inline chat use the same reactive surface binding. Passive chat
activation belongs to the source-surface intent bridge, which must provide the
existing Commenting binding before handle-down. Null-to-ready activation does
not replay a content swipe or manufacture a target. Native text selection,
long-press and scrolling do not start structural gestures.

Generated matrix Evidence/expanded rows are not data columns. Filtering and
reordering must retain authored provenance; whole columns are gated if canonical
rows are absent. Missing geometry returns `not_located`, never a table bounding
box. Source/context/mode/capability changes cancel capture and pending targets.

## Focused qualification

- Backend table-range, scoped-text, contract, runtime and deliverable-lifecycle
  suites cover persistence, legacy bytes/hashes, authority and continuity.
- Pure membership and real-renderer grip tests accompany existing feedback and
  annotation regressions.
- `npx playwright test --config playwright.table-range.config.ts` uses an
  isolated DEV-only synthetic fixture, not production save/direct-chat acceptance.
- `npm run check`, `npm run build`, `npm run build:standalone` retain existing
  startup Brotli, CSS and standalone gzip budgets.

Actual-app inline-chat saves, floating presentation integration and physical
touch-device/native selection acceptance remain with the integration owner.
