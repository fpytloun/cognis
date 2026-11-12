<script lang="ts">
  import { onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import type { Deliverable, DeliverableRevisionSummary, StepRun, TaskDeliverableFreshness } from '$lib/types/api';
  import { renderMarkdown, sanitizeHtml } from '$lib/markdown';
  import { deliverableRelationNote } from '$lib/step-output';
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import DeliverableRevisionBar from '$lib/components/DeliverableRevisionBar.svelte';
  import DocumentCommentCount from '$lib/components/DocumentCommentCount.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import { loadDeliverableRevision } from '$lib/deliverable-revisions';

  // Result tab of the step viewer: what the step produced, how its deliverable
  // relates to the step state, and the attempt's completion metadata.
  let {
    stepRun,
    deliverableFreshness,
    visibleStatus,
  } = $props<{
    stepRun: StepRun;
    deliverableFreshness?: Record<string, TaskDeliverableFreshness>;
    visibleStatus: string;
  }>();

  let hydratedDeliverable = $state<Deliverable | null>(null);
  let deliverableLoadError = $state('');
  let loadingDeliverable = $state(false);
  let stepDeliverableId = $state<string | null>(null);

  let revisionGeneration = 0;

  async function openRevision(revision: DeliverableRevisionSummary): Promise<void> {
    // Only the most recent selection may update the view.
    const generation = ++revisionGeneration;
    loadingDeliverable = true;
    deliverableLoadError = '';
    try {
      const result = await loadDeliverableRevision(revision);
      if (generation === revisionGeneration) hydratedDeliverable = result;
    } catch {
      if (generation === revisionGeneration) deliverableLoadError = 'This revision could not be loaded.';
    } finally {
      if (generation === revisionGeneration) loadingDeliverable = false;
    }
  }

  function stepOutputSummary(stepRun: StepRun): string {
    const val = stepRun.output?.summary;
    return typeof val === 'string' ? val : '';
  }

  function stepOutputContent(stepRun: StepRun): string {
    const val = stepRun.output?.content;
    return typeof val === 'string' ? val : '';
  }

  function stepDeliverables(stepRun: StepRun): Deliverable[] {
    return Array.isArray(stepRun.deliverables) ? stepRun.deliverables : [];
  }

  function latestDeliverable(stepRun: StepRun): Deliverable | null {
    return stepDeliverables(stepRun)[0] ?? null;
  }

  function hasRenderableRichPayload(deliverable: Deliverable | null): boolean {
    const payload = deliverable?.rich_payload;
    return Boolean(
      deliverable?.format === 'rich'
      && payload
      && typeof payload === 'object'
      && !Array.isArray(payload)
      && Array.isArray(payload.blocks)
      && payload.blocks.length > 0
    );
  }

  function renderDeliverableContent(deliverable: Deliverable | null): string {
    if (!deliverable?.content) return '';
    return deliverable.format === 'html'
      ? sanitizeHtml(deliverable.content)
      : renderMarkdown(deliverable.content);
  }

  function stepReasoningContent(stepRun: StepRun, deliverable: Deliverable | null): string {
    const content = stepOutputContent(stepRun);
    if (!content) return '';
    if (deliverable && content.trim() === deliverable.content.trim()) return '';
    return content;
  }

  function stepOutputClaims(stepRun: StepRun): string[] {
    const val = stepRun.output?.claims;
    return Array.isArray(val) ? val.map((item) => String(item)).filter(Boolean) : [];
  }

  function stepOutputError(stepRun: StepRun): string {
    const val = stepRun.output?.error;
    return typeof val === 'string' ? val : '';
  }

  function stepOutcomeStatus(stepRun: StepRun): string {
    const raw = stepRun.output?.outcome;
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
      const val = (raw as Record<string, unknown>).status;
      if (typeof val === 'string' && val) return val;
    }
    return 'success';
  }

  function stepOutcomeReason(stepRun: StepRun): string {
    const raw = stepRun.output?.outcome;
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
      const val = (raw as Record<string, unknown>).reason;
      if (typeof val === 'string' && val) return val;
    }
    return '';
  }

  function stepEvalFeedback(stepRun: StepRun): string {
    const val = stepRun.evaluation?.feedback;
    return typeof val === 'string' ? val : '';
  }

  function runtimeString(value: unknown): string {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    return '';
  }

  function runtimeEnvironment(stepRun: StepRun): Record<string, unknown> {
    const env = stepRun.runtime_info?.environment;
    return env && typeof env === 'object' ? (env as Record<string, unknown>) : {};
  }

  function runtimeRows(stepRun: StepRun): Array<{ label: string; value: string }> {
    const info = stepRun.runtime_info;
    if (!info) return [];
    const env = runtimeEnvironment(stepRun);
    return [
      { label: 'Executor', value: runtimeString(info.executor_id) || 'unresolved' },
      { label: 'Type', value: runtimeString(info.executor_type) || 'unknown' },
      { label: 'Runtime', value: runtimeString(info.runtime_source) || 'unknown' },
      { label: 'Selection', value: runtimeString(info.selection_source) || 'unknown' },
      { label: 'Fallback', value: runtimeString(info.fallback_used) },
      { label: 'User', value: runtimeString(env.user) || 'unknown' },
      { label: 'Home', value: runtimeString(env.home) || 'unknown' },
      { label: 'CWD', value: runtimeString(env.cwd) || 'unknown' },
      { label: 'Tools', value: runtimeString(info.visible_tool_count) || runtimeString(info.inventory_tool_count) || 'unknown' }
    ].filter((row) => row.value !== '');
  }

  const summary = $derived(stepOutputSummary(stepRun));
  const deliverables = $derived(stepDeliverables(stepRun));
  const latestDeliverableVersion = $derived(hydratedDeliverable ?? latestDeliverable(stepRun));
  const taskFreshness = $derived(latestDeliverableVersion
    ? deliverableFreshness?.[latestDeliverableVersion.deliverable_id] ?? stepRun.deliverable_freshness?.[latestDeliverableVersion.deliverable_id] ?? latestDeliverableVersion.task_freshness ?? null
    : null);
  const deliverableHtml = $derived(renderDeliverableContent(latestDeliverableVersion));
  const reasoningContent = $derived(stepReasoningContent(stepRun, latestDeliverableVersion));
  const claims = $derived(stepOutputClaims(stepRun));
  const stepError = $derived(stepOutputError(stepRun));
  const outcomeStatus = $derived(stepOutcomeStatus(stepRun));
  const outcomeReason = $derived(stepOutcomeReason(stepRun));
  const feedback = $derived(stepEvalFeedback(stepRun));
  const relationNote = $derived(latestDeliverableVersion ? deliverableRelationNote(visibleStatus, taskFreshness) : null);

  onMount(() => {
    let mounted = true;
    const projectedDeliverable = latestDeliverable(stepRun);
    if (projectedDeliverable?.deliverable_id) {
      loadingDeliverable = true;
      void api.deliverables
        .getForStepRun(stepRun.step_run_id, projectedDeliverable.deliverable_id)
        .then((deliverable: Deliverable) => {
          if (!mounted) return;
          if (deliverable.format === 'rich' && !hasRenderableRichPayload(deliverable)) {
            throw new Error('The full rich deliverable payload is unavailable or corrupt.');
          }
          hydratedDeliverable = deliverable;
          stepDeliverableId = deliverable.deliverable_id;
          deliverableLoadError = '';
        })
        .catch(() => {
          if (!mounted) return;
          deliverableLoadError = 'Full deliverable content could not be loaded.';
        })
        .finally(() => {
          if (!mounted) return;
          loadingDeliverable = false;
        });
    }
    return () => {
      mounted = false;
    };
  });
