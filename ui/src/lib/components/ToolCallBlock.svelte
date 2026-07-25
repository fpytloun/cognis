<script lang="ts">
  import Check from 'lucide-svelte/icons/check';
  import Copy from 'lucide-svelte/icons/copy';
  import { onMount } from 'svelte';
  import { isActiveToolStatus, type ToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineScope } from '$lib/chat-v2/types';
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
  import { delegationToolCallDisplayTitle, managedConversationToolPresentation, memoryToolPresentation, nativeInspectionToolPresentation, skillLoadDisplayName, stepTodoWriteStatusSummary, toolOutputHelperPresentation, webToolPresentation, workflowToolPresentation } from '$lib/tool-call-summary';
  import { formatStepQuestionResponse, normalizeStepQuestionAnswers, normalizeStepQuestions, type StepQuestionAnswer } from '$lib/tool-call-question-set';
  import { formatPatchPreparationProgressLabel, shouldShowPatchPreparationProgress } from '$lib/tool-call-progress';
  import { displayToolName } from '$lib/tools-display';
  import { renderMarkdown } from '$lib/markdown';
  import { cancellationOrigin, cancellationOriginLabel } from '$lib/cancellation-reason';

  let {
    item,
    getToolCall = () => null,
    onViewSession,
    scope,
  } = $props<{
    item: ToolCallTimelineItem;
    getToolCall?: (callId: string) => ToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    scope?: TimelineScope | undefined;
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
  let deliverablePreviewId = $state('');
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
      || (isManagedConversationTool() && isActiveToolStatus(item.status))
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
    if (!(delegationRunning || managedConversationRunning())) return;
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

  function deliverablePreviewUrl(deliverableId: string): string {
    const accessorConversationId = conversationId();
    const query = accessorConversationId
      ? `?accessor_conversation_id=${encodeURIComponent(accessorConversationId)}`
      : '';
    return `/api/v1/deliverables/${encodeURIComponent(deliverableId)}/view${query}`;
  }

  function isApplyPatchTool(target: ToolCallTimelineItem = item): boolean {
    return normalizedToolName(target).includes('applypatch');
  }

  function isDelegateTool(target: ToolCallTimelineItem = item): boolean {
    const name = normalizedToolName(target);
    return name === 'delegate'
      || name === 'retrysubsession'
      || name === 'followupsubsession'
      || name === 'forksubsession'
      || name === 'fork';
  }

  function isManagedConversationTool(target: ToolCallTimelineItem = item): boolean {
    return normalizedToolName(target).startsWith('agentconversation');
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

  function isRichMemoryTool(): boolean {
    return memoryToolPresentation(item) !== null;
  }

  function isRichManagedConversationTool(): boolean {
    return managedConversationToolPresentation(item) !== null;
  }

  function isRichNativeInspectionTool(): boolean {
    return nativeInspectionToolPresentation(item) !== null;
  }

  function isRichWebTool(): boolean {
    return webToolPresentation(item) !== null;
  }

  const sourceToolCall = $derived.by((): ToolCallTimelineItem | null => {
    const presentation = toolOutputHelperPresentation(item);
    if (!presentation) return null;
    return getToolCall(presentation.sourceCallId);
  });

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
    const detail = delegation?.error ?? delegation?.resultSummary ?? delegation?.summary;
    return cancellationOriginLabel(cancellationOrigin(status, detail))
      ?? (status ? status.charAt(0).toUpperCase() + status.slice(1) : '');
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

  const delegationOutputHtml = $derived.by(() => {
    const output = stripDelegateDisplayAnchors(delegationOutputText());
    return output ? renderMarkdown(output) : '';
  });

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

  function managedConversationRunning(): boolean {
    if (!isManagedConversationTool()) return false;
    return isActiveToolStatus(
      managedConversationToolPresentation(item)?.displayStatus || item.status,
    );
  }

  function managedConversationStatusText(status: string): string {
    const presentation = managedConversationToolPresentation(item);
    const detail = presentation?.error ?? presentation?.resultSummary;
    const cancellationLabel = cancellationOriginLabel(cancellationOrigin(status, detail));
    if (cancellationLabel) return cancellationLabel;
    if (!status) return isActiveToolStatus(item.status) ? 'Running' : item.status;
    return status.charAt(0).toUpperCase() + status.slice(1);
  }

  function managedConversationStatusTextClass(status: string, error = ''): string {
    const normalized = status.toLowerCase();
    if (error || normalized === 'failed' || normalized === 'error' || normalized === 'interrupted') return 'text-rose-300';
    if (normalized === 'completed' || normalized === 'complete' || normalized === 'idle' || normalized === 'closed') return 'text-emerald-300';
    return 'text-sky-300';
  }

  function managedConversationStatusDotClass(status: string, error = ''): string {
    const normalized = status.toLowerCase();
    if (error || normalized === 'failed' || normalized === 'error' || normalized === 'interrupted') return 'bg-rose-300';
    if (normalized === 'completed' || normalized === 'complete' || normalized === 'idle' || normalized === 'closed') return 'bg-emerald-300';
    return 'bg-sky-300';
  }

  function managedConversationHref(conversationId: string): string {
    return `/chat/${encodeURIComponent(conversationId)}`;
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

    const memoryPresentation = memoryToolPresentation(target);
    if (memoryPresentation) {
      return truncate(memoryPresentation.summary, 120);
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

    const managedPresentation = managedConversationToolPresentation(target);
    if (managedPresentation) {
      return truncate(managedPresentation.requestText || managedPresentation.resultSummary || managedPresentation.summary, 120);
    }

    const nativePresentation = nativeInspectionToolPresentation(target);
    if (nativePresentation) {
      return truncate(nativePresentation.requestText || nativePresentation.summary, 120);
    }

    const webPresentation = webToolPresentation(target);
    if (webPresentation) {
      return truncate(webPresentation.requestText || webPresentation.summary, 120);
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
    if (isDelegateTool(target) || name.includes('spawn')) {
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
    return shouldShowPatchPreparationProgress(item);
  }

  function preparingPatchText(): string {
    return formatPatchPreparationProgressLabel(item);
  }

  const formattedArguments = $derived.by(() => {
    if (!item.arguments) return '';
    return formatCallArguments(item);
  });

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

  const rawOutputText = $derived(cleanResult(item.result));
  const rawOutputData = $derived(formatOutput(rawOutputText, outputExpanded, item));
  const formattedArgumentsData = $derived(formatMaybeJson(formattedArguments, inputExpanded));
  const originalOutputText = $derived(cleanResult(sourceToolCall?.result));
  const originalOutputData = $derived.by(() =>
    sourceToolCall ? formatOutput(originalOutputText, outputExpanded, sourceToolCall) : emptyFormattedOutput()
  );

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

  function formatOutput(raw: string, showAll: boolean, target: ToolCallTimelineItem = item): {
    html: string | null;
    text: string;
    totalLines: number;
    hiddenCount: number;
  } {
    const json = formatMaybeJson(raw, showAll);
    if (json.html || target.isError || !isReadToolName(target.toolName)) return json;
    const language = inferLanguageFromPath(pathFromToolArguments(target.arguments));
    if (!language) return json;
    return { ...json, html: highlightToolOutput(json.text, language) };
  }

  function emptyFormattedOutput(): {
    html: string | null;
    text: string;
    totalLines: number;
    hiddenCount: number;
  } {
    return { html: null, text: '', totalLines: 0, hiddenCount: 0 };
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

  const parsedToolResult = $derived.by((): Record<string, unknown> | null => {
    if (item.result == null) return null;
    try {
      const parsed = JSON.parse(cleanResult(item.result));
      return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  });

  function stepRequestResponse(): string {
    return formatStepQuestionResponse(item, parsedToolResult);
  }

  function stepRequestAnswers(): StepQuestionAnswer[] {
    return normalizeStepQuestionAnswers(item, parsedToolResult);
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
    const error = parsedToolResult?.error;
    return typeof error === 'string' ? error : '';
  }

  function commandText(target: ToolCallTimelineItem = item): string {
    return typeof target.arguments?.command === 'string' ? target.arguments.command : target.toolName;
  }

  function terminalTitle(): string {
    return descriptionText() || commandText();
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

  function memoryItemClass(accent: string): string {
    if (accent === 'artifact') return 'border-violet-400/20 bg-violet-500/5';
    if (accent === 'category') return 'border-amber-400/20 bg-amber-500/5';
    return 'border-cyan-400/15 bg-slate-950/35';
  }

  function memoryItemTitleClass(accent: string): string {
    if (accent === 'artifact') return 'text-violet-100';
    if (accent === 'category') return 'text-amber-100';
    return 'text-cyan-50';
  }

  function nativeInspectionToneClass(error = ''): string {
    return error ? 'border-rose-500/30 bg-rose-500/5' : 'border-sky-500/25 bg-sky-500/5';
  }

  function nativeInspectionHeadingClass(error = ''): string {
    return error ? 'text-rose-300' : 'text-sky-300';
  }

  function nativeReadLineHtml(content: string, path: string): string {
    const language = inferLanguageFromPath(path);
    return highlightToolOutput(content, language ?? 'plaintext');
  }

  function savedMemoryItemLabel(items: Array<{ accent: string }>, index: number): string {
    const memoryCount = items.filter((item) => item.accent === 'memory').length;
    const current = items[index];
    if (current?.accent === 'artifact') return 'Attached artifact';
    if (current?.accent !== 'memory') return 'Detail';
    if (memoryCount === 1) return 'Saved memory';
    const ordinal = items.slice(0, index + 1).filter((item) => item.accent === 'memory').length;
    return `Saved memory ${ordinal}`;
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
                  {@html delegationOutputHtml}
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
                    <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formattedArguments}</pre>
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
      {#if isManagedConversationTool()}
        {@const managedPresentation = managedConversationToolPresentation(item)}
        {#if managedPresentation}
          {@const displayStatus = managedPresentation.displayStatus}
          <div class={`overflow-hidden rounded-2xl border ${managedPresentation.error ? 'border-rose-500/30 bg-rose-500/5' : 'border-violet-400/25 bg-violet-500/5'} text-sm text-slate-100 shadow-inner`}>
            <div class={`flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3 ${managedPresentation.error ? 'border-rose-500/15' : 'border-violet-400/15'}`}>
              <div class="min-w-0 flex-1 space-y-1.5">
                <p class={`text-[11px] font-medium uppercase tracking-[0.22em] ${managedPresentation.error ? 'text-rose-300' : 'text-violet-200'}`}>Managed conversation</p>
                <h4 class="truncate text-base font-semibold text-slate-50" title={managedPresentation.requestText || managedPresentation.title}>{managedPresentation.title}</h4>
                <div class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-400">
                  {#if managedPresentation.primaryConversation?.agentId}
                    <span class="font-mono text-slate-300">{managedPresentation.primaryConversation.agentId}</span>
                    <span class="text-slate-600">·</span>
                  {/if}
                  {#if managedPresentation.primaryConversation?.conversationId}
                    <span class="font-mono text-slate-400" title={managedPresentation.primaryConversation.conversationId}>{compactSessionId(managedPresentation.primaryConversation.conversationId)}</span>
                    <span class="text-slate-600">·</span>
                  {/if}
                  <span class={`inline-flex items-center gap-1.5 font-medium ${managedConversationStatusTextClass(displayStatus, managedPresentation.error)}`}>
                    <span class={`h-1.5 w-1.5 rounded-full ${managedConversationStatusDotClass(displayStatus, managedPresentation.error)}`}></span>
                    {managedConversationStatusText(displayStatus)}
                  </span>
                  {#if managedConversationRunning()}
                    <span class="text-slate-600">·</span>
                    <span class="inline-flex items-center gap-1.5 text-sky-300">
                      <LiveDots inline={true} size="sm" tone="sky" />
                    </span>
                  {:else if item.durationMs != null}
                    <span class="text-slate-600">·</span>
                    <span class="tabular-nums text-slate-400">{formatDurationMs(item.durationMs)}</span>
                  {/if}
                </div>
              </div>
              <div class="flex shrink-0 flex-wrap items-center gap-2">
                {#if managedPresentation.primaryConversation?.conversationId}
                  <a
                    class="rounded-lg border border-violet-300/30 bg-violet-500/10 px-3 py-1.5 text-xs font-medium text-violet-50 transition hover:border-violet-200/60 hover:bg-violet-500/20"
                    href={managedConversationHref(managedPresentation.primaryConversation.conversationId)}
                  >
                    Open conversation
                  </a>
                {/if}
              </div>
            </div>

            <div class="space-y-3 px-4 py-3">
              {#if managedPresentation.todos.length > 0 || managedPresentation.toolCallCount != null || managedPresentation.lastTool}
                <div class="flex min-w-0 flex-wrap items-center gap-2 text-xs text-slate-400">
                  {#if managedPresentation.todos.length > 0}
                    <TodoProgressPopover todos={managedPresentation.todos} label="Managed conversation todo progress" />
                    <span>{managedPresentation.todoSummary}</span>
                  {/if}
                  {#if managedPresentation.toolCallCount != null}
                    {#if managedPresentation.todos.length > 0}<span class="text-slate-600">·</span>{/if}
                    <span class="tabular-nums">{managedPresentation.toolCallCount} tool calls</span>
                  {/if}
                  {#if managedPresentation.lastTool}
                    {#if managedPresentation.todos.length > 0 || managedPresentation.toolCallCount != null}<span class="text-slate-600">·</span>{/if}
                    <span class="min-w-0 truncate">last: <span class="font-mono text-slate-300">{managedPresentation.lastTool}</span></span>
                  {/if}
                </div>
              {/if}
              {#if managedPresentation.resultSummary}
                <div class={`rounded-xl border px-3 py-2 text-xs leading-5 ${managedPresentation.error ? 'border-rose-400/25 bg-rose-500/10 text-rose-100' : 'border-violet-300/15 bg-slate-950/35 text-slate-200'}`}>
                  <p class="line-clamp-5 whitespace-pre-wrap">{managedPresentation.resultSummary}</p>
                </div>
              {/if}

              {#if managedPresentation.conversations.length > 1}
                <div class="space-y-2">
                  <p class="text-[10px] font-semibold uppercase tracking-[0.18em] text-violet-100/60">Conversations</p>
                  <div class="grid gap-2">
                    {#each managedPresentation.conversations as conversation}
                      <div class="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-violet-300/15 bg-slate-950/30 px-3 py-2 text-xs">
                        <div class="min-w-0">
                          <p class="truncate font-medium text-slate-100" title={conversation.title || conversation.conversationId}>{conversation.title || conversation.conversationId || 'Managed conversation'}</p>
                          <p class="mt-0.5 flex min-w-0 flex-wrap items-center gap-1.5 text-slate-400">
                            {#if conversation.agentId}<span class="font-mono">{conversation.agentId}</span>{/if}
                            {#if conversation.conversationId}<span class="font-mono" title={conversation.conversationId}>{compactSessionId(conversation.conversationId)}</span>{/if}
                            {#if conversation.status}<span class={managedConversationStatusTextClass(conversation.status, conversation.error)}>{managedConversationStatusText(conversation.status)}</span>{/if}
                          </p>
                        </div>
                        {#if conversation.conversationId}
                          <a class="rounded-lg border border-slate-600/70 bg-slate-900/70 px-2.5 py-1 text-[11px] font-medium text-slate-100 transition hover:border-violet-300/50 hover:bg-violet-500/10" href={managedConversationHref(conversation.conversationId)}>Open</a>
                        {/if}
                      </div>
                    {/each}
                  </div>
                </div>
              {/if}

              {#if managedPresentation.requestDetails.length > 0}
                <dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
                  {#each managedPresentation.requestDetails.slice(0, 4) as detail}
                    <dt class="text-violet-200/50">{detail.key}</dt>
                    <dd class="min-w-0 truncate text-violet-50/85" title={String(detail.value)}>{String(detail.value)}</dd>
                  {/each}
                </dl>
              {/if}
              {#if managedPresentation.primaryConversation?.controllerConversationId || managedPresentation.primaryConversation?.followUpConversationId}
                <div class="flex flex-wrap gap-3 text-xs">
                  {#if managedPresentation.primaryConversation?.controllerConversationId}
                    <a class="font-medium text-violet-200 underline decoration-violet-400/40 underline-offset-2 transition hover:text-violet-100" href={managedConversationHref(managedPresentation.primaryConversation.controllerConversationId)}>Controller conversation</a>
                  {/if}
                  {#if managedPresentation.primaryConversation?.followUpConversationId}
                    <a class="font-medium text-violet-200 underline decoration-violet-400/40 underline-offset-2 transition hover:text-violet-100" href={managedConversationHref(managedPresentation.primaryConversation.followUpConversationId)}>Follow-up conversation</a>
                  {/if}
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
                      <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formattedArguments}</pre>
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
                    <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formattedArguments}</pre>
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
        {#if isRichWebTool()}
          {@const webPresentation = webToolPresentation(item)}
          {#if webPresentation}
            <div class={`overflow-hidden rounded-2xl border ${webPresentation.error ? 'border-rose-500/30 bg-rose-500/5' : 'border-violet-500/25 bg-violet-500/5'}`}>
              <div class={`flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3 ${webPresentation.error ? 'border-rose-500/15' : 'border-violet-500/15'}`}>
                <div class="min-w-0 flex-1">
                  <p class={`text-xs font-medium uppercase tracking-widest ${webPresentation.error ? 'text-rose-300' : 'text-violet-300'}`}>{webPresentation.title}</p>
                  {#if webPresentation.requestText}
                    <a href={webPresentation.webKind === 'fetch' ? webPresentation.requestText : undefined} target="_blank" rel="noreferrer" class={`mt-1 block truncate text-sm font-semibold ${webPresentation.error ? 'text-rose-50' : 'text-violet-50'} ${webPresentation.webKind === 'fetch' ? 'hover:underline' : ''}`}>{webPresentation.requestText}</a>
                  {/if}
                  <p class={`mt-2 text-xs leading-5 ${webPresentation.error ? 'text-rose-100/80' : 'text-violet-100/80'}`}>{webPresentation.summary}</p>
                </div>
                {#if webPresentation.badges.length > 0}
                  <div class={`flex max-w-full flex-wrap justify-end gap-2 text-[11px] ${webPresentation.error ? 'text-rose-100/75' : 'text-violet-100/75'}`}>
                    {#each webPresentation.badges as badge}
                      <span class={`rounded-full border px-2 py-0.5 ${webPresentation.error ? 'border-rose-400/25' : 'border-violet-400/25'}`}>{badge}</span>
                    {/each}
                  </div>
                {/if}
              </div>

              {#if webPresentation.error}
                <p class="m-4 rounded-xl border border-rose-400/25 bg-rose-500/10 px-3 py-2 text-sm leading-6 text-rose-100">{webPresentation.error}</p>
              {/if}

              {#if webPresentation.answer}
                <section class="border-t border-violet-500/15 px-4 py-3">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-violet-300">Answer</p>
                  <div class="prose prose-sm prose-invert max-w-none rounded-xl border border-violet-400/15 bg-slate-950/35 px-3 py-2 text-slate-100">
                    {@html renderMarkdown(webPresentation.answer)}
                  </div>
                </section>
              {/if}

              {#if webPresentation.results.length > 0}
                <section class="space-y-2 border-t border-violet-500/15 px-4 py-3">
                  <p class="text-xs font-medium uppercase tracking-widest text-violet-300">Results</p>
                  {#each webPresentation.results as result, index (`${result.url}:${index}`)}
                    <article class="rounded-xl border border-violet-400/15 bg-slate-950/30 px-3 py-2">
                      <div class="flex flex-wrap items-start justify-between gap-2">
                        <a href={result.url} target="_blank" rel="noreferrer" class="min-w-0 text-sm font-semibold text-slate-100 hover:text-violet-200 hover:underline [overflow-wrap:anywhere]">{result.title}</a>
                        {#if result.domain || result.score}
                          <div class="flex gap-2 text-[10px] text-slate-400">
                            {#if result.domain}<span>{result.domain}</span>{/if}
                            {#if result.score}<span>{result.score}</span>{/if}
                          </div>
                        {/if}
                      </div>
                      {#if result.snippet}<p class="mt-2 text-xs leading-5 text-slate-300">{result.snippet}</p>{/if}
                    </article>
                  {/each}
                </section>
              {/if}

              {#if webPresentation.content}
                <section class="border-t border-violet-500/15 px-4 py-3">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-violet-300">Extracted content</p>
                  <pre class="max-h-[36vh] overflow-auto whitespace-pre-wrap rounded-xl border border-violet-400/15 bg-slate-950/35 px-3 py-2 text-xs leading-5 text-slate-100">{webPresentation.content}</pre>
                </section>
              {/if}

              {#if webPresentation.media.length > 0}
                <section class="space-y-2 border-t border-violet-500/15 px-4 py-3">
                  <p class="text-xs font-medium uppercase tracking-widest text-violet-300">Image references</p>
                  <div class="grid gap-2 md:grid-cols-2">
                    {#each webPresentation.media as media, index (`${media.url}:${index}`)}
                      <article class="rounded-xl border border-violet-400/15 bg-slate-950/30 px-3 py-2">
                        <a href={media.url} target="_blank" rel="noreferrer" class="block truncate text-sm font-medium text-slate-100 hover:text-violet-200 hover:underline" title={media.url}>{media.label}</a>
                        {#if media.source || media.sourcePageUrl}
                          <p class="mt-1 truncate text-[11px] text-slate-400">{media.sourcePageUrl || media.source}</p>
                        {/if}
                        {#if media.artifactRef}
                          <p class="mt-2 font-mono text-[10px] text-violet-100/75" title={media.artifactRef}>lazy artifact available</p>
                        {/if}
                      </article>
                    {/each}
                  </div>
                </section>
              {/if}

              {#if canOpenToolOutput(item) && scope}
                <div class="border-t border-violet-500/15 px-4 py-3">
                  <button class="rounded-lg border border-violet-500/30 bg-violet-500/10 px-3 py-1.5 text-xs font-medium text-violet-100 hover:bg-violet-500/20" type="button" onclick={() => { openToolOutput(item); }}>
                    {toolOutputOpenLabel(item)}
                  </button>
                </div>
              {/if}
            </div>
          {/if}
        {:else if isRichNativeInspectionTool()}
          {@const nativePresentation = nativeInspectionToolPresentation(item)}
          {#if nativePresentation}
            <div class={`overflow-hidden rounded-2xl border ${nativeInspectionToneClass(nativePresentation.error)} text-sm text-slate-100 shadow-inner`}>
              <div class={`flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3 ${nativePresentation.error ? 'border-rose-500/15' : 'border-sky-500/15'}`}>
                <div class="min-w-0 flex-1">
                  <p class={`text-xs font-medium uppercase tracking-widest ${nativeInspectionHeadingClass(nativePresentation.error)}`}>Native tool output</p>
                  <h4 class="mt-1 truncate text-sm font-semibold text-slate-50" title={nativePresentation.requestText || nativePresentation.title}>{nativePresentation.title}</h4>
                  <p class="mt-2 text-xs leading-5 text-slate-300">{nativePresentation.summary}</p>
                </div>
                {#if nativePresentation.badges.length > 0}
                  <div class="flex max-w-full flex-wrap justify-end gap-2 text-[11px] text-sky-100/75">
                    {#each nativePresentation.badges as badge}
                      <span class="rounded-full border border-sky-400/25 px-2 py-0.5">{badge}</span>
                    {/each}
                  </div>
                {/if}
              </div>

              <div class="space-y-3 px-4 py-3">
                <section class="rounded-xl border border-sky-400/15 bg-slate-950/30 px-3 py-2">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-sky-300">{nativePresentation.requestLabel}</p>
                  {#if nativePresentation.requestText}
                    <p class="font-mono text-sm text-slate-100 [overflow-wrap:anywhere]">{nativePresentation.requestText}</p>
                  {:else}
                    <p class="text-sm text-slate-400">No request summary was recorded.</p>
                  {/if}
                  {#if nativePresentation.requestDetails.length > 0}
                    <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-slate-400">
                      {#each nativePresentation.requestDetails.slice(0, 6) as detail}
                        <span class="rounded-full border border-slate-700/80 bg-slate-950/35 px-2 py-0.5">
                          <span class="text-slate-500">{detail.key}:</span>
                          <span class="ml-1 text-slate-200">{formatStructuredValue(detail.value)}</span>
                        </span>
                      {/each}
                    </div>
                  {/if}
                </section>

                {#if nativePresentation.error}
                  <div class="rounded-xl border border-rose-400/25 bg-rose-500/10 px-3 py-2 text-xs leading-5 text-rose-100">{nativePresentation.error}</div>
                {:else if nativePresentation.nativeKind === 'read' && nativePresentation.readLines.length > 0}
                  <section class="overflow-hidden rounded-xl border border-sky-400/15 bg-slate-950/35">
                    <div class="flex items-center justify-between gap-2 border-b border-sky-400/10 px-3 py-2">
                      <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-sky-100/60">File content</span>
                      {#if nativePresentation.path}
                        <span class="truncate font-mono text-[11px] text-slate-400" title={nativePresentation.path}>{nativePresentation.path}</span>
                      {/if}
                    </div>
                    <div class="max-h-[46vh] overflow-auto">
                      <table class="w-full border-collapse font-mono text-xs leading-5">
                        <tbody>
                          {#each nativePresentation.readLines as line}
                            <tr class="align-top hover:bg-slate-900/60">
                              <td class="select-none border-r border-slate-800/80 bg-slate-950/70 px-3 py-0 text-right tabular-nums text-slate-500">{line.lineNumber}</td>
                              <td class="min-w-0 px-3 py-0 text-slate-200">
                                <pre class="m-0 whitespace-pre">{@html nativeReadLineHtml(line.content, nativePresentation.path)}</pre>
                              </td>
                            </tr>
                          {/each}
                        </tbody>
                      </table>
                    </div>
                  </section>
                {:else if nativePresentation.grepGroups.length > 0}
                  <section class="space-y-2">
                    <p class="text-[10px] font-semibold uppercase tracking-[0.18em] text-sky-100/60">Matches</p>
                    {#each nativePresentation.grepGroups as group}
                      <article class="overflow-hidden rounded-xl border border-sky-400/15 bg-slate-950/35">
                        <div class="border-b border-sky-400/10 px-3 py-2">
                          <p class="truncate font-mono text-xs text-sky-100" title={group.path}>{group.path}</p>
                        </div>
                        <div class="max-h-[32vh] overflow-auto">
                          <table class="w-full border-collapse font-mono text-xs leading-5">
                            <tbody>
                              {#each group.matches as match}
                                <tr class={`${match.isMatch ? 'text-slate-100' : 'text-slate-400'} hover:bg-slate-900/60`}>
                                  <td class="select-none border-r border-slate-800/80 bg-slate-950/70 px-3 py-0 text-right tabular-nums text-slate-500">{match.lineNumber}</td>
                                  <td class="px-3 py-0 whitespace-pre">{match.text}</td>
                                </tr>
                              {/each}
                            </tbody>
                          </table>
                        </div>
                      </article>
                    {/each}
                  </section>
                {:else if nativePresentation.pathEntries.length > 0}
                  <section class="overflow-hidden rounded-xl border border-sky-400/15 bg-slate-950/35">
                    <div class="flex items-center justify-between gap-2 border-b border-sky-400/10 px-3 py-2">
                      <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-sky-100/60">Paths</span>
                      <span class="text-[11px] text-slate-500">{nativePresentation.pathEntries.length} item{nativePresentation.pathEntries.length === 1 ? '' : 's'}</span>
                    </div>
                    <ul class="max-h-[42vh] divide-y divide-slate-800/60 overflow-auto">
                      {#each nativePresentation.pathEntries as entry}
                        <li class="flex min-w-0 items-center gap-2 px-3 py-1.5 font-mono text-xs text-slate-200">
                          <span class="shrink-0 text-slate-500">{entry.kind === 'directory' ? 'dir' : entry.kind === 'file' ? 'file' : 'path'}</span>
                          <span class="min-w-0 truncate" title={entry.path}>{entry.path}</span>
                        </li>
                      {/each}
                    </ul>
                  </section>
                {:else if nativePresentation.outputText}
                  <section class="rounded-xl border border-sky-400/15 bg-slate-950/35">
                    <div class="border-b border-sky-400/10 px-3 py-2">
                      <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-sky-100/60">Output</span>
                    </div>
                    <pre class="max-h-[42vh] overflow-auto whitespace-pre-wrap px-3 py-2 text-xs leading-5 text-slate-200">{nativePresentation.outputText}</pre>
                  </section>
                {/if}

                {#if nativePresentation.footer}
                  <p class="rounded-xl border border-slate-700/70 bg-slate-950/35 px-3 py-2 text-xs leading-5 text-slate-400">{nativePresentation.footer}</p>
                {/if}

                {#if canOpenToolOutput(item) && scope}
                  <button class="rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-100 hover:bg-sky-500/20" type="button" onclick={() => { openToolOutput(item); }}>
                    {toolOutputOpenLabel(item)}
                  </button>
                {/if}
              </div>
            </div>

            {#if hasRawPayload()}
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
                        <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formattedArguments}</pre>
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
        {:else if isRichMemoryTool()}
          {@const memoryPresentation = memoryToolPresentation(item)}
          {#if memoryPresentation}
            <div class={`overflow-hidden rounded-2xl border ${memoryPresentation.error ? 'border-rose-500/30 bg-rose-500/5' : 'border-cyan-500/25 bg-cyan-500/5'}`}>
              <div class={`flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3 ${memoryPresentation.error ? 'border-rose-500/15' : 'border-cyan-500/15'}`}>
                <div class="min-w-0 flex-1">
                  <p class={`text-xs font-medium uppercase tracking-widest ${memoryPresentation.error ? 'text-rose-300' : 'text-cyan-300'}`}>Memory</p>
                  <h4 class={`mt-1 truncate text-sm font-semibold ${memoryPresentation.error ? 'text-rose-50' : 'text-cyan-50'}`}>{memoryPresentation.title}</h4>
                  <p class={`mt-2 text-xs leading-5 ${memoryPresentation.error ? 'text-rose-100/80' : 'text-cyan-100/80'}`}>{memoryPresentation.summary}</p>
                </div>
                {#if memoryPresentation.badges.length > 0}
                  <div class={`flex max-w-full flex-wrap justify-end gap-2 text-[11px] ${memoryPresentation.error ? 'text-rose-100/75' : 'text-cyan-100/75'}`}>
                    {#each memoryPresentation.badges as badge}
                      <span class={`rounded-full border px-2 py-0.5 ${memoryPresentation.error ? 'border-rose-400/25' : 'border-cyan-400/25'}`}>{badge}</span>
                    {/each}
                  </div>
                {/if}
              </div>

              {#if memoryPresentation.variant === 'saved'}
                <section class="space-y-3 px-4 py-3">
                  {#if memoryPresentation.error}
                    <div class="rounded-xl border border-rose-400/25 bg-rose-500/10 px-3 py-2">
                      <p class="text-sm leading-6 text-rose-100">{memoryPresentation.resultSummary}</p>
                      <p class="mt-2 text-xs leading-5 text-rose-100/85">{memoryPresentation.error}</p>
                    </div>
                  {:else if memoryPresentation.resultItems.length > 0}
                    {#each memoryPresentation.resultItems as resultItem, index (`${resultItem.title}:${index}`)}
                      <article class={`rounded-xl border px-3 py-3 ${memoryItemClass(resultItem.accent)}`}>
                        <div class="flex flex-wrap items-start justify-between gap-2">
                          <p class="text-xs font-medium uppercase tracking-widest text-cyan-300">
                            {savedMemoryItemLabel(memoryPresentation.resultItems, index)}
                          </p>
                          {#if resultItem.title && resultItem.title !== 'Memory'}
                            <span class="rounded-full border border-cyan-400/20 bg-slate-950/30 px-2 py-0.5 font-mono text-[10px] text-cyan-100/80">{resultItem.title}</span>
                          {/if}
                        </div>
                        {#if resultItem.body}
                          <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-100 [overflow-wrap:anywhere]">{resultItem.body}</p>
                        {:else if memoryPresentation.requestText}
                          <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-100 [overflow-wrap:anywhere]">{memoryPresentation.requestText}</p>
                        {/if}
                        {#if resultItem.meta.length > 0}
                          <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-slate-400">
                            {#each resultItem.meta as entry}
                              <span class="rounded-full border border-slate-700/80 bg-slate-950/35 px-2 py-0.5">
                                <span class="text-slate-500">{entry.key}:</span>
                                <span class="ml-1 text-slate-200">{formatStructuredValue(entry.value)}</span>
                              </span>
                            {/each}
                          </div>
                        {/if}
                      </article>
                    {/each}
                  {:else}
                    <article class="rounded-xl border border-cyan-400/15 bg-slate-950/30 px-3 py-3">
                      <p class="text-xs font-medium uppercase tracking-widest text-cyan-300">{memoryPresentation.requestLabel}</p>
                      {#if memoryPresentation.requestText}
                        <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-100 [overflow-wrap:anywhere]">{memoryPresentation.requestText}</p>
                      {:else}
                        <p class="mt-2 text-sm leading-6 text-slate-400">Memory save completed.</p>
                      {/if}
                    </article>
                  {/if}
                </section>
              {:else}
                <div class="grid gap-3 px-4 py-3 md:grid-cols-2">
                  <section class={`rounded-xl border px-3 py-2 ${memoryPresentation.error ? 'border-rose-400/15 bg-slate-950/30' : 'border-cyan-400/15 bg-slate-950/30'}`}>
                    <p class={`mb-2 text-xs font-medium uppercase tracking-widest ${memoryPresentation.error ? 'text-rose-300' : 'text-cyan-300'}`}>{memoryPresentation.requestLabel}</p>
                    {#if memoryPresentation.requestText}
                      <p class="whitespace-pre-wrap text-sm leading-6 text-slate-100 [overflow-wrap:anywhere]">{memoryPresentation.requestText}</p>
                    {:else}
                      <p class="text-sm leading-6 text-slate-400">No request summary was recorded.</p>
                    {/if}
                    {#if memoryPresentation.requestDetails.length > 0}
                      <dl class="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                        {#each memoryPresentation.requestDetails as entry}
                          <div>
                            <dt class={`font-medium uppercase tracking-wider ${memoryPresentation.error ? 'text-rose-100/70' : 'text-cyan-100/70'}`}>{entry.key}</dt>
                            <dd class="mt-1 whitespace-pre-wrap text-slate-100 [overflow-wrap:anywhere]">{formatStructuredValue(entry.value)}</dd>
                          </div>
                        {/each}
                      </dl>
                    {/if}
                  </section>

                  <section class={`rounded-xl border px-3 py-2 ${memoryPresentation.error ? 'border-rose-400/15 bg-rose-500/10' : 'border-cyan-400/15 bg-slate-950/30'}`}>
                    <p class={`mb-2 text-xs font-medium uppercase tracking-widest ${memoryPresentation.error ? 'text-rose-300' : 'text-cyan-300'}`}>{memoryPresentation.resultLabel}</p>
                    <p class={`text-sm leading-6 ${memoryPresentation.error ? 'text-rose-100' : 'text-slate-100'}`}>{memoryPresentation.resultSummary}</p>
                    {#if memoryPresentation.error}
                      <p class="mt-2 rounded-lg border border-rose-400/25 bg-rose-500/10 px-2 py-1.5 text-xs leading-5 text-rose-100">{memoryPresentation.error}</p>
                    {/if}
                    {#if memoryPresentation.resultDetails.length > 0}
                      <dl class="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                        {#each memoryPresentation.resultDetails as entry}
                          <div>
                            <dt class={`font-medium uppercase tracking-wider ${memoryPresentation.error ? 'text-rose-100/70' : 'text-cyan-100/70'}`}>{entry.key}</dt>
                            <dd class="mt-1 whitespace-pre-wrap text-slate-100 [overflow-wrap:anywhere]">{formatStructuredValue(entry.value)}</dd>
                          </div>
                        {/each}
                      </dl>
                    {/if}
                  </section>
                </div>

                {#if memoryPresentation.answer}
                  <section class="border-t border-cyan-500/15 px-4 py-3">
                    <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-300">Answer</p>
                    <div class="prose prose-sm prose-invert max-w-none rounded-xl border border-cyan-400/15 bg-slate-950/35 px-3 py-2 text-slate-100">
                      {@html renderMarkdown(memoryPresentation.answer)}
                    </div>
                  </section>
                {/if}

                {#if memoryPresentation.text}
                  <section class="border-t border-cyan-500/15 px-4 py-3">
                    <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-300">Text</p>
                    <pre class="max-h-[36vh] overflow-auto whitespace-pre-wrap rounded-xl border border-cyan-400/15 bg-slate-950/35 px-3 py-2 text-xs leading-5 text-slate-100">{memoryPresentation.text}</pre>
                  </section>
                {/if}

                {#if memoryPresentation.resultItems.length > 0}
                  <section class="space-y-2 border-t border-cyan-500/15 px-4 py-3">
                    <p class="text-xs font-medium uppercase tracking-widest text-cyan-300">Items</p>
                    {#each memoryPresentation.resultItems as resultItem, index (`${resultItem.title}:${index}`)}
                      <article class={`rounded-xl border px-3 py-2 ${memoryItemClass(resultItem.accent)}`}>
                        <div class="flex flex-wrap items-start justify-between gap-2">
                          <h5 class={`min-w-0 text-sm font-semibold [overflow-wrap:anywhere] ${memoryItemTitleClass(resultItem.accent)}`}>{resultItem.title}</h5>
                          <span class="rounded-full border border-slate-600/70 px-2 py-0.5 text-[10px] uppercase tracking-wider text-slate-300">{resultItem.accent}</span>
                        </div>
                        {#if resultItem.body}
                          <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-200 [overflow-wrap:anywhere]">{resultItem.body}</p>
                        {/if}
                        {#if resultItem.meta.length > 0}
                          <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-400">
                            {#each resultItem.meta as entry}
                              <span class="rounded-full border border-slate-700/80 bg-slate-950/35 px-2 py-0.5">
                                <span class="text-slate-500">{entry.key}:</span>
                                <span class="ml-1 text-slate-200">{formatStructuredValue(entry.value)}</span>
                              </span>
                            {/each}
                          </div>
                        {/if}
                      </article>
                    {/each}
                  </section>
                {/if}
              {/if}
            </div>

            {#if hasRawPayload()}
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
                        <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formattedArguments}</pre>
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
        {:else if isRichToolOutputHelper()}
          {@const outputHelper = toolOutputHelperPresentation(item)}
          {@const originalCall = sourceToolCall}
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
                    {#if canOpenToolOutput(originalCall) && scope}
                      <button class="mt-3 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-100 hover:bg-sky-500/20" type="button" onclick={() => { openToolOutput(originalCall); }}>
                        Open original output
                      </button>
                    {/if}
                  </div>
                {:else}
                  <div class="rounded-xl border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs leading-5 text-amber-100">
                    <p>The original tool call is not present in the loaded timeline page. The referenced call ID is <span class="font-mono">{outputHelper.sourceCallId}</span>.</p>
                    {#if scope}
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
                        <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formattedArguments}</pre>
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
            <div class="rounded-2xl border border-emerald-500/25 bg-emerald-500/5 px-4 py-3">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="text-xs font-medium uppercase tracking-widest text-emerald-300">Deliverable captured</p>
                  <h4 class="mt-1 truncate text-sm font-semibold text-emerald-50">{workflowPresentation.title}</h4>
                  <p class="mt-2 text-xs leading-5 text-emerald-100/75">{workflowPresentation.note}</p>
                </div>
                <div class="flex flex-wrap gap-2 text-[11px] text-emerald-100/75">
                  <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">{workflowPresentation.format}</span>
                  <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">{workflowPresentation.status}</span>
                  {#if workflowPresentation.length !== null}
                    <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">{workflowPresentation.length.toLocaleString()} chars</span>
                  {/if}
                   {#if workflowPresentation.version !== null}
                     <span class="rounded-full border border-emerald-400/25 px-2 py-0.5">v{workflowPresentation.version}</span>
                   {/if}
                   {#if workflowPresentation.deliverableId}
                     <button
                       class="rounded-full border border-emerald-400/35 bg-emerald-500/10 px-2 py-0.5 font-medium text-emerald-50 transition hover:bg-emerald-500/20"
                       type="button"
                       onclick={() => { deliverablePreviewId = workflowPresentation.deliverableId; }}
                     >
                       View deliverable
                     </button>
                   {/if}
                 </div>
              </div>
              {#if workflowPresentation.deliverableId || workflowPresentation.outputKeys.length > 0}
                <div class="mt-3 flex flex-wrap gap-3 border-t border-emerald-500/15 pt-2 text-[11px] text-emerald-100/70">
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
                      <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formattedArguments}</pre>
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
              Patch input is streaming from the provider. This will turn into the normal apply_patch call once the patch is complete.
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
            {#if canOpenToolOutput(item) && scope}
              <button class="mt-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-100 hover:bg-sky-500/20" type="button" onclick={() => { pinTerminal(); openToolOutput(item); }}>
                {toolOutputOpenLabel(item)}
              </button>
            {/if}
          </div>
        {/if}

        {#if (!hasDiffs() || rawExpanded) && !isBashTool() && !isDelegateTool() && !isRichWorkflowTool() && !isRichToolOutputHelper() && !isRichMemoryTool() && !isRichManagedConversationTool() && !isRichNativeInspectionTool() && !isRichWebTool()}
          {#if item.arguments && Object.keys(item.arguments).length > 0}
            {@const inputText = formattedArguments}
            {@const inputData = formattedArgumentsData}
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
            {@const outputText = rawOutputText}
            {@const outputData = rawOutputData}
            {#if !isBashTool()}
              <div>
                <p class="mb-1 text-xs font-medium uppercase tracking-widest text-slate-500">Output</p>
                <div class="relative">
                  <pre class={`max-h-[40vh] overflow-auto rounded-lg border bg-slate-950/60 p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-rose-500/30 text-rose-300' : 'border-slate-800/60 text-slate-300'}`}>{#if outputData.html}{@html outputData.html}{:else}{outputData.text}{/if}</pre>
                  <button class="copy-icon-button absolute right-2 top-2" onclick={() => void copyBox('output', outputText)} type="button" title="Copy output" aria-label="Copy output">
                    {#if copiedBox === 'output'}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}
                  </button>
                </div>
                {#if canOpenToolOutput(item) && scope}
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
                <pre class="max-h-[28vh] overflow-auto rounded-lg border border-slate-800/60 bg-slate-950/60 p-3 text-slate-300">{formattedArguments}</pre>
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
  {scope}
  callId={drawerItem.recoveryCallId ?? drawerItem.callId}
  toolName={drawerItem.toolName}
  isTerminal={isBashTool(drawerItem)}
  onClose={() => { outputDrawerOpen = false; outputDrawerTarget = null; }}
/>

{#if deliverablePreviewId}
  <div class="fixed inset-0 z-[2147483000] flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-sm">
    <dialog
      open
      class="flex h-[min(90vh,64rem)] w-[min(96vw,82rem)] flex-col overflow-hidden rounded-2xl border border-slate-700 bg-slate-950 p-0 shadow-2xl"
      aria-label="Deliverable preview"
    >
      <header class="flex items-center justify-between border-b border-slate-800 px-4 py-3">
        <h3 class="text-sm font-semibold text-slate-100">Deliverable preview</h3>
        <button
          class="rounded-lg border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:bg-slate-800"
          type="button"
          onclick={() => { deliverablePreviewId = ''; }}
        >
          Close
        </button>
      </header>
      <iframe
        class="min-h-0 flex-1 bg-slate-950"
        title="Deliverable preview"
        src={deliverablePreviewUrl(deliverablePreviewId)}
      ></iframe>
    </dialog>
  </div>
{/if}
