<script lang="ts">
  import Clock from 'lucide-svelte/icons/clock';
  import { onMount } from 'svelte';
  import LiveDots from './LiveDots.svelte';
  import { isActiveToolStatus, type ToolCallTimelineItem } from '$lib/timeline-render-model';
  import {
    delegationIsActive, delegationIsQueued, isPythonKernelResetTool, toolCallDisplayTitle, toolCallSubtitle, toolHeaderDuration,
  } from '$lib/tool-call-header';
  import { shouldShowPatchPreparationProgress } from '$lib/tool-call-progress';

  let {
    item, expanded = false, controlsId, onclick, density = 'default',
    summaryMode = 'default', compactLabelMode, now, busy = false, focusOnMount = false,
  } = $props<{
    item: ToolCallTimelineItem;
    expanded?: boolean;
    controlsId: string;
    onclick: (event: MouseEvent) => void;
    density?: 'default' | 'compact';
    summaryMode?: 'default' | 'command';
    compactLabelMode?: 'command' | 'description';
    now?: number;
    busy?: boolean;
    focusOnMount?: boolean;
  }>();
  let button: HTMLButtonElement;
  let localNow = $state(Date.now());
  const subtitle = $derived(toolCallSubtitle(item));
  const duration = $derived(toolHeaderDuration(item, now ?? localNow));
  const command = $derived(typeof item.arguments?.command === 'string' ? item.arguments.command : item.toolName);
  const description = $derived(typeof item.arguments?.description === 'string' ? item.arguments.description.trim() : '');
  const compactLabel = $derived(compactLabelMode === 'description' ? description || command : command);
  const statusColor = $derived(item.status === 'completed' ? 'text-success'
    : item.status === 'failed' ? 'text-danger'
    : item.status === 'denied' || item.status === 'waiting' ? 'text-warning' : 'text-info');
  const statusIcon = $derived(item.status === 'completed' ? '\u2713' : item.status === 'failed' ? '\u2717'
    : item.status === 'denied' ? '\u2298' : '');
  const isCompletedKernelReset = $derived(isPythonKernelResetTool(item)
    && (item.status === 'complete' || item.status === 'completed'));
  $effect(() => {
    if (now !== undefined || delegationIsQueued(item) || !(isActiveToolStatus(item.status) || delegationIsActive(item))) return;
    localNow = Date.now();
    const timer = window.setInterval(() => localNow = Date.now(), 1000);
    return () => window.clearInterval(timer);
  });
  onMount(() => {
    if (focusOnMount && document.activeElement === document.body) button.focus({ preventScroll: true });
  });
</script>

<button bind:this={button}
  class={`flex min-w-0 w-full text-left transition hover:bg-surface-hover ${density === 'compact' ? 'items-center gap-2 px-3 py-2' : 'items-start gap-3 px-4 py-3 sm:items-center'}`}
  {onclick} type="button" aria-expanded={expanded} aria-controls={controlsId} aria-busy={busy}>
  <span class={`legacy-quiet shrink-0 text-quiet ${density === 'compact' ? 'text-[10px]' : 'text-xs'}`}>{expanded ? '\u25BC' : '\u25B6'}</span>
  {#if summaryMode === 'command' || compactLabelMode}
    <span class="command-scroll min-w-0 flex-1 overflow-x-auto overscroll-x-contain whitespace-nowrap font-mono text-xs text-foreground [-webkit-overflow-scrolling:touch]"
      data-testid="tool-command-summary-scroll" title={compactLabel}>
      <span class="inline-block min-w-max font-mono"><span class="text-syntax-string">$</span>
        {compactLabelMode ? compactLabel : command.replace(/\s*\n\s*/g, ' ↵ ').trim()}
      </span>
    </span>
  {:else}
    <span class="min-w-0 flex flex-1 flex-col gap-0.5 sm:flex-row sm:items-center sm:gap-3">
      <span class="min-w-0 font-semibold text-cyan-tone [overflow-wrap:anywhere]" title={item.toolName}>{toolCallDisplayTitle(item)}</span>
      {#if subtitle}<span class="min-w-0 text-xs text-muted sm:flex-1 sm:truncate">{subtitle}</span>{/if}
    </span>
  {/if}
  <span class={`flex shrink-0 items-center gap-1.5 self-start text-xs font-medium ${statusColor} sm:self-auto`}>
    {#if item.status === 'waiting'}
      <Clock class="h-3.5 w-3.5" aria-hidden="true" /><span>Waiting for approval</span>
    {:else if delegationIsQueued(item)}
      <Clock class="h-3.5 w-3.5" aria-hidden="true" /><span>Queued</span>
    {:else if isActiveToolStatus(item.status)}
      <LiveDots inline={true} size="sm" tone="sky" />
      <span class="sr-only">{shouldShowPatchPreparationProgress(item) ? 'Preparing' : 'Running'}</span>
    {:else if isCompletedKernelReset}
      <span aria-hidden="true">&#x21bb;</span>
      <span>Kernel reset</span>
    {:else}
      <span>{statusIcon}</span>
      <span class:sr-only={density === 'compact'}>{item.status === 'denied' ? 'Denied' : item.status}</span>
    {/if}
    {#if duration}<span class="legacy-quiet text-quiet">{duration}</span>{/if}
  </span>
</button>

<style>
  .command-scroll { scrollbar-width: none; }
  .command-scroll::-webkit-scrollbar { display: none; width: 0; height: 0; }
</style>
