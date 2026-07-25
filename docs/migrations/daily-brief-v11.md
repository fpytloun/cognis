# Daily Brief v11 — Pulse v2 migration artifact

This is the exact reviewable patch for the next user-owned `daily-brief` skill
version. Apply it through the authenticated Cognis skill API/UI only after the
Pulse v2 authoring contract is deployed. Do not mutate the production database.

## Replacement fields

Keep the existing skill ID, name, description, tags, assets, and ownership.
Replace `instructions`, `prompt_templates`, and `steps` with the values below.
Set `steps` to an empty array so the skill remains instructions-only.

### `instructions`

```markdown
# Daily Brief v11

## Execution shape

- Run as one normal task using the generic task workflow. Do not create or invoke
  a decomposed skill workflow.
- Load the system skill `cognis-pulse-deliverable` before collection or
  synthesis. Follow its live `describe_tool`-first Pulse v2 contract.
- Collect independent source domains in parallel where concurrency is
  available. Every collector returns exactly one strict media-aware JSON
  envelope; collectors never write prose, Markdown, or a deliverable.
- The synthesis pass is the only authoring pass. It maps verified collector
  items, sources, and media into one Pulse v2 payload, validates the complete
  call, and writes one final deliverable.

## Collector envelope

Every collector returns exactly:

{
  "collector": "stable-domain-name",
  "status": "ok|partial|unavailable",
  "observed_at": "ISO-8601 timestamp with explicit offset",
  "items": [
    {
      "id": "stable-source-local-id",
      "kind": "agenda|task|inbox|news|ai|weather|market|fuel|figure|other",
      "title": "concise factual title",
      "summary": "concise factual summary",
      "value": null,
      "unit": null,
      "timestamp": "ISO-8601 timestamp with explicit offset or null",
      "source_id": "source-id",
      "source_url": "absolute source URL or null",
      "media_ids": [],
      "metadata": {}
    }
  ],
  "sources": [
    {
      "id": "source-id",
      "title": "source title",
      "url": "absolute URL",
      "publisher": "publisher or service",
      "observed_at": "ISO-8601 timestamp with explicit offset"
    }
  ],
  "media": [
    {
      "id": "media-id",
      "kind": "image|figure",
      "url": "absolute URL or null",
      "artifact_id": "artifact ID or null",
      "alt": "specific accessible description",
      "caption": "concise caption or null",
      "source_id": "source-id",
      "provenance": "publisher, service, or generated-artifact provenance",
      "observed_at": "ISO-8601 timestamp with explicit offset"
    }
  ],
  "errors": [
    {
      "kind": "infrastructure_error|source_unavailable",
      "message": "concise non-secret diagnostic",
      "retryable": true
    }
  ]
}

Use `infrastructure_error` only when Cognis/executor/tool transport prevented
collection. Use `source_unavailable` when the collector ran but the upstream
source had no usable current data. Preserve unavailable fields as null, use
empty arrays rather than prose, and never invent observations.

## Selection and mapping

1. Reject or isolate malformed collector output; never coerce prose into facts.
2. Deduplicate `sources` by stable ID and canonical URL. Number the selected
   Pulse sources sequentially from 1 and preserve item-to-source citations.
3. Map selected media by `media_ids`. Every rendered image/figure must preserve
   alt text and provenance. Omit media that lacks either.
4. Exclude zero-duration agenda events (`start == end`) and malformed intervals.
5. Inbox search limits are candidate windows, not content targets. From a
   15-result Gmail sample, include only messages with a concrete action,
   deadline, risk, payment/security consequence, or requested decision. Never
   chart inbox counts and never list low-value promotions, automated receipts,
   Renovate noise, or the rest of the sample.
6. Prefer omission over repeated unavailable cards. Across the whole brief,
   allow at most one compact degraded-data warning summarizing material gaps.
   Mark that warning block with `status: "unavailable"` (or
   `degraded_data: true`) so quality metadata can count it explicitly.
   Do not turn infrastructure failures into claims that the source itself was
   unavailable.

## Pulse v2 synthesis

1. Call `describe_tool({"tool":"write_deliverable"})`.
2. Require schema version `cognis.rich.pulse.v2`. Copy the exact returned
   `descriptor.extensions.presentation_contracts["rich:pulse"].valid_skeleton`.
3. Keep `action="rich:pulse"`, `metadata.presentation="pulse"`,
   `metadata.pulse_variant="daily"`, and `metadata.pulse_version=2`.
4. Fill the slots in order: hero; icon signal dashboard; compact agenda;
   editorial feature/research answer plus actions; cited News and AI
   accordions; visual monitoring; closing callout; numbered source list.
5. Include at least one non-agenda artifact image/figure or meaningful chart.
   A line chart has at least three real observations. Every chart has source and
   timestamp. Never use source counts, collector counts, inbox sample counts,
   unavailable counts, or other structural bookkeeping as a chart.
6. Link and cite every News/AI story. When there are multiple stories, keep
   them in progressive-disclosure accordion groups.
7. Call `validate_tool_call` with the complete proposed `write_deliverable`
   arguments. Repair every JSON-path issue; `valid=true` confirms the hard
   quality gate passed. Detailed quality counts and `quality_gate_passed` are
   produced after `write_deliverable` and supplied to the evaluator.
8. Call `write_deliverable` once. The top-level `content` is a concise,
   accessible fallback with key decisions and material source caveats.
9. If a focused Pulse repair still fails, author a new generic rich payload
   with `action="write_deliverable"` and no Pulse presentation, variant, or
   version metadata. Validate that generic fallback before writing it.

## Completion

- Use the generic task workflow completion contract.
- Report collector degradation by error kind and explicit generic fallback in
  completion metadata.
- Do not claim Pulse success from block structure alone; the evaluator consumes
  the server-produced quality metadata.
```

### `prompt_templates`

```json
{
  "daily_brief_v11": "Create today's daily brief as one generic task. First load the system skill cognis-pulse-deliverable. Collect independent domains in parallel as strict media-aware JSON using the v11 collector envelope. Distinguish infrastructure_error from source_unavailable, map verified sources and media, drop zero-duration agenda entries, and select only actionable or decision-relevant messages from any 15-result Gmail candidate window. Then describe write_deliverable, copy the live cognis.rich.pulse.v2 skeleton, author action rich:pulse with pulse_version 2, include at least one meaningful non-agenda visual, cite and link all News/AI stories in accordions, use at most one compact degraded-data warning, repair validation issues until valid=true, and write one final deliverable. Use a newly authored validated generic rich fallback without Pulse metadata only after a focused Pulse repair fails."
}
```

### `steps`

```json
[]
```

## Migration and rollback

1. Export or record the current `daily-brief` version ID for rollback.
2. Confirm `describe_tool(write_deliverable)` exposes `rich:pulse` schema
   `cognis.rich.pulse.v2`, requires `metadata.pulse_version=2`, and returns the
   v2 quality gate and skeleton.
3. Apply the exact replacement fields above through the authenticated skill
   API/UI. Do not use SQL or direct database edits.
4. Confirm the new version has no decomposed steps and contains only the
   `daily_brief_v11` prompt template.
5. Replay a production-like task with an executor outage, a zero-duration
   agenda record, and a 15-result Gmail candidate window. Verify one compact
   degradation warning at most, no low-value inbox sample, and a passing
   server-produced Pulse quality gate.
6. Verify the generic fallback path separately and confirm persisted Pulse v1
   deliverables still render unchanged.
7. On acceptance failure, restore the recorded prior version through normal
   skill version restore. Do not patch production rows manually.
