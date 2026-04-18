<script lang="ts">
  import type { ToolCallTimelineItem } from '$lib/chat';
  import LiveDots from '$lib/components/LiveDots.svelte';

  let { item } = $props<{ item: ToolCallTimelineItem }>();

  let expanded = $state(false);
  let inputExpanded = $state(false);
  let outputExpanded = $state(false);
  let evalExpanded = $state(false);
  let autoExpanded = $state(false);

  const LINES_PER_PAGE = 50;
  const startsExpanded = $derived(item.toolName.toLowerCase().replace(/_/g, '') === 'steprequestinput');

  $effect(() => {
    if (startsExpanded && !autoExpanded) {
      expanded = true;
      autoExpanded = true;
    }
  });

  function toggle(): void {
    expanded = !expanded;
  }

  function truncate(s: string, max = 80): string {
    return s.length > max ? `${s.slice(0, max)}...` : s;
  }

  function normalizedToolName(): string {
    return item.toolName.toLowerCase().replace(/_/g, '');
  }

  function isStepRequestInput(): boolean {
    return normalizedToolName() === 'steprequestinput';
  }

  function subtitle(): string {
    if (!item.arguments) {
      return '';
    }
    const args = item.arguments;
    // Normalize: strip underscores for matching (web_fetch -> webfetch)
    const name = normalizedToolName();

    // File operations
    if (name.includes('read') || name.includes('write') || name.includes('edit') || name.includes('patch') || name.includes('multiedit') || name === 'listdirectory') {
      if (typeof args.filePath === 'string') return args.filePath;
      if (typeof args.path === 'string') return args.path;
    }
    // Shell
    if (name.includes('bash') || name.includes('shell')) {
      if (typeof args.command === 'string') return truncate(args.command);
    }
    // Search
    if (name.includes('grep') || name.includes('glob')) {
      if (typeof args.pattern === 'string') return truncate(args.pattern);
    }
    // Web search
    if (name.includes('websearch') || name === 'search') {
      if (typeof args.query === 'string') return truncate(args.query);
    }
    // Web fetch / navigate
    if (name.includes('webfetch') || name.includes('navigate') || name.includes('fetch')) {
      if (typeof args.url === 'string') return truncate(args.url);
    }
    // Memory
    if (name.includes('memorysearch') || name.includes('memoryfind')) {
      if (typeof args.query === 'string') return truncate(args.query);
    }
    if (name.includes('memoryask')) {
      if (typeof args.question === 'string') return truncate(args.question);
    }
    if (name.includes('memoryadd')) {
      if (typeof args.content === 'string') return truncate(args.content);
    }
    // Delegation
    if (name.includes('delegate') || name.includes('fork') || name.includes('spawn')) {
      if (typeof args.task === 'string') return truncate(args.task);
    }
    // Step tools
    if (name.includes('stepcomplete')) {
      if (typeof args.summary === 'string') return truncate(args.summary);
    }

    // Fallback: show first string arg
    for (const value of Object.values(args)) {
      if (typeof value === 'string' && value.length > 0) {
        return truncate(value);
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

  /** Strip <tool_result> XML wrapper tags injected by the tool router. */
  function cleanResult(raw: string): string {
    return raw
      .replace(/^<tool_result[^>]*>\n?/, '')
      .replace(/\n?<\/tool_result>\s*$/, '');
  }

  function paginatedText(raw: string, showAll: boolean): { text: string; totalLines: number; hiddenCount: number } {
    const lines = raw.split('\n');
    const totalLines = lines.length;
    if (showAll || totalLines <= LINES_PER_PAGE) {
      return { text: raw, totalLines, hiddenCount: 0 };
    }
    return {
      text: lines.slice(0, LINES_PER_PAGE).join('\n'),
      totalLines,
      hiddenCount: totalLines - LINES_PER_PAGE
    };
  }

  function borderColor(): string {
    if (item.isError) return 'border-rose-500/40';
    return 'border-slate-800';
  }

  function evalDecisionColor(decision: string): string {
    if (decision === 'approve') return 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10';
    if (decision === 'deny') return 'text-rose-400 border-rose-500/40 bg-rose-500/10';
    if (decision === 'escalate') return 'text-amber-400 border-amber-500/40 bg-amber-500/10';
    return 'text-slate-400 border-slate-700 bg-slate-800/40';
  }

  function evalRiskColor(risk: string): string {
    if (risk === 'critical') return 'text-rose-400';
    if (risk === 'high') return 'text-amber-400';
    if (risk === 'medium') return 'text-yellow-400';
    return 'text-slate-400';
  }

  function stepRequestQuestion(): string {
    return typeof item.arguments?.question === 'string' ? item.arguments.question : '';
  }

  function stepRequestOptions(): string[] {
    if (!Array.isArray(item.arguments?.options)) return [];
    return item.arguments.options
      .map((option: unknown) => {
        if (typeof option === 'string') return option;
        if (option && typeof option === 'object') {
          const label = (option as Record<string, unknown>).label;
          return typeof label === 'string' ? label : '';
        }
        return '';
      })
      .filter((option: string) => option.length > 0);
  }

  function stepRequestContext(): string {
    const context = item.arguments?.context;
    if (typeof context === 'string') return context;
    if (context && typeof context === 'object') {
      const text = (context as Record<string, unknown>).context ?? (context as Record<string, unknown>).note;
      return typeof text === 'string' ? text : '';
    }
    return '';
  }

  function parsedToolResult(): Record<string, unknown> | null {
    if (item.result == null) return null;
    try {
      const parsed = JSON.parse(cleanResult(item.result));
      return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  }

  function stepRequestResponse(): string {
    const response = parsedToolResult()?.response;
    return typeof response === 'string' ? response : '';
  }

  function stepRequestError(): string {
    const error = parsedToolResult()?.error;
    return typeof error === 'string' ? error : '';
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
      {#if isStepRequestInput()}
        <div>
          <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Question</p>
          <div class="rounded-2xl border border-sky-500/20 bg-sky-500/5 px-4 py-3 text-sm text-sky-50">
            <p class="leading-6">{stepRequestQuestion() || 'The agent requested more input.'}</p>
            {#if stepRequestContext()}
              <p class="mt-2 text-xs text-sky-100/80">{stepRequestContext()}</p>
            {/if}
            {#if stepRequestOptions().length > 0}
              <div class="mt-3 flex flex-wrap gap-2">
                {#each stepRequestOptions() as option}
                  <span class="rounded-full border border-sky-400/30 bg-sky-400/10 px-3 py-1 text-[11px] text-sky-100">{option}</span>
                {/each}
              </div>
            {/if}
          </div>
        </div>

        <div>
          <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Resolution</p>
          {#if item.status === 'started'}
            <div class="rounded-2xl border border-slate-800/60 bg-slate-950/60 px-4 py-3">
              <LiveDots label="Waiting for user input" size="sm" inline={true} />
            </div>
          {:else if stepRequestError()}
            <div class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
              {stepRequestError()}
            </div>
          {:else if stepRequestResponse()}
            <div class="rounded-2xl border border-emerald-500/20 bg-emerald-500/5 px-4 py-3 text-sm text-emerald-50">
              <p class="text-xs font-medium uppercase tracking-widest text-emerald-300">User answer</p>
              <p class="mt-2 whitespace-pre-wrap leading-6">{stepRequestResponse()}</p>
            </div>
          {:else}
            <div class="rounded-2xl border border-slate-800/60 bg-slate-950/60 px-4 py-3 text-sm text-slate-400">
              No resolution was recorded for this input request.
            </div>
          {/if}
        </div>

      {:else}
        {#if item.arguments && Object.keys(item.arguments).length > 0}
          {@const inputData = paginatedText(formatArguments(), inputExpanded)}
          <div>
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Input</p>
            <pre class="max-h-[40vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-xs leading-5 text-slate-300">{inputData.text}</pre>
            {#if inputData.hiddenCount > 0}
              <button
                class="mt-1 text-xs text-sky-400 hover:text-sky-300"
                onclick={() => { inputExpanded = !inputExpanded; }}
                type="button"
              >
                {inputExpanded ? 'Show less' : `Show all (${inputData.totalLines} lines)`}
              </button>
            {/if}
          </div>
        {/if}

        {#if item.result != null}
          {@const cleaned = cleanResult(item.result)}
          {@const outputData = paginatedText(cleaned, outputExpanded)}
          <div>
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Output</p>
            <pre class={`max-h-[40vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 text-xs leading-5 ${item.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{outputData.text}</pre>
            {#if outputData.hiddenCount > 0}
              <button
                class="mt-1 text-xs text-sky-400 hover:text-sky-300"
                onclick={() => { outputExpanded = !outputExpanded; }}
                type="button"
              >
                {outputExpanded ? 'Show less' : `Show all (${outputData.totalLines} lines)`}
              </button>
            {/if}
          </div>
        {/if}
      {/if}

      <!-- Evaluation metadata (from Intaris) -->
      {#if item.evaluation}
        <div>
          <button
            class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-slate-500 transition hover:text-slate-300"
            onclick={() => { evalExpanded = !evalExpanded; }}
            type="button"
          >
            <span>{evalExpanded ? '\u25BC' : '\u25B6'}</span>
            <span>Evaluation</span>
            <span class={`rounded-full border px-2 py-0.5 text-[10px] font-semibold normal-case ${evalDecisionColor(item.evaluation.decision)}`}>
              {item.evaluation.decision}
            </span>
            {#if item.evaluation.risk}
              <span class={`text-[10px] normal-case ${evalRiskColor(item.evaluation.risk)}`}>
                {item.evaluation.risk} risk
              </span>
            {/if}
          </button>
          {#if evalExpanded}
            <div class="mt-2 space-y-2 rounded-lg border border-slate-800/60 bg-slate-950/40 p-3 text-xs">
              {#if item.evaluation.reasoning}
                <div>
                  <span class="font-medium text-slate-500">Reasoning:</span>
                  <span class="ml-1 text-slate-300">{item.evaluation.reasoning}</span>
                </div>
              {/if}
              <div class="flex flex-wrap gap-3 text-slate-400">
                {#if item.evaluation.path}
                  <span>Path: <span class="text-slate-300">{item.evaluation.path}</span></span>
                {/if}
                {#if item.evaluation.latency_ms != null}
                  <span>Latency: <span class="text-slate-300">{item.evaluation.latency_ms}ms</span></span>
                {/if}
              </div>
            </div>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</article>
