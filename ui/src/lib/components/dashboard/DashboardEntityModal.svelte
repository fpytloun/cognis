<script lang="ts">
  import { goto } from '$app/navigation';

  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import ConversationModalWorkspace from '$lib/components/dashboard/ConversationModalWorkspace.svelte';
  import DashboardModalShell from '$lib/components/dashboard/DashboardModalShell.svelte';
  import ScheduleDashboardModal from '$lib/components/dashboard/ScheduleDashboardModal.svelte';
  import TaskBrief from '$lib/components/task-cockpit/TaskBrief.svelte';
  import AttentionPanel from '$lib/components/task-cockpit/AttentionPanel.svelte';
  import WorkflowPhases from '$lib/components/task-cockpit/WorkflowPhases.svelte';
  import TaskProgressPanel from '$lib/components/task-cockpit/TaskProgressPanel.svelte';
  import TaskControlChat from '$lib/components/task-cockpit/TaskControlChat.svelte';
  import TaskDashboardControl from '$lib/components/dashboard/TaskDashboardControl.svelte';
  import TaskWorkPanel from '$lib/components/task-cockpit/TaskWorkPanel.svelte';
  import StepOutputModal from '$lib/components/tasks/StepOutputModal.svelte';
  import SessionLogsDrawer from '$lib/components/tasks/SessionLogsDrawer.svelte';
  import { api, asApiError } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import type { Agent, Conversation, QuestionSetAnswer, StepRun, TaskControlChatResponse, TaskDetail } from '$lib/types/api';

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
  }>();

  let conversationTab = $state<'chat' | 'activity'>('chat');
  let taskTab = $state<TaskTab>('control');
  let loading = $state(true);
  let error = $state<string | null>(null);
  let refreshing = $state(false);
  let refreshError = $state<string | null>(null);
  let actionBusy = $state(false);
  let actionError = $state<string | null>(null);
  let taskDetail = $state<TaskDetail | null>(null);
  let taskChat = $state<TaskControlChatResponse | null>(null);
  let taskChatLoading = $state(false);
  let taskChatError = $state<string | null>(null);
  let conversationDetail = $state<Conversation | null>(null);
  let outputStepRun = $state<StepRun | null>(null);
  let logsStepRun = $state<StepRun | null>(null);
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

  const taskTabs = [
    { id: 'description', label: 'Description' },
    { id: 'steps', label: 'Steps' },
    { id: 'control', label: 'Control' },
    { id: 'control-chat', label: 'Control chat' },
    { id: 'activity', label: 'Activity' },
    { id: 'deliverable', label: 'Final deliverable' }
  ];
  const resolvedConversationId = $derived(kind === 'task' ? taskChat?.conversation_id ?? null : conversationId);
  const agentId = $derived(kind === 'task' ? taskDetail?.agent_id ?? null : conversationDetail?.agent_id ?? null);
  const agent = $derived<Agent | null>(agents.find((item: Agent) => item.agent_id === agentId) ?? null);
  const statusLabel = $derived(kind === 'task' ? taskDetail?.status ?? null : conversationDetail?.status ?? null);
  const canMutateTask = $derived($auth.user !== null && $auth.user.role !== 'viewer');
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
    if (!taskId || taskChat || taskChatLoading) return;
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
          taskTab = ['completed', 'failed', 'cancelled'].includes(detail.status)
            ? 'deliverable'
            : detail.pending_pause
              ? 'description'
              : 'control';
        }
      } else if (kind === 'conversation' && conversationId) {
        const detail = await api.conversations.detail(conversationId, { includeState: false });
        if (request !== loadRequest || requestKey !== boundKey) return 'superseded';
        conversationDetail = detail;
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
        step_name: activePause.step_name,
        action,
        feedback: instruction?.trim() || null
      });
      await load(false);
    } catch (caught) {
      actionError = asApiError(caught).message || 'Could not submit the task response.';
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
      outputStepRun = hydrated;
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
      logsStepRun = hydrated;
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
    outputStepRun = null;
    logsStepRun = null;
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
    if (!active || outputStepRun || logsStepRun) return false;
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
  <ScheduleDashboardModal
    {scheduleId}
    {agents}
    {modalRefreshToken}
    {embedded}
    {onClose}
    {onMetadataChange}
  />
{:else}
  <DashboardModalShell
    {title}
    status={statusLabel}
    onExternal={canonicalHref ? openCanonical : undefined}
    dismissible={!outputStepRun && !logsStepRun && !stepViewerLoading}
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
    {#if !loading && refreshError}
      <div
        class="shrink-0 border-b border-amber-500/20 bg-amber-500/5 px-4 py-1.5 text-xs text-amber-200"
        data-testid="dashboard-entity-modal-refresh-status"
      >
        {`Could not refresh: ${refreshError}`}
      </div>
    {/if}
    {#if loading}
      <p class="p-6 text-center text-sm text-slate-400">Loading…</p>
    {:else if error}
      <div class="m-4 rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-100" role="alert">
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
            {#if activePause}
              <AttentionPanel pause={activePause} compact busy={actionBusy} onGate={respondToGate} onQuestion={respondToQuestion} />
            {:else if taskDetail.pending_pause}
              <div class="rounded-2xl border border-amber-500/30 bg-amber-500/5 p-4" data-testid="task-attention-summary">
                <p class="text-xs font-semibold uppercase tracking-[0.22em] text-amber-300">Attention</p>
                <p class="mt-2 text-sm text-slate-200">
                  {taskDetail.pending_pause.question ?? 'This task needs your decision.'}
                </p>
                <p class="mt-1 text-xs text-slate-400">Open the full task to complete this protected action.</p>
              </div>
            {/if}
            {#if actionError}<p class="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-100" role="alert">{actionError}</p>{/if}
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
              onStepSelect={() => undefined}
              onStepLogsOpen={(stepName) => { void openStepLogs(stepName); }}
              onStepOutputOpen={(stepName) => { void openStepOutput(stepName); }}
            />
            <TaskProgressPanel projection={taskDetail.progress} />
            {#if stepViewerLoading}
              <p class="rounded-xl border border-slate-800 p-3 text-sm text-slate-400" data-testid="dashboard-step-viewer-loading">Loading step details…</p>
            {:else if stepViewerError}
              <p class="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-100" role="alert">{stepViewerError}</p>
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
          <p class="p-6 text-center text-sm text-slate-400">Opening control chat…</p>
        {:else if taskTab === 'control-chat' && taskChatError}
          <div class="m-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-100" role="alert">
            <p>{taskChatError}</p>
            <Button class="mt-3" size="sm" variant="secondary" onclick={() => { taskChatError = null; void loadTaskChat(); }}>Try again</Button>
          </div>
        {:else if taskTab === 'activity'}
          <TaskWorkPanel stepRuns={taskDetail.step_runs} {agents} {canonicalDeliverableId} view="activity" />
        {:else if taskTab === 'deliverable'}
          <TaskWorkPanel
            stepRuns={taskDetail.step_runs}
            {agents}
            {canonicalDeliverableId}
            view="deliverable"
            deliverableCollapsedByDefault={false}
          />
        {/if}
      </div>
    {:else if kind === 'conversation' && resolvedConversationId}
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
  </DashboardModalShell>
{/if}

{#if outputStepRun}
  <StepOutputModal
    stepRun={outputStepRun}
    agentName={agents.find((item: Agent) => item.agent_id === outputStepRun?.agent_id)?.display_name ?? outputStepRun.agent_id}
    agentAvatarUrl={agents.find((item: Agent) => item.agent_id === outputStepRun?.agent_id)?.avatar_url ?? null}
    visibleStatus={outputStepRun.status}
    onclose={() => { outputStepRun = null; }}
  />
{/if}

{#if logsStepRun && taskId}
  <SessionLogsDrawer
    conversationId={logsStepRun.conversation_id ?? logsStepRun.session_id ?? ''}
    sessionId={logsStepRun.session_id ?? ''}
    stepRunId={logsStepRun.step_run_id}
    {taskId}
    stepName={logsStepRun.step_name}
    agent={agents.find((item: Agent) => item.agent_id === logsStepRun?.agent_id) ?? null}
    stepRun={logsStepRun}
    onclose={() => { logsStepRun = null; }}
  />
{/if}
