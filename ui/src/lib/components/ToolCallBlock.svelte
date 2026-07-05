<script lang="ts">
  import Check from 'lucide-svelte/icons/check';
  import Copy from 'lucide-svelte/icons/copy';
  import { onMount } from 'svelte';
  import { isActiveToolStatus, type ToolCallTimelineItem } from '$lib/chat';
  import FileDiffViewer from '$lib/components/FileDiffViewer.svelte';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import MessageAttachments from '$lib/components/MessageAttachments.svelte';
  import ToolOutputDrawer from '$lib/components/ToolOutputDrawer.svelte';
  import TodoProgressPopover from '$lib/components/TodoProgressPopover.svelte';
  import { visibleTodos as activeVisibleTodos } from '$lib/todos';
  import { addToast } from '$lib/stores/toasts';
  import { highlightJson, looksLikeJson, prettyPrintJson } from '$lib/syntax/json';
  import { renderTerminalOutput } from '$lib/syntax/terminal-output';
  import { highlightToolOutput, inferLanguageFromPath, isReadToolName, pathFromToolArguments } from '$lib/syntax/tool-output';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';
  import { canOpenToolOutput, toolOutputOpenLabel } from '$lib/tool-output-status';
  import { delegationToolCallDisplayTitle, skillLoadDisplayName, stepTodoWriteStatusSummary, toolOutputHelperPresentation, workflowToolPresentation } from '$lib/tool-call-summary';
  import { formatStepQuestionResponse, normalizeStepQuestionAnswers, normalizeStepQuestions, type StepQuestionAnswer } from '$lib/tool-call-question-set';
  import { displayToolName } from '$lib/tools-display';
  import { renderMarkdown, sanitizeHtml } from '$lib/markdown';

  let {
    item,
    sourceToolCalls = new Map<string, ToolCallTimelineItem>(),
    onViewSession,
  } = $props<{
    item: ToolCallTimelineItem;
    sourceToolCalls?: Map<string, ToolCallTimelineItem>;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
  }>();

  type StructuredEntry = { key: string; value: unknown };

  let expanded = $state(false);
  let inputExpanded = $state(false);
  let outputExpanded = $state(false);
  let rawExpanded = $state(false);
  let originalCallExpanded = $state(false);
  let evalExpanded = $state(false);
  let completedQuestionPage = $state(0);
  let autoExpanded = $state(false);
  let terminalPinned = $state(false);
  let outputDrawerOpen = $state(false);
  let terminalTailing = $state(true);
  let terminalEl = $state<HTMLPreElement | null>(null);
  let outputDrawerTarget = $state<ToolCallTimelineItem | null>(null);
  let copiedBox = $state<'input' | 'output' | null>(null);
  let delegateAutoExpanded = $state(false);
  let bashExpandTimer: number | null = null;
  let bashCollapseTimer: number | null = null;
  let bashAutoExpanded = false;
  let copyResetTimer: number | null = null;
  let delegateDurationNowMs = $state(Date.now());
  const nowDate = new Date();
  const drawerItem = $derived(outputDrawerTarget ?? item);

  const LINES_PER_PAGE = 50;
  const BASH_AUTO_EXPAND_DELAY_MS = 10_000;
  const BASH_AUTO_COLLAPSE_DELAY_MS = 4000;
  const startsExpanded = $derived(
    (isQuestionRequestTool() && item.status !== 'started')
      || ['requestauthchallenge', 'requestcredential'].includes(item.toolName.toLowerCase().replace(/_/g, ''))
      || ['writedeliverable', 'stepcomplete'].includes(item.toolName.toLowerCase().replace(/_/g, '')) && workflowToolPresentation(item) !== null
      // Delegate/fork: auto-expand while running so the delegation progress
      // (title, tool-call stats, todo pie) is visible without a manual click.
      || (isDelegateTool() && (isActiveToolStatus(item.status) || isDelegationActive()))
  );

  $effect(() => {
    if (startsExpanded && !autoExpanded) {
      expanded = true;
      autoExpanded = true;
      if (isDelegateTool() && isDelegationActive()) {
        delegateAutoExpanded = true;
      }
    }
  });

  $effect(() => {
    if (isDelegateTool() && delegateAutoExpanded && isDelegationTerminal()) {
      expanded = false;
      delegateAutoExpanded = false;
    }
  });

  $effect(() => {
    if (!isDelegateTool() || !delegationRunning) return;
    delegateDurationNowMs = Date.now();
    const timer = window.setInterval(() => {
      delegateDurationNowMs = Date.now();
    }, 1000);
    return () => window.clearInterval(timer);
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
    if (isDelegateTool()) {
      delegateAutoExpanded = false;
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

  function normalizedToolName(target: ToolCallTimelineItem = item): string {
    return target.toolName.toLowerCase().replace(/_/g, '');
  }

  function isBashTool(target: ToolCallTimelineItem = item): boolean {
    const name = normalizedToolName(target);
    return name.includes('bash') || name.includes('shell');
  }

  function conversationId(): string | null {
    const match = typeof window !== 'undefined' ? window.location.pathname.match(/\/chat\/([^/]+)/) : null;
    return match?.[1] ? decodeURIComponent(match[1]) : null;
  }

  function isApplyPatchTool(target: ToolCallTimelineItem = item): boolean {
    return normalizedToolName(target).includes('applypatch');
  }

  function isDelegateTool(target: ToolCallTimelineItem = item): boolean {
    const name = normalizedToolName(target);
    return name === 'delegate' || name === 'fork';
  }

  // Delegation details folded onto the delegate tool call (title/progress/
  // todos/result). Present only for delegate/fork tool calls.
  const delegation = $derived(item.delegation ?? null);
  const delegationVisibleTodos = $derived(activeVisibleTodos(delegation?.todos ?? undefined));
  const delegationRunning = $derived(
    isDelegateTool() && (isActiveToolStatus(item.status) || isDelegationActive())
  );

  function descriptionText(target: ToolCallTimelineItem = item): string {
    return typeof target.arguments?.description === 'string' ? target.arguments.description.trim() : '';
  }

  function patchFiles(target: ToolCallTimelineItem = item): string[] {
    const patch = target.arguments?.patchText;
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

  function isQuestionRequestTool(): boolean {
    const name = normalizedToolName();
    return name === 'steprequestquestions' || name === 'requestuserinput';
  }

  function isStepRequestInput(): boolean {
    return isQuestionRequestTool();
  }

  function isRichWorkflowTool(): boolean {
    return workflowToolPresentation(item) !== null;
  }

  function isRichToolOutputHelper(): boolean {
    return toolOutputHelperPresentation(item) !== null;
  }

  function sourceToolCall(): ToolCallTimelineItem | null {
    const presentation = toolOutputHelperPresentation(item);
    if (!presentation) return null;
    return sourceToolCalls.get(presentation.sourceCallId) ?? null;
  }

  function hasDeliverableFooter(presentation: ReturnType<typeof workflowToolPresentation>): boolean {
    return presentation?.kind === 'write_deliverable'
      && (Boolean(presentation.deliverableId) || presentation.outputKeys.length > 0);
  }

  function hasRawPayload(): boolean {
    return Boolean((item.arguments && Object.keys(item.arguments).length > 0) || item.result != null);
  }

  function isDelegationActive(): boolean {
    const status = item.delegation?.status ?? null;
    return status === 'started' || status === 'running' || status === 'active';
  }

  function isDelegationTerminal(): boolean {
    const status = item.delegation?.status ?? null;
    return status === 'completed'
      || status === 'complete'
      || status === 'failed'
      || status === 'cancelled'
      || status === 'canceled';
  }

  function delegationChildSessionId(): string {
    return delegation?.childSessionId ?? '';
  }

  function canViewDelegationSession(): boolean {
    const sessionId = delegationChildSessionId();
    return Boolean(onViewSession && sessionId.startsWith('sess_'));
  }

  function delegationTitle(): string {
    return delegation?.title
      || delegation?.summary
      || delegationToolCallDisplayTitle(item.arguments)
      || 'Delegated sub-session';
  }

  function delegationAgentLabel(): string {
    return delegation?.usedAgentId ?? delegation?.agentId ?? (
      typeof item.arguments?.agent_id === 'string' ? item.arguments.agent_id : ''
    );
  }

  function delegationStatusLabel(): string {
    return delegation?.status ?? (isActiveToolStatus(item.status) ? 'running' : item.status);
  }

  function delegationStatusDisplayText(): string {
    const status = delegationStatusLabel();
    return status ? status.charAt(0).toUpperCase() + status.slice(1) : '';
  }

  function delegationSummaryText(): string {
    if (delegation?.error) return delegation.error;
    if (delegationRunning) return delegation?.summary ?? 'Working…';
    return delegation?.resultSummary ?? delegation?.summary ?? '';
  }

  function delegationOutputText(): string {
    if (delegation?.error) return '';
    return delegation?.resultContent ?? delegation?.resultSummary ?? '';
  }

  function stripDelegateDisplayAnchors(content: string): string {
    const cleaned: string[] = [];
    let inFence = false;

    for (const line of content.split('\n')) {
      if (/^ {0,3}(```+|~~~+)/.test(line)) {
        inFence = !inFence;
        cleaned.push(line);
        continue;
      }
      if (!inFence && /^\[\[message:\d+\]\]\s*$/.test(line)) continue;
      if (!inFence && /^--- Assistant message \d+ ---\s*$/.test(line)) continue;
      cleaned.push(line);
    }

    return cleaned.join('\n').trim();
  }

  function delegationOutputHtml(): string {
    const output = stripDelegateDisplayAnchors(delegationOutputText());
    return output ? renderMarkdown(output) : '';
  }

  function parseTimeMs(value: string | null | undefined): number | null {
    if (!value) return null;
    const parsed = new Date(value).getTime();
    return Number.isNaN(parsed) ? null : parsed;
  }

  function formatDurationMs(durationMs: number | null | undefined): string {
    if (durationMs == null) return '';
    if (durationMs < 1000) return `${durationMs}ms`;
    if (durationMs < 60_000) return `${(durationMs / 1000).toFixed(1)}s`;
    const totalSeconds = Math.floor(durationMs / 1000);
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}m ${seconds.toString().padStart(2, '0')}s`;
  }

  function delegationDurationMs(): number | null {
    if (typeof delegation?.durationMs === 'number') return delegation.durationMs;
    if (typeof item.durationMs === 'number' && isDelegationTerminal()) return item.durationMs;
    const start = parseTimeMs(delegation?.startedAt ?? item.timestamp);
    if (start == null) return null;
    const end = delegationRunning ? delegateDurationNowMs : parseTimeMs(item.timestamp) ?? Date.now();
    return Math.max(0, end - start);
  }

  function delegationDurationText(): string {
    return formatDurationMs(delegationDurationMs());
  }

  function delegationStatusTextClass(): string {
    const status = delegationStatusLabel();
    if (status === 'completed') return 'text-emerald-300';
    if (status === 'failed' || status === 'cancelled' || status === 'canceled') return 'text-rose-300';
    return 'text-sky-300';
  }

  function delegationStatusDotClass(): string {
    const status = delegationStatusLabel();
    if (status === 'completed') return 'bg-emerald-300';
    if (status === 'failed' || status === 'cancelled' || status === 'canceled') return 'bg-rose-300';
    return 'bg-sky-300';
  }

  function compactSessionId(sessionId: string): string {
    if (!sessionId) return '';
    if (sessionId.length <= 18) return sessionId;
    return `${sessionId.slice(0, 10)}…${sessionId.slice(-6)}`;
  }

  function delegationProgressPercent(): number {
    const current = delegation?.toolCallCount;
    const max = delegation?.maxToolCalls;
    if (typeof current !== 'number' || typeof max !== 'number' || max <= 0) return 0;
    return Math.min((current / max) * 100, 100);
  }

  function hasDelegationProgressStats(): boolean {
    return delegation?.toolCallCount != null || Boolean(delegation?.lastTool);
  }

  function hasDelegationProgressLead(): boolean {
    return delegationVisibleTodos.length > 0
      || (delegationRunning && !delegation?.error)
      || (!delegationRunning && !delegation?.error && Boolean(delegationSummaryText()) && !delegationOutputText());
  }

  function toolCallSubtitle(target: ToolCallTimelineItem): string {
    // Normalize: strip underscores for matching (web_fetch -> webfetch)
    const name = normalizedToolName(target);

    const outputHelperPresentation = toolOutputHelperPresentation(target);
    if (outputHelperPresentation) {
      return truncate(outputHelperPresentation.summary, 120);
    }

    const workflowPresentation = workflowToolPresentation(target);
    if (workflowPresentation?.kind === 'write_deliverable') {
      return truncate(workflowPresentation.title, 120);
    }
    if (workflowPresentation?.kind === 'step_complete') {
      return truncate(workflowPresentation.summary, 120);
    }
    if (workflowPresentation?.kind === 'step_todo_write') {
      return truncate(workflowPresentation.statusSummary || `${workflowPresentation.count} todos`, 120);
    }

    if (name === 'skillload') {
      const skillName = skillLoadDisplayName(target);
      if (skillName) return truncate(skillName, 120);
    }

    if (name === 'steptodowrite') {
      const todoSummary = stepTodoWriteStatusSummary(target);
      if (todoSummary) return truncate(todoSummary, 120);
    }

    if (!target.arguments) {
      return '';
    }
    const args = target.arguments;

    if (name === 'skillload') {
      if (typeof args.skill === 'string') return truncate(args.skill);
      if (typeof args.skill_id === 'string') return truncate(args.skill_id);
    }

    // File operations
    if (name.includes('read') || name.includes('write') || name.includes('edit') || name.includes('patch') || name.includes('multiedit') || name === 'listdirectory') {
      if (isApplyPatchTool(target)) {
        const files = patchFiles(target);
        if (files.length > 0) return truncate(files.join(', '), 120);
      }
      if (typeof args.filePath === 'string') return args.filePath;
      if (typeof args.path === 'string') return args.path;
    }
    // Shell
    if (name.includes('bash') || name.includes('shell')) {
      const description = descriptionText(target);
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
      const displayTitle = delegationToolCallDisplayTitle(args);
      if (displayTitle) return truncate(displayTitle);
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

  function subtitle(): string {
    return toolCallSubtitle(item);
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
    return formatDurationMs(item.durationMs);
  }

  function isPreparingPatch(): boolean {
    return isApplyPatchTool()
      && item.status === 'started'
      && item.progressPhase === 'preparing_input'
      && !item.arguments?.patchText;
  }

  function preparingPatchText(): string {
    const lines = item.progressInputLines;
    const chars = item.progressInputChars;
    const parts: string[] = [];
    if (typeof lines === 'number' && lines > 0) parts.push(`${lines.toLocaleString()} lines`);
    if (typeof chars === 'number' && chars > 0) parts.push(`${chars.toLocaleString()} chars`);
    return parts.length > 0 ? `Preparing patch (${parts.join(', ')})` : 'Preparing patch';
  }

  function formatArguments(): string {
    if (!item.arguments) return '';
    return formatCallArguments(item);
  }

  function formatCallArguments(target: ToolCallTimelineItem): string {
    if (!target.arguments) return '';
    try {
      return JSON.stringify(target.arguments, null, 2);
    } catch {
      return String(target.arguments);
    }
  }

  function canShowInlineOriginalOutput(target: ToolCallTimelineItem): boolean {
    return cleanResult(target.result).length > 0 && cleanResult(target.result).length <= 4000;
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
    return formatStepQuestionResponse(item, parsedToolResult());
  }

  function stepRequestAnswers(): StepQuestionAnswer[] {
    return normalizeStepQuestionAnswers(item, parsedToolResult());
  }

  function completedQuestionPageIndex(answers: StepQuestionAnswer[]): number {
    return Math.min(completedQuestionPage, Math.max(answers.length - 1, 0));
  }

  function completedQuestionAt(answers: StepQuestionAnswer[]): StepQuestionAnswer | null {
    return answers[completedQuestionPageIndex(answers)] ?? null;
  }

  function goToCompletedQuestion(index: number, answers: StepQuestionAnswer[]): void {
    completedQuestionPage = Math.min(Math.max(index, 0), Math.max(answers.length - 1, 0));
  }

  function stepRequestError(): string {
    const error = parsedToolResult()?.error;
    return typeof error === 'string' ? error : '';
  }

  function commandText(target: ToolCallTimelineItem = item): string {
    return typeof target.arguments?.command === 'string' ? target.arguments.command : target.toolName;
  }

  function terminalTitle(): string {
    return descriptionText() || commandText();
  }

  function renderDeliverableContent(content: string, format: string): string {
    return format === 'html' ? sanitizeHtml(content) : renderMarkdown(content);
  }

  function formatStructuredValue(value: unknown): string {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }

  function outcomeClass(status: string): string {
    if (status === 'failed') return 'border-rose-500/40 bg-rose-500/10 text-rose-100';
    if (status === 'rejected') return 'border-amber-500/40 bg-amber-500/10 text-amber-100';
    return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-100';
  }

  function todoStatusClass(status: string): string {
    if (status === 'completed') return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100';
    if (status === 'cancelled' || status === 'canceled') return 'border-slate-600/60 bg-slate-800/50 text-slate-300';
    if (status === 'in_progress' || status === 'active' || status === 'running') return 'border-sky-500/35 bg-sky-500/10 text-sky-100';
    return 'border-amber-500/35 bg-amber-500/10 text-amber-100';
  }

  function hasStructuredEntries(entries: StructuredEntry[]): boolean {
    return entries.length > 0;
  }

  function workingDirectory(): string {
    const workdir = item.arguments?.workdir;
    return typeof workdir === 'string' && workdir ? workdir : '~';
  }

  function terminalPrompt(): string {
    return `${workingDirectory()} $ ${commandText()}`;
  }

  function openToolOutput(target: ToolCallTimelineItem): void {
    outputDrawerTarget = target;
    outputDrawerOpen = true;
  }

  function openReferencedToolOutput(callId: string): void {
    outputDrawerTarget = {
      id: `referenced-tool-output:${callId}`,
      kind: 'tool_call',
      callId,
      toolName: 'tool_output',
      displayToolName: 'Referenced tool output',
      status: 'completed',
      timestamp: null,
      sessionId: item.sessionId,
    };
    outputDrawerOpen = true;
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
      <span class="min-w-0 font-semibold text-cyan-300 [overflow-wrap:anywhere]" title={item.toolName}>{displayToolName(item.displayToolName ?? item.toolName)}</span>
      {#if subtitle()}
        <span class="min-w-0 text-xs text-slate-400 sm:flex-1 sm:truncate">{subtitle()}</span>
      {/if}
    </span>
    <span class={`flex shrink-0 items-center gap-1.5 self-start text-xs font-medium ${statusColor()} sm:self-auto`}>
      {#if isActiveToolStatus(item.status)}
        <LiveDots inline={true} size="sm" tone="sky" />
        <span class="sr-only">{isPreparingPatch() ? 'Preparing' : 'Running'}</span>
      {:else}
        <span>{statusIcon()}</span>
        <span>{item.status}</span>
      {/if}
      {#if isDelegateTool() && delegationDurationText()}
        <span class="text-slate-500">{delegationDurationText()}</span>
      {:else if durationText()}
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
      {#if isDelegateTool()}
        <div class="overflow-hidden rounded-2xl border border-sky-500/25 bg-slate-950/35 text-sm text-slate-100 shadow-inner">
          <div class="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
            <div class="min-w-0 flex-1 space-y-1.5">
              <p class="text-[11px] font-medium uppercase tracking-[0.22em] text-slate-400">Delegated sub-session</p>
              <h4 class="truncate text-base font-semibold text-slate-50" title={delegationTitle()}>{delegationTitle()}</h4>
              <div class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-400">
                {#if delegationAgentLabel()}
                  <span class="font-mono text-slate-300">{delegationAgentLabel()}</span>
                  <span class="text-slate-600">·</span>
                {/if}
                {#if delegationChildSessionId()}
                  <span class="font-mono text-slate-400" title={delegationChildSessionId()}>{compactSessionId(delegationChildSessionId())}</span>
                  <span class="text-slate-600">·</span>
                {/if}
                <span class={`inline-flex items-center gap-1.5 font-medium ${delegationStatusTextClass()}`}>
                  <span class={`h-1.5 w-1.5 rounded-full ${delegationStatusDotClass()}`}></span>
                  {delegationStatusDisplayText()}
                </span>
                {#if delegationDurationText()}
                  <span class="text-slate-600">·</span>
                  <span class="tabular-nums text-slate-400">{delegationDurationText()}</span>
                {/if}
              </div>
            </div>
            <div class="flex shrink-0 items-center">
              {#if canViewDelegationSession()}
                <button
                  class="rounded-lg border border-slate-600/70 bg-slate-900/70 px-3 py-1.5 text-xs font-medium text-slate-100 transition hover:border-sky-300/50 hover:bg-sky-500/10"
                  type="button"
                  onclick={() => { const sessionId = delegationChildSessionId(); if (sessionId) void onViewSession?.(sessionId); }}
                >
                  View session
                </button>
              {/if}
            </div>
          </div>

          <div class="space-y-3 border-t border-slate-800/70 px-4 py-3">
            {#if delegationRunning || hasDelegationProgressStats() || delegationVisibleTodos.length > 0}
              <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-300">
                {#if delegationVisibleTodos.length > 0}
                  <TodoProgressPopover todos={delegationVisibleTodos} label="Delegated todo progress" />
                {/if}
                {#if delegationRunning && !delegation?.error}
                  <span class="inline-flex min-w-0 items-center gap-2">
                    <LiveDots inline={true} size="sm" tone="sky" />
                    <span class="min-w-0 truncate">{delegationSummaryText()}</span>
                  </span>
                {:else if !delegation?.error && delegationSummaryText() && !delegationOutputText()}
                  <span class="min-w-0 whitespace-pre-wrap text-slate-300">{delegationSummaryText()}</span>
                {/if}
                {#if delegation?.toolCallCount != null}
                  {#if hasDelegationProgressLead()}
                    <span class="text-slate-600">·</span>
                  {/if}
                  <span class="tabular-nums text-slate-400">
                    {#if delegation?.maxToolCalls != null && delegation.maxToolCalls > 0}
                      {delegation.toolCallCount}/{delegation.maxToolCalls} tool calls
                    {:else}
                      {delegation.toolCallCount} tool calls
                    {/if}
                  </span>
                {/if}
                {#if delegation?.lastTool}
                  {#if hasDelegationProgressLead() || delegation?.toolCallCount != null}
                    <span class="text-slate-600">·</span>
                  {/if}
                  <span class="min-w-0 truncate text-slate-400" title={delegation.lastTool}>last: <span class="font-mono text-slate-300">{delegation.lastTool}</span></span>
                {/if}
              </div>
              {#if delegation?.toolCallCount != null && delegation?.maxToolCalls != null && delegation.maxToolCalls > 0}
                <div class="h-1 overflow-hidden rounded-full bg-slate-800/80">
                  <div class="h-full rounded-full bg-sky-400 transition-all duration-500" style={`width: ${delegationProgressPercent()}%`}></div>
                </div>
              {/if}
            {/if}

            {#if delegationOutputText() && !delegationRunning && !delegation?.error}
              <div class="rounded-xl border border-sky-400/15 bg-slate-950/35">
                <div class="flex items-center justify-between gap-2 border-b border-sky-400/10 px-3 py-2">
                  <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-sky-100/60">Output</span>
                  {#if delegation?.resultTruncated}
                    <span class="rounded-full border border-amber-300/25 bg-amber-500/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-widest text-amber-100">truncated</span>
                  {/if}
                </div>
                <div class="prose prose-sm prose-invert max-h-[34vh] max-w-none overflow-auto px-3 py-2 text-slate-200">
                  {@html delegationOutputHtml()}
                </div>
              </div>
            {:else if delegationSummaryText() && (!delegationRunning || delegation?.error)}
              <div class={`rounded-xl border px-3 py-2 text-xs leading-5 ${delegation?.error ? 'border-rose-400/25 bg-rose-500/10 text-rose-100' : 'border-slate-700/70 bg-slate-950/35 text-slate-300'}`}>
                <p class="line-clamp-5 whitespace-pre-wrap">{delegationSummaryText()}</p>
              </div>
            {/if}
          </div>
        </div>

        {#if hasRawPayload()}
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
                {#if item.arguments && Object.keys(item.arguments).length > 0}
                  <div>
                    <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Input</p>
                    <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formatArguments()}</pre>
                  </div>
                {/if}
                {#if item.result != null}
                  <div>
                    <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Output</p>
                    <div class="relative">
                      <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
                      <button class="copy-icon-button absolute right-2 top-2" onclick={() => void copyBox('output', rawOutputText)} type="button" title="Copy output" aria-label="Copy output">
                        {#if copiedBox === 'output'}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}
                      </button>
                    </div>
                  </div>
                {/if}
              </div>
            {/if}
          </div>
        {/if}
      {/if}
      {#if isStepRequestInput()}
        <div>
          {#if item.status === 'started'}
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Questions</p>
            <div class="space-y-3 rounded-2xl border border-sky-500/20 bg-sky-500/5 px-4 py-3 text-sm text-sky-50">
              {#if stepRequestContext()}
                <p class="text-xs leading-5 text-sky-100/80">{stepRequestContext()}</p>
              {/if}
              {#if normalizeStepQuestions(item).length > 0}
                {#each normalizeStepQuestions(item) as question, index}
                  <section class="rounded-xl border border-sky-400/15 bg-slate-950/35 px-3 py-2.5">
                    <div class="flex flex-wrap items-center gap-2">
                      <span class="rounded-full border border-sky-400/25 px-2 py-0.5 text-[10px] font-medium uppercase tracking-widest text-sky-100/80">#{index + 1}</span>
                      {#if question.header}
                        <span class="text-xs font-semibold uppercase tracking-widest text-sky-200">{question.header}</span>
                      {/if}
                      {#if question.multiple}
                        <span class="rounded-full border border-slate-600 px-2 py-0.5 text-[10px] text-slate-300">multi-select</span>
                      {/if}
                      {#if question.required}
                        <span class="rounded-full border border-slate-600 px-2 py-0.5 text-[10px] text-slate-300">required</span>
                      {/if}
                      {#if question.allow_custom}
                        <span class="rounded-full border border-slate-600 px-2 py-0.5 text-[10px] text-slate-300">custom</span>
                      {/if}
                    </div>
                    <p class="mt-2 leading-6">{question.question}</p>
                    {#if question.options.length > 0}
                      <div class="mt-3 flex flex-wrap gap-2">
                        {#each question.options as option}
                          <span class="rounded-full border border-sky-400/30 bg-sky-400/10 px-3 py-1 text-[11px] text-sky-100" title={option.description ?? ''}>{option.label}</span>
                        {/each}
                      </div>
                    {/if}
                  </section>
                {/each}
              {:else}
                <p class="leading-6">The agent requested more input.</p>
              {/if}
              <div class="rounded-2xl border border-slate-800/60 bg-slate-950/60 px-4 py-3">
                <LiveDots label="Waiting for user input" size="sm" inline={true} />
              </div>
            </div>
          {:else if stepRequestError()}
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Resolution</p>
            <div class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
              {stepRequestError()}
            </div>
          {:else if stepRequestAnswers().length > 0}
            {@const answers = stepRequestAnswers()}
            {@const answer = completedQuestionAt(answers)}
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Submitted answers</p>
            <div class="overflow-hidden rounded-2xl border border-emerald-500/20 bg-emerald-500/5 text-sm text-emerald-50">
              {#if answers.length > 1}
                <div class="flex items-center justify-between gap-3 border-b border-emerald-400/15 px-4 py-3">
                  <span class="font-semibold">Answered questions</span>
                  <span class="rounded-full border border-emerald-300/25 bg-slate-950/40 px-2 py-0.5 font-mono text-[11px] text-emerald-100">
                    {completedQuestionPageIndex(answers) + 1}/{answers.length}
                  </span>
                </div>
              {/if}
              <div class="max-h-[min(42vh,28rem)] overflow-y-auto overscroll-contain px-4 py-3">
                {#if answer}
                  <section class="rounded-2xl border border-emerald-400/20 bg-slate-950/30 p-3">
                    {#if answer.question.header}
                      <p class="text-xs uppercase tracking-[0.2em] text-emerald-100/70">{answer.question.header}</p>
                    {/if}
                    <p class="text-sm font-medium leading-6 text-emerald-50">{answer.question.question}</p>
                    <div class="mt-1 flex flex-wrap gap-2">
                      {#if answer.question.required}
                        <span class="rounded-full border border-emerald-300/20 px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-emerald-100/65">Required</span>
                      {:else}
                        <span class="rounded-full border border-emerald-300/15 px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-emerald-100/50">Optional</span>
                      {/if}
                      {#if answer.question.multiple}
                        <span class="rounded-full border border-emerald-300/15 px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-emerald-100/50">Multi-select</span>
                      {/if}
                    </div>
                    {#if answer.selected.length > 0}
                      <div class="mt-3 space-y-2">
                        {#each answer.selected as option, optionIndex (`${option.id}:${optionIndex}`)}
                          <div class={`flex w-full items-start gap-3 rounded-2xl border px-3 py-2 text-left text-xs ${option.unknown ? 'border-amber-400/30 bg-amber-400/10 text-amber-100' : 'border-emerald-400/30 bg-emerald-400/10 text-emerald-100'}`}>
                            <span class={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center border ${answer.question.multiple ? 'rounded' : 'rounded-full'} ${option.unknown ? 'border-amber-300/60 bg-amber-300/20' : 'border-emerald-300/70 bg-emerald-300 text-slate-950'}`}>
                              {#if answer.question.multiple}
                                ✓
                              {:else}
                                <span class={`h-1.5 w-1.5 rounded-full ${option.unknown ? 'bg-amber-100' : 'bg-slate-950'}`}></span>
                              {/if}
                            </span>
                            <span class="min-w-0">
                              <span class="block font-medium">{option.label}</span>
                              {#if option.description}
                                <span class="mt-0.5 block text-emerald-100/60">{option.description}</span>
                              {:else if option.unknown}
                                <span class="mt-0.5 block text-amber-100/65">Unknown option id from the submitted response.</span>
                              {/if}
                            </span>
                          </div>
                        {/each}
                      </div>
                    {/if}
                    {#if answer.custom}
                      <div class="mt-3 rounded-2xl border border-emerald-400/20 bg-slate-950/60 px-3 py-2">
                        <p class="mb-1 text-[11px] uppercase tracking-[0.18em] text-emerald-100/55">Custom answer</p>
                        <p class="whitespace-pre-wrap leading-6">{answer.custom}</p>
                      </div>
                    {/if}
                  </section>
                {/if}
                {#if answers.length > 1}
                  <div class="mt-3 grid gap-1" style={`grid-template-columns: repeat(${Math.min(answers.length, 7)}, minmax(0, 1fr));`} aria-hidden="true">
                    {#each answers as entry, index (`${entry.question.id}:${index}`)}
                      <span class={`h-1.5 rounded-full ${index === completedQuestionPageIndex(answers) ? 'bg-emerald-200' : 'bg-emerald-900/70'}`}></span>
                    {/each}
                  </div>
                {/if}
              </div>
              {#if answers.length > 1}
                <div class="flex items-center gap-2 border-t border-emerald-400/15 bg-slate-950/80 px-4 py-3">
                  {#if completedQuestionPageIndex(answers) > 0}
                    <button
                      class="rounded-lg border border-emerald-300/20 bg-emerald-300/10 px-3 py-1.5 text-xs text-emerald-100 transition hover:bg-emerald-300/20"
                      type="button"
                      aria-label="Previous answered question"
                      onclick={() => { goToCompletedQuestion(completedQuestionPageIndex(answers) - 1, answers); }}
                    >
                      ←
                    </button>
                  {/if}
                  {#if completedQuestionPageIndex(answers) < answers.length - 1}
                    <button
                      class="ml-auto rounded-lg border border-emerald-300/20 bg-emerald-300/10 px-3 py-1.5 text-xs text-emerald-100 transition hover:bg-emerald-300/20"
                      type="button"
                      onclick={() => { goToCompletedQuestion(completedQuestionPageIndex(answers) + 1, answers); }}
                    >
                      Next →
                    </button>
                  {/if}
                </div>
              {/if}
            </div>
          {:else if stepRequestResponse()}
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Submitted answers</p>
            <div class="rounded-2xl border border-emerald-500/20 bg-emerald-500/5 px-4 py-3 text-sm text-emerald-50">
              <p class="whitespace-pre-wrap leading-6">{stepRequestResponse()}</p>
            </div>
          {:else}
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Resolution</p>
            <div class="rounded-2xl border border-slate-800/60 bg-slate-950/60 px-4 py-3 text-sm text-slate-400">
              No resolution was recorded for this input request.
            </div>
          {/if}
        </div>

        {#if hasRawPayload()}
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
                {#if item.arguments && Object.keys(item.arguments).length > 0}
                  <div>
                    <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Input</p>
                    <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formatArguments()}</pre>
                  </div>
                {/if}
                {#if item.result != null}
                  <div>
                    <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Output</p>
                    <div class="relative">
                      <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
                      <button class="copy-icon-button absolute right-2 top-2" onclick={() => void copyBox('output', rawOutputText)} type="button" title="Copy output" aria-label="Copy output">
                        {#if copiedBox === 'output'}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}
                      </button>
                    </div>
                  </div>
                {/if}
              </div>
            {/if}
          </div>
        {/if}

      {:else}
        {#if isRichToolOutputHelper()}
          {@const outputHelper = toolOutputHelperPresentation(item)}
          {@const originalCall = sourceToolCall()}
          {#if outputHelper}
            <div class="overflow-hidden rounded-2xl border border-cyan-500/25 bg-cyan-500/5">
              <div class="flex flex-wrap items-start justify-between gap-3 border-b border-cyan-500/15 px-4 py-3">
                <div class="min-w-0">
                  <p class="text-xs font-medium uppercase tracking-widest text-cyan-300">Stored tool output query</p>
                  <h4 class="mt-1 truncate text-sm font-semibold text-cyan-50">{outputHelper.title}</h4>
                  <p class="mt-2 text-xs leading-5 text-cyan-100/80">{outputHelper.summary}</p>
                </div>
                <span class="rounded-full border border-cyan-400/25 px-2.5 py-1 font-mono text-[11px] text-cyan-100">{outputHelper.sourceCallId}</span>
              </div>

              <div class="grid gap-3 px-4 py-3 md:grid-cols-2">
                <section class="rounded-xl border border-cyan-400/15 bg-slate-950/30 px-3 py-2">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-300">Query</p>
                  <dl class="space-y-2 text-xs">
                    {#each outputHelper.queryEntries as entry}
                      <div>
                        <dt class="font-medium uppercase tracking-wider text-cyan-100/70">{entry.key}</dt>
                        <dd class="mt-1 font-mono text-slate-100 [overflow-wrap:anywhere]">{formatStructuredValue(entry.value)}</dd>
                      </div>
                    {/each}
                  </dl>
                </section>

                <section class="rounded-xl border border-cyan-400/15 bg-slate-950/30 px-3 py-2">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-300">Received</p>
                  <p class="text-xs leading-5 text-slate-100">{outputHelper.receivedSummary}</p>
                  {#if outputHelper.receivedDetails.length > 0}
                    <dl class="mt-2 space-y-2 text-xs">
                      {#each outputHelper.receivedDetails as entry}
                        <div>
                          <dt class="font-medium uppercase tracking-wider text-cyan-100/70">{entry.key}</dt>
                          <dd class="mt-1 font-mono text-slate-100">{formatStructuredValue(entry.value)}</dd>
                        </div>
                      {/each}
                    </dl>
                  {/if}
                  {#if outputHelper.continuationHint}
                    <p class="mt-2 rounded-lg border border-sky-400/20 bg-sky-500/10 px-2 py-1.5 text-xs text-sky-100">{outputHelper.continuationHint}</p>
                  {/if}
                </section>
              </div>

              <section class="border-t border-cyan-500/15 px-4 py-3">
                <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-300">Original tool call</p>
                {#if originalCall}
                  <div class="rounded-xl border border-cyan-400/15 bg-slate-950/30 px-3 py-2">
                    <div class="flex flex-wrap items-start justify-between gap-3">
                      <div class="min-w-0">
                        <p class="font-semibold text-slate-100 [overflow-wrap:anywhere]">{displayToolName(originalCall.displayToolName ?? originalCall.toolName)}</p>
                        {#if toolCallSubtitle(originalCall)}
                          <p class="mt-1 text-xs leading-5 text-slate-300 [overflow-wrap:anywhere]">{toolCallSubtitle(originalCall)}</p>
                        {/if}
                      </div>
                      <div class="flex flex-wrap gap-2 text-[11px] text-cyan-100/75">
                        <span class="rounded-full border border-cyan-400/25 px-2 py-0.5">{originalCall.status}</span>
                        {#if originalCall.timestamp}
                          <span class="rounded-full border border-cyan-400/25 px-2 py-0.5" title={formatAbsoluteTime(originalCall.timestamp)}>{formatCompactTime(originalCall.timestamp, nowDate)}</span>
                        {/if}
                      </div>
                    </div>
                    <div class="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                      <span>Call ID: <span class="font-mono text-slate-200">{originalCall.callId}</span></span>
                      {#if originalCall.outputSize != null}
                        <span>Output: <span class="text-slate-200">{originalCall.outputSize.toLocaleString()} chars</span></span>
                      {/if}
                      {#if originalCall.hasFullOutput}
                        <span class="rounded-full border border-emerald-400/25 px-2 py-0.5 text-emerald-100">full output available</span>
                      {/if}
                      {#if originalCall.anchorsAvailable}
                        <span class="rounded-full border border-sky-400/25 px-2 py-0.5 text-sky-100">anchors available</span>
                      {/if}
                    </div>
                    {#if canOpenToolOutput(originalCall) && conversationId()}
                      <button class="mt-3 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-100 hover:bg-sky-500/20" type="button" onclick={() => { openToolOutput(originalCall); }}>
                        Open original output
                      </button>
                    {/if}
                  </div>
                {:else}
                  <div class="rounded-xl border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs leading-5 text-amber-100">
                    <p>The original tool call is not present in the loaded timeline page. The referenced call ID is <span class="font-mono">{outputHelper.sourceCallId}</span>.</p>
                    {#if conversationId()}
                      <button class="mt-2 rounded-lg border border-amber-400/30 bg-amber-500/10 px-3 py-1.5 text-xs font-medium text-amber-100 hover:bg-amber-500/20" type="button" onclick={() => { openReferencedToolOutput(outputHelper.sourceCallId); }}>
                        Open referenced output
                      </button>
                    {/if}
                  </div>
                {/if}
              </section>
            </div>

            {#if originalCall}
              <div>
                <button
                  class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-slate-500 transition hover:text-slate-300"
                  onclick={() => { originalCallExpanded = !originalCallExpanded; }}
                  type="button"
                >
                  <span>{originalCallExpanded ? '▼' : '▶'}</span>
                  <span>Original call raw</span>
                </button>
                {#if originalCallExpanded}
                  {@const originalOutputText = cleanResult(originalCall.result)}
                  {@const originalOutputData = formatOutput(originalOutputText, outputExpanded)}
                  <div class="mt-2 space-y-2 rounded-lg border border-slate-800/60 bg-slate-950/40 p-3 text-xs">
                    {#if originalCall.arguments && Object.keys(originalCall.arguments).length > 0}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Input</p>
                        <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formatCallArguments(originalCall)}</pre>
                      </div>
                    {/if}
                    {#if canShowInlineOriginalOutput(originalCall)}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Output</p>
                        <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 text-xs leading-5 ${originalCall.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{#if originalOutputData.html}{@html originalOutputData.html}{:else}{originalOutputData.text}{/if}</pre>
                      </div>
                    {:else if originalCall.result}
                      <p class="rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-400">Original output is too large to embed here. Use “Open original output” instead.</p>
                    {/if}
                  </div>
                {/if}
              </div>
            {/if}

            {#if hasRawPayload()}
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
                    {#if item.arguments && Object.keys(item.arguments).length > 0}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Input</p>
                        <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formatArguments()}</pre>
                      </div>
                    {/if}
                    {#if item.result != null}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Output</p>
                        <div class="relative">
                          <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
                          <button class="copy-icon-button absolute right-2 top-2" onclick={() => void copyBox('output', rawOutputText)} type="button" title="Copy output" aria-label="Copy output">
                            {#if copiedBox === 'output'}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}
                          </button>
                        </div>
                      </div>
                    {/if}
                  </div>
                {/if}
              </div>
            {/if}
          {/if}
        {:else if workflowToolPresentation(item)}
          {@const workflowPresentation = workflowToolPresentation(item)}
          {#if workflowPresentation?.kind === 'write_deliverable'}
            <div class="overflow-hidden rounded-2xl border border-emerald-500/25 bg-emerald-500/5">
              <div class="flex flex-wrap items-center justify-between gap-2 border-b border-emerald-500/15 px-4 py-3">
                <div class="min-w-0">
                  <p class="text-xs font-medium uppercase tracking-widest text-emerald-300">Deliverable written</p>
                  <h4 class="mt-1 truncate text-sm font-semibold text-emerald-50">{workflowPresentation.title}</h4>
                </div>
                <div class="flex flex-wrap gap-2 text-[11px] text-emerald-100/75">
                  <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">{workflowPresentation.format}</span>
                  {#if workflowPresentation.length !== null}
                    <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">{workflowPresentation.length.toLocaleString()} chars</span>
                  {/if}
                  {#if workflowPresentation.version !== null}
                    <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">v{workflowPresentation.version}</span>
                  {/if}
                </div>
              </div>
              <div class="max-h-[50vh] overflow-auto px-4 py-3">
                {#if workflowPresentation.format === 'plain'}
                  <pre class="whitespace-pre-wrap text-sm leading-6 text-slate-100">{workflowPresentation.content}</pre>
                {:else}
                  <div class="prose prose-sm prose-invert max-w-none text-slate-100">{@html renderDeliverableContent(workflowPresentation.content, workflowPresentation.format)}</div>
                {/if}
              </div>
              {#if hasDeliverableFooter(workflowPresentation)}
                <div class="flex flex-wrap gap-3 border-t border-emerald-500/15 px-4 py-2 text-[11px] text-emerald-100/70">
                  {#if workflowPresentation.deliverableId}
                    <span>Deliverable: <span class="font-mono text-emerald-100">{workflowPresentation.deliverableId}</span></span>
                  {/if}
                  {#if workflowPresentation.outputKeys.length > 0}
                    <span>Outputs: <span class="text-emerald-100">{workflowPresentation.outputKeys.join(', ')}</span></span>
                  {/if}
                </div>
              {/if}
            </div>
          {:else if workflowPresentation?.kind === 'step_complete'}
            <div class="rounded-2xl border border-emerald-500/25 bg-emerald-500/5 px-4 py-3">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="text-xs font-medium uppercase tracking-widest text-emerald-300">Step completed</p>
                  <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-emerald-50">{workflowPresentation.summary}</p>
                </div>
                <span class={`rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider ${outcomeClass(workflowPresentation.outcomeStatus)}`}>
                  {workflowPresentation.outcomeStatus}
                </span>
              </div>
              {#if workflowPresentation.outcomeReason}
                <p class="mt-3 rounded-xl border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs leading-5 text-amber-100">{workflowPresentation.outcomeReason}</p>
              {/if}
              <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-emerald-100/75">
                {#if workflowPresentation.claims.length > 0}
                  <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">{workflowPresentation.claims.length} claims</span>
                {/if}
                {#if workflowPresentation.outputKeys.length > 0}
                  <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">outputs: {workflowPresentation.outputKeys.join(', ')}</span>
                {/if}
                {#if workflowPresentation.metadataKeys.length > 0}
                  <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">metadata: {workflowPresentation.metadataKeys.join(', ')}</span>
                {/if}
                {#if workflowPresentation.notificationMode}
                  <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">notify: {workflowPresentation.notificationMode}</span>
                {/if}
              </div>
              {#if workflowPresentation.claims.length > 0}
                <div class="mt-4">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-emerald-300">Claims</p>
                  <ul class="space-y-2">
                    {#each workflowPresentation.claims as claim}
                      <li class="rounded-xl border border-emerald-400/15 bg-slate-950/30 px-3 py-2 text-sm leading-6 text-emerald-50">{claim}</li>
                    {/each}
                  </ul>
                </div>
              {/if}
              {#if hasStructuredEntries(workflowPresentation.outputs) || hasStructuredEntries(workflowPresentation.metadata) || workflowPresentation.notificationReason}
                <div class="mt-4 grid gap-3 md:grid-cols-2">
                  {#if hasStructuredEntries(workflowPresentation.outputs)}
                    <section class="rounded-xl border border-emerald-400/15 bg-slate-950/30 px-3 py-2">
                      <p class="mb-2 text-xs font-medium uppercase tracking-widest text-emerald-300">Outputs</p>
                      <dl class="space-y-2 text-xs">
                        {#each workflowPresentation.outputs as entry}
                          <div>
                            <dt class="font-mono text-emerald-100">{entry.key}</dt>
                            <dd class="mt-1 whitespace-pre-wrap text-slate-200">{formatStructuredValue(entry.value)}</dd>
                          </div>
                        {/each}
                      </dl>
                    </section>
                  {/if}
                  {#if hasStructuredEntries(workflowPresentation.metadata)}
                    <section class="rounded-xl border border-emerald-400/15 bg-slate-950/30 px-3 py-2">
                      <p class="mb-2 text-xs font-medium uppercase tracking-widest text-emerald-300">Metadata</p>
                      <dl class="space-y-2 text-xs">
                        {#each workflowPresentation.metadata as entry}
                          <div>
                            <dt class="font-mono text-emerald-100">{entry.key}</dt>
                            <dd class="mt-1 whitespace-pre-wrap text-slate-200">{formatStructuredValue(entry.value)}</dd>
                          </div>
                        {/each}
                      </dl>
                    </section>
                  {/if}
                  {#if workflowPresentation.notificationReason}
                    <section class="rounded-xl border border-emerald-400/15 bg-slate-950/30 px-3 py-2">
                      <p class="mb-2 text-xs font-medium uppercase tracking-widest text-emerald-300">Notification reason</p>
                      <p class="whitespace-pre-wrap text-xs leading-5 text-slate-200">{workflowPresentation.notificationReason}</p>
                    </section>
                  {/if}
                </div>
              {/if}
            </div>
          {:else if workflowPresentation?.kind === 'step_todo_write'}
            <div class="rounded-2xl border border-sky-500/25 bg-sky-500/5 px-4 py-3">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="text-xs font-medium uppercase tracking-widest text-sky-300">Todos updated</p>
                  <p class="mt-1 text-sm text-sky-50">{workflowPresentation.statusSummary || `${workflowPresentation.count} todos`}</p>
                </div>
                <div class="flex flex-wrap gap-2 text-[11px] text-sky-100/75">
                  <span class="rounded-full border border-sky-400/25 px-2 py-0.5">{workflowPresentation.status}</span>
                  <span class="rounded-full border border-sky-400/25 px-2 py-0.5">{workflowPresentation.count} total</span>
                  {#if workflowPresentation.nonTerminalCount !== null}
                    <span class="rounded-full border border-sky-400/25 px-2 py-0.5">{workflowPresentation.nonTerminalCount} open</span>
                  {/if}
                  {#if workflowPresentation.unchanged}
                    <span class="rounded-full border border-amber-400/30 bg-amber-500/10 px-2 py-0.5 text-amber-100">unchanged</span>
                  {/if}
                </div>
              </div>
              <ul class="mt-3 space-y-2">
                {#each workflowPresentation.todos as todo, index}
                  <li class="flex gap-2 rounded-xl border border-sky-400/15 bg-slate-950/30 px-3 py-2">
                    <span class="mt-0.5 text-[11px] text-slate-500">#{index + 1}</span>
                    <div class="min-w-0 flex-1">
                      <p class="text-sm leading-5 text-slate-100">{todo.content}</p>
                    </div>
                    <span class={`h-fit rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ${todoStatusClass(todo.status)}`}>{todo.status}</span>
                  </li>
                {/each}
              </ul>
              {#if workflowPresentation.guidance}
                <p class="mt-3 rounded-xl border border-sky-400/20 bg-sky-500/10 px-3 py-2 text-xs leading-5 text-sky-100">{workflowPresentation.guidance}</p>
              {/if}
            </div>
          {/if}
          {#if hasRawPayload()}
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
                  {#if item.arguments && Object.keys(item.arguments).length > 0}
                    <div>
                      <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Input</p>
                      <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formatArguments()}</pre>
                    </div>
                  {/if}
                  {#if item.result != null}
                    <div>
                      <p class="mb-1 font-medium uppercase tracking-widest text-slate-500">Output</p>
                      <div class="relative">
                        <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
                        <button class="copy-icon-button absolute right-2 top-2" onclick={() => void copyBox('output', rawOutputText)} type="button" title="Copy output" aria-label="Copy output">
                          {#if copiedBox === 'output'}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}
                        </button>
                      </div>
                    </div>
                  {/if}
                </div>
              {/if}
            </div>
          {/if}
        {/if}

        {#if hasDiffs() && item.fileDiffs}
          <div>
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Diff</p>
            <FileDiffViewer diffs={item.fileDiffs} />
          </div>
        {/if}

        {#if isPreparingPatch()}
          <div class="rounded-2xl border border-sky-500/20 bg-sky-500/5 px-4 py-3 text-sm text-sky-50">
            <LiveDots label={preparingPatchText()} size="sm" inline={true} />
            <p class="mt-2 text-xs text-sky-100/70">
              Codex is streaming a native patch input. This will turn into the normal apply_patch call once the patch is complete.
            </p>
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
                {#if isActiveToolStatus(item.status)}
                  <LiveDots inline={true} size="sm" tone="emerald" />
                {:else}
                  <span>{item.status}</span>
                {/if}
              </div>
              <pre bind:this={terminalEl} onscroll={onTerminalScroll} onpointerdown={pinTerminal} class={`max-h-[50vh] overflow-auto p-3 pr-10 font-mono text-xs leading-5 ${item.isError ? 'text-rose-200' : 'text-emerald-100'}`}><span class="text-sky-300">{terminalPrompt()}</span>{#if outputText}
{@html renderTerminalOutput(`\n${outputText}`)}{/if}</pre>
            </div>
            {#if canOpenToolOutput(item) && conversationId()}
              <button class="mt-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-100 hover:bg-sky-500/20" type="button" onclick={() => { pinTerminal(); openToolOutput(item); }}>
                {toolOutputOpenLabel(item)}
              </button>
            {/if}
          </div>
        {/if}

        {#if (!hasDiffs() || rawExpanded) && !isBashTool() && !isDelegateTool() && !isRichWorkflowTool() && !isRichToolOutputHelper()}
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
                  <button class="mt-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-100 hover:bg-sky-500/20" type="button" onclick={() => { openToolOutput(item); }}>
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
  sessionId={drawerItem.sessionId}
  callId={drawerItem.recoveryCallId ?? drawerItem.callId}
  toolName={drawerItem.toolName}
  isTerminal={isBashTool(drawerItem)}
  onClose={() => { outputDrawerOpen = false; outputDrawerTarget = null; }}
/>