</script>

<div class="space-y-5" data-testid="step-result-view">
      {#if summary}
        <section>
          <p class="text-xs uppercase tracking-[0.25em] text-quiet">Summary</p>
          <div class="prose prose-sm prose-invert mt-3 max-w-none text-secondary">{@html renderMarkdown(summary)}</div>
        </section>
      {/if}

      {#if stepRun.runtime_info?.deterministic_step || stepRun.output?.metadata?.deterministic_step}
        <section class="rounded-3xl border border-separator bg-canvas/70 p-4 sm:p-5">
          <h3 class="text-sm font-medium text-foreground">Deterministic execution</h3>
          {#each [
            ['Tool', stepRun.runtime_info?.tool_name],
            ['Arguments', stepRun.runtime_info?.render],
            ['Condition expression', stepRun.runtime_info?.condition_expression],
            ['Condition evaluation', stepRun.runtime_info?.condition],
            ['Selected branch', stepRun.runtime_info?.selected_branch],
            ['Selected target', stepRun.runtime_info?.selected_target],
            ['Outputs', stepRun.output?.outputs],
          ] as [label, value]}
            {#if value !== undefined && value !== null}
              <h4 class="mt-4 text-xs text-muted">{label}</h4>
              <pre class="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words text-xs text-foreground">{typeof value === 'string' ? value : JSON.stringify(value, null, 2)}</pre>
            {/if}
          {/each}
        </section>
      {/if}

      {#if deliverables.length > 0}
        <section class="rounded-3xl border border-accent/20 bg-accent-subtle p-4 sm:p-5">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-quiet">Deliverable</p>
              {#if latestDeliverableVersion?.title}
                <p class="mt-2 text-sm font-medium text-strong">{latestDeliverableVersion.title}</p>
              {/if}
            </div>
            <div class="flex flex-wrap gap-2 text-xs text-secondary">
              {#each deliverables as deliverable}
                <span class={`rounded-full border px-2.5 py-1 ${deliverable.status === 'delivered' ? 'border-success-border bg-success-subtle text-success' : deliverable.status === 'approved' ? 'border-accent/30 bg-accent-subtle text-accent' : deliverable.status === 'rejected' ? 'border-accent/30 bg-accent-subtle text-accent' : 'border-control-border bg-surface/80 text-secondary'}`}>
                  Revision {deliverable.version} · {deliverable.status}
                  <DocumentCommentCount id={deliverable.deliverable_id} />
                </span>
              {/each}
            </div>
          </div>
          {#if relationNote}
            <p class="mt-3 text-sm text-secondary" data-testid="step-deliverable-relation">{relationNote}</p>
          {/if}
          {#if hydratedDeliverable}
            <div class="mt-3">
              <DeliverableRevisionBar current={hydratedDeliverable} anchorId={stepDeliverableId ?? hydratedDeliverable.deliverable_id} onSelect={(revision) => void openRevision(revision)} />
            </div>
          {/if}
          {#if loadingDeliverable}
            <p class="mt-4 text-sm text-muted">Loading full deliverable…</p>
          {:else if deliverableLoadError}
            <p class="mt-4 text-sm text-warning">{deliverableLoadError}</p>
          {/if}
          {#key `${latestDeliverableVersion?.deliverable_id}:${taskFreshness?.state ?? 'current'}`}
          <details open={!taskFreshness || taskFreshness.state === 'current'}>
          <summary class="mt-3 cursor-pointer text-xs text-muted">{taskFreshness?.label ?? 'Deliverable content'}</summary>
          {#if hasRenderableRichPayload(hydratedDeliverable)}
            <div class="mt-4">
              <RichDeliverable
                payload={hydratedDeliverable?.rich_payload}
                content={hydratedDeliverable?.content}
                title={hydratedDeliverable?.title ?? 'Deliverable'}
                instanceId={hydratedDeliverable?.deliverable_id}
                surface="embedded"
              />
            </div>
          {:else if deliverableHtml}
            <div class="prose prose-sm prose-invert mt-4 max-w-none text-secondary">{@html deliverableHtml}</div>
          {/if}
          </details>
          {/key}
          {#if latestDeliverableVersion?.evaluator_feedback}
            <div class="mt-4 rounded-2xl border border-accent/20 bg-accent-subtle px-4 py-3 text-sm text-accent">
              <p class="text-xs font-medium text-accent">Latest evaluator feedback</p>
              <p class="mt-1">{latestDeliverableVersion.evaluator_feedback}</p>
            </div>
          {/if}
        </section>
      {/if}

      <section class="rounded-3xl border border-separator bg-canvas/70 p-4 sm:p-5">
        <p class="text-xs uppercase tracking-[0.25em] text-quiet">Runtime</p>
        {#if runtimeRows(stepRun).length > 0}
          <dl class="mt-3 grid gap-2 text-xs sm:grid-cols-2">
            {#each runtimeRows(stepRun) as row}
              <div class="min-w-0 rounded-lg border border-separator/70 bg-surface/40 px-2.5 py-2">
                <dt class="text-quiet">{row.label}</dt>
                <dd class="mt-1 truncate font-mono text-secondary" title={row.value}>{row.value}</dd>
              </div>
            {/each}
          </dl>
        {:else}
          <p class="mt-3 text-sm text-warning">Runtime not recorded for this attempt.</p>
        {/if}
      </section>

      <Card class="border-separator bg-surface/60 p-4 sm:p-5">
        <p class="text-xs uppercase tracking-[0.25em] text-quiet">Completion metadata</p>

        {#if claims.length > 0}
          <div class="mt-4">
            <p class="text-sm font-medium text-foreground">Claims</p>
            <ul class="mt-2 space-y-1 text-sm text-muted">
              {#each claims as claim}
                <li class="flex items-start gap-2">
                  <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-muted"></span>
                  <span>{claim}</span>
                </li>
              {/each}
            </ul>
          </div>
        {/if}

        {#if outcomeStatus !== 'success'}
          <div class="mt-4 rounded-2xl border border-accent/20 bg-accent-subtle px-4 py-3 text-sm text-accent">
            <p class="text-xs font-medium text-accent">Outcome marker</p>
            <p class="mt-1">This attempt completed but reported <span class="font-semibold uppercase">{outcomeStatus}</span>{#if outcomeReason}: {outcomeReason}{/if}</p>
          </div>
        {/if}

        {#if stepError}
          <div class="mt-4 rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">
            <p class="font-medium">Error</p>
            <pre class="mt-2 whitespace-pre-wrap text-xs text-danger">{stepError}</pre>
          </div>
        {/if}

        {#if stepRun.evaluation}
          {@const evalDecision = String(stepRun.evaluation.decision ?? '')}
          {@const evalReasoning = String(stepRun.evaluation.reasoning ?? '')}
          {@const evalColor = evalDecision === 'approved' || evalDecision === 'approve' ? 'text-success' : evalDecision === 'revise' ? 'text-accent' : evalDecision === 'failed' || evalDecision === 'reject' ? 'text-danger' : 'text-accent'}
          <div class="mt-4 rounded-2xl border border-separator bg-canvas/80 px-4 py-3">
            <p class="text-xs font-medium uppercase tracking-widest text-quiet">Evaluation</p>
            <p class="mt-1 text-sm text-secondary">
              <span class="font-medium {evalColor}">{evalDecision}</span>
              {#if evalReasoning} - {evalReasoning}{/if}
            </p>
            {#if feedback}
              <p class="mt-2 rounded-lg border border-control-border/50 bg-surface/50 px-2 py-1.5 text-xs text-muted"><span class="font-medium text-quiet">Feedback:</span> {feedback}</p>
            {/if}
          </div>
        {/if}

        {#if claims.length === 0 && outcomeStatus === 'success' && !stepError && !stepRun.evaluation}
          <p class="mt-3 text-sm text-muted">No additional completion metadata was recorded for this attempt.</p>
        {/if}
      </Card>

      <div class="border-t border-dashed border-separator"></div>

      <section>
        <p class="text-xs uppercase tracking-[0.25em] text-quiet">Reasoning / progress</p>
        {#if reasoningContent}
          <div class="prose prose-sm prose-invert mt-3 max-w-none text-secondary">{@html renderMarkdown(reasoningContent)}</div>
        {:else}
          <p class="mt-3 text-sm text-muted">No separate reasoning text was recorded for this attempt.</p>
        {/if}
      </section>
</div>
