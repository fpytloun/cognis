# Daily Brief v13 migration

Daily Brief v13 is a DB-owned skill and schedule update that must be applied
through the authenticated Cognis API after the controller code containing the
Daily Brief acceptance contract is deployed.

Alembic revision 094's additive artifact schema, legacy source-identity
backfill, and lookup index are applied automatically by Cognis' idempotent
production schema bootstrap. That schema migration does not publish or alter
owner-specific skills or schedules; the authenticated steps below remain
required after deployment.

## Skill update

Publish a new immutable version of the existing `daily-brief` skill. Preserve
the skill ID, owner, tags, assets and enabled state. Do not rewrite v12.

- Replace `daily_brief_v12` with the `daily_brief_v13` prompt template.
- Keep `steps` empty.
- Require `validate_tool_call` for the exact `write_deliverable` arguments.
- Require `action="rich:pulse"`, Pulse v2 and `pulse_variant="daily"`.
- Require every rendered article to have one accurate citation/link and one
  source-native image materialized through
  `tool_artifact:* -> artifact_read -> att_*/art_* -> media.ref`.
- Omit an article when its source image cannot be materialized.
- Forbid generic rich/text fallback, diesel content, visible availability
  bookkeeping and `image_edit`.
- Permit `image_generate` at most once and only for a non-article editorial
  visual.

## Schedule update

Update the existing Daily Brief schedule in place. Preserve its schedule ID,
cron expression, timezone, agent/profile, delivery configuration, concurrency
policy and linked skill ID.

- Update `task_template.description` to reference the v13 contract.
- Update `task_template.expected_output` to require an accepted Daily Brief
  Pulse with
  `article_count == article_media_count == article_citation_count`.
- Do not copy user-instance IDs or schedule values into product code.

Runtime activation is version-aware. A loaded skill snapshot carries immutable
skill version ID/number/content hash plus the contract version extracted from
the `daily_brief_vN` template. Sessions using v12 remain governed by v12 even
when v13 is current; task-text/title fallback selects v13 only when no concrete
loaded-skill contract overrides it. This metadata is stored in existing session
events/cache and deliverable JSON metadata, so no database migration is needed.

Record the previous skill version ID and schedule payload before mutation so
the DB-owned update can be rolled back independently of the controller binary.
