<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { ArrowLeft, ArrowRight, CheckCircle2, ChevronDown, ChevronUp, Clock3, GitBranch, LoaderCircle, PanelRightOpen, PlayCircle, Settings2, Sparkles, Target } from 'lucide-svelte';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import SessionLogsDrawer from '$lib/components/tasks/SessionLogsDrawer.svelte';
  import StepOutputModal from '$lib/components/tasks/StepOutputModal.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import WorkflowDiagram from '$lib/components/workflows/WorkflowDiagram.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import { loadTaskPageData, refreshTaskPageData, shouldClearTaskFromError } from '$lib/task-detail';
  import { renderMarkdown } from '$lib/markdown';
  import { formatAbsoluteTime, formatDuration, formatRelativeTime } from '$lib/time';
  import { workflowToFormState } from '$lib/workflows';
  import type { Agent, Conversation, StepRun, Task, TaskDetail, Workflow } from '$lib/types/api';

  let loading = $state(true);
  let saving = $state(false);
  let error = $state('');
  let task = $state<TaskDetail | null>(null);
  let agents = $state<Agent[]>([]);
  let workflows = $state<Workflow[]>([]);
  let conversations = $state<Conversation[]>([]);
  let allTasks = $state<Task[]>([]);
  let dependencyTaskId = $state('');
  let gateFeedback = $state('');
  let stepResponse = $state('');
  let expandedStepHistory = $state<Set<string>>(new Set());
  let selectedStepName = $state('');
  let mobileStepDetailOpen = $state(false);
  let configModalOpen = $state(false);
  let outputModalStepRun = $state<StepRun | null>(null);
  let pollTimer: number | null = null;
  let tickNow = $state(Date.now());
  let durationTimer: ReturnType<typeof setInterval> | null = null;
  let visibilityHandler: (() => void) | null = null;

  // Session logs drawer
  let sessionDrawer = $state<{ conversationId: string; sessionId: string; stepName: string } | null>(null);

  let editForm = $state({
    title: '',
    description: '',
    priority: 0,
    expected_output: '',
    agent_id: '',
    workflow_id: '',
    delivery_mode: 'same_conversation',
    delivery_target: '',
    completion_mode_family: 'default' as 'default' | 'direct',
    allow_silent_completion: false
  });

  const statusColors: Record<string, string> = {
    pending: 'border-slate-600 text-slate-400',
    running: 'border-sky-600 text-sky-300',
    evaluating: 'border-violet-600 text-violet-300',
    approved: 'border-emerald-700 text-emerald-300',
    completed: 'border-emerald-700 text-emerald-300',
    failed: 'border-rose-700 text-rose-300',
    cancelled: 'border-slate-600 text-slate-500',
    paused: 'border-yellow-700 text-yellow-300',
    rejected: 'border-amber-700 text-amber-300',
  };

  const statusHints: Record<string, string> = {
    pending: 'Step is queued and waiting to start',
    running: 'Agent is actively working on this step',
    evaluating: 'Evaluator LLM is checking if the step objective was met',
    approved: 'Evaluator approved the step output',
    completed: 'Step finished (no evaluation or evaluation skipped)',
    failed: 'Step failed after exhausting all attempts',
    cancelled: 'Step was cancelled',
    paused: 'Step is paused waiting for human input',
    rejected: 'Evaluator rejected the output — agent will revise',
  };

  const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled'];
  const CANCELLABLE_STATUSES = ['queued', 'ready', 'running', 'paused', 'draft'];
  let isEditable = $derived(task != null && !TERMINAL_STATUSES.includes(task.status));
  let isCancellable = $derived(task != null && CANCELLABLE_STATUSES.includes(task.status));

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function taskIdFromRoute(): string {
    return $page.params.taskId ?? '';
  }

  function workflowName(workflowId: string | null): string {
    if (!workflowId) return 'Auto';
    return workflows.find((w) => w.workflow_id === workflowId)?.name ?? workflowId;
  }

  function agentFor(agentId: string | null): Agent | null {
    if (!agentId) return null;
    return agents.find((a) => a.agent_id === agentId) ?? null;
  }

  function agentName(agentId: string | null): string {
    const agent = agentFor(agentId);
    return agent?.display_name ?? agent?.name ?? agentId ?? 'Unknown';
  }

  function deliveryModeLabel(mode: string): string {
    const labels: Record<string, string> = {
      same_conversation: 'Same conversation',
      specific_conversation: 'Specific conversation',
      latest_active_for_agent: 'Latest active',
      preferred_channel: 'Preferred channel',
      silent: 'Silent'
    };
    return labels[mode] ?? mode;
  }

  function completionModeFamilyLabel(mode: 'default' | 'direct'): string {
    return mode === 'direct' ? 'Direct delivery' : 'Default delivery';
  }

  function priorityTone(priority: number): string {
    if (priority >= 80) return 'border-rose-500/40 bg-rose-500/10 text-rose-200';
    if (priority >= 50) return 'border-amber-500/40 bg-amber-500/10 text-amber-200';
    return 'border-slate-700 bg-slate-900/80 text-slate-300';
  }

  function completionModeLabel(taskDetail: TaskDetail): string {
    if (taskDetail.applied_completion_mode === 'silent') return 'Completed silently';
    if (taskDetail.applied_completion_mode === 'direct') return 'Completed via direct delivery';
    if (taskDetail.status === 'completed') return 'Completed';
    return taskDetail.status;
  }

  function toggleStepHistory(stepName: string): void {
    const next = new Set(expandedStepHistory);
    if (next.has(stepName)) next.delete(stepName);
    else next.add(stepName);
    expandedStepHistory = next;
  }

  function isMobileViewport(): boolean {
    return typeof window !== 'undefined' && window.matchMedia('(max-width: 1279px)').matches;
  }

  function openStepDetail(stepName: string, options: { mobileDrawer?: boolean } = {}): void {
    selectedStepName = stepName;
    if (options.mobileDrawer !== false && isMobileViewport()) {
      mobileStepDetailOpen = true;
    }
  }

  function closeMobileStepDetail(): void {
    mobileStepDetailOpen = false;
  }

  function closeConfigModal(): void {
    configModalOpen = false;
  }

  function openOutputModal(stepRun: StepRun): void {
    outputModalStepRun = stepRun;
  }

  function closeOutputModal(): void {
    outputModalStepRun = null;
  }

  function stepOutputSummary(stepRun: StepRun): string {
    const val = stepRun.output?.summary;
    return typeof val === 'string' ? val : '';
  }

  function stepOutputContent(stepRun: StepRun): string {
    const val = stepRun.output?.content;
    return typeof val === 'string' ? val : '';
  }

  function hasRecordedStepOutput(stepRun: StepRun | null): boolean {
    if (!stepRun?.output) return false;
    const output = stepRun.output;
    const summary = output.summary;
    const content = output.content;
    const claims = output.claims;
    const error = output.error;
    const outcome = output.outcome;
    return (
      (typeof summary === 'string' && summary.trim().length > 0) ||
      (typeof content === 'string' && content.trim().length > 0) ||
      (Array.isArray(claims) && claims.length > 0) ||
      (typeof error === 'string' && error.trim().length > 0) ||
      (typeof outcome === 'object' && outcome !== null)
    );
  }

  function stepOutputClaims(stepRun: StepRun): string[] {
    const claims = stepRun.output?.claims;
    return Array.isArray(claims) ? claims.filter((c): c is string => typeof c === 'string') : [];
  }

  function stepOutputError(stepRun: StepRun): string {
    const val = stepRun.output?.error;
    return typeof val === 'string' ? val : '';
  }

  function stepOutcomeStatus(stepRun: StepRun): string {
    const outcome = stepRun.output?.outcome;
    if (!outcome || typeof outcome !== 'object') return 'success';
    const status = (outcome as Record<string, unknown>).status;
    return typeof status === 'string' ? status : 'success';
  }

  function stepOutcomeReason(stepRun: StepRun): string {
    const outcome = stepRun.output?.outcome;
    if (!outcome || typeof outcome !== 'object') return '';
    const reason = (outcome as Record<string, unknown>).reason;
    return typeof reason === 'string' ? reason : '';
  }

  function displayStepStatus(stepRun: StepRun): string {
    const outcomeStatus = stepOutcomeStatus(stepRun);
    if (stepRun.status === 'approved' && outcomeStatus === 'rejected') {
      return 'rejected';
    }
    if (stepRun.status === 'approved' && outcomeStatus === 'failed') {
      return 'failed';
    }
    return stepRun.status;
  }

  function displayStepStatusHint(stepRun: StepRun): string {
    const outcomeStatus = stepOutcomeStatus(stepRun);
    if (stepRun.status === 'approved' && outcomeStatus === 'rejected') {
      return 'Step output was evaluator-approved, but the completed step rejected prior work';
    }
    if (stepRun.status === 'approved' && outcomeStatus === 'failed') {
      return 'Step output was evaluator-approved, but the completed step reported an operational failure';
    }
    return statusHints[stepRun.status] ?? stepRun.status;
  }

  function stepEvalFeedback(stepRun: StepRun): string {
    const val = stepRun.evaluation?.feedback;
    return typeof val === 'string' ? val : '';
  }

  function activeStepTodos(stepRun: StepRun): Array<{ content: string; status: string; priority: string }> {
    const todos = Array.isArray(stepRun.todos) ? stepRun.todos : [];
    return todos
      .map((todo: Record<string, unknown>) => {
        const content = typeof todo.content === 'string' ? todo.content.trim() : '';
        if (!content) return null;
        return {
          content,
          status: typeof todo.status === 'string' ? todo.status : 'pending',
          priority: typeof todo.priority === 'string' ? todo.priority : 'medium'
        };
      })
      .filter((todo): todo is { content: string; status: string; priority: string } => todo !== null)
      .filter((todo: { content: string; status: string; priority: string }) => !['completed', 'cancelled'].includes(todo.status));
  }

  function todoStatusClass(status: string): string {
    if (status === 'in_progress') return 'border-sky-500/30 bg-sky-500/10 text-sky-100';
    return 'border-amber-500/30 bg-amber-500/10 text-amber-100';
  }

  function todoPriorityClass(priority: string): string {
    if (priority === 'high') return 'text-rose-300';
    if (priority === 'low') return 'text-slate-400';
    return 'text-slate-300';
  }

  function openSessionLogs(stepRun: StepRun): void {
    const sessionId = String(stepRun.output?.session_id ?? stepRun.session_id ?? '');
    if (!sessionId || !task) return;
    let conversationId = stepRun.conversation_id;
    if (!conversationId) {
      const conv = conversations.find((c) =>
        c.context?.ref === task!.task_id && c.title?.includes(stepRun.step_name)
      ) ?? conversations.find((c) => c.context?.ref === task!.task_id);
      conversationId = conv?.conversation_id ?? sessionId;
    }
    sessionDrawer = {
      conversationId,
      sessionId,
      stepName: `${stepRun.step_name} (attempt ${stepRun.attempt})`
    };
  }

  function openSessionLogsForStep(stepName: string): void {
    const group = stepGroups.find((candidate) => candidate.stepName === stepName);
    if (!group?.latest) return;
    openSessionLogs(group.latest);
  }

  function openOutputModalForStep(stepName: string): void {
    const group = stepGroups.find((candidate) => candidate.stepName === stepName);
    if (!group?.latest) return;
    openOutputModal(group.latest);
  }

  interface StepGroup {
    stepName: string;
    stepType: string;
    attempts: StepRun[];
    latest: StepRun | null;
    workflowIndex: number;
  }

  function stepRunSortValue(stepRun: StepRun): number {
    return Date.parse(stepRun.updated_at ?? stepRun.completed_at ?? stepRun.started_at ?? '') || 0;
  }

  function defaultStepSelection(detail: TaskDetail | null, preferred = ''): string {
    if (!detail) return '';
    const available = new Set(detail.step_runs.map((run) => run.step_name));
    const workflow = workflows.find((candidate) => candidate.workflow_id === detail.workflow_id) ?? null;
    const workflowNames = workflow ? workflowToFormState(workflow).steps.map((step) => step.name) : [];
    if (preferred && (available.has(preferred) || workflowNames.includes(preferred))) return preferred;
    const activeName = detail.pending_pause?.step_name ?? detail.workflow_run?.current_step_name ?? '';
    if (activeName) return activeName;
    const latestRun = [...detail.step_runs].sort((a, b) => stepRunSortValue(b) - stepRunSortValue(a))[0];
    return latestRun?.step_name ?? workflowNames[0] ?? '';
  }

  // ---------------------------------------------------------------------------
  // Diagram helpers
  // ---------------------------------------------------------------------------

  /** Resolve the workflow definition for the diagram */
  let workflowDef = $derived.by(() => {
    if (!task?.workflow_id) return null;
    return workflows.find((w) => w.workflow_id === task!.workflow_id) ?? null;
  });

  let diagramSteps = $derived.by(() => {
    if (!workflowDef) return [];
    return workflowToFormState(workflowDef).steps;
  });

  let diagramActiveStep = $derived.by(() => {
    if (!task) return '';
    if (!['running', 'evaluating'].includes(task.status)) return '';
    return task.workflow_run?.current_step_name ?? '';
  });

  /** Build step status map from step_runs (latest attempt per step) */
  let diagramStepStatuses = $derived.by(() => {
    if (!task) return {};
    const map: Record<string, string> = {};
    const latestAttempts: Record<string, number> = {};
    for (const sr of task.step_runs) {
      const nextStatus = displayStepStatus(sr);
      if (!(sr.step_name in latestAttempts) || sr.attempt >= latestAttempts[sr.step_name]) {
        latestAttempts[sr.step_name] = sr.attempt;
        map[sr.step_name] = nextStatus;
      }
    }
    return map;
  });

  /** Build step duration map (latest attempt per step) */
  let diagramStepDurations = $derived.by(() => {
    if (!task) return {};
    const map: Record<string, string> = {};
    // Group by step_name, take the latest attempt
    const latestByStep = new Map<string, StepRun>();
    for (const sr of task.step_runs) {
      const existing = latestByStep.get(sr.step_name);
      if (!existing || sr.attempt > existing.attempt) {
        latestByStep.set(sr.step_name, sr);
      }
    }
    for (const [name, sr] of latestByStep) {
      const dur = formatDuration(sr.started_at, sr.completed_at, tickNow);
      if (dur) map[name] = dur;
    }
    return map;
  });

  let diagramSkippedSteps = $derived(task?.workflow_state?.skipped_steps ?? []);

  let stepGroups = $derived.by(() => {
    if (!task) return [] as StepGroup[];
    const groups = new Map<string, StepGroup>();
    const workflowStepNames = diagramSteps.map((step) => step.name);
    diagramSteps.forEach((step, index) => {
      groups.set(step.name, {
        stepName: step.name,
        stepType: step.type,
        attempts: [],
        latest: null,
        workflowIndex: index,
      });
    });
    for (const run of task.step_runs) {
      const existing = groups.get(run.step_name);
      if (existing) {
        existing.attempts.push(run);
      } else {
        groups.set(run.step_name, {
          stepName: run.step_name,
          stepType: run.step_type,
          attempts: [run],
          latest: null,
          workflowIndex: workflowStepNames.length + groups.size,
        });
      }
    }
    return [...groups.values()]
      .map((group) => {
        const attempts = [...group.attempts].sort((a, b) => b.attempt - a.attempt || stepRunSortValue(b) - stepRunSortValue(a));
        return {
          ...group,
          attempts,
          latest: attempts[0] ?? null,
        } satisfies StepGroup;
      })
      .sort((a, b) => a.workflowIndex - b.workflowIndex || (b.latest ? stepRunSortValue(b.latest) : 0) - (a.latest ? stepRunSortValue(a.latest) : 0));
  });

  let selectedStepGroup = $derived.by(() => {
    const selected = stepGroups.find((group) => group.stepName === selectedStepName);
    return selected ?? stepGroups[0] ?? null;
  });

  let stepAttemptCounts = $derived.by(() => Object.fromEntries(stepGroups.map((group) => [group.stepName, group.attempts.length])));

  let stepStateLabels = $derived.by(() => {
    const labels: Record<string, string> = {};
    for (const group of stepGroups) {
      if (task?.pending_pause?.step_name === group.stepName) {
        labels[group.stepName] = task.pending_pause.pause_type === 'gate' ? 'awaiting gate' : 'awaiting reply';
        continue;
      }
      const status = group.latest ? displayStepStatus(group.latest) : '';
      if (!status) continue;
      if (group.stepName === diagramActiveStep && ['running', 'evaluating'].includes(status)) {
        labels[group.stepName] = status === 'evaluating' ? 'evaluating' : 'live';
        continue;
      }
      labels[group.stepName] = status.replaceAll('_', ' ');
    }
    return labels;
  });

  let executionSummary = $derived.by(() => {
    if (!task) return null;
    const activeGroup = selectedStepGroup ?? stepGroups.find((group) => group.stepName === diagramActiveStep) ?? null;
    return {
      activeStepName: task.pending_pause?.step_name ?? diagramActiveStep ?? activeGroup?.stepName ?? '',
      activeLabel: task.pending_pause
        ? task.pending_pause.pause_type === 'gate'
          ? 'Waiting for approval'
          : 'Waiting for input'
        : task.status === 'running'
          ? diagramActiveStep
            ? 'Agent is executing'
            : 'Task is live'
          : task.status === 'paused'
            ? 'Task paused'
            : task.status,
    };
  });

  // ---------------------------------------------------------------------------
  // Statistics
  // ---------------------------------------------------------------------------

  let stats = $derived.by(() => {
    if (!task) return null;
    const runs = task.step_runs;
    const totalAttempts = runs.length;
    const completedSteps = new Set(
      runs
        .filter((r) => ['approved', 'completed'].includes(r.status) && stepOutcomeStatus(r) === 'success')
        .map((r) => r.step_name)
    ).size;
    const evalRevisions = runs.filter((r) => r.evaluation && String(r.evaluation.decision) === 'revise').length;
    const evalFailures = runs.filter((r) => r.evaluation && String(r.evaluation.decision) === 'failed').length;
    const multiAttemptSteps = new Set(
      runs.filter((r) => r.attempt > 1).map((r) => r.step_name)
    ).size;
    const skipped = diagramSkippedSteps.length;

    // Loop iterations from workflow state
    const loopIters = task.workflow_state?.loop_iterations;
    const totalLoops = loopIters ? Object.values(loopIters).reduce((a, b) => a + b, 0) : 0;

    // Unique step names that have run
    const uniqueSteps = new Set(runs.map((r) => r.step_name)).size;

    return {
      uniqueSteps,
      completedSteps,
      totalAttempts,
      evalRevisions,
      evalFailures,
      multiAttemptSteps,
      skipped,
      totalLoops,
    };
  });

  // ---------------------------------------------------------------------------
  // Origin / initiator
  // ---------------------------------------------------------------------------

  let sourceLabel = $derived.by(() => {
    if (!task) return '';
    const labels: Record<string, string> = {
      chat: 'Chat conversation',
      agent: 'Agent delegation',
      api: 'API request',
      scheduler: 'Scheduled',
      webhook: 'Webhook',
    };
    return labels[task.source_type] ?? task.source_type;
  });

  let sourceConversation = $derived.by(() => {
    if (!task?.source_ref) return null;
    if (task.source_type !== 'chat' && task.source_type !== 'agent') return null;
    return conversations.find((c) => c.conversation_id === task!.source_ref) ?? null;
  });

  let taskAgent = $derived(agentFor(task?.agent_id ?? null));

  let activePause = $derived.by(() => {
    if (!task?.pending_pause || task.status !== 'paused') return null;
    const pause = task.pending_pause;
    const currentStepName = task.workflow_run?.current_step_name;
    if (pause.step_name && currentStepName && pause.step_name !== currentStepName) {
      return {
        ...pause,
        question: pause.question ?? 'Task is paused and waiting for input.'
      };
    }
    return pause;
  });

  let dependencyTasks = $derived.by(() => {
    if (!task) return [] as Array<{ taskId: string; title: string; status: string }>;
    return task.dependencies.map((dependency) => {
      const linkedTask = allTasks.find((candidate) => candidate.task_id === dependency.depends_on);
      return {
        taskId: dependency.depends_on,
        title: linkedTask?.title ?? dependency.depends_on,
        status: linkedTask?.status ?? 'unknown'
      };
    });
  });

  let stepHasLogs = $derived.by(() => Object.fromEntries(stepGroups.map((group) => [group.stepName, Boolean(group.latest?.output?.session_id || group.latest?.session_id)])));
  let stepHasOutput = $derived.by(() => {
    const entries: Array<[string, boolean]> = [];
    for (const group of stepGroups) {
      const latest = group.latest;
      entries.push([group.stepName, hasRecordedStepOutput(latest)]);
    }
    return Object.fromEntries(entries);
  });

  // ---------------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------------

  async function loadTask(): Promise<void> {
    loading = true;
    error = '';
    try {
      const data = await loadTaskPageData(api, taskIdFromRoute());
      task = data.task;
      agents = data.agents;
      workflows = data.workflows;
      conversations = data.conversations;
      allTasks = data.allTasks;
      error = data.auxiliaryError;
      editForm = {
        title: task.title,
        description: task.description,
        expected_output: task.expected_output ?? '',
        priority: task.priority,
        agent_id: task.agent_id,
        workflow_id: task.workflow_id ?? '',
        delivery_mode: task.delivery.mode,
        delivery_target: task.delivery.target ?? '',
        completion_mode_family: task.completion_mode_family,
        allow_silent_completion: task.allow_silent_completion
      };
      selectedStepName = defaultStepSelection(task, selectedStepName);
    } catch (caughtError) {
      task = null;
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function refreshTaskOnly(): Promise<void> {
    if (document.hidden) return;
    try {
      const data = await refreshTaskPageData(api, taskIdFromRoute(), allTasks);
      task = data.task;
      allTasks = data.allTasks;
      error = data.auxiliaryError;
      selectedStepName = defaultStepSelection(task, selectedStepName);
    } catch (caughtError) {
      if (shouldClearTaskFromError(caughtError)) {
        task = null;
      }
      error = asApiError(caughtError).message;
    }
  }

  function stopPolling(): void {
    if (pollTimer !== null) { window.clearInterval(pollTimer); pollTimer = null; }
  }

  function startPolling(): void {
    stopPolling();
    if (document.hidden) return;
    pollTimer = window.setInterval(() => { void refreshTaskOnly(); }, 5000);
  }

  // ---------------------------------------------------------------------------
  // Task actions
  // ---------------------------------------------------------------------------

  async function saveTask(): Promise<boolean> {
    if (!task) return false;
    saving = true;
    try {
      error = '';
      const updatedTask = await api.tasks.update(task.task_id, {
        title: editForm.title,
        description: editForm.description,
        expected_output: editForm.expected_output || null,
        priority: Number(editForm.priority),
        agent_id: editForm.agent_id,
        workflow_id: editForm.workflow_id || null,
        delivery_mode: editForm.delivery_mode,
        delivery_target: editForm.delivery_mode === 'specific_conversation' ? editForm.delivery_target : null,
        completion_mode_family: editForm.completion_mode_family,
        allow_silent_completion: editForm.allow_silent_completion
      });
      task = await api.tasks.detail(updatedTask.task_id);
      addToast('Task updated.', 'success');
      return true;
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to update task');
      return false;
    } finally {
      saving = false;
    }
  }

  async function addDependency(): Promise<void> {
    if (!task || !dependencyTaskId) return;
    try {
      await api.tasks.addDependency(task.task_id, dependencyTaskId, true);
      dependencyTaskId = '';
      task = await api.tasks.detail(task.task_id);
      addToast('Dependency added.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to add dependency');
    }
  }

  async function removeDependency(dependsOn: string): Promise<void> {
    if (!task) return;
    const confirmed = await confirmAction({
      title: 'Remove dependency?',
      message: 'The task will no longer wait for this dependency before running.',
      confirmLabel: 'Remove dependency'
    });
    if (!confirmed) return;
    try {
      await api.tasks.removeDependency(task.task_id, dependsOn);
      task = await api.tasks.detail(task.task_id);
      addToast('Dependency removed.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to remove dependency');
    }
  }

  async function respondToGate(action: string): Promise<void> {
    if (!task) return;
    try {
      await api.tasks.gateResponse(task.task_id, {
        step_name: task.pending_pause?.step_name,
        action,
        feedback: gateFeedback || undefined
      });
      gateFeedback = '';
      task = await api.tasks.detail(task.task_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function respondToStepQuestion(response: string): Promise<void> {
    if (!task) return;
    try {
      await api.tasks.stepResponse(task.task_id, {
        step_name: task.pending_pause?.step_name,
        response
      });
      stepResponse = '';
      task = await api.tasks.detail(task.task_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function cancelTask(): Promise<void> {
    if (!task) return;
    const confirmed = await confirmAction({
      title: 'Cancel task?',
      message: 'This stops the task and marks it as cancelled. This action cannot be undone.',
      confirmLabel: 'Cancel task'
    });
    if (!confirmed) return;
    try {
      await api.tasks.cancel(task.task_id);
      task = await api.tasks.detail(task.task_id);
      addToast('Task cancelled.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to cancel task');
    }
  }

  function startDurationTimer(): void {
    if (durationTimer) return;
    durationTimer = setInterval(() => { tickNow = Date.now(); }, 1000);
  }

  function stopDurationTimer(): void {
    if (durationTimer) { clearInterval(durationTimer); durationTimer = null; }
  }

  // ---------------------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------------------

  onMount(() => {
    visibilityHandler = () => {
      if (document.hidden) stopPolling();
      else { void refreshTaskOnly(); startPolling(); }
    };
    document.addEventListener('visibilitychange', visibilityHandler);
    void loadTask().then(() => { startPolling(); startDurationTimer(); });
    return () => {
      stopPolling();
      stopDurationTimer();
      if (visibilityHandler) document.removeEventListener('visibilitychange', visibilityHandler);
    };
  });
</script>

<svelte:head>
  <title>{task ? `${task.title} · Task · Cognis` : 'Task · Cognis'}</title>
</svelte:head>

{#if loading}
  <LoadingState label="Loading task" description="Fetching workflow state, step runs, and dependency information." />
{:else if task}
  <section class="space-y-5">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div class="min-w-0 space-y-3">
        <Button size="sm" variant="secondary" onclick={() => goto('/tasks')}>Back to task board</Button>
        <div class="flex min-w-0 items-start gap-3">
          <AgentAvatar
            name={taskAgent?.display_name ?? taskAgent?.name ?? task.agent_id}
            avatarUrl={taskAgent?.avatar_url ?? null}
            class="h-11 w-11 rounded-2xl"
          />
          <div class="min-w-0">
            <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Task detail</p>
            <h1 class="mt-1 truncate text-2xl font-semibold text-white">{task.title}</h1>
            <div class="mt-2 flex flex-wrap items-center gap-2 text-sm text-slate-400">
              <span>Owner agent</span>
              <span class="font-medium text-slate-200">{agentName(task.agent_id)}</span>
              <span class="rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.2em] {priorityTone(task.priority)}">P{task.priority}</span>
              <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-[11px] text-slate-300">{deliveryModeLabel(task.delivery.mode)}</span>
              <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-[11px] text-slate-300">{completionModeFamilyLabel(task.completion_mode_family)}</span>
              {#if task.allow_silent_completion}
                <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-[11px] text-slate-300">Silent allowed</span>
              {/if}
            </div>
          </div>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <Button size="sm" variant="secondary" onclick={() => (configModalOpen = true)}>
          <Settings2 class="mr-1.5 h-3.5 w-3.5" />
          Configure
        </Button>
        {#if isCancellable}
          <Button size="sm" variant="danger" onclick={cancelTask}>Cancel task</Button>
        {/if}
        <span class="rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] {statusColors[task.status] ?? 'border-slate-700 text-slate-200'}">
          {completionModeLabel(task)}
        </span>
      </div>
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div class="space-y-5">
        <!-- Pipeline diagram -->
        {#if diagramSteps.length > 0}
          <Card class="overflow-hidden p-0">
            <div class="border-b border-slate-800/80 px-4 py-3 sm:px-5 sm:py-4">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Live workflow</p>
                  <div class="mt-2 flex flex-wrap items-center gap-2 text-sm text-slate-300">
                    {#if task.status === 'running' || task.status === 'evaluating'}
                      <span class="inline-flex h-5 w-5 items-center justify-center rounded-full border border-sky-500/30 bg-sky-500/10 text-sky-300">
                        <LoaderCircle class="h-3.5 w-3.5 animate-spin" />
                      </span>
                    {/if}
                    <span class="font-medium text-white">{executionSummary?.activeLabel ?? 'Workflow status'}</span>
                    {#if executionSummary?.activeStepName}
                      <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-xs text-slate-300">
                        {executionSummary.activeStepName}
                      </span>
                    {/if}
                  </div>
                </div>
                {#if selectedStepGroup}
                  <div class="flex items-center gap-2">
                  {#if stepHasOutput[selectedStepGroup.stepName]}
                    <Button size="sm" variant="secondary" onclick={() => openOutputModalForStep(selectedStepGroup.stepName)}>Open output</Button>
                  {/if}
                  {#if stepHasLogs[selectedStepGroup.stepName]}
                    <Button size="sm" variant="ghost" onclick={() => openSessionLogsForStep(selectedStepGroup.stepName)}>Open logs</Button>
                  {/if}
                  <Button class="xl:hidden" size="sm" variant="secondary" onclick={() => openStepDetail(selectedStepGroup.stepName)}>
                    <PanelRightOpen class="mr-1.5 h-3.5 w-3.5" />
                    Step detail
                  </Button>
                  </div>
                {/if}
              </div>
              {#if dependencyTasks.length > 0}
                <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/50 p-3">
                  <p class="text-[11px] font-medium uppercase tracking-[0.25em] text-slate-500">Direct dependencies</p>
                  <div class="mt-3 flex flex-wrap items-center gap-2">
                    {#each dependencyTasks as dependency}
                      <button
                        class="inline-flex items-center gap-2 rounded-full border border-slate-700 bg-slate-950/80 px-3 py-1.5 text-xs text-slate-200 transition hover:border-slate-600 hover:text-white"
                        onclick={() => goto(`/tasks/${dependency.taskId}`)}
                        type="button"
                      >
                        <span class="truncate max-w-[12rem]">{dependency.title}</span>
                        <span class="rounded-full border px-1.5 py-0.5 text-[10px] uppercase tracking-wide {statusColors[dependency.status] ?? 'border-slate-700 text-slate-400'}">{dependency.status}</span>
                      </button>
                      <ArrowRight class="h-3.5 w-3.5 text-slate-600" />
                    {/each}
                    <span class="inline-flex items-center rounded-full border border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-xs font-medium text-sky-100">{task.title}</span>
                  </div>
                </div>
              {/if}
              <div class="mt-4 flex gap-2 overflow-x-auto pb-1">
                {#each stepGroups as group}
                  {@const liveStatus = group.latest ? displayStepStatus(group.latest) : (task.pending_pause?.step_name === group.stepName ? 'paused' : 'pending')}
                  <button
                    class={`inline-flex shrink-0 items-center gap-2 rounded-full border px-3 py-1.5 text-xs transition ${selectedStepGroup?.stepName === group.stepName ? 'border-sky-400/60 bg-sky-500/10 text-sky-100' : 'border-slate-700 bg-slate-900/70 text-slate-300 hover:border-slate-600 hover:text-white'}`}
                    onclick={() => openStepDetail(group.stepName)}
                    type="button"
                  >
                    {#if ['approved', 'completed'].includes(liveStatus)}
                      <CheckCircle2 class="h-3.5 w-3.5 text-emerald-300" />
                    {:else if group.stepType === 'gate'}
                      <GitBranch class="h-3.5 w-3.5 text-amber-300" />
                    {:else}
                      <PlayCircle class="h-3.5 w-3.5 text-slate-400" />
                    {/if}
                    <span>{group.stepName}</span>
                    {#if group.attempts.length > 1}
                      <span class="rounded-full bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">x{group.attempts.length}</span>
                    {/if}
                  </button>
                {/each}
              </div>
            </div>
            <div class="px-3 py-3 sm:px-5 sm:py-4">
            <WorkflowDiagram
              steps={diagramSteps}
              interactionMode={workflowDef?.interaction?.mode?.toString() ?? 'explicit_gates'}
              activeStepName={diagramActiveStep}
              selectedStepName={selectedStepGroup?.stepName ?? ''}
              stepStatuses={diagramStepStatuses}
              stepDurations={diagramStepDurations}
              stepAttemptCounts={stepAttemptCounts}
              stepStateLabels={stepStateLabels}
              stepHasLogs={stepHasLogs}
              stepHasOutput={stepHasOutput}
              skippedSteps={diagramSkippedSteps}
              onStepSelect={(stepName) => openStepDetail(stepName)}
              onStepLogsOpen={openSessionLogsForStep}
              onStepOutputOpen={openOutputModalForStep}
            />
            </div>
          </Card>
        {/if}

        <!-- Pending pause -->
        {#if activePause}
          <Card class="overflow-hidden p-0">
            <div class="space-y-4">
              <div class="border-b border-slate-800/80 bg-gradient-to-r from-sky-500/10 via-slate-900 to-slate-900 px-5 py-4">
                <div class="flex flex-wrap items-center gap-2 text-xs uppercase tracking-[0.25em] text-slate-400">
                  <span>{activePause.pause_type === 'gate' ? 'Workflow gate' : 'Step question'}</span>
                  {#if activePause.step_name}
                    <span class="rounded-full border border-slate-700 bg-slate-950/70 px-2 py-0.5 text-[10px] tracking-[0.2em] text-slate-300">{activePause.step_name}</span>
                  {/if}
                </div>
                <h2 class="mt-3 text-lg font-semibold text-white">{activePause.question}</h2>
                <p class="mt-2 text-sm text-slate-400">
                  {activePause.pause_type === 'gate'
                    ? 'Review the latest attempt, give guidance if needed, then continue or stop the workflow.'
                    : 'Answer here to resume the active step without leaving the task view.'}
                </p>
              </div>

              <div class="px-5 pb-5">
              {#if activePause.pause_type === 'gate'}
                <div class="space-y-3">
                  <div class="rounded-2xl border border-sky-500/20 bg-sky-500/5 p-4">
                    <div class="flex items-center gap-2 text-sm font-medium text-sky-100">
                      <Sparkles class="h-4 w-4 text-sky-300" />
                      Optional instruction for the next attempt
                    </div>
                    <p class="mt-1 text-sm text-slate-400">This will be passed into the next execution when you continue or retry.</p>
                    <textarea bind:value={gateFeedback} class="mt-3 min-h-[120px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="Example: approve the direction, but tighten the final summary and validate edge cases before finishing."></textarea>
                  </div>
                  <div class="grid gap-2 sm:grid-cols-2">
                    {#each activePause.options ?? [] as option}
                      <Button class="justify-center" size="sm" onclick={() => respondToGate(String(option.action ?? 'continue'))}>{String(option.label ?? option.action ?? 'continue')}</Button>
                    {/each}
                    {#if (activePause.options ?? []).length === 0}
                      <Button class="justify-center" size="sm" onclick={() => respondToGate('continue')}>Continue workflow</Button>
                    {/if}
                    <Button class="justify-center" size="sm" variant="secondary" onclick={() => respondToGate('cancel')}>Stop task</Button>
                  </div>
                </div>
              {:else}
                <div class="space-y-3">
                  {#if (activePause.options ?? []).length > 0}
                    <div class="flex flex-wrap gap-2">
                      {#each activePause.options ?? [] as option}
                        <button class="rounded-full border border-slate-700 bg-slate-900/70 px-3 py-1.5 text-xs text-slate-200 transition hover:border-sky-400/40 hover:bg-sky-500/10 hover:text-white" onclick={() => { stepResponse = String(option.action ?? option.label ?? ''); }} type="button">{String(option.label ?? option.action ?? 'Use option')}</button>
                      {/each}
                    </div>
                  {/if}
                  <textarea bind:value={stepResponse} class="min-h-[120px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="Provide the answer that resumes the current step"></textarea>
                  <div class="flex flex-wrap gap-2">
                    <Button size="sm" onclick={() => respondToStepQuestion(stepResponse)}>Send response</Button>
                    {#each activePause.options ?? [] as option}
                      <Button size="sm" variant="secondary" onclick={() => respondToStepQuestion(String(option.action ?? option.label ?? ''))}>{String(option.label ?? option.action ?? 'Use option')}</Button>
                    {/each}
                  </div>
                </div>
              {/if}
              </div>
            </div>
          </Card>
        {/if}

        <!-- Workflow progress / step runs -->
        <Card class="overflow-hidden p-0">
          <div class="border-b border-slate-800/80 px-4 py-3 sm:px-5 sm:py-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Step detail</p>
                <h2 class="mt-1 text-lg font-semibold text-white">{selectedStepGroup?.stepName ?? workflowName(task.workflow_id)}</h2>
                <p class="mt-1 text-sm text-slate-400">Click the diagram or progress chips to focus a step. Latest attempt stays on top, earlier attempts collapse into history.</p>
              </div>
              {#if selectedStepGroup}
                <span class="rounded-full border border-slate-700 bg-slate-950/80 px-3 py-1 text-xs text-slate-300">
                  {selectedStepGroup.attempts.length} attempt{selectedStepGroup.attempts.length === 1 ? '' : 's'}
                </span>
              {/if}
            </div>
          </div>

          <div class="grid gap-4 px-4 py-4 sm:px-5 sm:py-5 lg:grid-cols-[260px_minmax(0,1fr)]">
            <div class="space-y-2">
              {#each stepGroups as group}
                {@const latestStatus = group.latest ? displayStepStatus(group.latest) : (task.pending_pause?.step_name === group.stepName ? 'paused' : 'pending')}
                {@const groupAgent = agentFor(group.latest?.agent_id ?? null)}
                <button
                  class={`w-full rounded-2xl border px-4 py-3 text-left transition ${selectedStepGroup?.stepName === group.stepName ? 'border-sky-400/50 bg-sky-500/10' : 'border-slate-800 bg-slate-950/50 hover:border-slate-700 hover:bg-slate-950/80'}`}
                  onclick={() => openStepDetail(group.stepName, { mobileDrawer: false })}
                  type="button"
                >
                  <div class="flex items-center justify-between gap-3">
                    <div class="min-w-0">
                      <div class="flex items-center gap-2">
                        {#if groupAgent}
                          <AgentAvatar name={groupAgent.display_name ?? groupAgent.name} avatarUrl={groupAgent.avatar_url} class="h-6 w-6 rounded-xl" />
                        {/if}
                        <p class="truncate font-medium text-white">{group.stepName}</p>
                      </div>
                      <p class="mt-1 text-xs text-slate-500">{group.stepType === 'gate' ? 'Gate' : 'Execution'} {#if group.attempts.length > 1}<span class="ml-1 text-slate-400">x{group.attempts.length}</span>{/if}</p>
                    </div>
                    <span class="rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider {statusColors[latestStatus] ?? 'border-slate-600 text-slate-400'}">{latestStatus}</span>
                  </div>
                </button>
              {/each}
            </div>

            <div class="space-y-4">
              {#if selectedStepGroup}
                {#if selectedStepGroup.latest}
                  {@const latestAttempt = selectedStepGroup.latest}
                  {@const summary = stepOutputSummary(latestAttempt)}
                  {@const claims = stepOutputClaims(latestAttempt)}
                  {@const stepError = stepOutputError(latestAttempt)}
                  {@const outcomeStatus = stepOutcomeStatus(latestAttempt)}
                  {@const outcomeReason = stepOutcomeReason(latestAttempt)}
                  {@const visibleStatus = displayStepStatus(latestAttempt)}
                  {@const feedback = stepEvalFeedback(latestAttempt)}
                  <article class="rounded-3xl border border-slate-800 bg-slate-950/60 p-4 sm:p-5">
                    <div class="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div class="flex flex-wrap items-center gap-2">
                          <h3 class="text-lg font-semibold text-white">{latestAttempt.step_name}</h3>
                          {#if selectedStepGroup.attempts.length > 1}
                            <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-sky-200">Latest attempt</span>
                          {/if}
                          <span class="text-xs text-slate-500">#{latestAttempt.attempt}</span>
                        </div>
                        <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                          <span>{latestAttempt.step_type === 'gate' ? 'Gate' : 'Run'}</span>
                          {#if latestAttempt.agent_id}
                            <span class="inline-flex items-center gap-2 text-slate-300">
                              <AgentAvatar name={agentName(latestAttempt.agent_id)} avatarUrl={agentFor(latestAttempt.agent_id)?.avatar_url ?? null} class="h-5 w-5 rounded-lg" />
                              {agentName(latestAttempt.agent_id)}
                            </span>
                          {/if}
                          {#if latestAttempt.started_at}
                            <Tooltip text={formatAbsoluteTime(latestAttempt.started_at)}>
                              <span class="inline-flex cursor-help items-center gap-1"><Clock3 class="h-3.5 w-3.5" />started {formatRelativeTime(latestAttempt.started_at)}</span>
                            </Tooltip>
                          {/if}
                          {#if latestAttempt.started_at}
                            <span class="font-mono text-slate-300">{formatDuration(latestAttempt.started_at, latestAttempt.completed_at, tickNow)}</span>
                          {/if}
                        </div>
                      </div>
                      <div class="flex items-center gap-2">
                        {#if latestAttempt.output?.session_id || latestAttempt.session_id}
                          <Button size="sm" variant="ghost" onclick={() => openSessionLogs(latestAttempt)}>Logs</Button>
                        {/if}
                        <Tooltip text={displayStepStatusHint(latestAttempt)}>
                          <span class="cursor-help rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider {statusColors[visibleStatus] ?? 'border-slate-600 text-slate-400'}">{visibleStatus}</span>
                        </Tooltip>
                      </div>
                    </div>

                    {#if outcomeStatus !== 'success'}
                      <div class="mt-4 rounded-2xl border border-amber-500/20 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
                        <p class="font-medium uppercase tracking-wide text-[11px] text-amber-300">Outcome marker</p>
                        <p class="mt-1">This attempt completed but reported <span class="font-semibold uppercase">{outcomeStatus}</span>{#if outcomeReason}: {outcomeReason}{/if}</p>
                      </div>
                    {/if}

                    {#if stepError}
                      <div class="mt-4 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
                        <p class="font-medium">Error</p>
                        <pre class="mt-2 whitespace-pre-wrap text-xs text-rose-300">{stepError}</pre>
                      </div>
                    {/if}

                    {#if summary && !stepError}
                      <div>
                        <p class="mt-4 text-xs uppercase tracking-[0.25em] text-slate-500">Summary</p>
                        <div class="prose prose-sm prose-invert mt-3 max-w-none text-slate-300">
                          {@html renderMarkdown(summary)}
                        </div>
                      </div>
                    {/if}

                    {#if activeStepTodos(latestAttempt).length > 0}
                      {@const todos = activeStepTodos(latestAttempt)}
                      <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-900/60 px-4 py-3">
                        <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Open todos</p>
                        <div class="mt-3 space-y-2">
                          {#each todos as todo}
                            <div class="rounded-2xl border px-3 py-3 text-sm {todoStatusClass(todo.status)}">
                              <div class="flex flex-wrap items-center justify-between gap-2">
                                <span class="font-medium">{todo.content}</span>
                                <div class="flex items-center gap-2 text-[10px] uppercase tracking-[0.18em]">
                                  <span class="rounded-full border border-current/20 px-2 py-0.5">{todo.status.replace('_', ' ')}</span>
                                  <span class={todoPriorityClass(todo.priority)}>{todo.priority}</span>
                                </div>
                              </div>
                            </div>
                          {/each}
                        </div>
                      </div>
                    {/if}

                    <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-900/60 px-4 py-3">
                      <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Completion metadata</p>
                      {#if claims.length > 0}
                        <ul class="mt-3 space-y-1 text-sm text-slate-400">
                          {#each claims as claim}
                            <li class="flex items-start gap-2">
                              <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-600"></span>
                              <span>{claim}</span>
                            </li>
                          {/each}
                        </ul>
                      {:else if outcomeStatus === 'success' && !stepError && !latestAttempt.evaluation}
                        <p class="mt-3 text-sm text-slate-400">No extra completion metadata was recorded for this attempt.</p>
                      {/if}

                      {#if hasRecordedStepOutput(latestAttempt)}
                        <div class="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-800 pt-3">
                          <Button size="sm" variant="secondary" onclick={() => openOutputModal(latestAttempt)}>Show full output</Button>
                          <span class="text-xs text-slate-500">Includes completion metadata and the finalized assistant output.</span>
                        </div>
                      {/if}
                    </div>

                    {#if latestAttempt.evaluation}
                      {@const evalDecision = String(latestAttempt.evaluation.decision ?? '')}
                      {@const evalReasoning = String(latestAttempt.evaluation.reasoning ?? '')}
                      {@const evalColor = evalDecision === 'approved' || evalDecision === 'approve' ? 'text-emerald-400' : evalDecision === 'revise' ? 'text-sky-400' : evalDecision === 'failed' || evalDecision === 'reject' ? 'text-rose-400' : 'text-amber-400'}
                      <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/80 px-4 py-3">
                        <p class="text-xs font-medium uppercase tracking-widest text-slate-500">Evaluation</p>
                        <p class="mt-1 text-sm text-slate-300">
                          <span class="font-medium {evalColor}">{evalDecision}</span>
                          {#if evalReasoning} - {evalReasoning}{/if}
                        </p>
                        {#if feedback}
                          <p class="mt-2 rounded-lg border border-slate-700/50 bg-slate-900/50 px-2 py-1.5 text-xs text-slate-400"><span class="font-medium text-slate-500">Feedback:</span> {feedback}</p>
                        {/if}
                      </div>
                    {/if}
                  </article>

                  {#if selectedStepGroup.attempts.length > 1}
                    <div class="rounded-3xl border border-slate-800/80 bg-slate-950/30 p-4">
                      <button class="flex w-full items-center justify-between gap-3 text-left" onclick={() => toggleStepHistory(selectedStepGroup.stepName)} type="button">
                        <div>
                          <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Attempt history</p>
                          <p class="mt-1 text-sm text-slate-300">{selectedStepGroup.attempts.length - 1} earlier attempt{selectedStepGroup.attempts.length === 2 ? '' : 's'} hidden behind the latest execution.</p>
                        </div>
                        {#if expandedStepHistory.has(selectedStepGroup.stepName)}
                          <ChevronUp class="h-4 w-4 text-slate-400" />
                        {:else}
                          <ChevronDown class="h-4 w-4 text-slate-400" />
                        {/if}
                      </button>

                      {#if expandedStepHistory.has(selectedStepGroup.stepName)}
                        <div class="mt-4 space-y-4 border-t border-dashed border-slate-800 pt-4">
                          {#each selectedStepGroup.attempts.slice(1) as stepRun (stepRun.step_run_id)}
                            {@const summary = stepOutputSummary(stepRun)}
                            {@const stepError = stepOutputError(stepRun)}
                            {@const visibleStatus = displayStepStatus(stepRun)}
                            <div class="relative pl-5">
                              <div class="absolute left-1.5 top-0 bottom-0 w-px bg-slate-800"></div>
                              <div class="absolute left-0 top-2 h-3 w-3 rounded-full border border-slate-600 bg-slate-950"></div>
                              <div class="mb-2 flex items-center justify-between gap-3 border-b border-slate-800/80 pb-2">
                                <div>
                                  <p class="text-xs font-semibold uppercase tracking-[0.25em] text-slate-500">Earlier attempt #{stepRun.attempt}</p>
                                  <p class="mt-1 text-xs text-slate-500">{stepRun.started_at ? formatAbsoluteTime(stepRun.started_at) : 'No start time recorded'}</p>
                                </div>
                                <span class="rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider {statusColors[visibleStatus] ?? 'border-slate-600 text-slate-400'}">{visibleStatus}</span>
                              </div>
                              {#if stepError}
                                <div class="rounded-xl border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                                  <pre class="whitespace-pre-wrap">{stepError}</pre>
                                </div>
                              {:else}
                                {#if summary}
                                  <div class="prose prose-sm prose-invert max-w-none text-slate-400">{@html renderMarkdown(summary)}</div>
                                {/if}
                                {#if hasRecordedStepOutput(stepRun)}
                                  <div class="mt-3 flex flex-wrap items-center gap-2">
                                    <Button size="sm" variant="ghost" onclick={() => openOutputModal(stepRun)}>Show full output</Button>
                                    <span class="text-xs text-slate-500">Opens the finalized result for this attempt.</span>
                                  </div>
                                {/if}
                              {/if}
                            </div>
                          {/each}
                        </div>
                      {/if}
                    </div>
                  {/if}
                {:else}
                  <div class="rounded-3xl border border-dashed border-slate-700 bg-slate-950/40 px-5 py-10 text-center">
                    <Target class="mx-auto h-10 w-10 text-slate-600" />
                    <p class="mt-4 text-sm text-slate-300">This step has not produced an attempt yet.</p>
                    <p class="mt-1 text-xs text-slate-500">When the workflow reaches it, execution details and logs will appear here.</p>
                  </div>
                {/if}
              {:else}
                <p class="text-sm text-slate-400">No steps have been executed yet.</p>
              {/if}
            </div>
          </div>
        </Card>
      </div>

      <div class="space-y-4 xl:hidden">
        <details class="group rounded-3xl border border-slate-800 bg-slate-950/40 p-4" open>
          <summary class="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-medium text-white">
            Task meta
            <ChevronDown class="h-4 w-4 text-slate-500 transition group-open:rotate-180" />
          </summary>
          <div class="mt-4 grid gap-4 text-sm text-slate-300 sm:grid-cols-2">
            <div class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
              <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Origin</p>
              <dl class="mt-3 space-y-2">
                <div class="flex justify-between gap-3"><dt class="text-slate-500">Source</dt><dd>{sourceLabel}</dd></div>
                {#if sourceConversation}
                  <div class="flex justify-between gap-3"><dt class="text-slate-500">Conversation</dt><dd><a href="/chat/{sourceConversation.conversation_id}" class="text-sky-400 hover:text-sky-300 hover:underline">{sourceConversation.title ?? 'Untitled'}</a></dd></div>
                {/if}
                <div class="flex justify-between gap-3"><dt class="text-slate-500">Agent</dt><dd class="inline-flex items-center gap-2"><AgentAvatar name={agentName(task.agent_id)} avatarUrl={taskAgent?.avatar_url ?? null} class="h-5 w-5 rounded-lg" />{agentName(task.agent_id)}</dd></div>
                <div class="flex justify-between gap-3"><dt class="text-slate-500">Workflow</dt><dd>{workflowName(task.workflow_id)}</dd></div>
              </dl>
            </div>
            <div class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
              <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Timing</p>
              <dl class="mt-3 space-y-2">
                {#if task.created_at}<div class="flex justify-between gap-3"><dt class="text-slate-500">Created</dt><dd>{formatRelativeTime(task.created_at)}</dd></div>{/if}
                {#if task.started_at}<div class="flex justify-between gap-3"><dt class="text-slate-500">Started</dt><dd>{formatRelativeTime(task.started_at)}</dd></div>{/if}
                {#if task.started_at}<div class="flex justify-between gap-3"><dt class="text-slate-500">Duration</dt><dd class="font-mono">{formatDuration(task.started_at, task.completed_at, tickNow)}</dd></div>{/if}
              </dl>
            </div>
          </div>
        </details>

        <details class="group rounded-3xl border border-slate-800 bg-slate-950/40 p-4">
          <summary class="flex cursor-pointer list-none items-center justify-between gap-3 text-sm font-medium text-white">
            Result
            <ChevronDown class="h-4 w-4 text-slate-500 transition group-open:rotate-180" />
          </summary>
          <div class="mt-4 space-y-4 text-sm text-slate-300">
            <div class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
              <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Result</p>
              <p class="mt-3 leading-6 text-slate-300">{task.result_summary ?? 'This task has not produced a final result yet.'}</p>
            </div>
          </div>
        </details>
      </div>

      <!-- Sidebar -->
      <div class="hidden space-y-5 xl:block">
        <!-- Origin -->
        <Card class="p-5">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Origin</p>
          <dl class="mt-3 space-y-2 text-sm">
            <div class="flex justify-between gap-3">
              <dt class="text-slate-500">Source</dt>
              <dd class="text-slate-300">{sourceLabel}</dd>
            </div>
            {#if sourceConversation}
              <div class="flex justify-between gap-3">
                <dt class="text-slate-500">Conversation</dt>
                <dd>
                  <a href="/chat/{sourceConversation.conversation_id}" class="text-sky-400 hover:text-sky-300 hover:underline">
                    {sourceConversation.title ?? 'Untitled'}
                  </a>
                </dd>
              </div>
            {:else if task.source_ref}
              <div class="flex justify-between gap-3">
                <dt class="text-slate-500">Reference</dt>
                <dd class="truncate text-slate-400" title={task.source_ref}>{task.source_ref}</dd>
              </div>
            {/if}
            {#if task.created_by}
              <div class="flex justify-between gap-3">
                <dt class="text-slate-500">Created by</dt>
                <dd class="truncate text-slate-300" title={task.created_by}>{task.created_by}</dd>
              </div>
            {/if}
            <div class="flex justify-between gap-3">
              <dt class="text-slate-500">Agent</dt>
              <dd class="inline-flex items-center gap-2 text-slate-300"><AgentAvatar name={agentName(task.agent_id)} avatarUrl={taskAgent?.avatar_url ?? null} class="h-5 w-5 rounded-lg" />{agentName(task.agent_id)}</dd>
            </div>
            <div class="flex justify-between gap-3">
              <dt class="text-slate-500">Workflow</dt>
              <dd class="text-slate-300">{workflowName(task.workflow_id)}</dd>
            </div>
          </dl>
        </Card>

        <!-- Timing -->
        <Card class="p-5">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Timing</p>
          <dl class="mt-3 space-y-2 text-sm">
            {#if task.created_at}
              <div class="flex justify-between">
                <dt class="text-slate-500">Created</dt>
                <dd class="text-slate-300" title={formatAbsoluteTime(task.created_at)}>{formatRelativeTime(task.created_at)}</dd>
              </div>
            {/if}
            {#if task.started_at}
              <div class="flex justify-between">
                <dt class="text-slate-500">Started</dt>
                <dd class="text-slate-300" title={formatAbsoluteTime(task.started_at)}>{formatRelativeTime(task.started_at)}</dd>
              </div>
            {/if}
            {#if task.completed_at}
              <div class="flex justify-between">
                <dt class="text-slate-500">Completed</dt>
                <dd class="text-slate-300" title={formatAbsoluteTime(task.completed_at)}>{formatRelativeTime(task.completed_at)}</dd>
              </div>
            {/if}
            {#if task.started_at}
              <div class="flex justify-between">
                <dt class="text-slate-500">Duration</dt>
                <dd class="font-mono text-slate-200">{formatDuration(task.started_at, task.completed_at, tickNow)}</dd>
              </div>
            {/if}
          </dl>
        </Card>

        <!-- Statistics -->
        {#if stats && stats.totalAttempts > 0}
          <Card class="p-5">
            <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Statistics</p>
            <dl class="mt-3 space-y-2 text-sm">
              <div class="flex justify-between">
                <dt class="text-slate-500">Steps completed</dt>
                <dd class="font-mono text-slate-200">{stats.completedSteps} / {stats.uniqueSteps}</dd>
              </div>
              <div class="flex justify-between">
                <dt class="inline-flex items-center gap-1 text-slate-500">
                  Total step runs
                  <Tooltip text="Total number of step execution attempts, including retries. Higher than step count when steps are retried after evaluation rejection.">
                    <span class="cursor-help text-slate-600">(?)</span>
                  </Tooltip>
                </dt>
                <dd class="font-mono text-slate-200">{stats.totalAttempts}</dd>
              </div>
              {#if stats.evalRevisions > 0}
                <div class="flex justify-between">
                  <dt class="inline-flex items-center gap-1 text-slate-500">
                    Eval revisions
                    <Tooltip text="Times the evaluator sent a step back for revision. The agent retries within the same step run.">
                      <span class="cursor-help text-slate-600">(?)</span>
                    </Tooltip>
                  </dt>
                  <dd class="font-mono text-amber-300">{stats.evalRevisions}</dd>
                </div>
              {/if}
              {#if stats.evalFailures > 0}
                <div class="flex justify-between">
                  <dt class="text-slate-500">Eval failures</dt>
                  <dd class="font-mono text-rose-300">{stats.evalFailures}</dd>
                </div>
              {/if}
              {#if stats.totalLoops > 0}
                <div class="flex justify-between">
                  <dt class="inline-flex items-center gap-1 text-slate-500">
                    Review loops
                    <Tooltip text="Times a review loop sent execution back to an earlier step (e.g. code review rejecting implementation back to the plan step).">
                      <span class="cursor-help text-slate-600">(?)</span>
                    </Tooltip>
                  </dt>
                  <dd class="font-mono text-amber-300">{stats.totalLoops}</dd>
                </div>
              {/if}
              {#if stats.multiAttemptSteps > 0}
                <div class="flex justify-between">
                  <dt class="inline-flex items-center gap-1 text-slate-500">
                    Re-executed steps
                    <Tooltip text="Steps that were executed more than once (full re-execution, not just in-place revision). Happens when a review loop sends execution back to an earlier step.">
                      <span class="cursor-help text-slate-600">(?)</span>
                    </Tooltip>
                  </dt>
                  <dd class="font-mono text-slate-200">{stats.multiAttemptSteps}</dd>
                </div>
              {/if}
              {#if stats.skipped > 0}
                <div class="flex justify-between">
                  <dt class="text-slate-500">Skipped steps</dt>
                  <dd class="font-mono text-slate-400">{stats.skipped}</dd>
                </div>
              {/if}
            </dl>
          </Card>
        {/if}

        <!-- Result -->
        <Card class="p-5">
          <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Result</p>
          {#if task.applied_completion_reason}
            <p class="mt-3 text-xs leading-5 text-slate-500">{task.applied_completion_reason}</p>
          {/if}
          <p class="mt-3 text-sm leading-6 text-slate-300">{task.result_summary ?? 'This task has not produced a final result yet.'}</p>
        </Card>
      </div>
    </div>
  </section>

  {#if mobileStepDetailOpen && selectedStepGroup}
    <div class="fixed inset-0 z-40 xl:hidden" role="presentation">
      <button class="absolute inset-0 bg-slate-950/80" onclick={closeMobileStepDetail} type="button" aria-label="Close step detail"></button>
      <div class="absolute inset-x-0 bottom-0 max-h-[82vh] overflow-y-auto rounded-t-[2rem] border-t border-slate-700 bg-slate-950 p-5 shadow-2xl">
        <div class="flex items-start justify-between gap-3">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Step detail</p>
            <h2 class="mt-1 text-lg font-semibold text-white">{selectedStepGroup.stepName}</h2>
            <p class="mt-1 text-sm text-slate-400">{selectedStepGroup.stepType === 'gate' ? 'Gate step' : 'Execution step'} with {selectedStepGroup.attempts.length} attempt{selectedStepGroup.attempts.length === 1 ? '' : 's'}.</p>
          </div>
          <Button size="sm" variant="secondary" onclick={closeMobileStepDetail}>Close</Button>
        </div>

        {#if selectedStepGroup.latest}
          {@const latestAttempt = selectedStepGroup.latest}
          {@const summary = stepOutputSummary(latestAttempt)}
          {@const claims = stepOutputClaims(latestAttempt)}
          {@const visibleStatus = displayStepStatus(latestAttempt)}
          <div class="mt-4 rounded-3xl border border-slate-800 bg-slate-900/60 p-4">
            <div class="flex items-center justify-between gap-3">
              <span class="rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider {statusColors[visibleStatus] ?? 'border-slate-600 text-slate-400'}">{visibleStatus}</span>
              {#if latestAttempt.output?.session_id || latestAttempt.session_id}
                <Button size="sm" variant="ghost" onclick={() => openSessionLogs(latestAttempt)}>Logs</Button>
              {/if}
            </div>
            {#if summary}
              <div class="prose prose-sm prose-invert mt-4 max-w-none text-slate-300">{@html renderMarkdown(summary)}</div>
            {/if}
            <div class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Completion metadata</p>
              {#if claims.length > 0}
                <ul class="mt-3 space-y-1 text-sm text-slate-400">
                  {#each claims as claim}
                    <li class="flex items-start gap-2">
                      <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-600"></span>
                      <span>{claim}</span>
                    </li>
                  {/each}
                </ul>
              {:else}
                <p class="mt-3 text-sm text-slate-400">Open the full output to inspect completion metadata and the finalized assistant output.</p>
              {/if}
              {#if hasRecordedStepOutput(latestAttempt)}
                <Button class="mt-4" size="sm" variant="secondary" onclick={() => openOutputModal(latestAttempt)}>Show full output</Button>
              {/if}
            </div>
          </div>
        {:else}
          <div class="mt-4 rounded-3xl border border-dashed border-slate-700 px-4 py-8 text-center text-sm text-slate-400">This step has not produced an attempt yet.</div>
        {/if}
      </div>
    </div>
  {/if}

  {#if configModalOpen}
    <div class="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/85 p-4" role="presentation">
      <button class="absolute inset-0" onclick={closeConfigModal} type="button" aria-label="Close configuration"></button>
      <div class="relative z-10 max-h-[90vh] w-full max-w-4xl overflow-y-auto rounded-[2rem] border border-slate-700 bg-slate-900 p-6 shadow-2xl">
        <div class="flex items-start justify-between gap-4 border-b border-slate-800 pb-4">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Task configuration</p>
            <h2 class="mt-1 text-xl font-semibold text-white">{task.title}</h2>
            <p class="mt-2 text-sm text-slate-400">Configuration is secondary to live execution, so edits live here while the main page stays focused on workflow progress.</p>
          </div>
          <Button size="sm" variant="secondary" onclick={closeConfigModal}>Close</Button>
        </div>

        <div class="mt-5 grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Title</span>
            <Input bind:value={editForm.title} disabled={!isEditable} />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Priority</span>
            <Input bind:value={editForm.priority} type="number" disabled={!isEditable} />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Agent</span>
            <select bind:value={editForm.agent_id} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
              {#each agents.filter((a) => a.agent_type === 'primary') as agent}
                <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
              {/each}
            </select>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Workflow</span>
            <select bind:value={editForm.workflow_id} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
              <option value="">Auto</option>
              {#each workflows as workflow}
                <option value={workflow.workflow_id}>{workflow.name}</option>
              {/each}
            </select>
          </label>
        </div>

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Description</span>
          <textarea bind:value={editForm.description} disabled={!isEditable} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 disabled:opacity-50"></textarea>
        </label>

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Expected output</span>
          <textarea bind:value={editForm.expected_output} disabled={!isEditable} class="min-h-[60px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 disabled:opacity-50"></textarea>
        </label>

        <div class="mt-4 grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Delivery mode</span>
            <select bind:value={editForm.delivery_mode} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
              <option value="same_conversation">Same conversation</option>
              <option value="specific_conversation">Specific conversation</option>
              <option value="latest_active_for_agent">Latest active</option>
              <option value="preferred_channel">Preferred channel</option>
            </select>
          </label>
          {#if editForm.delivery_mode === 'specific_conversation'}
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Delivery target</span>
              <select bind:value={editForm.delivery_target} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
                <option value="">Select conversation</option>
                {#each conversations as conversation}
                  <option value={conversation.conversation_id}>{conversation.title ?? conversation.conversation_id}</option>
                {/each}
              </select>
            </label>
          {/if}
        </div>

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Completion notification behavior</span>
          <div class="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto]">
            <select bind:value={editForm.completion_mode_family} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
              <option value="default">Default delivery</option>
              <option value="direct">Direct channel delivery</option>
            </select>
            <label class="inline-flex items-center gap-2 rounded-xl border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm text-slate-200 disabled:opacity-50">
              <input bind:checked={editForm.allow_silent_completion} disabled={!isEditable} class="h-4 w-4 rounded border-slate-600 bg-slate-950" type="checkbox" />
              <span>Allow silent completion</span>
            </label>
          </div>
        </label>

        <div class="mt-6 rounded-3xl border border-slate-800 bg-slate-950/40 p-4">
          <div class="flex items-center justify-between gap-3">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Dependencies</p>
              <p class="mt-1 text-sm text-slate-400">Only direct dependencies are shown in the live workflow. Manage them here.</p>
            </div>
          </div>
          <div class="mt-4 space-y-3">
            {#each task.dependencies as dependency}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3">
                <button class="min-w-0 truncate text-left text-sm text-slate-200 hover:text-white" onclick={() => goto(`/tasks/${dependency.depends_on}`)} type="button">{allTasks.find((candidate) => candidate.task_id === dependency.depends_on)?.title ?? dependency.depends_on}</button>
                {#if isEditable}
                  <Button size="sm" variant="danger" onclick={() => removeDependency(dependency.depends_on)}>Remove</Button>
                {/if}
              </div>
            {/each}
            {#if task.dependencies.length === 0}
              <p class="text-sm text-slate-400">No dependencies configured.</p>
            {/if}
            {#if isEditable}
              <div class="grid gap-3 border-t border-slate-800 pt-4 md:grid-cols-[minmax(0,1fr)_auto]">
                <select bind:value={dependencyTaskId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                  <option value="">Add dependency...</option>
                  {#each allTasks.filter((candidate) => candidate.task_id !== taskIdFromRoute()) as candidate}
                    <option value={candidate.task_id}>{candidate.title}</option>
                  {/each}
                </select>
                <Button class="justify-center" disabled={!dependencyTaskId} onclick={addDependency}>Add dependency</Button>
              </div>
            {/if}
          </div>
        </div>

        <div class="mt-6 flex justify-end gap-3 border-t border-slate-800 pt-4">
          <Button variant="secondary" onclick={closeConfigModal}>Close</Button>
          <Button disabled={saving || !isEditable} onclick={async () => { if (await saveTask()) closeConfigModal(); }}>{saving ? 'Saving...' : 'Save task'}</Button>
        </div>
      </div>
    </div>
  {/if}

  {#if outputModalStepRun}
    <StepOutputModal
      stepRun={outputModalStepRun}
      agentName={agentName(outputModalStepRun.agent_id)}
      agentAvatarUrl={agentFor(outputModalStepRun.agent_id)?.avatar_url ?? null}
      visibleStatus={displayStepStatus(outputModalStepRun)}
      onclose={closeOutputModal}
    />
  {/if}

  <!-- Session logs drawer -->
  {#if sessionDrawer}
    <SessionLogsDrawer
      conversationId={sessionDrawer.conversationId}
      sessionId={sessionDrawer.sessionId}
      stepName={sessionDrawer.stepName}
      onclose={() => (sessionDrawer = null)}
    />
  {/if}
{:else}
  <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error || 'Task not found.'}</p>
{/if}
