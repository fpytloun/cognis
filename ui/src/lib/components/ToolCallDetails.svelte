<script lang="ts">
  import { chatNavigation, followConversationLink } from '$lib/chat-v2/navigation';
  const navigation = chatNavigation();
  import Check from 'lucide-svelte/icons/check';
  import Clock from 'lucide-svelte/icons/clock';
  import Copy from 'lucide-svelte/icons/copy';
  import { onMount, untrack, type Snippet } from 'svelte';
  import ToolCallHeader from './ToolCallHeader.svelte';
  import DeliverableReferenceActions from './DeliverableReferenceActions.svelte';
  import {
    toolCallSubtitle as headerSubtitle, toolDetailsStartExpanded,
    isDelegationTool, delegationIsActive, delegationIsQueued, toolFrameClass,
  } from '$lib/tool-call-header';
  import { isActiveToolStatus, type ToolCallTimelineItem } from '$lib/timeline-render-model';
  import type { TimelineScope } from '$lib/chat-v2/types';
  import DeferredPrivateView from './ui/DeferredPrivateView.svelte';
  import { feedbackDiffViewer } from './feedback/diff-viewer-definition';
  import LiveDots from '$lib/components/LiveDots.svelte';
  import MessageAttachments from '$lib/components/MessageAttachments.svelte';
  import TodoProgressPopover from '$lib/components/TodoProgressPopover.svelte';
  import TodoStatusDot from '$lib/components/TodoStatusDot.svelte';
  import { visibleTodos as activeVisibleTodos } from '$lib/todos';
  import { addToast } from '$lib/stores/toasts';
  import { highlightJson, looksLikeJson, prettyPrintJson } from '$lib/syntax/json';
  import { highlightToolOutput, inferLanguageFromPath, isReadToolName, pathFromToolArguments } from '$lib/syntax/tool-output';
  import { formatAbsoluteTime, formatCompactTime } from '$lib/time';
  import { canOpenToolOutput, toolOutputOpenLabel } from '$lib/tool-output-status';
  import { delegationToolCallDisplayTitle, managedConversationStatusIsRunning, managedConversationToolPresentation, memoryToolPresentation, nativeInspectionToolPresentation, skillLoadDisplayName, stepTodoWriteStatusSummary, toolOutputHelperPresentation, webToolPresentation, workflowToolPresentation } from '$lib/tool-call-summary';
  import { formatStepQuestionResponse, normalizeStepQuestionAnswers, normalizeStepQuestions, type StepQuestionAnswer } from '$lib/tool-call-question-set';
  import { formatPatchPreparationProgressLabel, shouldShowPatchPreparationProgress } from '$lib/tool-call-progress';
  import { displayToolName } from '$lib/tools-display';
  import { renderMarkdown } from '$lib/markdown';
  import { cancellationOrigin, cancellationOriginLabel } from '$lib/cancellation-reason';
  import { chatV2Api } from '$lib/chat-v2/api';
  import { backgroundableToolCallConversationId } from '$lib/tool-call-background';

  let {
    item,
    getToolCall = () => null,
    onViewSession,
    scope,
    density = 'default',
    summaryMode = 'default',
    compactLabelMode,
    contextLabel,
    contextSessionId,
    initialExpanded,
    focusHeader = false,
    detailsContent,
  } = $props<{
    item: ToolCallTimelineItem;
    getToolCall?: (callId: string) => ToolCallTimelineItem | null;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    scope?: TimelineScope | undefined;
    density?: 'default' | 'compact';
    summaryMode?: 'default' | 'command';
    compactLabelMode?: 'command' | 'description' | undefined;
    contextLabel?: string | undefined;
    contextSessionId?: string | undefined;
    initialExpanded?: boolean;
    focusHeader?: boolean;
    detailsContent?: Snippet;
  }>();

  type StructuredEntry = { key: string; value: unknown };

  const requestedInitialExpansion = untrack(() => initialExpanded);
  let expanded = $state(requestedInitialExpansion ?? false);
  let inputExpanded = $state(false);
  let outputExpanded = $state(false);
  let rawExpanded = $state(false);
  let originalCallExpanded = $state(false);
  let evalExpanded = $state(false);
  let completedQuestionPage = $state(0);
  let autoExpanded = $state(requestedInitialExpansion !== undefined);
  let terminalPinned = $state(false);
  let outputDrawerOpen = $state(false);
  let outputDrawerLoaded = $state(false);
  let terminalTailing = $state(true);
  let terminalEl = $state<HTMLPreElement | null>(null);
  let TerminalOutputComponent = $state<null | typeof import('$lib/components/TerminalOutputView.svelte').default>(null);
  let outputDrawerTarget = $state<ToolCallTimelineItem | null>(null);
  let copiedBox = $state<'input' | 'output' | null>(null);
  let delegateAutoExpanded = $state(requestedInitialExpansion === true && isDelegateTool() && isDelegationActive());
  let bashExpandTimer: number | null = null;
  let bashCollapseTimer: number | null = null;
  let bashAutoExpanded = false;
  let copyResetTimer: number | null = null;
  let timingNowMs = $state(Date.now());
  let nowDate = $state(new Date());
  const drawerItem = $derived(outputDrawerTarget ?? item);
  let backgroundRequest = $state<'idle' | 'pending' | 'requested'>('idle');
  const backgroundConversationId = $derived(backgroundableToolCallConversationId(item, scope));

  $effect(() => {
    if (!backgroundConversationId && backgroundRequest !== 'idle') backgroundRequest = 'idle';
  });

  async function moveToBackground(): Promise<void> {
    const conversationId = backgroundConversationId;
    if (!conversationId || backgroundRequest !== 'idle') return;
    pinTerminal();
    backgroundRequest = 'pending';
    try {
      await chatV2Api.backgroundToolCall(conversationId, item.callId);
      backgroundRequest = 'requested';
    } catch (error) {
      backgroundRequest = 'idle';
      const message = error instanceof Error && error.message ? error.message : 'Request failed';
      addToast(message, 'error', 6_000, 'Could not move command to background');
    }
  }

  const LINES_PER_PAGE = 50;
  const BASH_AUTO_EXPAND_DELAY_MS = 10_000;
  const BASH_AUTO_COLLAPSE_DELAY_MS = 4000;
  const startsExpanded = $derived(toolDetailsStartExpanded(item));

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
    if (!(isActiveToolStatus(item.status) || delegationRunning || managedConversationRunning())) return;
    const updateNow = () => {
      const now = Date.now();
      timingNowMs = now;
      nowDate = new Date(now);
    };
    updateNow();
    const timer = window.setInterval(() => {
      updateNow();
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
      TerminalOutputComponent;
      terminalEl.scrollTop = terminalEl.scrollHeight;
    }
  });

  $effect(() => {
    if (!expanded || !isBashTool() || TerminalOutputComponent) return;
    let active = true;
    void import('$lib/components/TerminalOutputView.svelte').then((module) => {
      if (active) TerminalOutputComponent = module.default;
    });
    return () => {
      active = false;
    };
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

  const expandedContentId = $derived(
    `tool-call-content-${item.callId.replace(/[^a-zA-Z0-9_-]/g, '-')}`
  );

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
    return isDelegationTool(target);
  }

  function isManagedConversationTool(target: ToolCallTimelineItem = item): boolean {
    return normalizedToolName(target).startsWith('agentconversation');
  }

  // Delegation details folded onto the delegate tool call (title/progress/
  // todos/result). Present only for delegate/fork tool calls.
  const delegation = $derived(item.delegation ?? null);
  const delegationVisibleTodos = $derived(activeVisibleTodos(delegation?.todos ?? undefined));
  const delegationRunning = $derived(
    isDelegateTool() && !delegationIsQueued(item) && (isActiveToolStatus(item.status) || isDelegationActive())
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
    return delegationIsActive(item);
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
    if (delegationIsQueued(item)) return 'queued';
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
    if (delegationIsQueued(item)) return 'Not started yet. Waiting for earlier work or an available delegation slot.';
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
    if (delegationIsQueued(item)) return null;
    if (typeof delegation?.durationMs === 'number') return delegation.durationMs;
    if (typeof item.durationMs === 'number' && isDelegationTerminal()) return item.durationMs;
    const start = parseTimeMs(delegation?.startedAt ?? item.timestamp);
    if (start == null) return null;
    const end = delegationRunning ? timingNowMs : parseTimeMs(item.timestamp) ?? Date.now();
    return Math.max(0, end - start);
  }

  function delegationDurationText(): string {
    return formatDurationMs(delegationDurationMs());
  }

  function delegationStatusTextClass(): string {
    const status = delegationStatusLabel();
    if (status === 'completed') return 'legacy-status-success text-success';
    if (status === 'failed' || status === 'cancelled' || status === 'canceled') return 'legacy-status-error text-danger';
    return 'legacy-status-info text-info';
  }

  function delegationStatusDotClass(): string {
    const status = delegationStatusLabel();
    if (status === 'completed') return 'bg-success';
    if (status === 'failed' || status === 'cancelled' || status === 'canceled') return 'bg-danger';
    return 'bg-info';
  }

  function managedConversationRunning(): boolean {
    if (!isManagedConversationTool()) return false;
    return managedConversationStatusIsRunning(
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
    if (error || normalized === 'failed' || normalized === 'error' || normalized === 'interrupted') return 'legacy-status-error text-danger';
    if (normalized === 'completed' || normalized === 'complete' || normalized === 'idle' || normalized === 'closed') return 'legacy-status-success text-success';
    if (normalized === 'active') return 'legacy-status-active text-info';
    return 'legacy-status-info text-info';
  }

  function managedConversationStatusDotClass(status: string, error = ''): string {
    const normalized = status.toLowerCase();
    if (error || normalized === 'failed' || normalized === 'error' || normalized === 'interrupted') return 'bg-danger';
    if (normalized === 'completed' || normalized === 'complete' || normalized === 'idle' || normalized === 'closed') return 'bg-success';
    if (normalized === 'active') return 'bg-info';
    return 'bg-info';
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
    return headerSubtitle(target);
  }

  function subtitle(): string {
    return toolCallSubtitle(item);
  }

  function isWaitingToolStatus(): boolean {
    return item.status === 'waiting';
  }

  function statusIcon(): string {
    if (item.status === 'completed') return '\u2713';
    if (item.status === 'failed') return '\u2717';
    // `denied` is a terminal rejected outcome (declined escalation
    // approval), distinct from a `failed` execution error — use a
    // different glyph so the two are not visually conflated.
    if (item.status === 'denied') return '\u2298';
    return '';
  }

  function statusColor(): string {
    if (item.status === 'completed') return 'text-success';
    if (item.status === 'failed') return 'text-danger';
    if (item.status === 'denied') return 'text-warning';
    if (isWaitingToolStatus()) return 'text-warning';
    return 'text-info';
  }

  function executionStatusLabel(): 'Queued' | 'Running' | 'Waiting' | 'Success' | 'Failure' | 'Denied' {
    if (item.status === 'denied') return 'Denied';
    if (isWaitingToolStatus()) return 'Waiting';
    if (delegationIsQueued(item)) return 'Queued';
    if (isActiveToolStatus(item.status)) return 'Running';
    return item.isError || item.status === 'failed' ? 'Failure' : 'Success';
  }

  function executionStatusColor(): string {
    const status = executionStatusLabel();
    if (status === 'Failure') return 'text-danger';
    if (status === 'Denied') return 'text-warning';
    if (status === 'Waiting') return 'text-warning';
    if (status === 'Success') return 'text-success';
    return 'text-info';
  }

  function durationText(): string {
    if (item.durationMs != null) return formatDurationMs(item.durationMs);
    if (!isActiveToolStatus(item.status)) return '';
    const startedAt = parseTimeMs(item.timestamp);
    return startedAt == null ? '' : formatDurationMs(Math.max(0, timingNowMs - startedAt));
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
    if (item.isError) return 'border-danger-border';
    return 'border-separator';
  }

  function evalDecisionColor(decision: string): string {
    if (decision === 'approve') return 'legacy-tool bg-success-10 edge-success-40 text-success border-success-border bg-success-subtle';
    if (decision === 'deny') return 'legacy-tool bg-error-10 edge-error-40 text-danger border-danger-border bg-danger-subtle';
    if (decision === 'escalate') return 'legacy-tool bg-info-10 edge-info-40 text-info border-info-border bg-info-subtle';
    return 'text-muted border-control-border bg-raised/40';
  }

  function evalRiskColor(risk: string): string {
    if (risk === 'critical') return 'text-danger';
    if (risk === 'high') return 'text-info';
    if (risk === 'medium') return 'legacy-risk-medium text-warning';
    return 'text-muted';
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

  function commandSummaryText(): string {
    return commandText().replace(/\s*\n\s*/g, ' ↵ ').trim();
  }

  function terminalTitle(): string {
    return descriptionText() || commandText();
  }

  function compactCommandLabel(): string {
    return compactLabelMode === 'description'
      ? descriptionText() || commandText()
      : commandText();
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
    if (status === 'failed') return 'legacy-tool bg-error-10 edge-error-40 ink-error-100 border-danger-border bg-danger-subtle text-danger';
    if (status === 'rejected') return 'legacy-tool bg-warning-10 edge-warning-40 ink-warning-100 border-warning-border bg-warning-subtle text-warning';
    return 'legacy-tool bg-success-10 edge-success-40 ink-success-100 border-success-border bg-success-subtle text-success';
  }

  function todoStatusClass(status: string): string {
    if (status === 'completed') return 'legacy-tool bg-success-10 edge-success-30 ink-success-100 border-success-border bg-success-subtle text-success';
    if (status === 'cancelled' || status === 'canceled') return 'border-control-border bg-raised/50 text-secondary';
    if (status === 'in_progress' || status === 'active' || status === 'running') return 'legacy-tool bg-info-10 edge-info-35 ink-info-100 border-info-border bg-info-subtle text-info';
    return 'legacy-tool bg-warning-10 edge-warning-35 ink-warning-100 border-warning-border bg-warning-subtle text-warning';
  }

  function memoryItemClass(accent: string): string {
    if (accent === 'artifact') return 'legacy-tool bg-violet-5 edge-violet-20 border-violet-border bg-violet-subtle';
    if (accent === 'category') return 'legacy-tool bg-warning-5 edge-warning-category border-orange-border bg-orange-subtle';
    return 'legacy-tool edge-cyan-15 border-cyan-border bg-inset/35';
  }

  function memoryItemTitleClass(accent: string): string {
    if (accent === 'artifact') return 'legacy-tool ink-memory-artifact text-violet-tone';
    if (accent === 'category') return 'legacy-tool ink-memory-category text-orange-tone';
    return 'legacy-tool ink-memory text-cyan-tone';
  }

  function nativeInspectionToneClass(error = ''): string {
    return error ? 'legacy-native-tool legacy-tool bg-error-5 edge-error-30 border-danger-border bg-danger-subtle' : 'legacy-native-tool legacy-tool bg-native-5 edge-info-25 border-blue-border bg-blue-subtle';
  }

  function nativeInspectionHeadingClass(error = ''): string {
    return error ? 'legacy-error-heading text-danger' : 'legacy-native-heading text-blue-tone';
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
    outputDrawerLoaded = true;
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
    outputDrawerLoaded = true;
    outputDrawerOpen = true;
  }

  function onTerminalScroll(): void {
    if (!terminalEl) return;
    const atTail = terminalEl.scrollHeight - terminalEl.scrollTop - terminalEl.clientHeight < 16;
    terminalTailing = atTail;
    if (!atTail) pinTerminal();
  }
</script>

<article class={toolFrameClass(item, density)}>
  <ToolCallHeader {item} {density} {summaryMode} {compactLabelMode} {expanded}
    controlsId={expandedContentId} onclick={toggle} now={timingNowMs} focusOnMount={focusHeader} />

  <!-- Expanded content -->
  {#if expanded}
    <div id={expandedContentId} class={`space-y-3 border-t border-separator ${density === 'compact' ? 'px-3 py-2.5' : 'px-4 py-3'}`}>
      {#if contextLabel}
        <div class="legacy-quiet flex min-w-0 items-center justify-between gap-3 text-[10px] text-quiet">
          <span>Workstream</span>
          <span class="flex min-w-0 items-center gap-2">
            <span class="truncate text-right" title={contextLabel}>{contextLabel}</span>
            {#if onViewSession && contextSessionId?.startsWith('sess_')}
              <button
                class="shrink-0 font-medium text-accent transition hover:text-accent-hover"
                type="button"
                onclick={() => void onViewSession?.(contextSessionId)}
              >
                View session
              </button>
            {/if}
          </span>
        </div>
      {/if}
      {#if item.timestamp}
        <div class="legacy-quiet flex items-center justify-between gap-3 text-[11px] text-quiet">
          <span class={executionStatusColor()} data-testid="tool-execution-status">{executionStatusLabel()}</span>
          <span title={formatAbsoluteTime(item.timestamp)}>{formatCompactTime(item.timestamp, nowDate)}</span>
        </div>
      {/if}
      {#if detailsContent}
        {@render detailsContent()}
        {#if canOpenToolOutput(item) && scope}
          <button class="mt-2 rounded-lg border border-control-border bg-surface px-3 py-1.5 text-xs font-medium text-foreground hover:bg-surface-hover" type="button" onclick={() => { openToolOutput(item); }}>
            Full execution data…
          </button>
        {/if}
      {:else if isDelegateTool()}
        <div class="overflow-hidden rounded-2xl border border-info-border bg-inset/35 text-sm text-foreground shadow-inner">
          <div class="flex flex-wrap items-start justify-between gap-3 px-4 py-3">
            <div class="min-w-0 flex-1 space-y-1.5">
              <p class="text-[11px] font-medium uppercase tracking-[0.22em] text-muted">Delegated sub-session</p>
              <h4 class="legacy-tool-title truncate text-base font-semibold text-strong" title={delegationTitle()}>{delegationTitle()}</h4>
              <div class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
                {#if delegationAgentLabel()}
                  <span class="font-mono text-secondary">{delegationAgentLabel()}</span>
                  <span class="legacy-separator text-quiet">·</span>
                {/if}
                {#if delegationChildSessionId()}
                  <span class="font-mono text-muted" title={delegationChildSessionId()}>{compactSessionId(delegationChildSessionId())}</span>
                  <span class="legacy-separator text-quiet">·</span>
                {/if}
                <span class={`inline-flex items-center gap-1.5 font-medium ${delegationStatusTextClass()}`}>
                  <span class={`h-1.5 w-1.5 rounded-full ${delegationStatusDotClass()}`}></span>
                  {delegationStatusDisplayText()}
                </span>
                {#if delegationDurationText()}
                  <span class="legacy-separator text-quiet">·</span>
                  <span class="tabular-nums text-muted">{delegationDurationText()}</span>
                {/if}
              </div>
            </div>
            <div class="flex shrink-0 items-center">
              {#if canViewDelegationSession()}
                <button
                  class="rounded-lg border border-control-border bg-surface/70 px-3 py-1.5 text-xs font-medium text-accent transition hover:border-accent hover:bg-accent-subtle hover:text-accent-hover"
                  type="button"
                  onclick={() => { const sessionId = delegationChildSessionId(); if (sessionId) void onViewSession?.(sessionId); }}
                >
                  View session
                </button>
              {/if}
            </div>
          </div>

          <div class="space-y-3 border-t border-separator px-4 py-3">
            {#if delegationRunning || hasDelegationProgressStats() || delegationVisibleTodos.length > 0}
              <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-secondary">
                {#if delegationVisibleTodos.length > 0}
                  <TodoProgressPopover todos={delegationVisibleTodos} label="Delegated todo progress" />
                {/if}
                {#if delegationRunning && !delegation?.error}
                  <span class="inline-flex min-w-0 items-center gap-2">
                    <LiveDots inline={true} size="sm" tone="sky" />
                    <span class="min-w-0 truncate">{delegationSummaryText()}</span>
                  </span>
                {:else if !delegation?.error && delegationSummaryText() && !delegationOutputText()}
                  <span class="min-w-0 whitespace-pre-wrap text-secondary">{delegationSummaryText()}</span>
                {/if}
                {#if delegation?.toolCallCount != null}
                  {#if hasDelegationProgressLead()}
                    <span class="legacy-separator text-quiet">·</span>
                  {/if}
                  <span class="tabular-nums text-muted">
                    {#if delegation?.maxToolCalls != null && delegation.maxToolCalls > 0}
                      {delegation.toolCallCount}/{delegation.maxToolCalls} tool calls
                    {:else}
                      {delegation.toolCallCount} tool calls
                    {/if}
                  </span>
                {/if}
                {#if delegation?.lastTool}
                  {#if hasDelegationProgressLead() || delegation?.toolCallCount != null}
                    <span class="legacy-separator text-quiet">·</span>
                  {/if}
                  <span class="min-w-0 truncate text-muted" title={delegation.lastTool}>last: <span class="font-mono text-secondary">{delegation.lastTool}</span></span>
                {/if}
              </div>
              {#if delegation?.toolCallCount != null && delegation?.maxToolCalls != null && delegation.maxToolCalls > 0}
                <div class="h-1 overflow-hidden rounded-full bg-raised/80">
                  <div class="h-full rounded-full bg-info transition-all duration-500" style={`width: ${delegationProgressPercent()}%`}></div>
                </div>
              {/if}
            {/if}

            {#if delegationOutputText() && !delegationRunning && !delegation?.error}
              <div class="rounded-xl border border-separator bg-inset/35">
                <div class="flex items-center justify-between gap-2 border-b border-separator px-3 py-2">
                  <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-muted">Output</span>
                  {#if delegation?.resultTruncated}
                    <span class="legacy-tool bg-warning-10 edge-truncated ink-warning-100 rounded-full border border-warning-border bg-warning-subtle px-2 py-0.5 text-[10px] font-medium uppercase tracking-widest text-warning">truncated</span>
                  {/if}
                </div>
                <div class="prose prose-sm max-h-[34vh] max-w-none overflow-auto px-3 py-2 text-foreground">
                  {@html delegationOutputHtml}
                </div>
              </div>
            {:else if delegationSummaryText() && (!delegationRunning || delegation?.error)}
              <div class={`rounded-xl border px-3 py-2 text-xs leading-5 ${delegation?.error ? 'legacy-tool bg-error-10 edge-error-inner ink-error-100 border-danger-border bg-danger-subtle text-danger' : 'border-control-border bg-inset/35 text-secondary'}`}>
                <p class="line-clamp-5 whitespace-pre-wrap">{delegationSummaryText()}</p>
              </div>
            {/if}
          </div>
        </div>

        {#if hasRawPayload()}
          <div>
            <button
              class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
              onclick={() => { rawExpanded = !rawExpanded; }}
              type="button"
            >
              <span>{rawExpanded ? '▼' : '▶'}</span>
              <span>Raw payload</span>
            </button>
            {#if rawExpanded}
              <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
                {#if item.arguments && Object.keys(item.arguments).length > 0}
                  <div>
                    <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Input</p>
                    <pre class="max-h-[28vh] overflow-auto rounded-lg border border-separator bg-inset p-3 text-secondary">{formattedArguments}</pre>
                  </div>
                {/if}
                {#if item.result != null}
                  <div>
                    <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Output</p>
                    <div class="relative">
                      <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-inset p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
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
          <div class={`helper-frame helper-managed overflow-hidden rounded-2xl border ${managedPresentation.error ? 'helper-failed border-danger-border bg-danger-subtle' : 'border-violet-border bg-violet-subtle'} text-sm text-foreground`}>
            <div class={`helper-header flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3 ${managedPresentation.error ? 'border-danger-border' : 'border-violet-border'}`}>
              <div class="min-w-0 flex-1 space-y-1.5">
                <p class={`text-[11px] font-medium uppercase tracking-[0.22em] ${managedPresentation.error ? 'legacy-error-heading text-danger' : 'legacy-managed-heading text-violet-tone'}`}>Managed conversation</p>
                <h4 class="legacy-tool-title truncate text-base font-semibold text-strong" title={managedPresentation.requestText || managedPresentation.title}>{managedPresentation.title}</h4>
                <div class="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
                  {#if managedPresentation.primaryConversation?.agentId}
                    <span class="font-mono text-secondary">{managedPresentation.primaryConversation.agentId}</span>
                    <span class="legacy-separator text-quiet">·</span>
                  {/if}
                  {#if managedPresentation.primaryConversation?.conversationId}
                    <span class="font-mono text-muted" title={managedPresentation.primaryConversation.conversationId}>{compactSessionId(managedPresentation.primaryConversation.conversationId)}</span>
                    <span class="legacy-separator text-quiet">·</span>
                  {/if}
                  <span class={`inline-flex items-center gap-1.5 font-medium ${managedConversationStatusTextClass(displayStatus, managedPresentation.error)}`}>
                    <span class={`h-1.5 w-1.5 rounded-full ${managedConversationStatusDotClass(displayStatus, managedPresentation.error)}`}></span>
                    {managedConversationStatusText(displayStatus)}
                  </span>
                  {#if managedConversationRunning()}
                    <span class="legacy-separator text-quiet">·</span>
                    <span class="inline-flex items-center gap-1.5 text-info">
                      <LiveDots inline={true} size="sm" tone="sky" />
                    </span>
                  {:else if item.durationMs != null}
                    <span class="legacy-separator text-quiet">·</span>
                    <span class="tabular-nums text-muted">{formatDurationMs(item.durationMs)}</span>
                  {/if}
                </div>
              </div>
              <div class="flex shrink-0 flex-wrap items-center gap-2">
                {#if managedPresentation.primaryConversation?.conversationId}
                  <a
                    class="rounded-lg border border-control-border bg-surface px-3 py-1.5 text-xs font-medium text-accent transition hover:border-accent hover:bg-accent-subtle hover:text-accent-hover"
                    href={managedConversationHref(managedPresentation.primaryConversation.conversationId)}
                    onclick={(event) => followConversationLink(event, managedPresentation.primaryConversation!.conversationId!, navigation)}
                  >
                    Open conversation
                  </a>
                {/if}
              </div>
            </div>

            <div class="space-y-3 px-4 py-3">
              {#if managedPresentation.todos.length > 0 || managedPresentation.toolCallCount != null || managedPresentation.lastTool}
                <div class="flex min-w-0 flex-wrap items-center gap-2 text-xs text-muted">
                  {#if managedPresentation.todos.length > 0}
                    <TodoProgressPopover todos={managedPresentation.todos} label="Managed conversation todo progress" />
                    <span>{managedPresentation.todoSummary}</span>
                  {/if}
                  {#if managedPresentation.toolCallCount != null}
                    {#if managedPresentation.todos.length > 0}<span class="text-quiet">·</span>{/if}
                    <span class="tabular-nums">{managedPresentation.toolCallCount} tool calls</span>
                  {/if}
                  {#if managedPresentation.lastTool}
                    {#if managedPresentation.todos.length > 0 || managedPresentation.toolCallCount != null}<span class="text-quiet">·</span>{/if}
                    <span class="min-w-0 truncate">last: <span class="font-mono text-secondary">{managedPresentation.lastTool}</span></span>
                  {/if}
                </div>
              {/if}
              {#if managedPresentation.resultSummary}
                <div class={`rounded-xl border px-3 py-2 text-xs leading-5 ${managedPresentation.error ? 'legacy-tool bg-error-10 edge-error-inner ink-error-100 border-danger-border bg-danger-subtle text-danger' : 'legacy-tool edge-violet-result managed-result border-violet-border bg-inset/35 text-foreground'}`}>
                  <p class="line-clamp-5 whitespace-pre-wrap">{managedPresentation.resultSummary}</p>
                </div>
              {/if}

              {#if managedPresentation.conversations.length > 1}
                <div class="space-y-2">
                  <p class="text-[10px] font-semibold uppercase tracking-[0.18em] text-violet-tone">Conversations</p>
                  <div class="grid gap-2">
                    {#each managedPresentation.conversations as conversation}
                      <div class="legacy-tool edge-violet-result flex flex-wrap items-center justify-between gap-2 rounded-xl border border-violet-border bg-inset/30 px-3 py-2 text-xs">
                        <div class="min-w-0">
                          <p class="truncate font-medium text-foreground" title={conversation.title || conversation.conversationId}>{conversation.title || conversation.conversationId || 'Managed conversation'}</p>
                          <p class="mt-0.5 flex min-w-0 flex-wrap items-center gap-1.5 text-muted">
                            {#if conversation.agentId}<span class="font-mono">{conversation.agentId}</span>{/if}
                            {#if conversation.conversationId}<span class="font-mono" title={conversation.conversationId}>{compactSessionId(conversation.conversationId)}</span>{/if}
                            {#if conversation.status}<span class={managedConversationStatusTextClass(conversation.status, conversation.error)}>{managedConversationStatusText(conversation.status)}</span>{/if}
                          </p>
                        </div>
                        {#if conversation.conversationId}
                          <a class="rounded-lg border border-control-border bg-surface/70 px-2.5 py-1 text-[11px] font-medium text-accent transition hover:border-accent hover:bg-accent-subtle hover:text-accent-hover" href={managedConversationHref(conversation.conversationId)} onclick={(event) => followConversationLink(event, conversation.conversationId!, navigation)}>Open</a>
                        {/if}
                      </div>
                    {/each}
                  </div>
                </div>
              {/if}

              {#if managedPresentation.requestDetails.length > 0}
                <dl class="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs">
                  {#each managedPresentation.requestDetails.slice(0, 4) as detail}
                    <dt class="text-muted">{detail.key}</dt>
                    <dd class="min-w-0 truncate text-foreground" title={String(detail.value)}>{String(detail.value)}</dd>
                  {/each}
                </dl>
              {/if}
              {#if managedPresentation.primaryConversation?.controllerConversationId || managedPresentation.primaryConversation?.followUpConversationId}
                <div class="flex flex-wrap gap-3 text-xs">
                  {#if managedPresentation.primaryConversation?.controllerConversationId}
                    <a class="font-medium text-accent underline underline-offset-2 transition hover:text-accent-hover" href={managedConversationHref(managedPresentation.primaryConversation.controllerConversationId)}>Controller conversation</a>
                  {/if}
                  {#if managedPresentation.primaryConversation?.followUpConversationId}
                    <a class="font-medium text-accent underline underline-offset-2 transition hover:text-accent-hover" href={managedConversationHref(managedPresentation.primaryConversation.followUpConversationId)}>Follow-up conversation</a>
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
                class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
                onclick={() => { rawExpanded = !rawExpanded; }}
                type="button"
              >
                <span>{rawExpanded ? '▼' : '▶'}</span>
                <span>Raw payload</span>
              </button>
              {#if rawExpanded}
                <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
                  {#if item.arguments && Object.keys(item.arguments).length > 0}
                    <div>
                      <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Input</p>
                      <pre class="max-h-[28vh] overflow-auto rounded-lg border border-separator bg-inset p-3 text-secondary">{formattedArguments}</pre>
                    </div>
                  {/if}
                  {#if item.result != null}
                    <div>
                      <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Output</p>
                      <div class="relative">
                        <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-inset p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
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
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-quiet">Questions</p>
            <div class="legacy-tool bg-info-5 edge-info-20 ink-info-50 space-y-3 rounded-2xl border border-info-border bg-info-subtle px-4 py-3 text-sm text-foreground">
              {#if stepRequestContext()}
                <p class="text-xs leading-5 text-secondary">{stepRequestContext()}</p>
              {/if}
              {#if normalizeStepQuestions(item).length > 0}
                {#each normalizeStepQuestions(item) as question, index}
                  <section class="legacy-tool bg-question-inset edge-question-inset rounded-xl border border-info-border bg-surface px-3 py-2.5">
                    <div class="flex flex-wrap items-center gap-2">
                      <span class="legacy-tool edge-question-counter rounded-full border border-info-border px-2 py-0.5 text-[10px] font-medium uppercase tracking-widest text-info">#{index + 1}</span>
                      {#if question.header}
                        <span class="text-xs font-semibold uppercase tracking-widest text-info">{question.header}</span>
                      {/if}
                      {#if question.multiple}
                        <span class="rounded-full border border-control-border px-2 py-0.5 text-[10px] text-secondary">multi-select</span>
                      {/if}
                      {#if question.required}
                        <span class="rounded-full border border-control-border px-2 py-0.5 text-[10px] text-secondary">required</span>
                      {/if}
                      {#if question.allow_custom}
                        <span class="rounded-full border border-control-border px-2 py-0.5 text-[10px] text-secondary">custom</span>
                      {/if}
                    </div>
                    <p class="mt-2 leading-6">{question.question}</p>
                    {#if question.options.length > 0}
                      <div class="mt-3 flex flex-wrap gap-2">
                        {#each question.options as option}
                          <span class="legacy-tool bg-sky-option edge-sky-option ink-info-100 rounded-full border border-info-border bg-info-subtle px-3 py-1 text-[11px] text-info" title={option.description ?? ''}>{option.label}</span>
                        {/each}
                      </div>
                    {/if}
                  </section>
                {/each}
              {:else}
                <p class="leading-6">The agent requested more input.</p>
              {/if}
              <div class="rounded-2xl border border-separator bg-inset px-4 py-3">
                <LiveDots label="Waiting for user input" size="sm" inline={true} />
              </div>
            </div>
          {:else if stepRequestError()}
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-quiet">Resolution</p>
            <div class="legacy-tool bg-error-10 edge-error-30 ink-error-200 rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">
              {stepRequestError()}
            </div>
          {:else if stepRequestAnswers().length > 0}
            {@const answers = stepRequestAnswers()}
            {@const answer = completedQuestionAt(answers)}
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-quiet">Submitted answers</p>
            <div class="legacy-tool bg-success-5 edge-success-20 ink-success-50 overflow-hidden rounded-2xl border border-success-border bg-success-subtle text-sm text-foreground">
              {#if answers.length > 1}
                <div class="flex items-center justify-between gap-3 border-b border-success-border px-4 py-3">
                  <span class="font-semibold">Answered questions</span>
                  <span class="legacy-tool bg-answer-counter edge-answer-counter ink-success-100 rounded-full border border-success-border bg-inset px-2 py-0.5 font-mono text-[11px] text-success">
                    {completedQuestionPageIndex(answers) + 1}/{answers.length}
                  </span>
                </div>
              {/if}
              <div class="max-h-[min(42vh,28rem)] overflow-y-auto overscroll-contain px-4 py-3">
                {#if answer}
                  <section class="rounded-2xl border border-success-border bg-surface p-3">
                    {#if answer.question.header}
                      <p class="text-xs uppercase tracking-[0.2em] text-secondary">{answer.question.header}</p>
                    {/if}
                    <p class="text-sm font-medium leading-6 text-foreground">{answer.question.question}</p>
                    <div class="mt-1 flex flex-wrap gap-2">
                      {#if answer.question.required}
                        <span class="rounded-full border border-control-border px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-secondary">Required</span>
                      {:else}
                        <span class="rounded-full border border-control-border px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-muted">Optional</span>
                      {/if}
                      {#if answer.question.multiple}
                        <span class="rounded-full border border-control-border px-2 py-0.5 text-[10px] uppercase tracking-[0.18em] text-muted">Multi-select</span>
                      {/if}
                    </div>
                    {#if answer.selected.length > 0}
                      <div class="mt-3 space-y-2">
                        {#each answer.selected as option, optionIndex (`${option.id}:${optionIndex}`)}
                          <div class={`flex w-full items-start gap-3 rounded-2xl border px-3 py-2 text-left text-xs ${option.unknown ? 'legacy-tool bg-warning-option edge-warning-option ink-warning-100 border-warning-border bg-warning-subtle text-warning' : 'legacy-tool bg-success-option edge-success-option ink-answer-100 border-success-border bg-success-subtle text-foreground'}`}>
                            <span class={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center border ${answer.question.multiple ? 'rounded' : 'rounded-full'} ${option.unknown ? 'legacy-tool bg-answer-unknown border-warning-border bg-warning-subtle' : 'legacy-tool bg-answer-selected border-accent bg-accent text-on-accent'}`}>
                              {#if answer.question.multiple}
                                ✓
                              {:else}
                                <span class={`h-1.5 w-1.5 rounded-full ${option.unknown ? 'bg-warning' : 'bg-on-accent'}`}></span>
                              {/if}
                            </span>
                            <span class="min-w-0">
                              <span class="block font-medium">{option.label}</span>
                              {#if option.description}
                                <span class="mt-0.5 block text-secondary">{option.description}</span>
                              {:else if option.unknown}
                                <span class="mt-0.5 block text-warning">Unknown option id from the submitted response.</span>
                              {/if}
                            </span>
                          </div>
                        {/each}
                      </div>
                    {/if}
                    {#if answer.custom}
                      <div class="legacy-tool bg-answer-note edge-success-inner mt-3 rounded-2xl border border-success-border bg-inset px-3 py-2">
                        <p class="mb-1 text-[11px] uppercase tracking-[0.18em] text-secondary">Custom answer</p>
                        <p class="whitespace-pre-wrap leading-6">{answer.custom}</p>
                      </div>
                    {/if}
                  </section>
                {/if}
                {#if answers.length > 1}
                  <div class="mt-3 grid gap-1" style={`grid-template-columns: repeat(${Math.min(answers.length, 7)}, minmax(0, 1fr));`} aria-hidden="true">
                    {#each answers as entry, index (`${entry.question.id}:${index}`)}
                      <span class={`h-1.5 rounded-full ${index === completedQuestionPageIndex(answers) ? 'bg-accent' : 'bg-control-border'}`}></span>
                    {/each}
                  </div>
                {/if}
              </div>
              {#if answers.length > 1}
                <div class="legacy-tool bg-answer-footer edge-answer-footer flex items-center gap-2 border-t border-success-border bg-inset px-4 py-3">
                  {#if completedQuestionPageIndex(answers) > 0}
                    <button
                      class="rounded-lg border border-control-border bg-surface px-3 py-1.5 text-xs text-foreground transition hover:bg-surface-hover"
                      type="button"
                      aria-label="Previous answered question"
                      onclick={() => { goToCompletedQuestion(completedQuestionPageIndex(answers) - 1, answers); }}
                    >
                      ←
                    </button>
                  {/if}
                  {#if completedQuestionPageIndex(answers) < answers.length - 1}
                    <button
                      class="ml-auto rounded-lg border border-control-border bg-surface px-3 py-1.5 text-xs text-foreground transition hover:bg-surface-hover"
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
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-quiet">Submitted answers</p>
            <div class="legacy-tool bg-success-5 edge-success-20 ink-success-50 rounded-2xl border border-success-border bg-success-subtle px-4 py-3 text-sm text-foreground">
              <p class="whitespace-pre-wrap leading-6">{stepRequestResponse()}</p>
            </div>
          {:else}
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-quiet">Resolution</p>
            <div class="rounded-2xl border border-separator bg-inset px-4 py-3 text-sm text-muted">
              No resolution was recorded for this input request.
            </div>
          {/if}
        </div>

        {#if hasRawPayload()}
          <div>
            <button
              class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
              onclick={() => { rawExpanded = !rawExpanded; }}
              type="button"
            >
              <span>{rawExpanded ? '▼' : '▶'}</span>
              <span>Raw payload</span>
            </button>
            {#if rawExpanded}
              <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
                {#if item.arguments && Object.keys(item.arguments).length > 0}
                  <div>
                    <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Input</p>
                    <pre class="max-h-[28vh] overflow-auto rounded-lg border border-separator bg-inset p-3 text-secondary">{formattedArguments}</pre>
                  </div>
                {/if}
                {#if item.result != null}
                  <div>
                    <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Output</p>
                    <div class="relative">
                      <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-inset p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
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
            <div class={`overflow-hidden rounded-2xl border ${webPresentation.error ? 'legacy-tool bg-error-5 edge-error-30 border-danger-border bg-danger-subtle' : 'legacy-tool bg-violet-5 edge-violet-25 border-violet-border bg-violet-subtle'}`}>
              <div class={`flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3 ${webPresentation.error ? 'border-danger-border' : 'border-violet-border'}`}>
                <div class="min-w-0 flex-1">
                  <p class={`text-xs font-medium uppercase tracking-widest ${webPresentation.error ? 'legacy-error-heading text-danger' : 'text-violet-tone'}`}>{webPresentation.title}</p>
                  {#if webPresentation.requestText}
                    <a href={webPresentation.webKind === 'fetch' ? webPresentation.requestText : undefined} target="_blank" rel="noreferrer" class={`mt-1 block truncate text-sm font-semibold ${webPresentation.error ? 'legacy-error-title text-danger' : 'legacy-web-title text-accent hover:text-accent-hover'} ${webPresentation.webKind === 'fetch' ? 'hover:underline' : ''}`}>{webPresentation.requestText}</a>
                  {/if}
                  <p class={`mt-2 text-xs leading-5 ${webPresentation.error ? 'legacy-error-summary text-danger' : 'legacy-web-summary text-secondary'}`}>{webPresentation.summary}</p>
                </div>
                {#if webPresentation.badges.length > 0}
                  <div class={`flex max-w-full flex-wrap justify-end gap-2 text-[11px] ${webPresentation.error ? 'legacy-error-badges text-danger' : 'legacy-web-badges text-violet-tone'}`}>
                    {#each webPresentation.badges as badge}
                      <span class={`rounded-full border px-2 py-0.5 ${webPresentation.error ? 'border-danger-border' : 'border-violet-border'}`}>{badge}</span>
                    {/each}
                  </div>
                {/if}
              </div>

              {#if webPresentation.error}
                <p class="legacy-tool bg-error-10 edge-error-inner ink-error-100 m-4 rounded-xl border border-danger-border bg-danger-subtle px-3 py-2 text-sm leading-6 text-danger">{webPresentation.error}</p>
              {/if}
              {#if webPresentation.warning}
                <p class="legacy-tool bg-warning-10 edge-warning-inner ink-warning-100 m-4 rounded-xl border border-warning-border bg-warning-subtle px-3 py-2 text-sm leading-6 text-warning">{webPresentation.warning}</p>
              {/if}

              {#if webPresentation.answer}
                <section class="border-t border-violet-border px-4 py-3">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-violet-tone">Answer</p>
                  <div class="legacy-tool edge-violet-15 prose prose-sm max-w-none rounded-xl border border-violet-border bg-inset/35 px-3 py-2 text-foreground">
                    {@html renderMarkdown(webPresentation.answer)}
                  </div>
                </section>
              {/if}

              {#if webPresentation.results.length > 0}
                <section class="space-y-2 border-t border-violet-border px-4 py-3">
                  <p class="text-xs font-medium uppercase tracking-widest text-violet-tone">Results</p>
                  {#each webPresentation.results as result, index (`${result.url}:${index}`)}
                    <article class="legacy-tool edge-violet-15 rounded-xl border border-violet-border bg-inset/30 px-3 py-2">
                      <div class="flex flex-wrap items-start justify-between gap-2">
                        <a href={result.url} target="_blank" rel="noreferrer" class="min-w-0 text-sm font-semibold text-accent hover:text-accent-hover hover:underline [overflow-wrap:anywhere]">{result.title}</a>
                        {#if result.domain || result.score || result.resultType || result.publishedDate || result.freshness || result.sourceEngine}
                          <div class="flex flex-wrap gap-2 text-[10px] text-muted">
                            {#if result.domain}<span>{result.domain}</span>{/if}
                            {#if result.resultType}<span>{result.resultType}</span>{/if}
                            {#if result.publishedDate}<span>{result.publishedDate}</span>{/if}
                            {#if result.freshness}<span>{result.freshness}</span>{/if}
                            {#if result.sourceEngine}<span>{result.sourceEngine}</span>{/if}
                            {#if result.score}<span>{result.score}</span>{/if}
                          </div>
                        {/if}
                      </div>
                       {#if result.snippet}<p class="mt-2 text-xs leading-5 text-secondary">{result.snippet}</p>{/if}
                       {#if result.recommendation}<p class="mt-1 text-[10px] uppercase tracking-wide text-violet-tone">Fetch: {result.recommendation}</p>{/if}
                    </article>
                  {/each}
                </section>
              {/if}

              {#if webPresentation.content}
                <section class="border-t border-violet-border px-4 py-3">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-violet-tone">Extracted content</p>
                  <pre class="legacy-tool edge-violet-15 max-h-[36vh] overflow-auto whitespace-pre-wrap rounded-xl border border-violet-border bg-inset/35 px-3 py-2 text-xs leading-5 text-foreground">{webPresentation.content}</pre>
                </section>
              {/if}

              {#if webPresentation.media.length > 0}
                <section class="space-y-2 border-t border-violet-border px-4 py-3">
                  <p class="text-xs font-medium uppercase tracking-widest text-violet-tone">Image references</p>
                  <div class="grid gap-2 md:grid-cols-2">
                    {#each webPresentation.media as media, index (`${media.url}:${index}`)}
                      <article class="legacy-tool edge-violet-15 rounded-xl border border-violet-border bg-inset/30 px-3 py-2">
                        <a href={media.url} target="_blank" rel="noreferrer" class="block truncate text-sm font-medium text-accent hover:text-accent-hover hover:underline" title={media.url}>{media.label}</a>
                        {#if media.source || media.sourcePageUrl}
                          <p class="mt-1 truncate text-[11px] text-muted">{media.sourcePageUrl || media.source}</p>
                        {/if}
                        {#if media.artifactRef}
                          <p class="mt-2 font-mono text-[10px] text-violet-tone" title={media.artifactRef}>lazy artifact available</p>
                        {/if}
                      </article>
                    {/each}
                  </div>
                </section>
              {/if}

              {#if canOpenToolOutput(item) && scope}
                <div class="border-t border-violet-border px-4 py-3">
                  <button class="legacy-tool bg-violet-10 edge-violet-30 ink-violet-100 rounded-lg border border-violet-border bg-violet-subtle px-3 py-1.5 text-xs font-medium text-violet-tone hover:bg-surface-hover" type="button" onclick={() => { openToolOutput(item); }}>
                    {toolOutputOpenLabel(item)}
                  </button>
                </div>
              {/if}
            </div>
          {/if}
        {:else if isRichNativeInspectionTool()}
          {@const nativePresentation = nativeInspectionToolPresentation(item)}
          {#if nativePresentation}
            <div class={`overflow-hidden rounded-2xl border ${nativeInspectionToneClass(nativePresentation.error)} text-sm text-foreground shadow-inner`}>
              <div class={`flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3 ${nativePresentation.error ? 'border-danger-border' : 'border-blue-border'}`}>
                <div class="min-w-0 flex-1">
                  <p class={`text-xs font-medium uppercase tracking-widest ${nativeInspectionHeadingClass(nativePresentation.error)}`}>Native tool output</p>
                  <h4 class="legacy-tool-title mt-1 truncate text-sm font-semibold text-strong" title={nativePresentation.requestText || nativePresentation.title}>{nativePresentation.title}</h4>
                  <p class="mt-2 text-xs leading-5 text-secondary">{nativePresentation.summary}</p>
                </div>
                {#if nativePresentation.badges.length > 0}
                  <div class="legacy-native-badges flex max-w-full flex-wrap justify-end gap-2 text-[11px] text-blue-tone">
                    {#each nativePresentation.badges as badge}
                      <span class="rounded-full border border-blue-border px-2 py-0.5">{badge}</span>
                    {/each}
                  </div>
                {/if}
              </div>

              <div class="space-y-3 px-4 py-3">
                <section class="legacy-tool edge-native-15 rounded-xl border border-blue-border bg-inset/30 px-3 py-2">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-blue-tone">{nativePresentation.requestLabel}</p>
                  {#if nativePresentation.requestText}
                    <p class="font-mono text-sm text-foreground [overflow-wrap:anywhere]">{nativePresentation.requestText}</p>
                  {:else}
                    <p class="text-sm text-muted">No request summary was recorded.</p>
                  {/if}
                  {#if nativePresentation.requestDetails.length > 0}
                    <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-muted">
                      {#each nativePresentation.requestDetails.slice(0, 6) as detail}
                        <span class="rounded-full border border-control-border bg-inset/35 px-2 py-0.5">
                          <span class="legacy-quiet text-quiet">{detail.key}:</span>
                          <span class="ml-1 text-foreground">{formatStructuredValue(detail.value)}</span>
                        </span>
                      {/each}
                    </div>
                  {/if}
                </section>

                {#if nativePresentation.error}
                  <div class="legacy-tool bg-error-10 edge-error-inner ink-error-100 rounded-xl border border-danger-border bg-danger-subtle px-3 py-2 text-xs leading-5 text-danger">{nativePresentation.error}</div>
                {:else if nativePresentation.nativeKind === 'read' && nativePresentation.readLines.length > 0}
                  <section class="legacy-tool edge-native-15 overflow-hidden rounded-xl border border-blue-border bg-inset/35">
                    <div class="flex items-center justify-between gap-2 border-b border-blue-border px-3 py-2">
                      <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-blue-tone">File content</span>
                      {#if nativePresentation.path}
                        <span class="truncate font-mono text-[11px] text-muted" title={nativePresentation.path}>{nativePresentation.path}</span>
                      {/if}
                    </div>
                    <div class="max-h-[46vh] overflow-auto">
                      <table class="w-full border-collapse font-mono text-xs leading-5">
                        <tbody>
                          {#each nativePresentation.readLines as line}
                            <tr class="align-top hover:bg-surface-hover">
                              <td class="legacy-quiet select-none border-r border-separator bg-inset px-3 py-0 text-right tabular-nums text-quiet">{line.lineNumber}</td>
                              <td class="min-w-0 px-3 py-0 text-foreground">
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
                    <p class="text-[10px] font-semibold uppercase tracking-[0.18em] text-blue-tone">Matches</p>
                    {#each nativePresentation.grepGroups as group}
                      <article class="legacy-tool edge-native-15 overflow-hidden rounded-xl border border-blue-border bg-inset/35">
                        <div class="border-b border-blue-border px-3 py-2">
                          <p class="legacy-native-path truncate font-mono text-xs text-blue-tone" title={group.path}>{group.path}</p>
                        </div>
                        <div class="max-h-[32vh] overflow-auto">
                          <table class="w-full border-collapse font-mono text-xs leading-5">
                            <tbody>
                              {#each group.matches as match}
                                <tr class={`${match.isMatch ? 'text-foreground' : 'text-muted'} hover:bg-surface-hover`}>
                                  <td class="legacy-quiet select-none border-r border-separator bg-inset px-3 py-0 text-right tabular-nums text-quiet">{match.lineNumber}</td>
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
                  <section class="legacy-tool edge-native-15 overflow-hidden rounded-xl border border-blue-border bg-inset/35">
                    <div class="flex items-center justify-between gap-2 border-b border-blue-border px-3 py-2">
                      <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-blue-tone">Paths</span>
                      <span class="legacy-quiet text-[11px] text-quiet">{nativePresentation.pathEntries.length} item{nativePresentation.pathEntries.length === 1 ? '' : 's'}</span>
                    </div>
                    <ul class="max-h-[42vh] divide-y divide-separator overflow-auto">
                      {#each nativePresentation.pathEntries as entry}
                        <li class="flex min-w-0 items-center gap-2 px-3 py-1.5 font-mono text-xs text-foreground">
                          <span class="legacy-quiet shrink-0 text-quiet">{entry.kind === 'directory' ? 'dir' : entry.kind === 'file' ? 'file' : 'path'}</span>
                          <span class="min-w-0 truncate" title={entry.path}>{entry.path}</span>
                        </li>
                      {/each}
                    </ul>
                  </section>
                {:else if nativePresentation.outputText}
                  <section class="legacy-tool edge-native-15 rounded-xl border border-blue-border bg-inset/35">
                    <div class="border-b border-blue-border px-3 py-2">
                      <span class="text-[10px] font-semibold uppercase tracking-[0.18em] text-blue-tone">Output</span>
                    </div>
                    <pre class="max-h-[42vh] overflow-auto whitespace-pre-wrap px-3 py-2 text-xs leading-5 text-foreground">{nativePresentation.outputText}</pre>
                  </section>
                {/if}

                {#if nativePresentation.footer}
                  <p class="rounded-xl border border-control-border bg-inset/35 px-3 py-2 text-xs leading-5 text-muted">{nativePresentation.footer}</p>
                {/if}

                {#if canOpenToolOutput(item) && scope}
                  <button class="rounded-lg border border-control-border bg-surface px-3 py-1.5 text-xs font-medium text-foreground hover:bg-surface-hover" type="button" onclick={() => { openToolOutput(item); }}>
                    {toolOutputOpenLabel(item)}
                  </button>
                {/if}
              </div>
            </div>

            {#if hasRawPayload()}
              <div>
                <button
                  class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
                  onclick={() => { rawExpanded = !rawExpanded; }}
                  type="button"
                >
                  <span>{rawExpanded ? '▼' : '▶'}</span>
                  <span>Raw payload</span>
                </button>
                {#if rawExpanded}
                  <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
                    {#if item.arguments && Object.keys(item.arguments).length > 0}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Input</p>
                        <pre class="max-h-[28vh] overflow-auto rounded-lg border border-separator bg-inset p-3 text-secondary">{formattedArguments}</pre>
                      </div>
                    {/if}
                    {#if item.result != null}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Output</p>
                        <div class="relative">
                          <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-inset p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
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
            <div class={`helper-frame overflow-hidden rounded-2xl border ${memoryPresentation.error ? 'legacy-tool bg-error-5 edge-error-30 border-danger-border bg-danger-subtle' : 'legacy-tool bg-cyan-5 edge-cyan-25 border-cyan-border bg-cyan-subtle'}`}>
              <div class={`flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3 ${memoryPresentation.error ? 'border-danger-border' : 'border-cyan-border'}`}>
                <div class="min-w-0 flex-1">
                  <p class={`text-xs font-medium uppercase tracking-widest ${memoryPresentation.error ? 'legacy-error-heading text-danger' : 'text-cyan-tone'}`}>Memory</p>
                  <h4 class={`mt-1 truncate text-sm font-semibold ${memoryPresentation.error ? 'legacy-error-title text-danger' : 'legacy-memory-title text-foreground'}`}>{memoryPresentation.title}</h4>
                  <p class={`mt-2 text-xs leading-5 ${memoryPresentation.error ? 'legacy-error-summary text-danger' : 'legacy-memory-summary text-secondary'}`}>{memoryPresentation.summary}</p>
                </div>
                {#if memoryPresentation.badges.length > 0}
                  <div class={`flex max-w-full flex-wrap justify-end gap-2 text-[11px] ${memoryPresentation.error ? 'legacy-error-badges text-danger' : 'legacy-memory-badges text-cyan-tone'}`}>
                    {#each memoryPresentation.badges as badge}
                      <span class={`rounded-full border px-2 py-0.5 ${memoryPresentation.error ? 'border-danger-border' : 'border-cyan-border'}`}>{badge}</span>
                    {/each}
                  </div>
                {/if}
              </div>

              {#if memoryPresentation.variant === 'saved'}
                <section class="space-y-3 px-4 py-3">
                  {#if memoryPresentation.error}
                    <div class="px-3 py-2">
                      <p class="text-sm leading-6 text-danger">{memoryPresentation.error}</p>
                    </div>
                  {:else if memoryPresentation.resultItems.length > 0}
                    {#each memoryPresentation.resultItems as resultItem, index (`${resultItem.title}:${index}`)}
                      <article class={`rounded-xl border px-3 py-3 ${memoryItemClass(resultItem.accent)}`}>
                        <div class="flex flex-wrap items-start justify-between gap-2">
                          <p class="text-xs font-medium uppercase tracking-widest text-cyan-tone">
                            {savedMemoryItemLabel(memoryPresentation.resultItems, index)}
                          </p>
                          {#if resultItem.title && resultItem.title !== 'Memory'}
                            <span class="legacy-tool edge-cyan-20 rounded-full border border-cyan-border bg-inset/30 px-2 py-0.5 font-mono text-[10px] text-cyan-tone">{resultItem.title}</span>
                          {/if}
                        </div>
                        {#if resultItem.body}
                          <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-foreground [overflow-wrap:anywhere]">{resultItem.body}</p>
                        {:else if memoryPresentation.requestText}
                          <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-foreground [overflow-wrap:anywhere]">{memoryPresentation.requestText}</p>
                        {/if}
                        {#if resultItem.meta.length > 0}
                          <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-muted">
                            {#each resultItem.meta as entry}
                              <span class="rounded-full border border-control-border bg-inset/35 px-2 py-0.5">
                                <span class="legacy-quiet text-quiet">{entry.key}:</span>
                                <span class="ml-1 text-foreground">{formatStructuredValue(entry.value)}</span>
                              </span>
                            {/each}
                          </div>
                        {/if}
                      </article>
                    {/each}
                  {:else}
                    <article class="legacy-tool edge-cyan-15 rounded-xl border border-cyan-border bg-inset/30 px-3 py-3">
                      <p class="text-xs font-medium uppercase tracking-widest text-cyan-tone">{memoryPresentation.requestLabel}</p>
                      {#if memoryPresentation.requestText}
                        <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-foreground [overflow-wrap:anywhere]">{memoryPresentation.requestText}</p>
                      {:else}
                        <p class="mt-2 text-sm leading-6 text-muted">Memory save completed.</p>
                      {/if}
                    </article>
                  {/if}
                </section>
              {:else}
                <div class="grid gap-3 px-4 py-3 md:grid-cols-2">
                  <section class={`rounded-xl border px-3 py-2 ${memoryPresentation.error ? 'legacy-tool edge-error-15 border-danger-border bg-inset/30' : 'legacy-tool edge-cyan-15 border-cyan-border bg-inset/30'}`}>
                    <p class={`mb-2 text-xs font-medium uppercase tracking-widest ${memoryPresentation.error ? 'legacy-error-heading text-danger' : 'text-cyan-tone'}`}>{memoryPresentation.requestLabel}</p>
                    {#if memoryPresentation.requestText}
                      <p class="whitespace-pre-wrap text-sm leading-6 text-foreground [overflow-wrap:anywhere]">{memoryPresentation.requestText}</p>
                    {:else}
                      <p class="text-sm leading-6 text-muted">No request summary was recorded.</p>
                    {/if}
                    {#if memoryPresentation.requestDetails.length > 0}
                      <dl class="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                        {#each memoryPresentation.requestDetails as entry}
                          <div>
                            <dt class={`font-medium uppercase tracking-wider ${memoryPresentation.error ? 'text-danger' : 'text-secondary'}`}>{entry.key}</dt>
                            <dd class="mt-1 whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]">{formatStructuredValue(entry.value)}</dd>
                          </div>
                        {/each}
                      </dl>
                    {/if}
                  </section>

                  <section class={`rounded-xl border px-3 py-2 ${memoryPresentation.error ? 'legacy-tool bg-error-10 edge-error-15 border-danger-border bg-danger-subtle' : 'legacy-tool edge-cyan-15 border-cyan-border bg-inset/30'}`}>
                    <p class={`mb-2 text-xs font-medium uppercase tracking-widest ${memoryPresentation.error ? 'legacy-error-heading text-danger' : 'text-cyan-tone'}`}>{memoryPresentation.resultLabel}</p>
                    <p class={`text-sm leading-6 ${memoryPresentation.error ? 'text-danger' : 'text-foreground'}`}>{memoryPresentation.resultSummary}</p>
                    {#if memoryPresentation.error}
                      <p class="legacy-tool bg-error-10 edge-error-inner ink-error-100 mt-2 rounded-lg border border-danger-border bg-danger-subtle px-2 py-1.5 text-xs leading-5 text-danger">{memoryPresentation.error}</p>
                    {/if}
                    {#if memoryPresentation.resultDetails.length > 0}
                      <dl class="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                        {#each memoryPresentation.resultDetails as entry}
                          <div>
                            <dt class={`font-medium uppercase tracking-wider ${memoryPresentation.error ? 'text-danger' : 'text-secondary'}`}>{entry.key}</dt>
                            <dd class="mt-1 whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]">{formatStructuredValue(entry.value)}</dd>
                          </div>
                        {/each}
                      </dl>
                    {/if}
                  </section>
                </div>

                {#if memoryPresentation.answer}
                  <section class="border-t border-cyan-border px-4 py-3">
                    <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-tone">Answer</p>
                    <div class="legacy-tool edge-cyan-15 prose prose-sm max-w-none rounded-xl border border-cyan-border bg-inset/35 px-3 py-2 text-foreground">
                      {@html renderMarkdown(memoryPresentation.answer)}
                    </div>
                  </section>
                {/if}

                {#if memoryPresentation.text}
                  <section class="border-t border-cyan-border px-4 py-3">
                    <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-tone">Text</p>
                    <pre class="legacy-tool edge-cyan-15 max-h-[36vh] overflow-auto whitespace-pre-wrap rounded-xl border border-cyan-border bg-inset/35 px-3 py-2 text-xs leading-5 text-foreground">{memoryPresentation.text}</pre>
                  </section>
                {/if}

                {#if memoryPresentation.resultItems.length > 0}
                  <section class="space-y-2 border-t border-cyan-border px-4 py-3">
                    <p class="text-xs font-medium uppercase tracking-widest text-cyan-tone">Items</p>
                    {#each memoryPresentation.resultItems as resultItem, index (`${resultItem.title}:${index}`)}
                      <article class={`rounded-xl border px-3 py-2 ${memoryItemClass(resultItem.accent)}`}>
                        <div class="flex flex-wrap items-start justify-between gap-2">
                          <h5 class={`min-w-0 text-sm font-semibold [overflow-wrap:anywhere] ${memoryItemTitleClass(resultItem.accent)}`}>{resultItem.title}</h5>
                          <span class="rounded-full border border-control-border px-2 py-0.5 text-[10px] uppercase tracking-wider text-secondary">{resultItem.accent}</span>
                        </div>
                        {#if resultItem.body}
                          <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-foreground [overflow-wrap:anywhere]">{resultItem.body}</p>
                        {/if}
                        {#if resultItem.meta.length > 0}
                          <div class="mt-2 flex flex-wrap gap-2 text-[11px] text-muted">
                            {#each resultItem.meta as entry}
                              <span class="rounded-full border border-control-border bg-inset/35 px-2 py-0.5">
                                <span class="legacy-quiet text-quiet">{entry.key}:</span>
                                <span class="ml-1 text-foreground">{formatStructuredValue(entry.value)}</span>
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
                  class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
                  onclick={() => { rawExpanded = !rawExpanded; }}
                  type="button"
                >
                  <span>{rawExpanded ? '▼' : '▶'}</span>
                  <span>Raw payload</span>
                </button>
                {#if rawExpanded}
                  <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
                    {#if item.arguments && Object.keys(item.arguments).length > 0}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Input</p>
                        <pre class="max-h-[28vh] overflow-auto rounded-lg border border-separator bg-inset p-3 text-secondary">{formattedArguments}</pre>
                      </div>
                    {/if}
                    {#if item.result != null}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Output</p>
                        <div class="relative">
                          <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-inset p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
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
            <div class="legacy-tool bg-cyan-5 edge-cyan-25 overflow-hidden rounded-2xl border border-cyan-border bg-cyan-subtle">
              <div class="flex flex-wrap items-start justify-between gap-3 border-b border-cyan-border px-4 py-3">
                <div class="min-w-0">
                  <p class="text-xs font-medium uppercase tracking-widest text-cyan-tone">Stored tool output query</p>
                  <h4 class="mt-1 truncate text-sm font-semibold text-foreground">{outputHelper.title}</h4>
                  <p class="mt-2 text-xs leading-5 text-secondary">{outputHelper.summary}</p>
                </div>
                <span class="rounded-full border border-cyan-border px-2.5 py-1 font-mono text-[11px] text-cyan-tone">{outputHelper.sourceCallId}</span>
              </div>

              <div class="grid gap-3 px-4 py-3 md:grid-cols-2">
                <section class="legacy-tool edge-cyan-15 rounded-xl border border-cyan-border bg-inset/30 px-3 py-2">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-tone">Query</p>
                  <dl class="space-y-2 text-xs">
                    {#each outputHelper.queryEntries as entry}
                      <div>
                        <dt class="font-medium uppercase tracking-wider text-secondary">{entry.key}</dt>
                        <dd class="mt-1 font-mono text-foreground [overflow-wrap:anywhere]">{formatStructuredValue(entry.value)}</dd>
                      </div>
                    {/each}
                  </dl>
                </section>

                <section class="legacy-tool edge-cyan-15 rounded-xl border border-cyan-border bg-inset/30 px-3 py-2">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-tone">Received</p>
                  <p class="text-xs leading-5 text-foreground">{outputHelper.receivedSummary}</p>
                  {#if outputHelper.receivedDetails.length > 0}
                    <dl class="mt-2 space-y-2 text-xs">
                      {#each outputHelper.receivedDetails as entry}
                        <div>
                          <dt class="font-medium uppercase tracking-wider text-secondary">{entry.key}</dt>
                          <dd class="mt-1 font-mono text-foreground">{formatStructuredValue(entry.value)}</dd>
                        </div>
                      {/each}
                    </dl>
                  {/if}
                  {#if outputHelper.continuationHint}
                    <p class="legacy-tool bg-info-10 edge-info-hint ink-info-100 mt-2 rounded-lg border border-info-border bg-info-subtle px-2 py-1.5 text-xs text-info">{outputHelper.continuationHint}</p>
                  {/if}
                </section>
              </div>

              <section class="border-t border-cyan-border px-4 py-3">
                <p class="mb-2 text-xs font-medium uppercase tracking-widest text-cyan-tone">Original tool call</p>
                {#if originalCall}
                  <div class="legacy-tool edge-cyan-15 rounded-xl border border-cyan-border bg-inset/30 px-3 py-2">
                    <div class="flex flex-wrap items-start justify-between gap-3">
                      <div class="min-w-0">
                        <p class="font-semibold text-foreground [overflow-wrap:anywhere]">{displayToolName(originalCall.displayToolName ?? originalCall.toolName)}</p>
                        {#if toolCallSubtitle(originalCall)}
                          <p class="mt-1 text-xs leading-5 text-secondary [overflow-wrap:anywhere]">{toolCallSubtitle(originalCall)}</p>
                        {/if}
                      </div>
                      <div class="flex flex-wrap gap-2 text-[11px] text-secondary">
                        <span class="rounded-full border border-cyan-border px-2 py-0.5">{originalCall.status}</span>
                        {#if originalCall.timestamp}
                          <span class="rounded-full border border-cyan-border px-2 py-0.5" title={formatAbsoluteTime(originalCall.timestamp)}>{formatCompactTime(originalCall.timestamp, nowDate)}</span>
                        {/if}
                      </div>
                    </div>
                    <div class="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-muted">
                      <span>Call ID: <span class="font-mono text-foreground">{originalCall.callId}</span></span>
                      {#if originalCall.outputSize != null}
                        <span>Output: <span class="text-foreground">{originalCall.outputSize.toLocaleString()} chars</span></span>
                      {/if}
                      {#if originalCall.hasFullOutput}
                        <span class="rounded-full border border-success-border px-2 py-0.5 text-success">full output available</span>
                      {/if}
                      {#if originalCall.anchorsAvailable}
                        <span class="rounded-full border border-info-border px-2 py-0.5 text-info">anchors available</span>
                      {/if}
                    </div>
                    {#if canOpenToolOutput(originalCall) && scope}
                      <button class="mt-3 rounded-lg border border-control-border bg-surface px-3 py-1.5 text-xs font-medium text-foreground hover:bg-surface-hover" type="button" onclick={() => { openToolOutput(originalCall); }}>
                        Open original output
                      </button>
                    {/if}
                  </div>
                {:else}
                  <div class="legacy-tool bg-warning-10 edge-warning-25 ink-warning-100 rounded-xl border border-warning-border bg-warning-subtle px-3 py-2 text-xs leading-5 text-warning">
                    <p>The original tool call is not present in the loaded timeline page. The referenced call ID is <span class="font-mono">{outputHelper.sourceCallId}</span>.</p>
                    {#if scope}
                      <button class="mt-2 rounded-lg border border-control-border bg-surface px-3 py-1.5 text-xs font-medium text-foreground hover:bg-surface-hover" type="button" onclick={() => { openReferencedToolOutput(outputHelper.sourceCallId); }}>
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
                  class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
                  onclick={() => { originalCallExpanded = !originalCallExpanded; }}
                  type="button"
                >
                  <span>{originalCallExpanded ? '▼' : '▶'}</span>
                  <span>Original call raw</span>
                </button>
                {#if originalCallExpanded}
                  <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
                    {#if originalCall.arguments && Object.keys(originalCall.arguments).length > 0}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Input</p>
                        <pre class="max-h-[28vh] overflow-auto rounded-lg border border-separator bg-inset p-3 text-secondary">{formatCallArguments(originalCall)}</pre>
                      </div>
                    {/if}
                    {#if canShowInlineOriginalOutput(originalCall)}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Output</p>
                        <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-inset p-3 text-xs leading-5 ${originalCall.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if originalOutputData.html}{@html originalOutputData.html}{:else}{originalOutputData.text}{/if}</pre>
                      </div>
                    {:else if originalCall.result}
                      <p class="rounded-lg border border-separator bg-inset p-3 text-muted">Original output is too large to embed here. Use “Open original output” instead.</p>
                    {/if}
                  </div>
                {/if}
              </div>
            {/if}

            {#if hasRawPayload()}
              <div>
                <button
                  class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
                  onclick={() => { rawExpanded = !rawExpanded; }}
                  type="button"
                >
                  <span>{rawExpanded ? '▼' : '▶'}</span>
                  <span>Raw payload</span>
                </button>
                {#if rawExpanded}
                  <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
                    {#if item.arguments && Object.keys(item.arguments).length > 0}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Input</p>
                        <pre class="max-h-[28vh] overflow-auto rounded-lg border border-separator bg-inset p-3 text-secondary">{formattedArguments}</pre>
                      </div>
                    {/if}
                    {#if item.result != null}
                      <div>
                        <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Output</p>
                        <div class="relative">
                          <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-inset p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
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
            <div class="legacy-tool bg-success-5 edge-success-25 rounded-2xl border border-success-border bg-success-subtle px-4 py-3">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="text-xs font-medium uppercase tracking-widest text-success">Deliverable captured</p>
                  <h4 class="mt-1 truncate text-sm font-semibold text-foreground">{workflowPresentation.title}</h4>
                  <p class="mt-2 text-xs leading-5 text-secondary">{workflowPresentation.note}</p>
                </div>
                <div class="flex flex-wrap gap-2 text-[11px] text-secondary">
                  <span class="rounded-full border border-success-border px-2 py-0.5">{workflowPresentation.format}</span>
                  <span class="rounded-full border border-success-border px-2 py-0.5">{workflowPresentation.status}</span>
                  {#if workflowPresentation.length !== null}
                    <span class="rounded-full border border-success-border px-2 py-0.5">{workflowPresentation.length.toLocaleString()} chars</span>
                  {/if}
                   {#if workflowPresentation.version !== null}
                     <span class="rounded-full border border-success-border px-2 py-0.5">v{workflowPresentation.version}</span>
                   {/if}
                   {#if workflowPresentation.deliverableId}
                      {#key workflowPresentation.deliverableId}
                        <DeliverableReferenceActions
                          item={{ ...item, kind: 'assistant_deliverable', deliverable_id: workflowPresentation.deliverableId, title: workflowPresentation.title, format: workflowPresentation.format }}
                          conversationId={scope?.conversation_id || conversationId() || ''}
                        />
                      {/key}
                   {/if}
                 </div>
              </div>
              {#if workflowPresentation.deliverableId || workflowPresentation.outputKeys.length > 0}
                <div class="mt-3 flex flex-wrap gap-3 border-t border-success-border pt-2 text-[11px] text-secondary">
                  {#if workflowPresentation.deliverableId}
                    <span>Deliverable: <span class="font-mono text-foreground">{workflowPresentation.deliverableId}</span></span>
                  {/if}
                  {#if workflowPresentation.outputKeys.length > 0}
                    <span>Outputs: <span class="text-foreground">{workflowPresentation.outputKeys.join(', ')}</span></span>
                  {/if}
                </div>
              {/if}
            </div>
          {:else if workflowPresentation?.kind === 'step_complete'}
            <div class="legacy-tool bg-success-5 edge-success-25 rounded-2xl border border-success-border bg-success-subtle px-4 py-3">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="text-xs font-medium uppercase tracking-widest text-success">Step completed</p>
                  <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-foreground">{workflowPresentation.summary}</p>
                </div>
                <span class={`rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider ${outcomeClass(workflowPresentation.outcomeStatus)}`}>
                  {workflowPresentation.outcomeStatus}
                </span>
              </div>
              {#if workflowPresentation.outcomeReason}
                <p class="legacy-tool bg-warning-10 edge-warning-inner ink-warning-100 mt-3 rounded-xl border border-warning-border bg-warning-subtle px-3 py-2 text-xs leading-5 text-warning">{workflowPresentation.outcomeReason}</p>
              {/if}
              <div class="mt-3 flex flex-wrap gap-2 text-[11px] text-secondary">
                {#if workflowPresentation.claims.length > 0}
                  <span class="rounded-full border border-success-border px-2 py-0.5">{workflowPresentation.claims.length} claims</span>
                {/if}
                {#if workflowPresentation.outputKeys.length > 0}
                  <span class="rounded-full border border-success-border px-2 py-0.5">outputs: {workflowPresentation.outputKeys.join(', ')}</span>
                {/if}
                {#if workflowPresentation.metadataKeys.length > 0}
                  <span class="rounded-full border border-success-border px-2 py-0.5">metadata: {workflowPresentation.metadataKeys.join(', ')}</span>
                {/if}
                {#if workflowPresentation.notificationMode}
                  <span class="rounded-full border border-success-border px-2 py-0.5">notify: {workflowPresentation.notificationMode}</span>
                {/if}
              </div>
              {#if workflowPresentation.claims.length > 0}
                <div class="mt-4">
                  <p class="mb-2 text-xs font-medium uppercase tracking-widest text-success">Claims</p>
                  <ul class="space-y-2">
                    {#each workflowPresentation.claims as claim}
                      <li class="legacy-tool edge-success-inner rounded-xl border border-success-border bg-inset/30 px-3 py-2 text-sm leading-6 text-foreground">{claim}</li>
                    {/each}
                  </ul>
                </div>
              {/if}
              {#if hasStructuredEntries(workflowPresentation.outputs) || hasStructuredEntries(workflowPresentation.metadata) || workflowPresentation.notificationReason}
                <div class="mt-4 grid gap-3 md:grid-cols-2">
                  {#if hasStructuredEntries(workflowPresentation.outputs)}
                    <section class="legacy-tool edge-success-inner rounded-xl border border-success-border bg-inset/30 px-3 py-2">
                      <p class="mb-2 text-xs font-medium uppercase tracking-widest text-success">Outputs</p>
                      <dl class="space-y-2 text-xs">
                        {#each workflowPresentation.outputs as entry}
                          <div>
                            <dt class="font-mono text-secondary">{entry.key}</dt>
                            <dd class="mt-1 whitespace-pre-wrap text-foreground">{formatStructuredValue(entry.value)}</dd>
                          </div>
                        {/each}
                      </dl>
                    </section>
                  {/if}
                  {#if hasStructuredEntries(workflowPresentation.metadata)}
                    <section class="legacy-tool edge-success-inner rounded-xl border border-success-border bg-inset/30 px-3 py-2">
                      <p class="mb-2 text-xs font-medium uppercase tracking-widest text-success">Metadata</p>
                      <dl class="space-y-2 text-xs">
                        {#each workflowPresentation.metadata as entry}
                          <div>
                            <dt class="font-mono text-secondary">{entry.key}</dt>
                            <dd class="mt-1 whitespace-pre-wrap text-foreground">{formatStructuredValue(entry.value)}</dd>
                          </div>
                        {/each}
                      </dl>
                    </section>
                  {/if}
                  {#if workflowPresentation.notificationReason}
                    <section class="legacy-tool edge-success-inner rounded-xl border border-success-border bg-inset/30 px-3 py-2">
                      <p class="mb-2 text-xs font-medium uppercase tracking-widest text-success">Notification reason</p>
                      <p class="whitespace-pre-wrap text-xs leading-5 text-foreground">{workflowPresentation.notificationReason}</p>
                    </section>
                  {/if}
                </div>
              {/if}
            </div>
          {:else if workflowPresentation?.kind === 'step_todo_write'}
            <div class="legacy-tool bg-info-5 edge-todo-25 rounded-2xl border border-info-border bg-info-subtle px-4 py-3">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0">
                  <p class="text-xs font-medium uppercase tracking-widest text-info">Todos updated</p>
                  <p class="mt-1 text-sm text-foreground">{workflowPresentation.statusSummary || `${workflowPresentation.count} todos`}</p>
                </div>
                <div class="flex flex-wrap gap-2 text-[11px] text-secondary">
                  <span class="rounded-full border border-info-border px-2 py-0.5">{workflowPresentation.status}</span>
                  <span class="rounded-full border border-info-border px-2 py-0.5">{workflowPresentation.count} total</span>
                  {#if workflowPresentation.nonTerminalCount !== null}
                    <span class="rounded-full border border-info-border px-2 py-0.5">{workflowPresentation.nonTerminalCount} open</span>
                  {/if}
                  {#if workflowPresentation.unchanged}
                    <span class="legacy-tool bg-warning-10 edge-warning-option ink-warning-100 rounded-full border border-warning-border bg-warning-subtle px-2 py-0.5 text-warning">unchanged</span>
                  {/if}
                </div>
              </div>
              <ul class="mt-3 space-y-2">
                {#each workflowPresentation.todos as todo, index}
                  <li class="flex gap-2 rounded-xl border border-info-border bg-inset/30 px-3 py-2">
                    <span class="legacy-quiet mt-0.5 text-[11px] text-quiet">#{index + 1}</span>
                    <div class="min-w-0 flex-1">
                      <p class="text-sm leading-5 text-foreground">{todo.content}</p>
                    </div>
                    <span class={`inline-flex h-fit items-center gap-1.5 rounded-full border px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ${todoStatusClass(todo.status)}`}>
                      <TodoStatusDot status={todo.status} class="h-1.5 w-1.5" labelled={false} />
                      {todo.status}
                    </span>
                  </li>
                {/each}
              </ul>
              {#if workflowPresentation.guidance}
                <p class="legacy-tool bg-info-10 edge-info-hint ink-info-100 mt-3 rounded-xl border border-info-border bg-info-subtle px-3 py-2 text-xs leading-5 text-info">{workflowPresentation.guidance}</p>
              {/if}
            </div>
          {/if}
          {#if hasRawPayload()}
            <div>
              <button
                class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
                onclick={() => { rawExpanded = !rawExpanded; }}
                type="button"
              >
                <span>{rawExpanded ? '▼' : '▶'}</span>
                <span>Raw payload</span>
              </button>
              {#if rawExpanded}
                <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
                  {#if item.arguments && Object.keys(item.arguments).length > 0}
                    <div>
                      <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Input</p>
                      <pre class="max-h-[28vh] overflow-auto rounded-lg border border-separator bg-inset p-3 text-secondary">{formattedArguments}</pre>
                    </div>
                  {/if}
                  {#if item.result != null}
                    <div>
                      <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Output</p>
                      <div class="relative">
                        <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-inset p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
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
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-quiet">Diff</p>
            <DeferredPrivateView definition={feedbackDiffViewer} viewProps={{ diffs: item.fileDiffs }} label="file changes" />
          </div>
        {/if}

        {#if isPreparingPatch()}
          <div class="legacy-tool bg-info-5 edge-info-20 ink-info-50 rounded-2xl border border-info-border bg-info-subtle px-4 py-3 text-sm text-foreground">
            <LiveDots label={preparingPatchText()} size="sm" inline={true} />
            <p class="mt-2 text-xs text-secondary">
              Patch input is streaming from the provider. This will turn into the normal apply_patch call once the patch is complete.
            </p>
          </div>
        {/if}

        {#if hasDiffs()}
          <div>
            <button
              class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
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
            <div class={`overflow-hidden rounded-xl border ${item.isError ? 'border-danger-border' : 'border-control-border'} bg-inset shadow-inner`}>
              <div class="flex items-center justify-between border-b border-separator bg-chrome px-3 py-2 text-[11px] text-muted">
                <span class="command-scroll min-w-0 flex-1 overflow-x-auto whitespace-nowrap font-medium text-secondary" data-testid="tool-terminal-description-scroll">{terminalTitle()}</span>
                <span class="ml-3 flex shrink-0 items-center gap-2">
                  {#if backgroundConversationId}
                    <button
                      class="rounded-md border border-control-border bg-surface px-2 py-0.5 text-[11px] font-medium text-foreground transition hover:bg-surface-hover disabled:cursor-default disabled:opacity-60"
                      type="button"
                      data-testid="tool-move-to-background"
                      title="Keep the command running in the background and let the agent continue"
                      disabled={backgroundRequest !== 'idle'}
                      onclick={() => void moveToBackground()}
                    >
                      {backgroundRequest === 'idle' ? 'Move to background' : 'Moving to background…'}
                    </button>
                  {/if}
                  {#if isActiveToolStatus(item.status)}
                    <LiveDots inline={true} size="sm" tone="emerald" />
                  {/if}
                </span>
              </div>
              <pre bind:this={terminalEl} onscroll={onTerminalScroll} onpointerdown={pinTerminal} class={`max-h-[50vh] overflow-auto p-3 pr-10 font-mono text-xs leading-5 ${item.isError ? 'text-danger' : 'text-foreground'}`}><span class="text-accent">{terminalPrompt()}</span>{#if outputText}
              {#if TerminalOutputComponent}<TerminalOutputComponent text={`\n${outputText}`} />{:else}{`\n${outputText}`}{/if}{/if}</pre>
            </div>
            {#if canOpenToolOutput(item) && scope}
              <button class="mt-2 rounded-lg border border-control-border bg-surface px-3 py-1.5 text-xs font-medium text-foreground hover:bg-surface-hover" type="button" onclick={() => { pinTerminal(); openToolOutput(item); }}>
                {toolOutputOpenLabel(item)}
              </button>
            {/if}
          </div>
        {/if}

        {#if !detailsContent && (!hasDiffs() || rawExpanded) && !isBashTool() && !isDelegateTool() && !isRichWorkflowTool() && !isRichToolOutputHelper() && !isRichMemoryTool() && !isRichManagedConversationTool() && !isRichNativeInspectionTool() && !isRichWebTool()}
          {#if item.arguments && Object.keys(item.arguments).length > 0}
            {@const inputText = formattedArguments}
            {@const inputData = formattedArgumentsData}
            <div>
              <p class="mb-1 text-xs font-medium uppercase tracking-widest text-quiet">Input</p>
              <div class="relative">
                <pre class="max-h-[40vh] overflow-auto rounded-lg border border-separator bg-inset p-3 pr-10 text-xs leading-5 text-secondary">{#if inputData.html}{@html inputData.html}{:else}{inputData.text}{/if}</pre>
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
                  class="mt-1 text-xs text-accent hover:text-accent-hover"
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
                <p class="mb-1 text-xs font-medium uppercase tracking-widest text-quiet">Output</p>
                <div class="relative">
                  <pre class={`max-h-[40vh] overflow-auto rounded-lg border bg-inset p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if outputData.html}{@html outputData.html}{:else}{outputData.text}{/if}</pre>
                  <button class="copy-icon-button absolute right-2 top-2" onclick={() => void copyBox('output', outputText)} type="button" title="Copy output" aria-label="Copy output">
                    {#if copiedBox === 'output'}<Check class="h-3.5 w-3.5" />{:else}<Copy class="h-3.5 w-3.5" />{/if}
                  </button>
                </div>
                {#if canOpenToolOutput(item) && scope}
                  <button class="mt-2 rounded-lg border border-control-border bg-surface px-3 py-1.5 text-xs font-medium text-foreground hover:bg-surface-hover" type="button" onclick={() => { openToolOutput(item); }}>
                    {toolOutputOpenLabel(item)}
                  </button>
                {/if}
                {#if outputData.hiddenCount > 0}
                  <button class="mt-1 text-xs text-accent hover:text-accent-hover" onclick={() => { outputExpanded = !outputExpanded; }} type="button">
                    {outputExpanded ? 'Show less' : `Show all (${outputData.totalLines} lines)`}
                  </button>
                {/if}
              </div>
            {/if}
          {/if}
        {/if}

        {#if item.attachments && item.attachments.length > 0}
          <div>
            <p class="mb-1 text-xs font-medium uppercase tracking-widest text-quiet">Artifacts</p>
            <MessageAttachments attachments={item.attachments} />
          </div>
        {/if}
      {/if}

      {#if (isBashTool() || isRichWebTool()) && hasRawPayload()}
        <div>
          <button
            class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
            onclick={() => { rawExpanded = !rawExpanded; }}
            type="button"
          >
            <span>{rawExpanded ? '▼' : '▶'}</span>
            <span>Raw payload</span>
          </button>
          {#if rawExpanded}
            <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
              {#if item.arguments && Object.keys(item.arguments).length > 0}
                <div>
                  <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Input</p>
                  <pre class="max-h-[28vh] overflow-auto rounded-lg border border-separator bg-inset p-3 text-secondary">{formattedArguments}</pre>
                </div>
              {/if}
              {#if item.result != null}
                <div>
                  <p class="mb-1 font-medium uppercase tracking-widest text-quiet">Output</p>
                  <div class="relative">
                    <pre class={`max-h-[32vh] overflow-auto rounded-lg border bg-inset p-3 pr-10 text-xs leading-5 ${item.isError ? 'border-danger-border text-danger' : 'border-separator text-secondary'}`}>{#if rawOutputData.html}{@html rawOutputData.html}{:else}{rawOutputData.text}{/if}</pre>
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

      <!-- Evaluation metadata (from Intaris) -->
      {#if item.evaluation}
        <div>
          <button
            class="flex items-center gap-2 text-xs font-medium uppercase tracking-widest text-quiet transition hover:text-secondary"
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
            <div class="mt-2 space-y-2 rounded-lg border border-separator bg-inset/40 p-3 text-xs">
              {#if item.evaluation.reasoning}
                <div>
                  <span class="legacy-quiet font-medium text-quiet">Reasoning:</span>
                  <span class="ml-1 text-secondary">{item.evaluation.reasoning}</span>
                </div>
              {/if}
              <div class="flex flex-wrap gap-3 text-muted">
                {#if item.evaluation.path}
                  <span>Path: <span class="text-secondary">{item.evaluation.path}</span></span>
                {/if}
                {#if item.evaluation.latency_ms != null}
                  <span>Latency: <span class="text-secondary">{item.evaluation.latency_ms}ms</span></span>
                {/if}
              </div>
            </div>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</article>

<style>
  .helper-managed {
    background: var(--helper-managed-bg, rgb(var(--color-violet-subtle)));
    border-color: var(--helper-managed-border, rgb(var(--color-violet-border)));
  }
  .helper-managed > .helper-header {
    border-color: var(--helper-managed-header-border, rgb(var(--color-violet-border)));
  }
  .helper-failed {
    background: var(--helper-error-bg, rgb(var(--color-danger-subtle)));
    border-color: var(--helper-error-border, rgb(var(--color-danger-border)));
  }
  .helper-failed > .helper-header {
    border-color: var(--helper-error-header-border, rgb(var(--color-danger-border)));
  }
  .tool-frame, .helper-frame { border-width: var(--tool-group-border, 1px); }
  .command-scroll {
    scrollbar-width: none;
  }

  .command-scroll::-webkit-scrollbar {
    display: none;
    width: 0;
    height: 0;
  }
</style>

{#if outputDrawerLoaded}
  {#await import('$lib/components/ToolOutputDrawer.svelte').then((module) => module.default) then ToolOutputDrawer}
    <ToolOutputDrawer
      open={outputDrawerOpen}
      {scope}
      callId={drawerItem.recoveryCallId ?? drawerItem.callId}
      toolName={drawerItem.toolName}
      isTerminal={isBashTool(drawerItem)}
      onClose={() => { outputDrawerOpen = false; outputDrawerTarget = null; }}
    />
  {/await}
{/if}
