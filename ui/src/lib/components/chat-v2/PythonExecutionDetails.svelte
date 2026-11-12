<script lang="ts">
  import LiveDots from '$lib/components/LiveDots.svelte';
  import ToolCallBlock from '$lib/components/ToolCallBlock.svelte';
  import type { TimelineScope } from '$lib/chat-v2/types';
  import type { ToolCallTimelineItem } from '$lib/timeline-render-model';
  import { pythonExecutionOutput } from '$lib/chat-v2/python-execution';
  import { highlightToolOutput } from '$lib/syntax/tool-output';

  let {
    item,
    nestedCalls = [],
    getToolCall = () => null,
    onViewSession,
    scope
  } = $props<{
    item: ToolCallTimelineItem;
    nestedCalls?: ToolCallTimelineItem[];
    getToolCall?: (callId: string) => ToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    scope?: TimelineScope | undefined;
  }>();

  const description = $derived(
    typeof item.arguments?.description === 'string' ? item.arguments.description.trim() : ''
  );
  const code = $derived(typeof item.arguments?.code === 'string' ? item.arguments.code : '');
  const highlightedCode = $derived(highlightToolOutput(code, 'python'));
  const display = $derived(pythonExecutionOutput(item));
  const active = $derived(['started', 'running', 'waiting'].includes(item.status));
</script>

{#if description}
  <section class="rounded-xl border border-info-border/60 bg-info-subtle/30 px-3 py-2.5">
    <p class="mb-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-muted">What this run does</p>
    <p class="whitespace-pre-wrap text-sm leading-6 text-foreground">{description}</p>
  </section>
{/if}
{#if item.kernelGeneration != null}
  <div class="flex items-center gap-2 text-xs text-muted">
    <span class="h-1.5 w-1.5 rounded-full bg-info"></span>
    Kernel generation <span class="font-mono text-secondary">{item.kernelGeneration}</span>
  </div>
{/if}
{#if code}
  <section class="overflow-hidden rounded-xl border border-control-border bg-inset/60">
    <div class="flex items-center justify-between border-b border-separator bg-chrome/80 px-3 py-2">
      <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-muted">Python source</span>
      <span class="font-mono text-[10px] text-quiet">python</span>
    </div>
    <pre data-testid="python-execution-code" class="max-h-[44vh] overflow-auto p-3 font-mono text-xs leading-5 text-secondary"><code>{@html highlightedCode}</code></pre>
  </section>
{/if}
{#if nestedCalls.length > 0}
  <section class="space-y-2 rounded-xl border border-separator/80 bg-inset/25 p-3" aria-label="Nested tool activity">
    <div class="flex items-center justify-between gap-3">
      <h4 class="text-[10px] font-semibold uppercase tracking-[0.18em] text-muted">Nested tool activity</h4>
      <span class="text-xs tabular-nums text-quiet">{nestedCalls.length} {nestedCalls.length === 1 ? 'call' : 'calls'}</span>
    </div>
    <div class="space-y-2">
      {#each nestedCalls as nestedCall (nestedCall.id)}
        <ToolCallBlock
          item={nestedCall}
          getToolCall={getToolCall}
          onViewSession={onViewSession}
          {scope}
          density="compact"
        />
      {/each}
    </div>
  </section>
{/if}
{#if display.output}
  <section class="overflow-hidden rounded-xl border border-separator bg-inset/45">
    <div class="flex items-center justify-between gap-2 border-b border-separator px-3 py-2">
      <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-muted">{active ? 'Live output' : 'Output'}</span>
      {#if active}<LiveDots inline={true} size="sm" tone="sky" />{/if}
    </div>
    <pre data-testid="python-execution-output" class="max-h-[36vh] overflow-auto p-3 font-mono text-xs leading-5 text-secondary">{display.output}</pre>
  </section>
{/if}
{#if display.result != null || display.error}
  <section class={`overflow-hidden rounded-xl border ${item.isError ? 'border-danger-border' : 'border-separator'} bg-inset/45`}>
    <div class="flex items-center justify-between gap-2 border-b border-separator px-3 py-2">
      <span class={`text-[10px] font-semibold uppercase tracking-[0.18em] ${item.isError ? 'text-danger' : 'text-muted'}`}>{item.isError ? 'Execution error' : 'Result'}</span>
      {#if item.isError}<span aria-hidden="true" class="text-danger">!</span>{/if}
    </div>
    <pre data-testid="python-execution-result" class={`max-h-[36vh] overflow-auto p-3 font-mono text-xs leading-5 ${item.isError ? 'text-danger' : 'text-secondary'}`}>{display.error ?? display.result}</pre>
  </section>
{/if}
{#if display.envelope}
  <details class="text-xs text-muted">
    <summary class="cursor-pointer py-1">Raw execution data</summary>
    <pre class="mt-2 max-h-[36vh] overflow-auto rounded-lg border border-separator bg-inset p-3 font-mono text-xs">{item.result}</pre>
  </details>
{/if}
