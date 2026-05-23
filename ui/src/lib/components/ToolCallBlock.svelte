<script lang="ts">
  import Check from 'lucide-svelte/icons/check';
  import Copy from 'lucide-svelte/icons/copy';
  import { onMount } from 'svelte';
  import type { ToolCallTimelineItem } from '$lib/chat';
  import FileDiffViewer from '$lib/components/FileDiffViewer.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import MessageAttachments from '$lib/components/MessageAttachments.svelte';
  import ToolOutputDrawer from '$lib/components/ToolOutputDrawer.svelte';
  import { addToast } from '$lib/stores/toasts';
  import { highlightJson, looksLikeJson, prettyPrintJson } from '$lib/syntax/json';
  import { renderTerminalOutput } from '$lib/syntax/terminal-output';
  import { highlightToolOutput, inferLanguageFromPath, isReadToolName, pathFromToolArguments } from '$lib/syntax/tool-output';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';
  import { canOpenToolOutput, toolOutputOpenLabel } from '$lib/tool-output-status';

  let { item } = $props<{ item: ToolCallTimelineItem }>();

  let expanded = $state(false);
  let inputExpanded = $state(false);
  let outputExpanded = $state(false);
  let rawExpanded = $state(false);
  let evalExpanded = $state(false);
  let autoExpanded = $state(false);
  let terminalPinned = $state(false);
  let outputDrawerOpen = $state(false);
  let terminalTailing = $state(true);
  let terminalEl = $state<HTMLPreElement | null>(null);
  let copiedBox = $state<'input' | 'output' | null>(null);
  let bashExpandTimer: number | null = null;
  let bashCollapseTimer: number | null = null;
  let bashAutoExpanded = false;
  let copyResetTimer: number | null = null;
  const nowDate = new Date();

  const LINES_PER_PAGE = 50;
  const BASH_AUTO_EXPAND_DELAY_MS = 450;
  const BASH_AUTO_COLLAPSE_DELAY_MS = 4000;
  const startsExpanded = $derived(['steprequestinput', 'requestauthchallenge', 'requestcredential'].includes(item.toolName.toLowerCase().replace(/_/g, '')));

  $effect(() => {
    if (startsExpanded && !autoExpanded) {
      expanded = true;
      autoExpanded = true;
    }
  });

  $effect(() => {
    if (isBashTool() && item.status === 'started' && !autoExpanded && bashExpandTimer === null) {
      clearBashCollapseTimer();
      bashExpandTimer = window.setTimeout(() => {
        bashExpandTimer = null;
        if (isBashTool() && item.status === 'started' && !autoExpanded && !terminalPinned) {
          expanded = true;
          autoExpanded = true;
          bashAutoExpanded = true;
        }
      }, BASH_AUTO_EXPAND_DELAY_MS);
    }
  });

  $effect(() => {
    if (isBashTool() && item.status !== 'started') {
      clearBashExpandTimer();
      if (
        bashAutoExpanded &&
        expanded &&
        !terminalPinned &&
        !outputDrawerOpen &&
        bashCollapseTimer === null
      ) {
        bashCollapseTimer = window.setTimeout(() => {
          bashCollapseTimer = null;
          if (
            isBashTool() &&
            item.status !== 'started' &&
            bashAutoExpanded &&
            expanded &&
            !terminalPinned &&
            !outputDrawerOpen
          ) {
            expanded = false;
            autoExpanded = false;
            bashAutoExpanded = false;
          }
        }, BASH_AUTO_COLLAPSE_DELAY_MS);
      }
    }
  });

  $effect(() => {
    if (isBashTool() && terminalEl && terminalTailing) {
      item.result;
      terminalEl.scrollTop = terminalEl.scrollHeight;
    }
  });

  onMount(() => {
    return () => {
      if (copyResetTimer !== null) {
        window.clearTimeout(copyResetTimer);
      }
      clearBashExpandTimer();
      clearBashCollapseTimer();
    };
  });

  function toggle(): void {
    if (isBashTool()) {
      terminalPinned = true;
      clearBashCollapseTimer();
    }
    expanded = !expanded;
  }

  function clearBashExpandTimer(): void {
    if (bashExpandTimer !== null) {
      window.clearTimeout(bashExpandTimer);
      bashExpandTimer = null;
    }
  }

  function clearBashCollapseTimer(): void {
    if (bashCollapseTimer !== null) {
      window.clearTimeout(bashCollapseTimer);
      bashCollapseTimer = null;
    }
  }

  function pinTerminal(): void {
    terminalPinned = true;
    clearBashCollapseTimer();
  }

  function truncate(s: string, max = 80): string {
    return s.length > max ? `${s.slice(0, max)}...` : s;
  }

  function normalizedToolName(): string {
    return item.toolName.toLowerCase().replace(/_/g, '');
  }

  function isBashTool(): boolean {
    const name = normalizedToolName();
    return name.includes('bash') || name.includes('shell');
  }

  function conversationId(): string | null {
    const match = typeof window !== 'undefined' ? window.location.pathname.match(/\/chat\/([^/]+)/) : null;
    return match?.[1] ? decodeURIComponent(match[1]) : null;
  }

  function isApplyPatchTool(): boolean {
    return normalizedToolName().includes('applypatch');
  }

  function descriptionText(): string {
    return typeof item.arguments?.description === 'string' ? item.arguments.description.trim() : '';
  }

  function patchFiles(): string[] {
    const patch = item.arguments?.patchText;
    if (typeof patch !== 'string') return [];
    const files = new Set<string>();
    for (const line of patch.split('\n')) {
      const match = line.match(/^(?:\*\*\* Update File:|\*\*\* Add File:|\*\*\* Delete File:|--- a\/|\+\+\+ b\/)(.+)$/);
      if (!match) continue;
      const path = match[1]?.trim();
      if (path && path !== '/dev/null') files.add(path);
    }
    return Array.from(files);
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
      if (isApplyPatchTool()) {
        const files = patchFiles();
        if (files.length > 0) return truncate(files.join(', '), 120);
      }
      if (typeof args.filePath === 'string') return args.filePath;
      if (typeof args.path === 'string') return args.path;
    }
    // Shell
    if (name.includes('bash') || name.includes('shell')) {
      const description = descriptionText();
      if (description) return truncate(description, 120);
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
    return 'text-sky-400';
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

  async function copyBox(kind: 'input' | 'output', text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      copiedBox = kind;
      if (copyResetTimer !== null) {
        window.clearTimeout(copyResetTimer);
      }
      copyResetTimer = window.setTimeout(() => {
        copiedBox = null;
        copyResetTimer = null;
      }, 1600);
    } catch {
      addToast(`Failed to copy ${kind}`, 'error');
    }
  }

  /** Strip <tool_result> XML wrapper tags injected by the tool router. */
  function cleanResult(raw: string | null | undefined): string {
    if (raw == null) return '';
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

  /**
   * Return pretty-printed + JSON-highlighted HTML when ``raw`` parses
   * as JSON; otherwise return ``null`` so the caller falls back to
   * rendering the raw text without syntax colouring.
   */
  function formatMaybeJson(raw: string, showAll: boolean): {
    html: string | null;
    text: string;
    totalLines: number;
    hiddenCount: number;
  } {
    if (looksLikeJson(raw)) {
      const pretty = prettyPrintJson(raw);
      const paginated = paginatedText(pretty, showAll);
      return {
        html: highlightJson(paginated.text),
        text: paginated.text,
        totalLines: paginated.totalLines,
        hiddenCount: paginated.hiddenCount,
      };
    }
    const paginated = paginatedText(raw, showAll);
    return { html: null, ...paginated };
  }

  function formatOutput(raw: string, showAll: boolean): {
    html: string | null;
    text: string;
    totalLines: number;
    hiddenCount: number;
  } {
    const json = formatMaybeJson(raw, showAll);
    if (json.html || item.isError || !isReadToolName(item.toolName)) return json;
    const language = inferLanguageFromPath(pathFromToolArguments(item.arguments));
    if (!language) return json;
    return { ...json, html: highlightToolOutput(json.text, language) };
  }

  function borderColor(): string {
    if (item.isError) return 'border-rose-500/40';
    return 'border-slate-800';
  }

  function evalDecisionColor(decision: string): string {
    if (decision === 'approve') return 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10';
    if (decision === 'deny') return 'text-rose-400 border-rose-500/40 bg-rose-500/10';
    if (decision === 'escalate') return 'text-sky-400 border-sky-500/40 bg-sky-500/10';
    return 'text-slate-400 border-slate-700 bg-slate-800/40';
  }

  function evalRiskColor(risk: string): string {
    if (risk === 'critical') return 'text-rose-400';
    if (risk === 'high') return 'text-sky-400';
    if (risk === 'medium') return 'text-yellow-400';
    return 'text-slate-400';
  }

  function hasDiffs(): boolean {
    return Boolean(item.fileDiffs && item.fileDiffs.length > 0);
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

  function commandText(): string {
    return typeof item.arguments?.command === 'string' ? item.arguments.command : item.toolName;
  }

  function terminalTitle(): string {
    return descriptionText() || commandText();
  }

  function workingDirectory(): string {
    const workdir = item.arguments?.workdir;
    return typeof workdir === 'string' && workdir ? workdir : '~';
  }

  function terminalPrompt(): string {
    return `${workingDirectory()} $ ${commandText()}`;
  }

  function onTerminalScroll(): void {
    if (!terminalEl) return;
    const atTail = terminalEl.scrollHeight - terminalEl.scrollTop - terminalEl.clientHeight < 16;
    terminalTailing = atTail;
    if (!atTail) pinTerminal();
  }
</script>

<article class={`min-w-0 overflow-hidden rounded-2xl border bg-slate-900/80 text-sm shadow-card ${borderColor()}`}>
  <!-- Header row (always visible, clickable) -->
  <button
    class="flex min-w-0 w-full items-start gap-3 px-4 py-3 text-left transition hover:bg-slate-800/40 sm:items-center"
    onclick={toggle}
    type="button"
  >
    <span class="text-xs text-slate-500">{expanded ? '\u25BC' : '\u25B6'}</span>
    <span class="min-w-0 flex flex-1 flex-col gap-0.5 sm:flex-row sm:items-center sm:gap-3">
      <span class="min-w-0 font-semibold text-cyan-300 [overflow-wrap:anywhere]">{item.toolName}</span>
      {#if subtitle()}
        <span class="min-w-0 text-xs text-slate-400 sm:flex-1 sm:truncate">{subtitle()}</span>
      {/if}
    </span>
    <span class={`flex shrink-0 items-center gap-1.5 self-start text-xs font-medium ${statusColor()} sm:self-auto`}>
      {#if item.status === 'started'}
        <span class="inline-block h-3 w-3 animate-spin rounded-full border border-sky-400 border-t-transparent"></span>
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
      {#if item.timestamp}
        <div class="flex items-center justify-between gap-3 text-[11px] text-slate-500">
          <span>Executed</span>
          <span title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, nowDate)}</span>
        </div>
      {/if}
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
        {#if hasDiffs() && item.fileDiffs}
          <div>
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Diff</p>
            <FileDiffViewer diffs={item.fileDiffs} />
          </div>
        {/if}

        {#if hasDiffs()}
          <div>
            <button
              class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-slate-500 transition hover:text-slate-300"
              onclick={() => { rawExpanded = !rawExpanded; }}
              type="button"
            >
              <span>{rawExpanded ? '\u25BC' : '\u25B6'}</span>
              <span>Raw</span>
            </button>
          </div>
        {/if}

        {#if isBashTool()}
          {@const outputText = cleanResult(item.result)}
          <div>
            <div class={`overflow-hidden rounded-xl border ${item.isError ? 'border-rose-500/30' : 'border-slate-700/70'} bg-[#05070a] shadow-inner`}>
              <div class="flex items-center justify-between border-b border-white/10 bg-slate-950/90 px-3 py-2 text-[11px] text-slate-400">
                <span class="truncate font-medium text-slate-300">{terminalTitle()}</span>
                <span>{item.status === 'started' ? 'live' : item.status}</span>
              </div>
              <pre bind:this={terminalEl} onscroll={onTerminalScroll} onpointerdown={pinTerminal} class={`max-h-[50vh] overflow-auto p-3 pr-10 font-mono text-xs leading-5 ${item.isError ? 'text-rose-200' : 'text-emerald-100'}`}><span class="text-sky-300">{terminalPrompt()}</span>{#if outputText}
{@html renderTerminalOutput(`\n${outputText}`)}{:else if item.status === 'started'}
Running...{/if}</pre>
            </div>
            {#if canOpenToolOutput(item) && conversationId()}
              <button class="mt-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-100 hover:bg-sky-500/20" type="button" onclick={() => { pinTerminal(); outputDrawerOpen = true; }}>
                {toolOutputOpenLabel(item)}
              </button>
            {/if}
          </div>
        {/if}

        {#if (!hasDiffs() || rawExpanded) && !isBashTool()}
          {#if item.arguments && Object.keys(item.arguments).length > 0}
            {@const inputText = formatArguments()}
            {@const inputData = formatMaybeJson(inputText, inputExpanded)}
            <div>
              <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Input</p>
              <div class="relative">
                <pre class="max-h-[40vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 pr-10 text-xs leading-5 text-slate-300">{#if inputData.html}{@html inputData.html}{:else}{inputData.text}{/if}</pre>
                <button
                  class="copy-icon-button absolute right-2 top-2"
                  onclick={() => void copyBox('input', inputText)}
                  type="button"
                  title="Copy input"
                  aria-label="Copy input"
                >
                  {#if copiedBox === 'input'}
                    <Check class="h-3.5 w-3.5" />
                  {:else}
                    <Copy class="h-3.5 w-3.5" />
                  {/if}
                </button>
              </div>
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
            {@const outputText = cleanResult(item.result)}
            {@const outputData = formatOutput(outputText, outputExpanded)}
            {#if !isBashTool()}
              <div>
                <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Output</p>
                <div class="relative">
                  <pre class={`max-h-[40vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{#if outputData.html}{@html outputData.html}{:else}{outputData.text}{/if}</pre>
                  <button class="copy-icon-button absolute right-2 top-2" onclick={() => void copyBox('output', outputText)} type="button" title="Copy output" aria-label="Copy output">
                    {#if copiedBox === 'output'}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}
                  </button>
                </div>
                {#if canOpenToolOutput(item) && conversationId()}
                  <button class="mt-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-100 hover:bg-sky-500/20" type="button" onclick={() => { outputDrawerOpen = true; }}>
                    {toolOutputOpenLabel(item)}
                  </button>
                {/if}
                {#if outputData.hiddenCount > 0}
                  <button class="mt-1 text-xs text-sky-400 hover:text-sky-300" onclick={() => { outputExpanded = !outputExpanded; }} type="button">
                    {outputExpanded ? 'Show less' : `Show all (${outputData.totalLines} lines)`}
                  </button>
                {/if}
              </div>
            {/if}
          {/if}
        {/if}

        {#if item.attachments && item.attachments.length > 0}
          <div>
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Artifacts</p>
            <MessageAttachments attachments={item.attachments} />
          </div>
        {/if}
      {/if}

      {#if isBashTool() && (item.result != null || (item.arguments && Object.keys(item.arguments).length > 0))}
        {@const rawOutputText = cleanResult(item.result)}
        {@const rawOutputData = formatOutput(rawOutputText, outputExpanded)}
        <div>
          <button
            class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-slate-500 transition hover:text-slate-300"
            onclick={() => { rawExpanded = !rawExpanded; }}
            type="button"
          >
            <span>{rawExpanded ? '▼' : '▶'}</span>
            <span>Raw payload</span>
          </button>
          {#if rawExpanded}
            <div class="mt-2 space-y-2 rounded-lg border border-slate-800/60 bg-slate-950/40 p-3 text-xs">
              <div>
                <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Input</p>
                <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formatArguments()}</pre>
              </div>
              <div>
                <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Output</p>
                <div class="relative">
                  <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
                  <button class="copy-icon-button absolute right-2 top-2" onclick={() => void copyBox('output', rawOutputText)} type="button" title="Copy output" aria-label="Copy output">
                    {#if copiedBox === 'output'}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}
                  </button>
                </div>
              </div>
            </div>
          {/if}
        </div>
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

<ToolOutputDrawer
  open={outputDrawerOpen}
  conversationId={conversationId()}
  sessionId={item.sessionId}
  callId={item.recoveryCallId ?? item.callId}
  toolName={item.toolName}
  isTerminal={isBashTool()}
  onClose={() => { outputDrawerOpen = false; }}
/>
