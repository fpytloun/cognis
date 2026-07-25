# Daily Brief v10 — exact skill patch

This document is the reviewable patch artifact for upgrading the user-owned
`daily-brief` skill from v9 to v10. Apply it through the normal Cognis skill
update API/UI after WS6 is deployed. **Do not mutate the production database
directly.**

## Replacement fields

Keep the existing skill ID, name, description, tags, assets, and ownership.
Replace `instructions`, `prompt_templates`, and `steps` with the exact values
below. Set `steps` to an empty array so the skill remains instructions-only.

### `instructions`

```markdown
# Daily Brief v10

## Execution shape

- Run as one normal task using the generic task workflow. Do not create, save,
  compose, or invoke a decomposed skill workflow.
- Load `cognis-pulse-deliverable` before synthesis and follow its
  `describe_tool`-first authoring contract.
- Collect source domains independently where concurrency is available, but
  require every collector to return only strict JSON matching the collector
  envelope below. No collector may write prose, Markdown, or a deliverable.
- The synthesis pass is the only authoring pass. It consumes collector JSON,
  copies the exact `daily` skeleton returned by the live `rich:pulse`
  descriptor, fills every required slot, validates the complete tool call,
  and writes one final deliverable.

## Collector envelope

Every collector returns exactly:

{
  "collector": "stable-domain-name",
  "status": "ok|partial|unavailable",
  "observed_at": "ISO-8601 timestamp with explicit offset",
  "items": [
    {
      "id": "stable-source-local-id",
      "kind": "agenda|task|news|ai|weather|market|fuel|figure|other",
      "title": "concise factual title",
      "summary": "concise factual summary",
      "value": null,
      "unit": null,
      "timestamp": "ISO-8601 timestamp with explicit offset or null",
      "source_id": "source-id",
      "source_url": "absolute source URL or null",
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
  "errors": []
}

Collectors must emit valid JSON, preserve unavailable fields as null, use
empty arrays rather than prose explanations, and never invent observations.

## Synthesis

1. Reject or isolate malformed collector output; do not silently coerce prose
   into facts.
2. Deduplicate sources and select the smallest decision-relevant set of facts.
3. Call `describe_tool({"tool":"write_deliverable"})`.
4. Copy the live daily skeleton from the exact returned path
   `descriptor.extensions.presentation_contracts["rich:pulse"].valid_skeleton`.
   Do not recreate it from memory.
5. Fill the exact daily slots without changing their order:
   hero; 3–4 metric signal grid/dashboard; day agenda; primary contextual
   columns; bounded knowledge section; monitoring section containing
   figures/charts/metrics; closing callout; source list.
6. Keep titles natural and content-specific. Do not add a top-level Markdown
   essay, table of contents, or academic numbering.
7. Set `action` to `rich:pulse`, `metadata.presentation` to `pulse`, and
   `metadata.pulse_variant` to `daily`. Keep `toc` and publication numbering
   false or absent.
8. Call `validate_tool_call` with the complete proposed
   `write_deliverable` arguments.
9. Fix every returned JSON-path issue and validate again.
10. Call `write_deliverable` once validation succeeds. The top-level
    `content` must be a concise accessible fallback containing the key
    decisions and source caveats.
11. If a valid Pulse cannot be produced after a focused repair, explicitly
    author a new generic rich payload with `action` set to `write_deliverable`
    and neither `metadata.presentation` nor `metadata.pulse_variant`, validate
    it, and write that fallback. Do not relabel or reuse the rejected Pulse
    payload. Never persist invalid Pulse.

## Completion

- Use only the generic task workflow completion contract.
- Do not create or retain decomposed skill steps.
- Report collector degradation and explicit generic fallback in completion
  metadata.
```

### `prompt_templates`

```json
{
  "daily_brief_v10": "Create today's daily brief. Run this as one generic task workflow. Load cognis-pulse-deliverable. Collect each source domain as strict JSON using the collector envelope in the skill instructions. Then call describe_tool for write_deliverable, copy descriptor.extensions.presentation_contracts[\"rich:pulse\"].valid_skeleton exactly, fill its slots from verified collector JSON, set action to rich:pulse, validate the complete call, repair all JSON-path issues, and write one final deliverable. Use a newly authored generic rich payload with action write_deliverable and without Pulse metadata only after a focused Pulse repair fails. Do not create or use a decomposed skill workflow."
}
```

### `steps`

```json
[]
```

## Migration from v9

1. Export or record the current v9 skill version ID for rollback.
2. Confirm WS6 is deployed and `describe_tool(write_deliverable)` returns the
   registered `rich:pulse` operation with schema version
   `cognis.rich.pulse.v1`.
3. Update the user-owned `daily-brief` skill through the authenticated skill
   API/UI with the exact replacement fields above. Do not use SQL or direct DB
   edits.
4. Confirm the resulting version has no decomposed steps and contains the
   `daily_brief_v10` prompt template.
5. Run one test task through the generic task workflow. Verify collectors are
   strict JSON, Pulse validation succeeds before persistence, and chat,
   standalone HTML, and PDF render the same deliverable.
6. If acceptance fails, restore the recorded v9 version through normal skill
   version restore; do not patch production rows manually.
