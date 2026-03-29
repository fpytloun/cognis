<script lang="ts">
  import type { ToolCallTimelineItem } from '$lib/chat';

  let { item } = $props<{ item: ToolCallTimelineItem }>();

  let expanded = $state(false);

  function toggle(): void {
    expanded = !expanded;
  }

  function subtitle(): string {
    if (!item.arguments) {
      return '';
    }
    const args = item.arguments;
    const name = item.toolName.toLowerCase();

    if (name.includes('read') || name.includes('write') || name.includes('edit')) {
      if (typeof args.filePath === 'string') return args.filePath;
      if (typeof args.path === 'string') return args.path;
    }
    if (name.includes('bash')) {
      if (typeof args.command === 'string') return args.command.length > 80 ? `${args.command.slice(0, 80)}...` : args.command;
    }
    if (name.includes('grep')) {
      if (typeof args.pattern === 'string') return args.pattern;
    }
    if (name.includes('glob')) {
      if (typeof args.pattern === 'string') return args.pattern;
    }
    if (name.includes('webfetch') || name.includes('navigate')) {
      if (typeof args.url === 'string') return args.url.length > 80 ? `${args.url.slice(0, 80)}...` : args.url;
    }

    // Fallback: show first string arg
    for (const value of Object.values(args)) {
      if (typeof value === 'string' && value.length > 0) {
        return value.length > 80 ? `${value.slice(0, 80)}...` : value;
      }
    }
    return '';
  }

  function statusIcon(): string {
    if (item.status === 'completed') return '\u2713';
    if (item.status === 'failed') return '\u2717';
    return '';
  }

  function statusColor(): string {
    if (item.status === 'completed') return 'text-emerald-400';
    if (item.status === 'failed') return 'text-rose-400';
    return 'text-amber-400';
  }

  function durationText(): string {
    if (item.durationMs == null) return '';
    if (item.durationMs < 1000) return `${item.durationMs}ms`;
    return `${(item.durationMs / 1000).toFixed(1)}s`;
  }

  function formatArguments(): string {
    if (!item.arguments) return '';
    try {
      return JSON.stringify(item.arguments, null, 2);
    } catch {
      return String(item.arguments);
    }
  }

  const MAX_OUTPUT_LINES = 50;

  function truncatedResult(): { text: string; hiddenCount: number } {
    if (!item.result) return { text: '', hiddenCount: 0 };
    const lines = item.result.split('\n');
    if (lines.length <= MAX_OUTPUT_LINES) {
      return { text: item.result, hiddenCount: 0 };
    }
    return {
      text: lines.slice(0, MAX_OUTPUT_LINES).join('\n'),
      hiddenCount: lines.length - MAX_OUTPUT_LINES
    };
  }

  function borderColor(): string {
    if (item.isError) return 'border-rose-500/40';
    return 'border-slate-800';
  }
</script>

<article class={`rounded-2xl border bg-slate-900/80 text-sm shadow-card ${borderColor()}`}>
  <!-- Header row (always visible, clickable) -->
  <button
    class="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-slate-800/40"
    onclick={toggle}
    type="button"
  >
    <span class="text-xs text-slate-500">{expanded ? '\u25BC' : '\u25B6'}</span>
    <span class="font-semibold text-cyan-300">{item.toolName}</span>
    {#if subtitle()}
      <span class="min-w-0 flex-1 truncate text-xs text-slate-400">{subtitle()}</span>
    {:else}
      <span class="flex-1"></span>
    {/if}
    <span class={`flex items-center gap-1.5 text-xs font-medium ${statusColor()}`}>
      {#if item.status === 'started'}
        <span class="inline-block h-3 w-3 animate-spin rounded-full border border-amber-400 border-t-transparent"></span>
        <span>running</span>
      {:else}
        <span>{statusIcon()}</span>
        <span>{item.status}</span>
      {/if}
      {#if durationText()}
        <span class="text-slate-500">{durationText()}</span>
      {/if}
    </span>
  </button>

  <!-- Expanded content -->
  {#if expanded}
    <div class="space-y-3 border-t border-slate-800/60 px-4 py-3">
      {#if item.arguments && Object.keys(item.arguments).length > 0}
        <div>
          <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Input</p>
          <pre class="max-h-[40vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-xs leading-5 text-slate-300">{formatArguments()}</pre>
        </div>
      {/if}

      {#if item.result != null}
        {@const output = truncatedResult()}
        <div>
          <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Output</p>
          <pre class={`max-h-[40vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 text-xs leading-5 ${item.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{output.text}{#if output.hiddenCount > 0}
{`\n... ${output.hiddenCount} more lines`}{/if}</pre>
        </div>
      {/if}
    </div>
  {/if}
</article>
