<script lang="ts">
  import { setContext } from 'svelte';
  import { writable } from 'svelte/store';
  import { DELIVERABLE_SHARING_CONTEXT } from '$lib/deliverable-permissions';
  import AssistantDeliverableBlock from '$lib/components/AssistantDeliverableBlock.svelte';
  import { goto } from '$app/navigation';

  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { useSectionConnectivity } from '$lib/connectivity-section.svelte';
  import ConversationModalWorkspace from '$lib/components/dashboard/ConversationModalWorkspace.svelte';
  import AppContactChat from '$lib/components/AppContactChat.svelte';
  import { appContactBindingId } from '$lib/app-contacts';
  import CompactConversationChat from '$lib/components/chat-v2/CompactConversationChat.svelte';
  import DashboardModalShell from '$lib/components/dashboard/DashboardModalShell.svelte';
  import ScheduleDashboardModal from '$lib/components/dashboard/ScheduleDashboardModal.svelte';
  import ScheduleDetailWorkspace from '$lib/components/schedules/ScheduleDetailWorkspace.svelte';
  import TaskBrief from '$lib/components/task-cockpit/TaskBrief.svelte';
  import AttentionPanel from '$lib/components/task-cockpit/AttentionPanel.svelte';
  import WorkflowPhases from '$lib/components/task-cockpit/WorkflowPhases.svelte';
  import TaskProgressPanel from '$lib/components/task-cockpit/TaskProgressPanel.svelte';
  import TaskControlChat from '$lib/components/task-cockpit/TaskControlChat.svelte';
  import TaskDashboardControl from '$lib/components/dashboard/TaskDashboardControl.svelte';
  import TaskWorkPanel from '$lib/components/task-cockpit/TaskWorkPanel.svelte';
  import SessionLogsDrawer from '$lib/components/tasks/SessionLogsDrawer.svelte';
  import { api, asApiError } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import type { Agent, Conversation, QuestionSetAnswer, StepRun, TaskControlChatResponse, TaskDetail } from '$lib/types/api';
  import PinConversationButton from '$lib/components/chat/PinConversationButton.svelte';
  import { conversationWorkspace as retainedWorkspace } from '$lib/dashboard/conversation-workspace';

  type TaskTab = 'description' | 'steps' | 'control' | 'control-chat' | 'activity' | 'deliverable';

  let {
    kind,
    taskId = null,
    conversationId = null,
    scheduleId = null,
    agents = [],
    modalRefreshToken = 0,
    embedded = false,
    active = true,
    onClose,
    onConversationInitialLoaded,
    onTaskMutationSettled,
    onMetadataChange,
    inspectorOpen = true,
    inspectorControlInHeader = false,
    onInspectorStateChange,
    onContinueConversation,
    onOpenTask,
    canMutateSchedule = true,
  } = $props<{
    kind: 'task' | 'conversation' | 'schedule';
    taskId?: string | null;
    conversationId?: string | null;
    scheduleId?: string | null;
    agents?: Agent[];
    modalRefreshToken?: number;
    embedded?: boolean;
    active?: boolean;
    onClose: () => void;
    onConversationInitialLoaded?: (conversationId: string) => void | Promise<void>;
    onTaskMutationSettled?: (taskId: string, changed: boolean) => void | Promise<void>;
    onMetadataChange?: (metadata: {
      title: string;
      status: string | null;
      agentId: string | null;
    }) => void;
    inspectorOpen?: boolean;
    inspectorControlInHeader?: boolean;
    onInspectorStateChange?: (open: boolean) => void;
    onContinueConversation?: (conversation: Conversation) => void;
    onOpenTask?: (taskId: string) => void;
    canMutateSchedule?: boolean;
  }>();

  let conversationTab = $state<'chat' | 'activity'>('chat');
  let taskTab = $state<TaskTab>('control');
  let loading = $state(true);
  let error = $state<string | null>(null);
  let refreshing = $state(false);
  let refreshError = $state<string | null>(null);
  // Load/refresh errors stay hidden while the global connection banner
  // applies; the detail reloads when the connection is restored.
  const net = useSectionConnectivity({
    hasError: () => Boolean(error || refreshError),
    retry: () => load(false),
  });
  let actionBusy = $state(false);
  let actionError = $state<string | null>(null);
  let taskDetail = $state<TaskDetail | null>(null);
  const sharingPermission = writable(false);
  setContext(DELIVERABLE_SHARING_CONTEXT, sharingPermission);
  let taskChat = $state<TaskControlChatResponse | null>(null);
  let taskChatLoading = $state(false);
  let taskChatError = $state<string | null>(null);
  let conversationDetail = $state<Conversation | null>(null);
  const contactBindingId = $derived(appContactBindingId(conversationDetail));
  // One step viewer serves both the Result and the Transcript entry points.
  let viewerStepRun = $state<StepRun | null>(null);
  let viewerTab = $state<'result' | 'transcript'>('result');
  let stepViewerLoading = $state(false);
  let stepViewerError = $state<string | null>(null);
  let stepViewerRequest = 0;
  let loadRequest = 0;
  let boundKey = '';
  let seenModalRefreshToken = $state<number | null>(null);
  let conversationWorkspace = $state<{
    handleEscape(): boolean;
    toggleInspector(open?: boolean): Promise<void>;
  } | null>(null);
  let modalInspectorOpen = $state(true);
  let modalInspectorControlInHeader = $derived(
    inspectorControlInHeader || (!embedded && kind === 'conversation'),
  );

  $effect(() => {
    modalInspectorOpen = inspectorOpen;
  });

  const taskTabs = $derived([
    { id: 'description', label: 'Description' },
    { id: 'steps', label: 'Steps' },
    { id: 'control', label: 'Control' },
    { id: 'control-chat', label: 'Control chat' },
    { id: 'activity', label: 'Activity' },
    { id: 'deliverable', label: 'Deliverable' }
  ].filter((tab) => canMutateTask || !['control-chat', 'activity'].includes(tab.id)));
  const resolvedConversationId = $derived(kind === 'task' ? taskChat?.conversation_id ?? null : conversationId);
  const agentId = $derived(kind === 'task' ? taskDetail?.agent_id ?? null : conversationDetail?.agent_id ?? null);
  const agent = $derived<Agent | null>(agents.find((item: Agent) => item.agent_id === agentId) ?? null);
  const statusLabel = $derived(kind === 'task' ? taskDetail?.status ?? null : conversationDetail?.status ?? null);
  const canMutateTask = $derived(
    $auth.user !== null && $auth.user.role !== 'viewer'
      && taskDetail?.created_by === $auth.user.email
  );
  $effect(() => {
    sharingPermission.set(kind === 'task' ? canMutateTask : Boolean(conversationDetail && !conversationDetail.is_readonly_for_caller));
  });
  const title = $derived(kind === 'task' ? taskDetail?.title ?? 'Task' : conversationDetail?.title ?? 'Conversation');
  const canonicalHref = $derived(kind === 'task' && taskId ? `/tasks/${taskId}` : conversationId ? `/chat/${conversationId}` : null);
  const canonicalDeliverableId = $derived(
    typeof taskDetail?.result_data?.final_deliverable_id === 'string'
      ? taskDetail.result_data.final_deliverable_id
      : null
  );
  const activePause = $derived(
    taskDetail?.pending_pause &&
    (
      taskDetail.pending_pause.pause_type === 'gate' ||
      (
        ['step_input', 'step_question'].includes(taskDetail.pending_pause.pause_type) &&
        (taskDetail.pending_pause.questions?.length ?? 0) > 0
      )
    )
      ? taskDetail.pending_pause
      : null
  );

  async function loadTaskChat(): Promise<void> {
    if (!canMutateTask || !taskId || taskChat || taskChatLoading) return;
    taskChatLoading = true;
    taskChatError = null;
    try {
      taskChat = await api.tasks.controlChat(taskId);
    } catch (caught) {
      taskChatError = asApiError(caught).message || 'Could not open task control chat.';
    } finally {
      taskChatLoading = false;
    }
  }

  type DetailLoadResult = 'refreshed' | 'superseded' | 'not-found' | 'failed';

  async function load(setDefaultTab = true): Promise<DetailLoadResult> {
    const request = ++loadRequest;
    const requestKey = boundKey;
    const blocking = (
      (kind === 'task' && taskDetail === null)
      || (kind === 'conversation' && conversationDetail === null)
    );
    if (blocking) {
      loading = true;
      error = null;
    } else {
      refreshing = true;
      refreshError = null;
    }
    try {
      if (kind === 'task' && taskId) {
        const detail = await api.tasks.detail(taskId);
        if (request !== loadRequest || requestKey !== boundKey) return 'superseded';
        taskDetail = detail;
        onMetadataChange?.({ title: detail.title ?? 'Task', status: detail.status, agentId: detail.agent_id });
        if (setDefaultTab) {
          taskTab = detail.created_by !== $auth.user?.email
            ? 'description'
            : ['completed', 'failed', 'cancelled'].includes(detail.status)
            ? 'deliverable'
            : detail.pending_pause
              ? 'description'
              : 'control';
        }
      } else if (kind === 'conversation' && conversationId) {
        const detail = await api.conversations.detail(conversationId, { includeState: false });
        if (request !== loadRequest || requestKey !== boundKey) return 'superseded';
        conversationDetail = detail;
        retainedWorkspace.updateConversation(detail);
        onMetadataChange?.({
          title: detail.title ?? 'Conversation',
          status: detail.status ?? null,
          agentId: detail.agent_id ?? null,
        });
      }
    } catch (caught) {
      if (request !== loadRequest || requestKey !== boundKey) return 'superseded';
      const apiError = asApiError(caught);
      const message = apiError.message || 'Could not open this item.';
      if (blocking) error = message;
      else refreshError = message;
      return apiError.status === 404 ? 'not-found' : 'failed';
    } finally {
      if (request === loadRequest && requestKey === boundKey) {
        if (blocking) loading = false;
        else refreshing = false;
      }
    }
    return 'refreshed';
  }

  async function respondToGate(action: string, instruction?: string): Promise<void> {
    if (!taskId || !activePause) return;
    actionBusy = true;
    actionError = null;
    try {
      await api.tasks.gateResponse(taskId, {
        pause_id: activePause.pause_id,
        step_name: activePause.step_name,
        action,
        feedback: instruction?.trim() || null
      });
      await load(false);
    } catch (caught) {
      actionError = asApiError(caught).message || 'Could not submit the task response.';
      await load(false);
    } finally {
      actionBusy = false;
    }
  }

  async function respondToQuestion(answers: QuestionSetAnswer[]): Promise<void> {
    if (!taskId) return;
    actionBusy = true;
    actionError = null;
    try {
      await api.tasks.stepResponse(taskId, { mode: 'structured', answers });
      await load(false);
    } catch (caught) {
      actionError = asApiError(caught).message || 'Could not submit the task response.';
    } finally {
      actionBusy = false;
    }
  }

  async function refreshTaskControl(): Promise<DetailLoadResult> {
    return load(false);
  }

  function latestStepRun(stepName: string): StepRun | null {
    const matches = (taskDetail?.step_runs ?? []).filter((run) => run.step_name === stepName);
    return matches.sort((left, right) =>
      (right.updated_at ?? right.completed_at ?? '').localeCompare(left.updated_at ?? left.completed_at ?? '')
    )[0] ?? null;
  }

  async function hydrateStepRun(stepRun: StepRun): Promise<StepRun> {
    if (!stepRun.is_projection) return stepRun;
    const detail = await api.tasks.stepRunDetail(stepRun.step_run_id);
    if (taskDetail) {
      taskDetail = {
        ...taskDetail,
        step_runs: taskDetail.step_runs.map((run) =>
          run.step_run_id === detail.step_run_id ? detail : run
        )
      };
    }
    return detail;
  }

  async function openStepOutput(stepName: string): Promise<void> {
    const run = latestStepRun(stepName);
    if (!run) return;
    const request = ++stepViewerRequest;
    stepViewerLoading = true;
    stepViewerError = null;
    try {
      const hydrated = await hydrateStepRun(run);
      if (request !== stepViewerRequest) return;
      viewerTab = 'result';
      viewerStepRun = hydrated;
    } catch (caught) {
      if (request === stepViewerRequest) {
        stepViewerError = asApiError(caught).message || 'Could not load step output.';
      }
    } finally {
      if (request === stepViewerRequest) stepViewerLoading = false;
    }
  }

  async function openStepLogs(stepName: string): Promise<void> {
    const run = latestStepRun(stepName);
    if (!run) return;
    const request = ++stepViewerRequest;
    stepViewerLoading = true;
    stepViewerError = null;
    try {
      const hydrated = await hydrateStepRun(run);
      if (request !== stepViewerRequest) return;
      viewerTab = 'transcript';
      viewerStepRun = hydrated;
    } catch (caught) {
      if (request === stepViewerRequest) {
        stepViewerError = asApiError(caught).message || 'Could not load step logs.';
      }
    } finally {
      if (request === stepViewerRequest) stepViewerLoading = false;
    }
  }

  function changeTaskTab(id: string): void {
    taskTab = id as TaskTab;
    if (taskTab === 'steps') return;
    stepViewerRequest += 1;
    stepViewerLoading = false;
    stepViewerError = null;
  }

  $effect(() => {
    const key = `${kind}:${taskId ?? ''}:${conversationId ?? ''}`;
    if (key === boundKey) return;
    loadRequest += 1;
    boundKey = key;
    conversationTab = 'chat';
    taskDetail = null;
    taskChat = null;
    taskChatError = null;
    conversationDetail = null;
    loading = true;
    error = null;
    refreshing = false;
    refreshError = null;
    viewerStepRun = null;
    stepViewerRequest += 1;
    stepViewerLoading = false;
    stepViewerError = null;
    void load();
  });

  $effect(() => {
    const token = modalRefreshToken;
    if (seenModalRefreshToken === null) {
      seenModalRefreshToken = token;
      return;
    }
    if (token === seenModalRefreshToken) return;
    seenModalRefreshToken = token;
    if (boundKey) void load(false);
  });

  $effect(() => {
    if (kind === 'task' && taskTab === 'control-chat' && taskDetail && !taskChatError) void loadTaskChat();
  });

  function openCanonical(event: MouseEvent): void {
    event.stopPropagation();
    if (!canonicalHref) return;
    onClose();
    void goto(canonicalHref);
  }

  export function handleEscape(): boolean {
    if (conversationWorkspace?.handleEscape()) return true;
    if (!active || viewerStepRun) return false;
    if (!stepViewerLoading) return false;
    stepViewerRequest += 1;
    stepViewerLoading = false;
    return true;
  }

  function handleInspectorStateChange(open: boolean): void {
    modalInspectorOpen = open;
    onInspectorStateChange?.(open);
  }

  export function toggleInspector(open = !modalInspectorOpen): Promise<void> | undefined {
    return conversationWorkspace?.toggleInspector(open);
  }

