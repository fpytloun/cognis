<script lang="ts">
  import { onMount } from 'svelte';

  import { api } from '$lib/api/client';
  import type { Deliverable, StepRun } from '$lib/types/api';
  import { renderMarkdown, sanitizeHtml } from '$lib/markdown';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import Button from '$lib/components/ui/Button.svelte';

  let {
    stepRun,
    agentName,
    agentAvatarUrl = null,
    visibleStatus,
    onclose,
  } = $props<{
    stepRun: StepRun;
    agentName: string;
    agentAvatarUrl?: string | null;
    visibleStatus: string;
    onclose: () => void;
  }>();

  let container: HTMLDivElement | null = null;
  let previousFocus: HTMLElement | null = null;
  let overlayId = $state<string | null>(null);
  let hydratedDeliverable = $state<Deliverable | null>(null);
  let deliverableLoadError = $state('');
  let loadingDeliverable = $state(false);

  function stepOutputSummary(stepRun: StepRun): string {
    const val = stepRun.output?.summary;
    return typeof val === 'string' ? val : '';
  }

  function stepOutputContent(stepRun: StepRun): string {
    const val = stepRun.output?.content;
    return typeof val === 'string' ? val : '';
  }

  function latestDeliverable(stepRun: StepRun) {
    return stepRun.deliverables[0] ?? null;
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

  function focusableElements(): HTMLElement[] {
    if (!container) return [];
    return Array.from(container.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')).filter((element) => !element.hasAttribute('disabled'));
  }

  function trapFocus(event: KeyboardEvent): void {
    if (!isTopOverlay(overlayId)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      onclose();
      return;
    }
    if (event.key !== 'Tab') return;
    const elements = focusableElements();
    if (elements.length === 0) return;
    const first = elements[0];
    const last = elements[elements.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  const summary = $derived(stepOutputSummary(stepRun));
  const latestDeliverableVersion = $derived(hydratedDeliverable ?? latestDeliverable(stepRun));
  const deliverableHtml = $derived(renderDeliverableContent(latestDeliverableVersion));
  const reasoningContent = $derived(stepReasoningContent(stepRun, latestDeliverableVersion));
  const claims = $derived(stepOutputClaims(stepRun));
  const stepError = $derived(stepOutputError(stepRun));
  const outcomeStatus = $derived(stepOutcomeStatus(stepRun));
  const outcomeReason = $derived(stepOutcomeReason(stepRun));
  const feedback = $derived(stepEvalFeedback(stepRun));

  onMount(() => {
    const handle = registerOverlay({ kind: 'blocking', blocksChrome: true });
    overlayId = handle.id;
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    queueMicrotask(() => {
      focusableElements()[0]?.focus();
    });
    document.addEventListener('keydown', trapFocus);
    const projectedDeliverable = latestDeliverable(stepRun);
    if (projectedDeliverable?.deliverable_id) {
      loadingDeliverable = true;
      void api.deliverables
        .getForStepRun(stepRun.step_run_id, projectedDeliverable.deliverable_id)
        .then((deliverable: Deliverable) => {
          hydratedDeliverable = deliverable;
          deliverableLoadError = '';
        })
        .catch(() => {
          deliverableLoadError = 'Full deliverable content could not be loaded.';
        })
        .finally(() => {
          loadingDeliverable = false;
        });
    }
    return () => {
      handle.unregister();
      overlayId = null;
      document.removeEventListener('keydown', trapFocus);
      queueMicrotask(() => previousFocus?.focus());
    };
  });
</script>

<!--
  Full step output modal. Bounded by the shared app-shell offsets so
  it stays above the mobile header and bottom tab bar while still
  scrolling internally.
-->
<div
  class="app-viewport-overlay app-safe-fullscreen z-[95] items-stretch justify-center overflow-y-auto overscroll-contain bg-slate-950/85 backdrop-blur sm:items-center"
  role="presentation"
>
  <button class="absolute inset-0" onclick={onclose} type="button" aria-label="Close full output"></button>
  <div
    bind:this={container}
    class="relative z-10 flex max-h-full w-full max-w-3xl flex-col overflow-hidden rounded-[1.75rem] border border-slate-800 bg-slate-950 shadow-card"
    role="dialog"
    aria-modal="true"
    data-blocking-overlay
    aria-labelledby="step-output-title"
  >
    <div class="shrink-0 border-b border-slate-800 px-5 py-4 sm:px-6">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Full output</p>
          <h2 class="mt-2 text-xl font-semibold text-white" id="step-output-title">{stepRun.step_name}</h2>
          <div class="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
            <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 uppercase tracking-wide">Attempt #{stepRun.attempt}</span>
            <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 uppercase tracking-wide">{visibleStatus}</span>
            {#if latestDeliverableVersion}
              <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-2.5 py-1 uppercase tracking-wide text-sky-200">Deliverable v{latestDeliverableVersion.version}</span>
            {/if}
            <span class="inline-flex items-center gap-2 text-slate-300">
              <AgentAvatar name={agentName} avatarUrl={agentAvatarUrl} class="h-5 w-5 rounded-lg" />
              {agentName}
            </span>
          </div>
        </div>
        <Button variant="secondary" size="sm" onclick={onclose}>Close</Button>
      </div>
    </div>

    <div class="min-h-0 flex-1 space-y-5 overflow-y-auto overflow-x-hidden overscroll-contain px-5 py-5 sm:px-6">
      {#if summary}
        <section>
          <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Summary</p>
          <div class="prose prose-sm prose-invert mt-3 max-w-none text-slate-300">{@html renderMarkdown(summary)}</div>
        </section>
      {/if}

      {#if stepRun.deliverables.length > 0}
        <section class="rounded-3xl border border-sky-500/20 bg-sky-500/5 p-4 sm:p-5">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Deliverable</p>
              {#if latestDeliverableVersion?.title}
                <p class="mt-2 text-sm font-medium text-white">{latestDeliverableVersion.title}</p>
              {/if}
            </div>
            <div class="flex flex-wrap gap-2 text-[11px] uppercase tracking-wide text-slate-300">
              {#each stepRun.deliverables as deliverable}
                <span class={`rounded-full border px-2.5 py-1 ${deliverable.status === 'delivered' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200' : deliverable.status === 'approved' ? 'border-sky-500/30 bg-sky-500/10 text-sky-200' : deliverable.status === 'rejected' ? 'border-sky-500/30 bg-sky-500/10 text-sky-200' : 'border-slate-700 bg-slate-900/80 text-slate-300'}`}>
                  v{deliverable.version} {deliverable.status}
                </span>
              {/each}
            </div>
          </div>
          {#if loadingDeliverable}
            <p class="mt-4 text-sm text-slate-400">Loading full deliverable…</p>
          {:else if deliverableLoadError}
            <p class="mt-4 text-sm text-amber-200">{deliverableLoadError}</p>
          {/if}
          {#if latestDeliverableVersion?.format === 'rich'}
            <div class="mt-4">
              <RichDeliverable
                payload={latestDeliverableVersion.rich_payload}
                content={latestDeliverableVersion.content}
                 title={latestDeliverableVersion.title ?? 'Deliverable'}
                 instanceId={latestDeliverableVersion.deliverable_id}
                 surface="standalone"
              />
            </div>
          {:else if deliverableHtml}
            <div class="prose prose-sm prose-invert mt-4 max-w-none text-slate-300">{@html deliverableHtml}</div>
          {/if}
          {#if latestDeliverableVersion?.evaluator_feedback}
            <div class="mt-4 rounded-2xl border border-sky-500/20 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
              <p class="font-medium uppercase tracking-wide text-[11px] text-sky-300">Latest evaluator feedback</p>
              <p class="mt-1">{latestDeliverableVersion.evaluator_feedback}</p>
            </div>
          {/if}
        </section>
      {/if}

      <section class="rounded-3xl border border-slate-800 bg-slate-950/70 p-4 sm:p-5">
        <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Runtime</p>
        {#if runtimeRows(stepRun).length > 0}
          <dl class="mt-3 grid gap-2 text-xs sm:grid-cols-2">
            {#each runtimeRows(stepRun) as row}
              <div class="min-w-0 rounded-lg border border-slate-800/70 bg-slate-900/40 px-2.5 py-2">
                <dt class="text-slate-500">{row.label}</dt>
                <dd class="mt-1 truncate font-mono text-slate-300" title={row.value}>{row.value}</dd>
              </div>
            {/each}
          </dl>
        {:else}
          <p class="mt-3 text-sm text-amber-200">Runtime not recorded for this attempt.</p>
        {/if}
      </section>

      <section class="rounded-3xl border border-slate-800 bg-slate-900/60 p-4 sm:p-5">
        <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Completion metadata</p>

        {#if claims.length > 0}
          <div class="mt-4">
            <p class="text-sm font-medium text-slate-200">Claims</p>
            <ul class="mt-2 space-y-1 text-sm text-slate-400">
              {#each claims as claim}
                <li class="flex items-start gap-2">
                  <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-600"></span>
                  <span>{claim}</span>
                </li>
              {/each}
            </ul>
          </div>
        {/if}

        {#if outcomeStatus !== 'success'}
          <div class="mt-4 rounded-2xl border border-sky-500/20 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            <p class="font-medium uppercase tracking-wide text-[11px] text-sky-300">Outcome marker</p>
            <p class="mt-1">This attempt completed but reported <span class="font-semibold uppercase">{outcomeStatus}</span>{#if outcomeReason}: {outcomeReason}{/if}</p>
          </div>
        {/if}

        {#if stepError}
          <div class="mt-4 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
            <p class="font-medium">Error</p>
            <pre class="mt-2 whitespace-pre-wrap text-xs text-rose-300">{stepError}</pre>
          </div>
        {/if}

        {#if stepRun.evaluation}
          {@const evalDecision = String(stepRun.evaluation.decision ?? '')}
          {@const evalReasoning = String(stepRun.evaluation.reasoning ?? '')}
          {@const evalColor = evalDecision === 'approved' || evalDecision === 'approve' ? 'text-emerald-400' : evalDecision === 'revise' ? 'text-sky-400' : evalDecision === 'failed' || evalDecision === 'reject' ? 'text-rose-400' : 'text-sky-400'}
          <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/80 px-4 py-3">
            <p class="text-xs font-medium uppercase tracking-widest text-slate-500">Evaluation</p>
            <p class="mt-1 text-sm text-slate-300">
              <span class="font-medium {evalColor}">{evalDecision}</span>
              {#if evalReasoning} - {evalReasoning}{/if}
            </p>
            {#if feedback}
              <p class="mt-2 rounded-lg border border-slate-700/50 bg-slate-900/50 px-2 py-1.5 text-xs text-slate-400"><span class="font-medium text-slate-500">Feedback:</span> {feedback}</p>
            {/if}
          </div>
        {/if}

        {#if claims.length === 0 && outcomeStatus === 'success' && !stepError && !stepRun.evaluation}
          <p class="mt-3 text-sm text-slate-400">No additional completion metadata was recorded for this attempt.</p>
        {/if}
      </section>

      <div class="border-t border-dashed border-slate-800"></div>

      <section>
        <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Reasoning / progress</p>
        {#if reasoningContent}
          <div class="prose prose-sm prose-invert mt-3 max-w-none text-slate-300">{@html renderMarkdown(reasoningContent)}</div>
        {:else}
          <div class="mt-3 rounded-2xl border border-slate-800 bg-slate-900/50 px-4 py-3 text-sm text-slate-400">
            No separate reasoning text was recorded for this attempt.
          </div>
        {/if}
      </section>
    </div>
  </div>
</div>
