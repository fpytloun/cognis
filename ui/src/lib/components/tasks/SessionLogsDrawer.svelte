<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import { portal } from '$lib/actions/portal';
  import ArrowLeft from 'lucide-svelte/icons/arrow-left';
  import Maximize2 from 'lucide-svelte/icons/maximize-2';
  import Minimize2 from 'lucide-svelte/icons/minimize-2';
  import X from 'lucide-svelte/icons/x';
  import { api } from '$lib/api/client';
  import { wsClient } from '$lib/ws/client';
  import ChatWorkspace from '$lib/components/chat-v2/ChatWorkspace.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import SessionLogHeader from '$lib/components/chat-v2/SessionLogHeader.svelte';
  import SessionDetailsButton from '$lib/components/session/SessionDetailsButton.svelte';
  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import StatusBadge from '$lib/components/ui/StatusBadge.svelte';
  import StepResultView from '$lib/components/tasks/StepResultView.svelte';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import type { Agent, Escalation, Notification, StepRun, TaskDeliverableFreshness } from '$lib/types/api';
  import type { TimelineScope } from '$lib/chat-v2/types';

  let {
    conversationId,
    sessionId,
    stepRunId = null,
    taskId = null,
    stepName = '',
    agent = null,
    stepRun = null,
    deliverableFreshness,
    initialTab = 'transcript',
    visibleStatus = null,
    attempts = [],
    agents = [],
    backLabel = 'Parent session',
    onBack,
    onViewSession,
    onclose
  } = $props<{
    conversationId: string;
    sessionId: string;
    stepRunId?: string | null;
    taskId?: string | null;
    stepName?: string;
    agent?: Agent | null;
    stepRun?: StepRun | null;
    deliverableFreshness?: Record<string, TaskDeliverableFreshness>;
    /** Step viewer tab to open on; the Result tab exists only for a step run. */
    initialTab?: 'result' | 'transcript';
    /** Task-aware step status (for example paused while the task is paused). */
    visibleStatus?: string | null;
    attempts?: StepRun[];
    agents?: Agent[];
    backLabel?: string;
    onBack?: (() => void) | undefined;
    onViewSession?: ((sessionId: string) => void | Promise<void>) | undefined;
    onclose: () => void;
  }>();

  let panel = $state<HTMLElement | null>(null);
  let overlayId = $state<string | null>(null);
  let previousFocus: HTMLElement | null = null;
  let runtimeActive = $state(false);
  let escalations = $state<Escalation[]>([]);
  let escalationBusy = $state<string | null>(null);
  let sessionDetailsOpen = $state(false);
  let fullscreen = $state(false);
  let workspace: ChatWorkspace | undefined = $state();
  let chosenAttempt = $state<StepRun | null>(null);
  let attemptError = $state('');
  let attemptLoading = $state(false);
  let attemptGeneration = 0;
  let focusedScope = $state<TimelineScope | null>(null);
  let focusedRootKey = $state('');
  let focusedSessionId = $state('');
  let escalationGeneration = 0;
  let escalationRequest = 0;
  let escalationError = $state('');
  const displayedStepRun = $derived(chosenAttempt ?? stepRun);
  const displayedSessionId = $derived(chosenAttempt?.session_id ?? sessionId);
  const displayedConversationId = $derived(chosenAttempt?.conversation_id ?? conversationId);
  const displayedStepRunId = $derived(chosenAttempt?.step_run_id ?? stepRunId);
  const displayedAgent = $derived(agents.find((item: Agent) => item.agent_id === displayedStepRun?.agent_id) ?? agent);
  const stepAttempts = $derived(attempts
    .filter((item: StepRun) => item.step_name === displayedStepRun?.step_name && item.session_id)
    .toSorted((left: StepRun, right: StepRun) =>
      (left.attempt_number ?? 1) - (right.attempt_number ?? 1) || left.attempt - right.attempt));
  const hasTranscript = $derived(Boolean(displayedSessionId));
  // One step viewer: Result shows what the step produced, Transcript the session.
  let activeTab = $state<'result' | 'transcript'>(untrack(() => initialTab));
  const shownTab = $derived(!displayedStepRun ? 'transcript' : !hasTranscript ? 'result' : activeTab);
  const viewerTabs = $derived([
    { id: 'result', label: 'Result' },
    ...(hasTranscript ? [{ id: 'transcript', label: 'Transcript' }] : []),
  ]);
  const resultStatus = $derived(
    chosenAttempt || !visibleStatus ? displayedStepRun?.status ?? '' : visibleStatus
  );
  const displayedAttemptNumber = $derived(
    stepAttempts.findIndex((item: StepRun) => item.step_run_id === displayedStepRunId) + 1
      || displayedStepRun?.attempt_number || displayedStepRun?.attempt || 1);

  $effect(() => {
    void stepRunId;
    chosenAttempt = null;
    attemptGeneration += 1;
    attemptLoading = false;
    attemptError = '';
  });

  async function selectAttempt(id: string): Promise<void> {
    const generation = ++attemptGeneration;
    attemptLoading = true;
    attemptError = '';
    try {
      const detail = await api.tasks.stepRunDetail(id);
      if (generation === attemptGeneration) chosenAttempt = detail;
    } catch (error) {
      if (generation === attemptGeneration) attemptError = error instanceof Error ? error.message : 'Could not load attempt.';
    } finally {
      if (generation === attemptGeneration) attemptLoading = false;
    }
  }

  const scope = $derived.by<TimelineScope>(() => displayedStepRunId && taskId
    ? {
        key: `task_step:${displayedStepRunId}`,
        kind: 'task_step',
        conversation_id: displayedConversationId,
        session_id: displayedSessionId,
        task_id: taskId,
        step_run_id: displayedStepRunId,
        label: stepName
      }
    : {
        key: `session:${displayedSessionId}`,
        kind: 'session',
        conversation_id: displayedConversationId,
        session_id: displayedSessionId,
        label: stepName
      });
  const interactionScope = $derived(focusedRootKey === scope.key && focusedScope ? focusedScope : scope);
  const interactionSessionId = $derived(focusedRootKey === scope.key && focusedSessionId ? focusedSessionId : displayedSessionId);
  const interactionKey = $derived(`${interactionScope.key}:${interactionSessionId}`);

  function handleKeydown(event: KeyboardEvent): void {
    if (!isTopOverlay(overlayId)) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!workspace?.handleEscape()) onclose();
      return;
    }
    if (event.key !== 'Tab' || !panel) return;
    const elements = Array.from(panel.querySelectorAll<HTMLElement>('button, select, [href], [tabindex]:not([tabindex="-1"])'))
      // The hidden tab panel stays mounted; its controls are not reachable.
      .filter((element) => !element.hasAttribute('disabled') && !element.closest('.hidden'));
    if (elements.length === 0) return;
    const first = elements[0];
    const last = elements[elements.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleBackdropClick(event: MouseEvent): void {
    if (event.target === event.currentTarget) onclose();
  }

  function notificationEscalation(item: Notification): Escalation | null {
    if (item.notification_type !== 'escalation' || item.status !== 'pending' || item.session_id !== interactionSessionId) return null;
    return {
      call_id: item.notification_id,
      session_id: item.session_id,
      tool_name: typeof item.payload.tool_name === 'string' ? item.payload.tool_name : null,
      arguments_display:
        item.payload.arguments_display
        && typeof item.payload.arguments_display === 'object'
        && !Array.isArray(item.payload.arguments_display)
          ? item.payload.arguments_display as Record<string, unknown>
          : null,
      decision: 'escalate',
      resolved: false,
      reasoning: typeof item.payload.reasoning === 'string' ? item.payload.reasoning : null,
      risk: typeof item.payload.risk === 'string' ? item.payload.risk : null,
      timeout_seconds: typeof item.payload.timeout_seconds === 'number' ? item.payload.timeout_seconds : 300,
      received_at: item.created_at ? Date.parse(item.created_at) : Date.now(),
    };
  }

  async function refreshEscalations(): Promise<void> {
    const selectedScope = interactionKey;
    const request = ++escalationRequest;
    const notifications = await api.notifications.list(interactionScope.conversation_id ?? displayedConversationId, { sessionId: interactionSessionId });
    if (interactionKey !== selectedScope || request !== escalationRequest) return;
    escalations = notifications
      .map(notificationEscalation)
      .filter((item): item is Escalation => item !== null)
      .sort((left, right) => (left.received_at ?? 0) - (right.received_at ?? 0));
  }

  function secondsRemaining(item: Escalation): number {
    return Math.max(Math.ceil((item.timeout_seconds ?? 300) - (Date.now() - (item.received_at ?? Date.now())) / 1000), 0);
  }

  async function resolveEscalation(item: Escalation, decision: 'approve' | 'deny'): Promise<void> {
    const generation = escalationGeneration;
    escalationBusy = item.call_id;
    try {
      await api.notifications.resolve(item.call_id, { decision });
      if (generation === escalationGeneration) await refreshEscalations();
    } catch (error) {
      if (generation === escalationGeneration) escalationError = error instanceof Error ? error.message : 'Could not resolve pending action.';
    } finally {
      if (generation === escalationGeneration) escalationBusy = null;
    }
  }

  const activityLabel = $derived(displayedStepRun
    ? displayedStepRun.status === 'evaluating'
      ? 'Evaluator is reviewing…'
      : displayedStepRun.status === 'running'
        ? 'Agent is working…'
        : displayedStepRun.status || 'Paused'
    : runtimeActive
      ? 'Agent is working…'
      : 'Paused'
  );

  onMount(() => {
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const handle = registerOverlay({ kind: 'sheet', blocksChrome: true });
    overlayId = handle.id;
    queueMicrotask(() => panel?.querySelector<HTMLElement>('button')?.focus());
    const unsubscribe = wsClient.subscribe(message => {
      if ((message.type === 'escalation' && message.session_id === interactionSessionId)
        || (message.type === 'escalation_resolved' && escalations.some(item => item.call_id === message.call_id))
        || (message.type === 'scope_invalidated' && message.reason === 'notification_state_changed'
          && (message.session_id === interactionSessionId || message.conversation_id === interactionScope.conversation_id))) {
        void refreshEscalations().catch(() => {});
      }
    });
    return () => {
      unsubscribe();
      attemptGeneration += 1;
      escalationGeneration += 1;
      escalationRequest += 1;
      handle.unregister();
      overlayId = null;
      queueMicrotask(() => previousFocus?.focus());
    };
  });

  $effect(() => {
    void interactionKey;
    escalationGeneration += 1;
    escalationBusy = null;
    escalationError = '';
    escalations = [];
    // A step without a recorded session (Result only) has nothing to approve.
    if (!interactionSessionId) return;
    untrack(() => { void refreshEscalations().catch(() => {}); });
  });
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div
  use:portal
  class="app-viewport-overlay z-[95] flex justify-end overflow-hidden bg-scrim/60"
  onclick={handleBackdropClick}
  role="presentation"
  data-testid="session-logs-overlay"
  data-overlay-id={overlayId}
>
  <div
    bind:this={panel}
    class={`app-safe-side-panel ml-auto flex h-full min-h-0 w-full flex-col overflow-hidden overscroll-contain border-l border-control-border bg-elevated shadow-overlay ${fullscreen ? 'max-w-none' : 'max-w-6xl'}`}
    role="dialog"
    aria-modal="true"
    aria-label={displayedStepRun ? `Step: ${stepName || displayedStepRun.step_name}` : `Session logs: ${stepName || sessionId}`}
    data-testid="session-logs-panel" data-find-container
    data-fullscreen={fullscreen}
  >
    <SessionLogHeader title={stepName || sessionId} testId="session-logs-header">
      {#snippet leading()}
        {#if onBack}
          <button class="touch-target-compact inline-flex shrink-0 items-center justify-center rounded-lg text-muted hover:bg-raised hover:text-strong" onclick={onBack} title={`Back to ${backLabel}`} aria-label={`Back to ${backLabel}`} type="button">
            <ArrowLeft class="h-4 w-4" />
          </button>
        {/if}
      {/snippet}
      {#snippet metadata()}
        <div class={stepAttempts.length > 1 ? 'relative h-[32px]' : undefined} data-testid="session-activity-status">
          {#if stepAttempts.length > 1}
            <!-- Keep the native touch target full-sized without expanding the metadata line. -->
            <select class="touch-target-compact absolute bottom-0 left-0 !w-auto max-w-full rounded-lg border-0 bg-transparent text-xs text-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent" aria-label="Step attempt"
              value={displayedStepRunId ?? ''} disabled={attemptLoading}
              onchange={(event) => { void selectAttempt(event.currentTarget.value); }}>
              {#each stepAttempts as attempt, index (attempt.step_run_id)}
                <option value={attempt.step_run_id}>Attempt {index + 1} of {stepAttempts.length} · {attempt.status}</option>
              {/each}
            </select>
          {:else if displayedStepRun && shownTab === 'result'}
            <span class="inline-flex items-center gap-2">Attempt {displayedAttemptNumber} <StatusBadge status={resultStatus} /></span>
          {:else}
            {#if displayedStepRun}Attempt {displayedAttemptNumber} · {/if}{activityLabel}
          {/if}
        </div>
      {/snippet}
      {#snippet actions()}
        <button
          class="touch-target-compact hidden shrink-0 items-center justify-center rounded-lg border border-control-border text-muted hover:bg-raised hover:text-strong sm:inline-flex"
          onclick={() => { fullscreen = !fullscreen; }}
          aria-label={fullscreen ? 'Exit fullscreen logs' : 'Open fullscreen logs'}
          aria-pressed={fullscreen}
          type="button"
        >
          {#if fullscreen}<Minimize2 class="h-4 w-4" />{:else}<Maximize2 class="h-4 w-4" />{/if}
        </button>
        {#if shownTab === 'transcript'}
        <SessionDetailsButton
          open={sessionDetailsOpen}
          ariaControls={workspace?.getInspectorState().controlsId ?? `dashboard-conversation-inspector-${displayedConversationId}`}
          testId="session-logs-header-info"
          onclick={() => { void workspace?.toggleInspector(); }}
        />
        {/if}
        <button class="touch-target-compact inline-flex shrink-0 items-center justify-center rounded-lg border border-control-border text-muted hover:bg-raised hover:text-strong" onclick={onclose} aria-label="Close" type="button"><X class="h-4 w-4" /></button>
      {/snippet}
    </SessionLogHeader>
    {#if displayedStepRun}
      <div class="shrink-0 px-4">
        <AccessibleTabs
          tabs={viewerTabs}
          activeId={shownTab}
          idPrefix="step-viewer"
          ariaLabel="Step viewer"
          testIdPrefix="step-viewer-tab"
          onChange={(id) => { activeTab = id as 'result' | 'transcript'; }}
        />
      </div>
    {/if}
    {#if attemptError}<p class="px-4 py-2 text-xs text-danger" role="alert">{attemptError}</p>{/if}
    {#if escalationError}<p class="px-4 py-2 text-xs text-danger" role="alert">{escalationError}</p>{/if}
    {#if shownTab === 'result' && displayedStepRun}
      <div class="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-5 sm:px-6" role="tabpanel" id="step-viewer-panel-result" aria-labelledby="step-viewer-tab-result">
        {#key displayedStepRun.step_run_id}
          <StepResultView stepRun={displayedStepRun} {deliverableFreshness} visibleStatus={resultStatus} />
        {/key}
      </div>
    {/if}
    {#if hasTranscript}
    <!-- Stays mounted while Result is shown so switching back keeps the timeline position. -->
    <div
      class={shownTab === 'transcript' ? 'flex min-h-0 flex-1 flex-col' : 'hidden'}
      role={displayedStepRun ? 'tabpanel' : undefined}
      id={displayedStepRun ? 'step-viewer-panel-transcript' : undefined}
      aria-labelledby={displayedStepRun ? 'step-viewer-tab-transcript' : undefined}
    >
    {#if escalations.length > 0}
      {@const escalation = escalations[0]}
      <div class="shrink-0 border-b border-separator px-4 py-3">
        <EscalationPrompt
          item={escalation}
          secondsRemaining={secondsRemaining(escalation)}
          pending={escalationBusy === escalation.call_id}
          queuedCount={escalations.length - 1}
          onApprove={() => resolveEscalation(escalation, 'approve')}
          onDeny={() => resolveEscalation(escalation, 'deny')}
        />
      </div>
    {/if}
    <ChatWorkspace
      bind:this={workspace}
      conversationId={displayedConversationId}
      sessionId={displayedSessionId}
      timelineScope={scope}
      agent={displayedAgent}
      agents={agents.length ? agents : agent ? [agent] : []}
      stepRun={displayedStepRun}
      {deliverableFreshness}
      readOnly
      inspectorOpen={false}
      inspectorControlInHeader
      inspectorAsRegion
      onInspectorStateChange={(open) => { sessionDetailsOpen = open; }}
      onRuntimeActiveChange={(active) => { runtimeActive = active; }}
      onDisplayedScopeChange={(next, selectedSessionId) => {
        focusedRootKey = scope.key;
        focusedScope = next;
        focusedSessionId = selectedSessionId;
      }}
      emptyLabel="No events recorded yet."
    />
    </div>
    {/if}
  </div>
</div>