</script>

{#if kind === 'schedule' && scheduleId}
  {#if onOpenTask}
    <DashboardModalShell title="Schedule" {embedded} {onClose} testId="dashboard-schedule-modal">
      <div class="min-h-0 min-w-0 flex-1 overflow-auto">
        {#key scheduleId}<ScheduleDetailWorkspace id={scheduleId} embedded canMutate={canMutateSchedule} onBack={onClose} {onOpenTask} />{/key}
      </div>
    </DashboardModalShell>
  {:else}
  <ScheduleDashboardModal
    {scheduleId}
    {agents}
    {modalRefreshToken}
    {embedded}
    {onClose}
    {onMetadataChange}
  />
  {/if}
{:else}
  <DashboardModalShell
    {title}
    status={statusLabel}
    onExternal={canonicalHref ? openCanonical : undefined}
    dismissible={!viewerStepRun && !stepViewerLoading}
    onEscape={handleEscape}
    {onClose}
    testId="dashboard-entity-modal"
    {embedded}
    inspectorOpen={kind === 'conversation' ? modalInspectorOpen : undefined}
    inspectorControlsId={kind === 'conversation' && resolvedConversationId
      ? `dashboard-conversation-inspector-${resolvedConversationId}`
      : undefined}
    onToggleInspector={kind === 'conversation' && resolvedConversationId
      ? () => { void toggleInspector(); }
      : undefined}
  >
    {#if !loading && refreshError && !net.hideErrors}
      <div
        class="shrink-0 border-b border-warning-border bg-warning-subtle px-4 py-1.5 text-xs text-warning"
        data-testid="dashboard-entity-modal-refresh-status"
      >
        {`Could not refresh: ${refreshError}`}
      </div>
    {/if}
    {#if loading || (error && net.hideErrors)}
      <p class="p-6 text-center text-sm text-muted">Loading…</p>
    {:else if error}
      <div class="m-4 rounded-xl border border-danger-border bg-danger-subtle p-4 text-sm text-danger" role="alert">
        <p>{error}</p>
        <Button class="mt-3" size="sm" variant="secondary" onclick={() => void load()}>Try again</Button>
      </div>
    {:else if kind === 'task' && taskDetail}
      <AccessibleTabs
        tabs={taskTabs}
        activeId={taskTab}
        idPrefix="dashboard-task-modal"
        ariaLabel="Task cockpit"
        testIdPrefix="dashboard-task-modal-tab"
        onChange={changeTaskTab}
      />
      <div
        id={`dashboard-task-modal-panel-${taskTab}`}
        role="tabpanel"
        aria-labelledby={`dashboard-task-modal-tab-${taskTab}`}
        class={`min-h-0 flex-1 ${taskTab === 'control-chat' ? 'overflow-hidden' : 'overflow-y-auto overscroll-contain p-3 sm:p-5'}`}
        data-testid={`dashboard-task-modal-panel-${taskTab}`}
      >
        {#if taskTab === 'description'}
          <div class="space-y-4">
            {#if activePause && canMutateTask}
              <AttentionPanel pause={activePause} compact busy={actionBusy} onGate={respondToGate} onQuestion={respondToQuestion} />
            {:else if taskDetail.pending_pause}
              <div class="rounded-2xl border border-warning-border bg-warning-subtle p-4" data-testid="task-attention-summary">
                <p class="text-xs font-semibold uppercase tracking-[0.22em] text-warning">Attention</p>
                <p class="mt-2 text-sm text-foreground">
                  {taskDetail.pending_pause.question ?? 'This task needs your decision.'}
                </p>
                <p class="mt-1 text-xs text-muted">Open the full task to complete this protected action.</p>
              </div>
            {/if}
            {#if actionError}<p class="rounded-xl border border-danger-border bg-danger-subtle p-3 text-sm text-danger" role="alert">{actionError}</p>{/if}
            <TaskBrief
              task={taskDetail}
              workflowLabel={taskDetail.workflow_id ?? 'No workflow'}
              projectLabel={taskDetail.project_id ?? 'No project'}
              agentLabel={agent?.display_name ?? agent?.name ?? taskDetail.agent_id}
            />
          </div>
        {:else if taskTab === 'steps'}
          <div class="space-y-4">
            <WorkflowPhases
              projection={taskDetail.workflow_projection}
              taskStatus={taskDetail.status}
              onStepSelect={() => undefined}
              onStepLogsOpen={canMutateTask ? (stepName) => { void openStepLogs(stepName); } : undefined}
              onStepOutputOpen={canMutateTask ? (stepName) => { void openStepOutput(stepName); } : undefined}
            />
            <TaskProgressPanel projection={taskDetail.progress} />
            {#if stepViewerLoading}
              <p class="rounded-xl border border-separator p-3 text-sm text-muted" data-testid="dashboard-step-viewer-loading">Loading step details…</p>
            {:else if stepViewerError}
              <p class="rounded-xl border border-danger-border bg-danger-subtle p-3 text-sm text-danger" role="alert">{stepViewerError}</p>
            {/if}
          </div>
        {:else if taskTab === 'control'}
          <TaskDashboardControl
            task={taskDetail}
            canMutate={canMutateTask}
            onRefresh={refreshTaskControl}
            onMutationSettled={onTaskMutationSettled}
          />
        {:else if taskTab === 'control-chat' && taskChat}
          <div class="h-full" data-testid="dashboard-entity-modal-chat-panel">
            <TaskControlChat chat={taskChat} {agent} onSent={async () => { await load(false); }} />
          </div>
        {:else if taskTab === 'control-chat' && taskChatLoading}
          <p class="p-6 text-center text-sm text-muted">Opening control chat…</p>
        {:else if taskTab === 'control-chat' && taskChatError}
          <div class="m-4 rounded-xl border border-warning-border bg-warning-subtle p-4 text-sm text-warning" role="alert">
            <p>{taskChatError}</p>
            <Button class="mt-3" size="sm" variant="secondary" onclick={() => { taskChatError = null; void loadTaskChat(); }}>Try again</Button>
          </div>
        {:else if taskTab === 'activity'}
          <TaskWorkPanel stepRuns={taskDetail.step_runs} {agents} {canonicalDeliverableId} view="activity" />
          {:else if taskTab === 'deliverable'}
            {#if !canMutateTask}
              {#if canonicalDeliverableId}
                <AssistantDeliverableBlock
                  item={{ id: canonicalDeliverableId, kind: 'assistant_deliverable', deliverableId: canonicalDeliverableId, format: 'markdown' }}
                  loadDeliverable={(id) => api.deliverables.get(id)}
                  allowSharing={false}
                />
              {:else}
                <p class="text-sm text-muted">No published final deliverable. Other outputs are available in the project's Resources section.</p>
              {/if}
            {:else}
            <TaskWorkPanel
            stepRuns={taskDetail.step_runs}
            finalDeliverable={taskDetail.final_deliverable}
            outputPresentation={taskDetail.output_presentation}
            {agents}
            {canonicalDeliverableId}
            view="deliverable"
            deliverableCollapsedByDefault={false}
            />
            {/if}
        {/if}
      </div>
    {:else if kind === 'conversation' && resolvedConversationId}
      {#if !embedded && !contactBindingId}
        <div class="flex shrink-0 justify-end border-b border-separator px-3"><PinConversationButton conversation={conversationDetail} /></div>
      {/if}
      {#if contactBindingId}
        <AppContactChat contactId={contactBindingId} {agent}
          onInitialLoaded={() => onConversationInitialLoaded?.(resolvedConversationId)} />
      {:else if conversationDetail?.is_readonly_for_caller}
        <div class="flex items-center justify-between gap-3 border-b border-separator px-4 py-3 text-sm text-secondary" data-testid="shared-modal-readonly">
          <span>Shared conversation · Read-only</span>
          {#if onContinueConversation && conversationDetail.can_continue}
            <Button size="sm" variant="secondary" onclick={() => onContinueConversation?.(conversationDetail!)}>Continue in my chat</Button>
          {/if}
        </div>
        <div class="min-h-0 flex-1">
          <CompactConversationChat conversationId={resolvedConversationId} {agent} readOnly embedded />
        </div>
      {:else}
      <ConversationModalWorkspace
        bind:this={conversationWorkspace}
        conversationId={resolvedConversationId}
        sessionId={conversationDetail?.active_session_id ?? ''}
        {agent}
        {agents}
        onInitialLoaded={onConversationInitialLoaded}
        inspectorOpen={modalInspectorOpen}
        inspectorControlInHeader={modalInspectorControlInHeader}
        onInspectorStateChange={handleInspectorStateChange}
      />
      {/if}
    {/if}
  </DashboardModalShell>
{/if}

{#if viewerStepRun && taskId}
  {#key `${viewerStepRun.step_run_id}:${viewerTab}`}
    <SessionLogsDrawer
      conversationId={viewerStepRun.conversation_id ?? viewerStepRun.session_id ?? ''}
      sessionId={viewerStepRun.session_id ?? ''}
      stepRunId={viewerStepRun.step_run_id}
      {taskId}
      stepName={viewerStepRun.step_name}
      agent={agents.find((item: Agent) => item.agent_id === viewerStepRun?.agent_id) ?? null}
      stepRun={viewerStepRun}
      initialTab={viewerTab}
      deliverableFreshness={taskDetail?.deliverable_freshness}
      attempts={taskDetail?.step_runs ?? []}
      {agents}
      onclose={() => { viewerStepRun = null; }}
    />
  {/key}
{/if}
