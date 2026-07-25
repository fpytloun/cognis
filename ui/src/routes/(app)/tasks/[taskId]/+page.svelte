<script lang="ts">
import { goto } from '$app/navigation';
import { page } from '$app/stores';
import ArrowLeft from 'lucide-svelte/icons/arrow-left';
import ArrowRight from 'lucide-svelte/icons/arrow-right';
import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
import ChevronDown from 'lucide-svelte/icons/chevron-down';
import ChevronUp from 'lucide-svelte/icons/chevron-up';
import Clock3 from 'lucide-svelte/icons/clock-3';
import GitBranch from 'lucide-svelte/icons/git-branch';
import LoaderCircle from 'lucide-svelte/icons/loader-circle';
import MessageSquarePlus from 'lucide-svelte/icons/message-square-plus';
import MoreVertical from 'lucide-svelte/icons/more-vertical';
import PanelRightOpen from 'lucide-svelte/icons/panel-right-open';
import PlayCircle from 'lucide-svelte/icons/play-circle';
import Settings2 from 'lucide-svelte/icons/settings-2';
import Sparkles from 'lucide-svelte/icons/sparkles';
import Target from 'lucide-svelte/icons/target';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import CredentialRequestForm from '$lib/components/CredentialRequestForm.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import AgentSelect from '$lib/components/AgentSelect.svelte';
  import AgentProfileSelect from '$lib/components/AgentProfileSelect.svelte';
  import SessionPolicyEditor from '$lib/components/SessionPolicyEditor.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import SessionLogsDrawer from '$lib/components/tasks/SessionLogsDrawer.svelte';
  import StepOutputModal from '$lib/components/tasks/StepOutputModal.svelte';
  import TaskComments from '$lib/components/tasks/TaskComments.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import Tooltip from '$lib/components/ui/Tooltip.svelte';
  import WorkflowDiagram from '$lib/components/workflows/WorkflowDiagram.svelte';
  import {
    clearQuestionDraft,
    readQuestionDraft,
    writeQuestionDraft,
    type QuestionDraftAnswers,
  } from '$lib/interactive-drafts';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import {
    isTaskRerunnable,
    loadTaskPageData,
    refreshTaskPageData,
    shouldClearTaskFromError
  } from '$lib/task-detail';
  import { renderMarkdown } from '$lib/markdown';
  import { normalizeSelectedAgentProfileId } from '$lib/agents';
  import { policyFromText, policyText } from '$lib/session-policy';
  import { formatAbsoluteTime, formatDuration, formatRelativeTime } from '$lib/time';
  import { workflowToFormState, type WorkflowStepFormState } from '$lib/workflows';
  import type {
    Agent,
    Conversation,
    Deliverable,
    Escalation,
    Notification,
    Project,
    QuestionSetAnswer,
    QuestionSetQuestion,
    Session,
    StepRun,
    Task,
    TaskDetail,
    Workflow
  } from '$lib/types/api';

  let loading = $state(true);
  let saving = $state(false);
  let rerunBusy = $state(false);
  let chatBusyKey = $state<string | null>(null);
  let error = $state('');
  let task = $state<TaskDetail | null>(null);
  let agents = $state<Agent[]>([]);
  let workflows = $state<Workflow[]>([]);
  let conversations = $state<Conversation[]>([]);
  let allTasks = $state<Task[]>([]);
  let dependencyTaskId = $state('');
  let gateFeedback = $state('');
  let stepResponse = $state('');
  let stepQuestionAnswers = $state<QuestionDraftAnswers>({});
  let lastStepQuestionNotificationId = $state<string | null>(null);
  let expandedStepHistory = $state<Set<string>>(new Set());
  let selectedStepName = $state('');
  let selectedAttemptByStep = $state<Record<string, string>>({});
  let revisionTargetSeed = $state<string | null>(null);
  let commentsRef = $state<TaskComments | null>(null);
  let mobileStepDetailOpen = $state(false);
  let configModalOpen = $state(false);
  let taskActionsOpen = $state(false);
  let outputModalStepRun = $state<StepRun | null>(null);
  let loadingStepRunId = $state<string | null>(null);
  let stepRunDetailLoadKey = 0;
  let stepHistoryLoading = $state<Set<string>>(new Set());
  let stepHistoryLoaded = $state<Set<string>>(new Set());
  let taskEscalations = $state<Escalation[]>([]);
  let taskCredentialRequest = $state<Notification | null>(null);
  let taskEscalationBusyCallId = $state<string | null>(null);
  let pollTimer: number | null = null;
  let tickNow = $state(Date.now());
  let durationTimer: ReturnType<typeof setInterval> | null = null;
  let visibilityHandler: (() => void) | null = null;

  type SessionDrawerState = {
    conversationId: string;
    sessionId: string;
    stepRunId: string | null;
    stepName: string;
    agent: Agent | null;
  };

  // Session logs drawer
  let sessionDrawer = $state<SessionDrawerState | null>(null);
  let sessionDrawerBackStack = $state<SessionDrawerState[]>([]);
  const sessionDrawerStepRun = $derived.by(() => {
    const stepRunId = sessionDrawer?.stepRunId;
    if (!stepRunId || !task) return null;
    return task.step_runs.find((run) => run.step_run_id === stepRunId) ?? null;
  });

  let editForm = $state({
    title: '',
    description: '',
    priority: 0,
    expected_output: '',
    agent_id: '',
    agent_profile_id: '',
    workflow_id: '',
    project_id: '',
    delivery_mode: 'same_conversation',
    delivery_target: '',
    completion_mode_family: 'default' as 'default' | 'direct',
    allow_silent_completion: false,
    interaction_mode_override: '' as '' | 'none' | 'explicit_gates' | 'step_requests',
    allow_policy_text: '',
    deny_policy_text: ''
  });
  let projects = $state<Project[]>([]);
  let projectWorkflowOptions = $state<Workflow[]>([]);
  let projectWorkflowOptionsLoaded = $state(false);
  let projectWorkflowLoadKey = 0;
  let lastProjectWorkflowKey = $state('');

  const statusColors: Record<string, string> = {
    pending: 'border-slate-600 text-slate-400',
    running: 'border-sky-600 text-sky-300',
    evaluating: 'border-violet-600 text-violet-300',
    approved: 'border-emerald-700 text-emerald-300',
    completed: 'border-emerald-700 text-emerald-300',
    failed: 'border-rose-700 text-rose-300',
    cancelled: 'border-slate-600 text-slate-500',
    paused: 'border-yellow-700 text-yellow-300',
    rejected: 'border-sky-700 text-sky-300',
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
  let isRerunnable = $derived(isTaskRerunnable(task));
  let selectedEditAgent = $derived(agents.find((agent) => agent.agent_id === editForm.agent_id) ?? null);

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

  function currentWorkflow(): Workflow | null {
    if (!task?.workflow_id) return null;
    const workflowId = task.workflow_id;
    return workflows.find((workflow) => workflow.workflow_id === workflowId) ?? null;
  }

  async function promoteWorkflowFromTask(): Promise<void> {
    if (!task?.workflow_id) return;
    await goto(`/workflows?draftFrom=${encodeURIComponent(task.workflow_id)}`);
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
    if (priority >= 50) return 'border-sky-500/40 bg-sky-500/10 text-sky-200';
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
    if (next.has(stepName)) {
      next.delete(stepName);
    } else {
      next.add(stepName);
      void loadStepHistory(stepName);
    }
    expandedStepHistory = next;
  }

  function setStepHistoryLoading(stepName: string, loadingHistory: boolean): void {
    const next = new Set(stepHistoryLoading);
    if (loadingHistory) next.add(stepName);
    else next.delete(stepName);
    stepHistoryLoading = next;
  }

  function markStepHistoryLoaded(stepName: string): void {
    const next = new Set(stepHistoryLoaded);
    next.add(stepName);
    stepHistoryLoaded = next;
  }

  async function loadStepHistory(stepName: string): Promise<void> {
    if (!task || stepHistoryLoaded.has(stepName) || stepHistoryLoading.has(stepName)) return;
    setStepHistoryLoading(stepName, true);
    try {
      let cursor: string | null = null;
      const historyRuns: StepRun[] = [];
      do {
        const page = await api.tasks.stepHistorySummary(task.task_id, stepName, {
          limit: 100,
          cursor
        });
        historyRuns.push(...page.items);
        cursor = page.has_more ? page.cursor : null;
      } while (cursor);
      if (!task) return;
      const runsById = new Map(task.step_runs.map((run) => [run.step_run_id, run]));
      for (const run of historyRuns) {
        runsById.set(run.step_run_id, runsById.get(run.step_run_id) ?? run);
      }
      task = { ...task, step_runs: [...runsById.values()] };
      markStepHistoryLoaded(stepName);
    } catch (err) {
      const apiError = asApiError(err);
      addToast(apiError.message || 'Could not load step history.', 'error');
    } finally {
      setStepHistoryLoading(stepName, false);
    }
  }

  function isMobileViewport(): boolean {
    // Aligns with Tailwind's `lg` breakpoint (1024px), which is the app-wide
    // mobile/desktop pivot after the PWA overhaul.
    return typeof window !== 'undefined' && window.matchMedia('(max-width: 1023px)').matches;
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

  function mobilePrimaryActionLabel(): string {
    if (isRerunnable) return 'Re-run task';
    if (isEditable) return 'Configure';
    return 'Actions';
  }

  function openMobilePrimaryAction(): void {
    if (isRerunnable) {
      void rerunTask();
      return;
    }
    if (isEditable) {
      configModalOpen = true;
      return;
    }
    taskActionsOpen = true;
  }

  function isProjectedStepRun(stepRun: StepRun | null): boolean {
    return stepRun?.is_projection === true;
  }

  function replaceStepRun(stepRun: StepRun): void {
    if (!task) return;
    const nextRuns = [...task.step_runs];
    const existingIndex = nextRuns.findIndex((run) => run.step_run_id === stepRun.step_run_id);
    if (existingIndex >= 0) {
      nextRuns[existingIndex] = stepRun;
    } else {
      nextRuns.push(stepRun);
    }
    task = { ...task, step_runs: nextRuns };
  }

  async function loadStepRunDetail(stepRunId: string): Promise<StepRun | null> {
    const key = ++stepRunDetailLoadKey;
    loadingStepRunId = stepRunId;
    try {
      const detail = await api.tasks.stepRunDetail(stepRunId);
      if (key !== stepRunDetailLoadKey) return null;
      replaceStepRun(detail);
      if (outputModalStepRun?.step_run_id === stepRunId) {
        outputModalStepRun = detail;
      }
      return detail;
    } catch (err) {
      const apiError = asApiError(err);
      addToast(apiError.message || 'Could not load step details.', 'error');
      return null;
    } finally {
      if (key === stepRunDetailLoadKey) {
        loadingStepRunId = null;
      }
    }
  }

  function openOutputModal(stepRun: StepRun): void {
    if (isProjectedStepRun(stepRun)) {
      outputModalStepRun = stepRun;
      void loadStepRunDetail(stepRun.step_run_id);
      return;
    }
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

  function latestDeliverable(stepRun: StepRun): Deliverable | null {
    return stepRun.deliverables[0] ?? null;
  }

  function finalTaskResultTitle(detail: TaskDetail | null): string {
    const title = detail?.result_data?.final_title;
    return typeof title === 'string' ? title : '';
  }

  function finalTaskResultFormat(detail: TaskDetail | null): string {
    const format = detail?.result_data?.final_format;
    return typeof format === 'string' ? format : '';
  }

  function hasFinalTaskOutput(detail: TaskDetail | null): boolean {
    const content = detail?.result_data?.final_content;
    return typeof content === 'string' && content.trim().length > 0;
  }

  function taskPauseLabel(pauseType: string | null | undefined): string {
    if (pauseType === 'escalation') return 'awaiting approval';
    if (pauseType === 'gate') return 'awaiting gate';
    return 'awaiting reply';
  }

  function taskPauseSummaryLabel(pauseType: string | null | undefined): string {
    if (pauseType === 'escalation') return 'Waiting for escalation approval';
    if (pauseType === 'gate') return 'Waiting for gate review';
    return 'Waiting for input';
  }

  function sortEscalations(items: Escalation[]): Escalation[] {
    return [...items].sort((left, right) => (left.received_at ?? 0) - (right.received_at ?? 0));
  }

  function isEscalationExpired(item: Escalation, now = tickNow): boolean {
    const receivedAt = item.received_at ?? now;
    const timeoutSeconds = item.timeout_seconds ?? 300;
    return now - receivedAt >= timeoutSeconds * 1000;
  }

  function taskEscalationFromNotification(notification: Notification): Escalation | null {
    if (notification.notification_type !== 'escalation' || notification.task_id !== task?.task_id) {
      return null;
    }
    return {
      call_id: notification.notification_id,
      session_id: notification.session_id,
      tool_name: typeof notification.payload.tool_name === 'string' ? notification.payload.tool_name : null,
      decision: 'escalate',
      resolved: false,
      reasoning: typeof notification.payload.reasoning === 'string' ? notification.payload.reasoning : null,
      risk: typeof notification.payload.risk === 'string' ? notification.payload.risk : null,
      timeout_seconds:
        typeof notification.payload.timeout_seconds === 'number'
          ? notification.payload.timeout_seconds
          : 300,
      received_at: notification.created_at ? Date.parse(notification.created_at) : Date.now()
    } satisfies Escalation;
  }

  function escalationSecondsRemaining(item: Escalation): number {
    const receivedAt = item.received_at ?? tickNow;
    const timeoutSeconds = item.timeout_seconds ?? 300;
    const elapsedSeconds = (tickNow - receivedAt) / 1000;
    return Math.max(Math.ceil(timeoutSeconds - elapsedSeconds), 0);
  }

  function attemptCountForGroup(group: StepGroup | null): number {
    if (!group) return 0;
    return group.attempts.length;
  }

  function attemptLabel(stepRun: StepRun): string {
    return `Attempt #${stepRun.attempt_number}`;
  }

  function stepTryLabel(stepRun: StepRun): string {
    return stepRun.attempt > 1 ? `step try #${stepRun.attempt}` : '';
  }

  function hasRecordedStepOutput(stepRun: StepRun | null): boolean {
    if (!stepRun) return false;
    if (stepRun.deliverable_id) return true;
    if (stepRun.deliverables.length > 0) return true;
    if (!stepRun.output) return false;
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

  function runtimeString(value: unknown): string {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    return '';
  }

  function runtimeEnvironment(stepRun: StepRun): Record<string, unknown> {
    const env = stepRun.runtime_info?.environment;
    return env && typeof env === 'object' ? (env as Record<string, unknown>) : {};
  }

  function runtimeInfo(stepRun: StepRun): Record<string, unknown> {
    return stepRun.runtime_info && typeof stepRun.runtime_info === 'object' ? stepRun.runtime_info : {};
  }

  function toolsLoadedLabel(info: Record<string, unknown>): string {
    const visible = runtimeString(info.visible_tool_count);
    const inventory = runtimeString(info.inventory_tool_count);
    if (visible && inventory && visible !== inventory) return `${visible} visible / ${inventory} loaded`;
    if (visible || inventory) return `${visible || inventory} loaded`;
    return '';
  }

  function runtimeSummaryRows(stepRun: StepRun): Array<{ label: string; value: string }> {
    const info = runtimeInfo(stepRun);
    if (Object.keys(info).length === 0) return [];
    return [
      { label: 'Executor', value: runtimeString(info.executor_id) || 'unresolved' },
      { label: 'Tools loaded', value: toolsLoadedLabel(info) },
      { label: 'Model target', value: runtimeString(info.resolved_model) || runtimeString(info.model) },
      { label: 'Reasoning', value: runtimeString(info.reasoning_effort) }
    ].filter((row) => row.value !== '');
  }

  function runtimeCompactRows(stepRun: StepRun): Array<{ label: string; value: string }> {
    const info = runtimeInfo(stepRun);
    if (Object.keys(info).length === 0) return [];
    return [
      { label: 'Executor', value: runtimeString(info.executor_id) || 'unresolved' },
      { label: 'Model', value: runtimeString(info.resolved_model) || runtimeString(info.model) || 'default' },
      { label: 'Reasoning', value: runtimeString(info.reasoning_effort) || 'default' }
    ];
  }

  function runtimeDebugRows(stepRun: StepRun): Array<{ label: string; value: string }> {
    const info = runtimeInfo(stepRun);
    if (Object.keys(info).length === 0) return [];
    const env = runtimeEnvironment(stepRun);
    return [
      { label: 'Provider target', value: runtimeString(info.resolved_provider_id) },
      { label: 'Executor type', value: runtimeString(info.executor_type) || 'unknown' },
      { label: 'Runtime', value: runtimeString(info.runtime_source) || 'unknown' },
      { label: 'Selection', value: runtimeString(info.selection_source) || 'unknown' },
      { label: 'Fallback', value: runtimeString(info.fallback_used) },
      { label: 'Tool strategy', value: runtimeString(info.strategy) },
      { label: 'Discovery', value: runtimeString(info.discovery_mode) },
      { label: 'Step profile', value: runtimeString(info.step_profile_id) },
      { label: 'Profile mode', value: runtimeString(info.step_profile_mode) },
      { label: 'User', value: runtimeString(env.user) || 'unknown' },
      { label: 'Home', value: runtimeString(env.home) || 'unknown' },
      { label: 'CWD', value: runtimeString(env.cwd) || 'unknown' }
    ].filter((row) => row.value !== '');
  }

  function runtimeRows(stepRun: StepRun): Array<{ label: string; value: string }> {
    return [...runtimeSummaryRows(stepRun), ...runtimeDebugRows(stepRun)];
  }

  function gateEvaluation(stepRun: StepRun): Record<string, unknown> | null {
    const gate = runtimeInfo(stepRun).gate_evaluation;
    return gate && typeof gate === 'object' ? (gate as Record<string, unknown>) : null;
  }

  function gateConditionRows(stepRun: StepRun): Array<{
    expression: string;
    operator: string;
    actual: string;
    expected: string;
    passed: boolean;
    error: string;
  }> {
    const gate = gateEvaluation(stepRun);
    const raw = Array.isArray(gate?.conditions) ? gate.conditions : [];
    return raw
      .filter((item): item is Record<string, unknown> => item !== null && typeof item === 'object')
      .map((item) => ({
        expression: runtimeString(item.expression) || '(empty condition)',
        operator: runtimeString(item.operator) || 'expression',
        actual: runtimeString(item.referenced_values) || '{}',
        expected: runtimeString(item.expected_values) || '{}',
        passed: item.passed === true,
        error: runtimeString(item.error)
      }));
  }

  function gateSummaryRows(stepRun: StepRun): Array<{ label: string; value: string }> {
    const gate = gateEvaluation(stepRun);
    if (!gate) return [];
    return [
      { label: 'Passed', value: gate.passed === true ? 'yes' : 'no' },
      { label: 'Action', value: runtimeString(gate.action_taken) },
      { label: 'Next', value: runtimeString(gate.next_step) },
      { label: 'Skipped', value: runtimeString(gate.skipped_steps) }
    ].filter((row) => row.value !== '');
  }

  function runtimeMissingMessage(stepRun: StepRun): string {
    return stepRun.runtime_info ? '' : 'Runtime not recorded for this attempt.';
  }

  function workflowStepSpec(stepName: string): WorkflowStepFormState | null {
    return diagramSteps.find((step) => step.name === stepName) ?? null;
  }

  function stepQuestionsAllowed(spec: WorkflowStepFormState | null): boolean {
    if (!spec?.allowQuestions) return false;
    const override = (task as { interaction_mode_override?: string | null } | null)?.interaction_mode_override ?? null;
    if (override === 'none' || override === 'explicit_gates') return false;
    if (override === 'step_requests') return true;
    return workflowDef?.interaction?.mode?.toString() === 'step_requests';
  }

  function stepSpecSummaryRows(group: StepGroup | null): Array<{ label: string; value: string }> {
    if (!group) return [];
    const spec = workflowStepSpec(group.stepName);
    const profile = spec
      ? spec.stepProfileId
        ? `${spec.stepProfileId} (${spec.stepProfileMode})`
        : `default (${spec.stepProfileMode})`
      : 'default';
    return [
      { label: 'Step type', value: group.stepType === 'gate' ? 'Gate' : 'Run' },
      { label: 'Step profile', value: profile }
    ];
  }

  function stepSpecRows(group: StepGroup | null, stepRun: StepRun | null): Array<{ label: string; value: string }> {
    if (!group) return [];
    const spec = workflowStepSpec(group.stepName);
    const runtime = stepRun ? runtimeInfo(stepRun) : {};
    const reasoning = spec?.reasoningEffort || runtimeString(runtime.reasoning_effort) || 'default';
    const input = spec
      ? spec.inputText
        ? `${spec.inputMode}: ${spec.inputText}`
        : spec.inputMode
      : '';
    const profile = spec
      ? spec.stepProfileId
        ? `${spec.stepProfileId} (${spec.stepProfileMode})`
        : `default (${spec.stepProfileMode})`
      : '';
    return [
      { label: 'Step type', value: group.stepType === 'gate' ? 'Gate' : 'Run' },
      { label: 'Configured reasoning', value: reasoning },
      { label: 'Evaluation', value: spec?.type === 'run' ? (spec.evaluate ? `enabled, max ${spec.maxAttempts}` : 'disabled') : '' },
      { label: 'Deliverable', value: spec ? (spec.requireDeliverable ? 'required' : 'optional') : '' },
      { label: 'Input', value: input },
      { label: 'Agent override', value: spec?.agentOverride ?? '' },
      { label: 'Step profile', value: profile },
      { label: 'Tool search', value: spec ? (spec.stepProfileAllowToolSearch ? 'enabled' : 'disabled') : '' },
      { label: 'Questions', value: spec ? (stepQuestionsAllowed(spec) ? 'allowed' : 'not allowed') : '' }
    ].filter((row) => row.value !== '');
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

  /**
   * Tiny coloured dot that carries the todo's status on its own, so
   * the row can collapse to a single line of text without a bordered
   * pill. Matches the chat-side compact rendering.
   */
  function todoStatusDot(status: string): string {
    if (status === 'in_progress') return 'bg-sky-400';
    if (status === 'completed') return 'bg-emerald-400';
    if (status === 'cancelled') return 'bg-slate-600';
    return 'bg-sky-400';
  }

  function todoPriorityClass(priority: string): string {
    if (priority === 'high') return 'text-rose-300';
    if (priority === 'low') return 'text-slate-500';
    return 'text-slate-400';
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
    sessionDrawerBackStack = [];
    sessionDrawer = {
      conversationId,
      sessionId,
      stepRunId: stepRun.step_run_id,
      stepName: `${stepRun.step_name} (attempt ${stepRun.attempt})`,
      agent: agentFor(stepRun.agent_id)
    };
    if (isProjectedStepRun(stepRun)) void loadStepRunDetail(stepRun.step_run_id);
  }

  async function openSessionLogsById(sessionId: string): Promise<void> {
    const currentDrawer = sessionDrawer;
    const conversationId = currentDrawer?.conversationId;
    if (!conversationId) return;
    if (currentDrawer?.sessionId === sessionId) return;
    let sessionRow: Session | null = null;
    try {
      const sessions = await api.conversations.sessions(conversationId);
      sessionRow = sessions.find((candidate) => candidate.session_id === sessionId) ?? null;
    } catch {
      // The log endpoint will still report a useful error if the session is inaccessible.
    }
    if (currentDrawer) {
      sessionDrawerBackStack = [...sessionDrawerBackStack, currentDrawer];
    }
    sessionDrawer = {
      conversationId,
      sessionId,
      stepRunId: null,
      stepName: sessionRow?.delegation_task ?? sessionRow?.agent_id ?? sessionId,
      agent: agentFor(sessionRow?.agent_id ?? null)
    };
  }

  function goBackSessionLogs(): void {
    const previous = sessionDrawerBackStack[sessionDrawerBackStack.length - 1];
    if (!previous) return;
    sessionDrawerBackStack = sessionDrawerBackStack.slice(0, -1);
    sessionDrawer = previous;
  }

  function closeSessionLogs(): void {
    sessionDrawer = null;
    sessionDrawerBackStack = [];
  }

  function pickAttemptForStep(stepName: string): StepRun | null {
    const group = stepGroups.find((candidate) => candidate.stepName === stepName);
    if (!group) return null;
    const stepRunId = selectedAttemptByStep[stepName];
    if (stepRunId) {
      const match = group.attempts.find((run) => run.step_run_id === stepRunId);
      if (match) return match;
    }
    return group.latest ?? null;
  }

  function openSessionLogsForStep(stepName: string): void {
    const stepRun = pickAttemptForStep(stepName);
    if (!stepRun) return;
    openSessionLogs(stepRun);
  }

  async function openTaskChat(): Promise<void> {
    if (!task || chatBusyKey) return;
    chatBusyKey = `task:${task.task_id}`;
    try {
      const result = await api.tasks.chat(task.task_id);
      addToast('Opened a chat continuation for this task.', 'success');
      await goto(`/chat/${result.conversation_id}`);
    } catch (err) {
      const apiError = asApiError(err);
      addToast(apiError.message || 'Could not open task chat.', 'error');
    } finally {
      chatBusyKey = null;
    }
  }

  async function openStepChat(stepRun: StepRun): Promise<void> {
    if (!task || chatBusyKey) return;
    chatBusyKey = `step:${stepRun.step_run_id}`;
    try {
      const result = await api.tasks.stepChat(task.task_id, stepRun.step_run_id);
      addToast('Opened a chat continuation for this step.', 'success');
      await goto(`/chat/${result.conversation_id}`);
    } catch (err) {
      const apiError = asApiError(err);
      addToast(apiError.message || 'Could not open step chat.', 'error');
    } finally {
      chatBusyKey = null;
    }
  }

  function openOutputModalForStep(stepName: string): void {
    const stepRun = pickAttemptForStep(stepName);
    if (!stepRun) return;
    openOutputModal(stepRun);
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

  /** Build step duration map (accumulated across attempts per step) */
  let diagramStepDurations = $derived.by(() => {
    if (!task) return {};
    const map: Record<string, string> = {};
    const totals = new Map<string, number>();
    for (const sr of task.step_runs) {
      const seconds = sr.duration_seconds;
      if (typeof seconds === 'number') {
        totals.set(sr.step_name, (totals.get(sr.step_name) ?? 0) + seconds);
        continue;
      }
      const started = sr.started_at ? Date.parse(sr.started_at) : NaN;
      const ended = sr.completed_at ? Date.parse(sr.completed_at) : tickNow;
      if (Number.isFinite(started) && Number.isFinite(ended)) {
        totals.set(sr.step_name, (totals.get(sr.step_name) ?? 0) + Math.max(0, (ended - started) / 1000));
      }
    }
    for (const [name, seconds] of totals) {
      const dur = formatDuration(new Date(0).toISOString(), new Date(seconds * 1000).toISOString(), tickNow);
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
        const attempts = [...group.attempts].sort((a, b) => b.attempt_number - a.attempt_number || b.attempt - a.attempt || stepRunSortValue(b) - stepRunSortValue(a));
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

  let selectedAttempt = $derived.by(() => {
    const group = selectedStepGroup;
    if (!group) return null;
    const stepRunId = selectedAttemptByStep[group.stepName];
    if (stepRunId) {
      const match = group.attempts.find((run) => run.step_run_id === stepRunId);
      if (match) return match;
    }
    return group.latest;
  });

  let isLatestAttemptSelected = $derived.by(() => {
    const group = selectedStepGroup;
    if (!group?.latest) return true;
    const attempt = selectedAttempt;
    if (!attempt) return true;
    return attempt.step_run_id === group.latest.step_run_id;
  });

  function preserveLoadedStepRuns(nextTask: TaskDetail, previousRuns: StepRun[]): TaskDetail {
    const nextRunsById = new Map(nextTask.step_runs.map((run) => [run.step_run_id, run]));
    for (const previous of previousRuns) {
      const next = nextRunsById.get(previous.step_run_id);
      if (stepHistoryLoaded.has(previous.step_name) && !next) {
        nextRunsById.set(previous.step_run_id, previous);
        continue;
      }
      if (next && !isProjectedStepRun(previous)) {
        const heavyPayloadMayBeStale =
          previous.status !== next.status || previous.deliverable_id !== next.deliverable_id;
        if (heavyPayloadMayBeStale) {
          nextRunsById.set(previous.step_run_id, next);
          continue;
        }
        nextRunsById.set(previous.step_run_id, {
          ...previous,
          status: next.status,
          attempt: next.attempt,
          attempt_number: next.attempt_number,
          superseded_by_step_run_id: next.superseded_by_step_run_id,
          deliverable_id: next.deliverable_id,
          require_deliverable: next.require_deliverable,
          started_at: next.started_at,
          completed_at: next.completed_at,
          updated_at: next.updated_at,
          duration_seconds: next.duration_seconds,
          accumulated_duration_seconds: next.accumulated_duration_seconds,
          latest_attempt_duration_seconds: next.latest_attempt_duration_seconds,
          is_projection: false
        });
      }
    }
    return { ...nextTask, step_runs: [...nextRunsById.values()] };
  }

  $effect(() => {
    const stepRun = selectedAttempt;
    if (!stepRun || !isProjectedStepRun(stepRun)) return;
    void loadStepRunDetail(stepRun.step_run_id);
  });

  function selectAttempt(stepName: string, stepRunId: string): void {
    selectedAttemptByStep = { ...selectedAttemptByStep, [stepName]: stepRunId };
  }

  function clearAttemptOverride(stepName: string): void {
    if (!(stepName in selectedAttemptByStep)) return;
    const next = { ...selectedAttemptByStep };
    delete next[stepName];
    selectedAttemptByStep = next;
  }

  let revisionStepOptions = $derived.by(() => {
    const seen = new Set<string>();
    const options: Array<{ name: string; label: string }> = [];
    for (const step of diagramSteps) {
      if (seen.has(step.name)) continue;
      seen.add(step.name);
      options.push({ name: step.name, label: step.name });
    }
    for (const group of stepGroups) {
      if (seen.has(group.stepName)) continue;
      seen.add(group.stepName);
      options.push({ name: group.stepName, label: group.stepName });
    }
    return options;
  });

  function startRevisionForStep(stepName: string): void {
    revisionTargetSeed = stepName;
    if (commentsRef) commentsRef.setRevisionTarget(stepName);
    if (typeof document !== 'undefined') {
      window.requestAnimationFrame(() => {
        const el = document.getElementById('task-comments-anchor');
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
  }

  async function handleCommentSubmitted(comment: { intent: string }): Promise<void> {
    // Clear the one-shot revision seed so the form resumes following the
    // user's currently selected workflow step on subsequent comments.
    revisionTargetSeed = null;
    if (comment.intent === 'answer_pause' || comment.intent === 'request_revision') {
      await refreshTaskOnly();
    }
  }

  async function loadProjectWorkflowOptions(projectId: string): Promise<void> {
    const key = ++projectWorkflowLoadKey;
    projectWorkflowOptionsLoaded = false;
    try {
      const next = await api.workflows.listAll({ project_id: projectId || null });
      if (key !== projectWorkflowLoadKey) return;
      projectWorkflowOptions = next;
      projectWorkflowOptionsLoaded = true;
      // Drop the stale workflow_id if it is no longer eligible for the new project.
      if (
        editForm.workflow_id &&
        !next.some((workflow) => workflow.workflow_id === editForm.workflow_id)
      ) {
        editForm.workflow_id = '';
      }
    } catch {
      if (key === projectWorkflowLoadKey) {
        projectWorkflowOptions = [];
        projectWorkflowOptionsLoaded = false;
      }
    }
  }

  $effect(() => {
    if (!configModalOpen) return;
    const key = `${editForm.project_id}`;
    if (key === lastProjectWorkflowKey) return;
    lastProjectWorkflowKey = key;
    void loadProjectWorkflowOptions(editForm.project_id);
  });

  $effect(() => {
    editForm.agent_profile_id = normalizeSelectedAgentProfileId(
      selectedEditAgent,
      editForm.agent_profile_id
    );
  });

  let stepAttemptCounts = $derived.by(() => Object.fromEntries(stepGroups.map((group) => [group.stepName, attemptCountForGroup(group)])));

  let stepStateLabels = $derived.by(() => {
    const labels: Record<string, string> = {};
    for (const group of stepGroups) {
      if (task?.pending_pause?.step_name === group.stepName) {
        labels[group.stepName] = taskPauseLabel(task.pending_pause.pause_type);
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
        ? taskPauseSummaryLabel(task.pending_pause.pause_type)
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
    const latestByStep = new Map<string, StepRun>();
    for (const run of runs) {
      const existing = latestByStep.get(run.step_name);
      if (!existing || run.attempt >= existing.attempt) {
        latestByStep.set(run.step_name, run);
      }
    }
    const latestRuns = [...latestByStep.values()];
    const totalAttempts = latestRuns.reduce((sum, run) => sum + Math.max(run.attempt, 1), 0);
    const completedSteps = new Set(
      runs
        .filter((r) => ['approved', 'completed'].includes(r.status) && stepOutcomeStatus(r) === 'success')
        .map((r) => r.step_name)
    ).size;
    const evalRevisions = latestRuns.reduce((sum, run) => sum + Math.max(run.attempt - 1, 0), 0);
    const evalFailures = runs.filter((r) => r.evaluation && String(r.evaluation.decision) === 'failed').length;
    const multiAttemptSteps = new Set(
      latestRuns.filter((r) => r.attempt > 1).map((r) => r.step_name)
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
  let creatorAgent = $derived(agentFor(task?.created_by_agent_id ?? null));

  let activePause = $derived.by(() => {
    if (!task?.pending_pause || task.status !== 'paused') return null;
    const pause = task.pending_pause;
    if (pause.pause_type === 'escalation') return null;
    if (pause.pause_type === 'credential_request') return null;
    const currentStepName = task.workflow_run?.current_step_name;
    if (pause.step_name && currentStepName && pause.step_name !== currentStepName) {
      return {
        ...pause,
        question: pause.question ?? pause.questions?.[0]?.question ?? 'Task is paused and waiting for input.'
      };
    }
    return pause;
  });

  let activeStepQuestions = $derived.by(() => {
    if (!activePause || activePause.pause_type === 'gate') return [] as QuestionSetQuestion[];
    return activePause.questions ?? [];
  });

  function stepQuestionState(questionId: string): { selected: string[]; custom: string } {
    return stepQuestionAnswers[questionId] ?? { selected: [], custom: '' };
  }

  function taskQuestionDraftNamespace(): string {
    return `task:${taskIdFromRoute()}`;
  }

  function activeStepQuestionNotificationId(): string | null {
    const notificationId = activePause?.pause_id;
    return typeof notificationId === 'string' && notificationId ? notificationId : null;
  }

  function persistStepQuestionDraft(): void {
    writeQuestionDraft(
      taskQuestionDraftNamespace(),
      activeStepQuestionNotificationId(),
      stepQuestionAnswers,
    );
  }

  function restoreStepQuestionDraft(): QuestionDraftAnswers {
    return readQuestionDraft(taskQuestionDraftNamespace(), activeStepQuestionNotificationId());
  }

  function clearActiveStepQuestionDraft(): void {
    clearQuestionDraft(taskQuestionDraftNamespace(), activeStepQuestionNotificationId());
  }

  $effect(() => {
    const notificationId = activeStepQuestionNotificationId();
    if (notificationId === lastStepQuestionNotificationId) {
      return;
    }
    lastStepQuestionNotificationId = notificationId;
    stepQuestionAnswers = notificationId ? restoreStepQuestionDraft() : {};
  });

  function setStepQuestionCustom(questionId: string, value: string): void {
    const current = stepQuestionState(questionId);
    stepQuestionAnswers = {
      ...stepQuestionAnswers,
      [questionId]: { ...current, custom: value }
    };
    persistStepQuestionDraft();
  }

  function toggleStepQuestionOption(question: QuestionSetQuestion, optionId: string): void {
    const current = stepQuestionState(question.id);
    const selected = new Set(current.selected);
    if (question.multiple) {
      if (selected.has(optionId)) {
        selected.delete(optionId);
      } else {
        selected.add(optionId);
      }
    } else {
      selected.clear();
      selected.add(optionId);
    }
    stepQuestionAnswers = {
      ...stepQuestionAnswers,
      [question.id]: { ...current, selected: Array.from(selected) }
    };
    persistStepQuestionDraft();
  }

  function buildStepQuestionReply(questions: QuestionSetQuestion[]): QuestionSetAnswer[] {
    return questions.map((question) => {
      const current = stepQuestionState(question.id);
      const custom = current.custom.trim();
      return {
        question_id: question.id,
        selected_option_ids: current.selected,
        custom_answer: custom ? custom : null
      };
    });
  }

  function stepQuestionOptionSelected(questionId: string, optionId: string): boolean {
    return stepQuestionState(questionId).selected.includes(optionId);
  }

  function stepQuestionAnswersSatisfyRequired(
    questions: QuestionSetQuestion[],
    answers: QuestionSetAnswer[]
  ): boolean {
    const answersById = new Map(answers.map((answer) => [answer.question_id, answer]));
    return questions.every((question) => {
      if (!question.required) return true;
      const answer = answersById.get(question.id);
      return Boolean(answer && (answer.selected_option_ids.length > 0 || answer.custom_answer?.trim()));
    });
  }

  function currentStepQuestionReplyValid(): boolean {
    const questions = activeStepQuestions;
    const answers = buildStepQuestionReply(questions);
    return answers.some((answer) => answer.selected_option_ids.length > 0 || answer.custom_answer?.trim())
      && stepQuestionAnswersSatisfyRequired(questions, answers);
  }

  function buildStepQuestionReplyWithOverride(
    questions: QuestionSetQuestion[],
    override: QuestionSetAnswer
  ): QuestionSetAnswer[] {
    return questions.map((question) => {
      if (question.id === override.question_id) return override;
      const current = stepQuestionState(question.id);
      const custom = current.custom.trim();
      return {
        question_id: question.id,
        selected_option_ids: current.selected,
        custom_answer: custom ? custom : null
      };
    });
  }

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

  let stepHasLogs = $derived.by(() => {
    const entries: Array<[string, boolean]> = [];
    for (const group of stepGroups) {
      const stepRunId = selectedAttemptByStep[group.stepName];
      const candidate = stepRunId
        ? group.attempts.find((run) => run.step_run_id === stepRunId) ?? group.latest
        : group.latest;
      entries.push([group.stepName, Boolean(candidate?.output?.session_id || candidate?.session_id)]);
    }
    return Object.fromEntries(entries);
  });
  let stepHasOutput = $derived.by(() => {
    const entries: Array<[string, boolean]> = [];
    for (const group of stepGroups) {
      const stepRunId = selectedAttemptByStep[group.stepName];
      const candidate = stepRunId
        ? group.attempts.find((run) => run.step_run_id === stepRunId) ?? group.latest
        : group.latest;
      entries.push([group.stepName, hasRecordedStepOutput(candidate)]);
    }
    return Object.fromEntries(entries);
  });

  async function refreshTaskEscalations(): Promise<void> {
    if (!task) {
      taskEscalations = [];
      taskCredentialRequest = null;
      return;
    }
    const notifications = await api.notifications.list(null, { taskId: task.task_id });
    taskEscalations = sortEscalations(
      notifications
        .map((notification) => taskEscalationFromNotification(notification))
        .filter((notification): notification is Escalation => notification !== null)
        .filter((notification) => !isEscalationExpired(notification))
    );
    taskCredentialRequest = notifications.find(
      (notification) => notification.notification_type === 'credential_request' && notification.status === 'pending'
    ) ?? null;
  }

  // ---------------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------------

  async function loadTask(): Promise<void> {
    loading = true;
    error = '';
    try {
      const data = await loadTaskPageData(api, taskIdFromRoute());
      task = preserveLoadedStepRuns(data.task, task?.step_runs ?? []);
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
        agent_profile_id: task.agent_profile_id ?? '',
        workflow_id: task.workflow_id ?? '',
        project_id: task.project_id ?? '',
        delivery_mode: task.delivery.mode,
        delivery_target: task.delivery.target ?? '',
        completion_mode_family: task.completion_mode_family,
        allow_silent_completion: task.allow_silent_completion,
        interaction_mode_override: task.interaction_mode_override ?? '',
        allow_policy_text: policyText(task.session_policy, 'allow_policies'),
        deny_policy_text: policyText(task.session_policy, 'deny_policies')
      };
      selectedStepName = defaultStepSelection(task, selectedStepName);
      try {
        projects = await api.projects.list();
      } catch {
        projects = [];
      }
      void loadProjectWorkflowOptions(editForm.project_id);
      try {
        await refreshTaskEscalations();
      } catch {
        taskEscalations = [];
        taskCredentialRequest = null;
      }
    } catch (caughtError) {
      task = null;
      taskEscalations = [];
      taskCredentialRequest = null;
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function refreshTaskOnly(): Promise<void> {
    if (document.hidden) return;
    try {
      const previousRuns = task?.step_runs ?? [];
      const data = await refreshTaskPageData(api, taskIdFromRoute(), allTasks);
      task = preserveLoadedStepRuns(data.task, previousRuns);
      allTasks = data.allTasks;
      error = data.auxiliaryError;
      selectedStepName = defaultStepSelection(task, selectedStepName);
      const activeSelectedAttempt = selectedAttempt;
      if (
        activeSelectedAttempt &&
        !isProjectedStepRun(activeSelectedAttempt) &&
        ['running', 'evaluating'].includes(activeSelectedAttempt.status)
      ) {
        void loadStepRunDetail(activeSelectedAttempt.step_run_id);
      }
      const drawerStepRun = sessionDrawerStepRun;
      if (drawerStepRun && (
        isProjectedStepRun(drawerStepRun) ||
        ['running', 'evaluating'].includes(drawerStepRun.status)
      )) {
        void loadStepRunDetail(drawerStepRun.step_run_id);
      }
      try {
        await refreshTaskEscalations();
      } catch {
        taskEscalations = [];
        taskCredentialRequest = null;
      }
      try {
        await commentsRef?.refresh();
      } catch {
        // best-effort: comments will retry on next poll
      }
    } catch (caughtError) {
      if (shouldClearTaskFromError(caughtError)) {
        task = null;
        taskEscalations = [];
        taskCredentialRequest = null;
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
        agent_profile_id: editForm.agent_profile_id || null,
        workflow_id: editForm.workflow_id || null,
        project_id: editForm.project_id || null,
        delivery_mode: editForm.delivery_mode,
        delivery_target: editForm.delivery_mode === 'specific_conversation' ? editForm.delivery_target : null,
        completion_mode_family: editForm.completion_mode_family,
        allow_silent_completion: editForm.allow_silent_completion,
        interaction_mode_override: editForm.interaction_mode_override || null,
        session_policy: policyFromText(editForm.allow_policy_text, editForm.deny_policy_text)
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

  async function respondToStepQuestion(answersOverride?: QuestionSetAnswer[]): Promise<void> {
    if (!task) return;
    const questions = task.pending_pause?.questions ?? [];
    const answers = answersOverride ?? buildStepQuestionReply(questions);
    if (!stepQuestionAnswersSatisfyRequired(questions, answers)) {
      error = 'Answer all required questions before sending.';
      return;
    }
    try {
      await api.tasks.stepResponse(task.task_id, {
        mode: 'structured',
        answers
      });
      stepResponse = '';
      clearActiveStepQuestionDraft();
      stepQuestionAnswers = {};
      task = await api.tasks.detail(task.task_id);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    }
  }

  async function submitStepQuestionOption(question: QuestionSetQuestion, optionId: string): Promise<void> {
    if (!task) return;
    if (activeStepQuestions.length !== 1 || question.multiple) {
      toggleStepQuestionOption(question, optionId);
      return;
    }
    const custom = stepQuestionState(question.id).custom.trim();
    await respondToStepQuestion(buildStepQuestionReplyWithOverride(activeStepQuestions, {
      question_id: question.id,
      selected_option_ids: [optionId],
      custom_answer: custom ? custom : null
    }));
  }

  async function respondToEscalation(notificationId: string, decision: 'approve' | 'deny'): Promise<void> {
    taskEscalationBusyCallId = notificationId;
    try {
      await api.notifications.resolve(notificationId, { decision });
      await refreshTaskOnly();
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      if (taskEscalationBusyCallId === notificationId) {
        taskEscalationBusyCallId = null;
      }
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

  async function rerunTask(): Promise<void> {
    if (!task || rerunBusy) return;
    rerunBusy = true;
    try {
      const rerun = await api.tasks.rerun(task.task_id);
      error = '';
      if (rerun.created_new) {
        addToast('Created a new rerun task.', 'success');
        await goto(`/tasks/${rerun.task_id}`);
        return;
      }
      await refreshTaskOnly();
      addToast('Task resumed.', 'success');
    } catch (caughtError) {
      error = asApiError(caughtError).message;
      addToast(error, 'error', 4_000, 'Unable to re-run task');
    } finally {
      rerunBusy = false;
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
  <!--
    Task detail should behave like a normal full-width page, not a
    horizontally pannable canvas. Keep the root locked to the viewport
    width and let only explicitly wide children (workflow diagram,
    chip rows, code/table blocks) own horizontal scrolling.
  -->
  <section class="min-h-0 min-w-0 w-full max-w-full space-y-5 overflow-x-hidden px-3 py-4 sm:px-5 sm:py-6">
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
            <h1 class="mt-1 break-words text-2xl font-semibold text-white" title={task.title}>{task.title}</h1>
            <div class="mt-2 flex flex-wrap items-center gap-2 text-sm text-slate-400">
              <span>Owner agent</span>
              <span class="font-medium text-slate-200">{agentName(task.agent_id)}</span>
              <span class="rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.2em] {priorityTone(task.priority)}">P{task.priority}</span>
              {#if task.attempt_number > 1}
                <span class="rounded-full border border-amber-500/30 bg-amber-500/10 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.2em] text-amber-200" title="Task has been revised">Attempt #{task.attempt_number}</span>
              {/if}
              {#if task.project_id}
                <a href="/projects/{task.project_id}" class="rounded-full border border-violet-500/30 bg-violet-500/10 px-2.5 py-1 text-[11px] font-medium tracking-wide text-violet-200 hover:border-violet-400/60 hover:text-violet-100">Project</a>
              {/if}
              <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-[11px] text-slate-300">{deliveryModeLabel(task.delivery.mode)}</span>
              <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-[11px] text-slate-300">{completionModeFamilyLabel(task.completion_mode_family)}</span>
              {#if task.allow_silent_completion}
                <span class="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-[11px] text-slate-300">Silent allowed</span>
              {/if}
            </div>
          </div>
        </div>
      </div>
      <div class="flex w-full flex-col items-start gap-3 sm:w-auto sm:items-end">
        <span class="rounded-full border px-3 py-1 text-xs font-semibold uppercase tracking-[0.2em] {statusColors[task.status] ?? 'border-slate-700 text-slate-200'}">
          {completionModeLabel(task)}
        </span>
        <div class="flex w-full flex-wrap gap-2 sm:w-auto sm:justify-end">
          <Button class="flex-1 justify-center sm:flex-none lg:hidden" size="sm" disabled={rerunBusy} onclick={openMobilePrimaryAction}>
            {mobilePrimaryActionLabel()}
          </Button>
          <Button class="lg:hidden" aria-label="More task actions" size="sm" variant="secondary" onclick={() => (taskActionsOpen = true)}>
            <MoreVertical class="h-3.5 w-3.5" />
          </Button>

          {#if isRerunnable}
            <Button class="hidden lg:inline-flex" size="sm" disabled={rerunBusy} onclick={rerunTask}>
              Re-run task
            </Button>
          {/if}
          <Button class="hidden lg:inline-flex" size="sm" variant="secondary" onclick={() => (configModalOpen = true)}>
            <Settings2 class="mr-1.5 h-3.5 w-3.5" />
            Configure
          </Button>
          <Button class="hidden lg:inline-flex" size="sm" variant="secondary" disabled={chatBusyKey !== null} onclick={openTaskChat}>
            {#if chatBusyKey === `task:${task.task_id}`}
              <LoaderCircle class="mr-1.5 h-3.5 w-3.5 animate-spin" />
            {:else}
              <MessageSquarePlus class="mr-1.5 h-3.5 w-3.5" />
            {/if}
            Chat about task
          </Button>
          {#if isCancellable}
            <Button class="hidden lg:inline-flex" size="sm" variant="danger" onclick={cancelTask}>Cancel task</Button>
          {/if}
        </div>
      </div>
    </div>

    {#if error}
      <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <div class="grid min-w-0 gap-5 lg:grid-cols-[minmax(0,1fr)_380px]">
      <div class="min-w-0 space-y-5">
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
                  {@const selectedTopAttempt = pickAttemptForStep(selectedStepGroup.stepName)}
                  <div class="flex items-center gap-2">
                    {#if selectedTopAttempt?.session_id || selectedTopAttempt?.output?.session_id}
                      <Button size="sm" variant="secondary" disabled={chatBusyKey !== null} onclick={() => openStepChat(selectedTopAttempt)}>
                        {#if chatBusyKey === `step:${selectedTopAttempt.step_run_id}`}
                          <LoaderCircle class="mr-1.5 h-3.5 w-3.5 animate-spin" />
                        {:else}
                          <MessageSquarePlus class="mr-1.5 h-3.5 w-3.5" />
                        {/if}
                        Chat about step
                      </Button>
                    {:else}
                      <Tooltip text="No step session recorded yet.">
                        <button type="button" aria-disabled="true" class="inline-flex items-center justify-center rounded-xl border border-slate-700 bg-slate-900/60 px-3 py-1.5 text-sm font-medium text-slate-500 cursor-not-allowed">
                          Chat about step
                        </button>
                      </Tooltip>
                    {/if}
                    {#if stepHasOutput[selectedStepGroup.stepName]}
                      <Button size="sm" variant="secondary" onclick={() => openOutputModalForStep(selectedStepGroup.stepName)}>Open output</Button>
                    {/if}
                    {#if stepHasLogs[selectedStepGroup.stepName]}
                      <Button size="sm" variant="secondary" onclick={() => openSessionLogsForStep(selectedStepGroup.stepName)}>Open logs</Button>
                    {/if}
                    <Button class="lg:hidden" size="sm" variant="secondary" onclick={() => openStepDetail(selectedStepGroup.stepName)}>
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
                        class="inline-flex items-center gap-2 rounded-full border border-slate-700 bg-slate-950/80 px-3.5 py-2 text-sm text-slate-200 transition hover:border-slate-600 hover:text-white md:py-1.5 md:text-xs"
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
                      <GitBranch class="h-3.5 w-3.5 text-sky-300" />
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

        <div id="task-comments-anchor">
          <TaskComments
            bind:this={commentsRef}
            task={task}
            stepOptions={revisionStepOptions}
            initialTargetStep={revisionTargetSeed ?? selectedStepName}
            onSubmitted={handleCommentSubmitted}
          />
        </div>

        {#if taskEscalations.length > 0}
          {@const activeEscalation = taskEscalations[0]}
          <EscalationPrompt
            item={activeEscalation}
            secondsRemaining={escalationSecondsRemaining(activeEscalation)}
            pending={taskEscalationBusyCallId === activeEscalation.call_id}
            queuedCount={taskEscalations.length - 1}
            onApprove={() => respondToEscalation(activeEscalation.call_id, 'approve')}
            onDeny={() => respondToEscalation(activeEscalation.call_id, 'deny')}
          />
        {/if}

        {#if taskCredentialRequest}
          <CredentialRequestForm
            notification={taskCredentialRequest}
            onResolved={async () => {
              taskCredentialRequest = null;
              await refreshTaskOnly();
            }}
          />
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
                <h2 class="mt-3 text-lg font-semibold text-white">
                  {activePause.pause_type === 'gate'
                    ? activePause.question
                    : activeStepQuestions[0]?.question ?? 'Step question'}
                </h2>
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
                <div class="space-y-4">
                  {#each activeStepQuestions as question, questionIndex (question.id)}
                    {@const answerState = stepQuestionState(question.id)}
                    <div class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
                      <div class="flex flex-wrap items-center gap-2">
                        <span class="rounded-full border border-slate-700 px-2 py-0.5 text-[10px] uppercase tracking-[0.2em] text-slate-400">Question {questionIndex + 1}</span>
                        {#if question.multiple}
                          <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[10px] uppercase tracking-[0.2em] text-sky-200">Multi-select</span>
                        {/if}
                        {#if !question.required}
                          <span class="rounded-full border border-slate-700 px-2 py-0.5 text-[10px] uppercase tracking-[0.2em] text-slate-400">Optional</span>
                        {/if}
                      </div>
                      {#if question.header}
                        <p class="mt-3 text-xs uppercase tracking-[0.2em] text-slate-500">{question.header}</p>
                      {/if}
                      <p class="mt-2 text-sm font-medium text-slate-100">{question.question}</p>
                      {#if question.options.length > 0}
                        <div class="mt-3 space-y-2">
                          {#each question.options as option (option.id)}
                            <button
                              type="button"
                              class={`flex w-full items-start gap-3 rounded-2xl border px-3 py-2 text-left text-xs transition ${stepQuestionOptionSelected(question.id, option.id) ? 'border-sky-400/70 bg-sky-500/20 text-sky-50' : 'border-slate-700 bg-slate-900/70 text-slate-200 hover:border-sky-400/40 hover:bg-sky-500/10 hover:text-white'}`}
                              onclick={() => { void submitStepQuestionOption(question, option.id); }}
                            >
                              <span class={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center border ${question.multiple ? 'rounded' : 'rounded-full'} ${stepQuestionOptionSelected(question.id, option.id) ? 'border-sky-200 bg-sky-300 text-slate-950' : 'border-slate-500 bg-slate-950/40'}`}>
                                {#if stepQuestionOptionSelected(question.id, option.id)}
                                  {#if question.multiple}
                                    ✓
                                  {:else}
                                    <span class="h-1.5 w-1.5 rounded-full bg-slate-950"></span>
                                  {/if}
                                {/if}
                              </span>
                              <span class="min-w-0">
                                <span class="block font-medium">{option.label}</span>
                                {#if option.description}
                                  <span class="mt-0.5 block text-slate-400">{option.description}</span>
                                {/if}
                              </span>
                            </button>
                          {/each}
                        </div>
                      {/if}
                      {#if question.allow_custom}
                        <textarea
                          value={answerState.custom}
                          oninput={(event) => setStepQuestionCustom(question.id, event.currentTarget.value)}
                          class="mt-3 min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"
                          placeholder="Optional custom answer"
                        ></textarea>
                      {/if}
                    </div>
                  {/each}
                  {#if activeStepQuestions.length === 0}
                    <p class="rounded-2xl border border-rose-500/20 bg-rose-500/5 p-4 text-sm text-rose-200">This step question has no question-set payload and cannot be answered from the task view.</p>
                  {/if}
                  <div class="flex flex-wrap gap-2">
                    <Button size="sm" disabled={activeStepQuestions.length === 0 || !currentStepQuestionReplyValid()} onclick={() => respondToStepQuestion()}>Send response</Button>
                  </div>
                </div>
              {/if}
              </div>
            </div>
          </Card>
        {/if}

        <!--
          Desktop-only inline step detail. On mobile we rely on the dedicated
          bottom sheet so the page keeps one clear detail pattern.
        -->
        <Card class="hidden overflow-hidden p-0 lg:block">
          <div class="border-b border-slate-800/80 px-4 py-3 sm:px-5 sm:py-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div class="min-w-0">
                <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Step detail</p>
                <h2 class="mt-1 text-lg font-semibold text-white">{selectedStepGroup?.stepName ?? workflowName(task.workflow_id)}</h2>
                {#if !selectedStepGroup?.latest}
                  <p class="mt-1 text-sm text-slate-400">Pick a step from the Live workflow above to focus it here.</p>
                {/if}
              </div>
              {#if selectedStepGroup && selectedStepGroup.attempts.length > 1}
                <div class="flex max-w-full flex-wrap items-center gap-2 overflow-x-auto">
                  <span class="text-xs uppercase tracking-[0.2em] text-slate-500">Attempts</span>
                  {#each selectedStepGroup.attempts as run (run.step_run_id)}
                    {@const isSelected = (selectedAttempt?.step_run_id ?? selectedStepGroup.latest?.step_run_id) === run.step_run_id}
                    {@const isLatestRun = selectedStepGroup.latest?.step_run_id === run.step_run_id}
                    {@const status = displayStepStatus(run)}
                    <button
                      type="button"
                      class={`inline-flex shrink-0 items-center gap-2 rounded-full border px-3 py-1.5 text-xs transition ${isSelected ? 'border-sky-400/60 bg-sky-500/10 text-sky-100' : 'border-slate-700 bg-slate-950/70 text-slate-300 hover:border-slate-600 hover:text-white'}`}
                      onclick={() => isLatestRun ? clearAttemptOverride(selectedStepGroup.stepName) : selectAttempt(selectedStepGroup.stepName, run.step_run_id)}
                      aria-pressed={isSelected}
                    >
                      <span class="font-mono text-[11px]">{attemptLabel(run)}</span>
                      {#if stepTryLabel(run)}<span class="text-[10px] text-slate-500">{stepTryLabel(run)}</span>{/if}
                      {#if isLatestRun}<span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-sky-200">latest</span>{/if}
                      <span class="rounded-full border px-1.5 py-0.5 text-[10px] uppercase tracking-wider {statusColors[status] ?? 'border-slate-600 text-slate-400'}">{status}</span>
                    </button>
                  {/each}
                  {#if !isLatestAttemptSelected}
                    <button type="button" class="text-xs text-sky-300 hover:text-sky-200" onclick={() => clearAttemptOverride(selectedStepGroup.stepName)}>Show latest</button>
                  {/if}
                </div>
              {:else if selectedStepGroup?.latest}
                <span class="rounded-full border border-slate-700 bg-slate-950/80 px-3 py-1 text-xs text-slate-300">
                  {attemptLabel(selectedStepGroup.latest)}
                </span>
              {/if}
            </div>
          </div>

          <div class="min-w-0 space-y-4 px-4 py-4 sm:px-5 sm:py-5">
              {#if selectedStepGroup}
                {#if selectedStepGroup.latest}
                  {@const attempt = selectedAttempt ?? selectedStepGroup.latest}
                  {@const summary = stepOutputSummary(attempt)}
                  {@const claims = stepOutputClaims(attempt)}
                  {@const stepError = stepOutputError(attempt)}
                  {@const outcomeStatus = stepOutcomeStatus(attempt)}
                  {@const outcomeReason = stepOutcomeReason(attempt)}
                  {@const visibleStatus = displayStepStatus(attempt)}
                  {@const feedback = stepEvalFeedback(attempt)}
                  {@const isLatest = isLatestAttemptSelected}
                  <article class="rounded-3xl border border-slate-800 bg-slate-950/60 p-4 sm:p-5">
                    <div class="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div class="flex flex-wrap items-center gap-2">
                          <h3 class="text-lg font-semibold text-white">{attempt.step_name}</h3>
                          {#if selectedStepGroup.attempts.length > 1 && isLatest}
                            <span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-sky-200">Latest attempt</span>
                          {:else if !isLatest}
                            <span class="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-amber-200">Earlier attempt</span>
                          {/if}
                          <span class="text-xs text-slate-500">{attemptLabel(attempt)}</span>
                          {#if stepTryLabel(attempt)}<span class="text-xs text-slate-500">{stepTryLabel(attempt)}</span>{/if}
                        </div>
                        <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                          <span>{attempt.step_type === 'gate' ? 'Gate' : 'Run'}</span>
                          {#if attempt.agent_id}
                            <span class="inline-flex items-center gap-2 text-slate-300">
                              <AgentAvatar name={agentName(attempt.agent_id)} avatarUrl={agentFor(attempt.agent_id)?.avatar_url ?? null} class="h-5 w-5 rounded-lg" />
                              {agentName(attempt.agent_id)}
                            </span>
                          {/if}
                          {#if attempt.started_at}
                            <Tooltip text={formatAbsoluteTime(attempt.started_at)}>
                              <span class="inline-flex cursor-help items-center gap-1"><Clock3 class="h-3.5 w-3.5" />started {formatRelativeTime(attempt.started_at)}</span>
                            </Tooltip>
                          {/if}
                          {#if attempt.started_at}
                            <span class="font-mono text-slate-300">{formatDuration(attempt.started_at, attempt.completed_at, tickNow)}</span>
                          {/if}
                        </div>
                      </div>
                      <div class="flex items-center gap-2">
                        {#if attempt.session_id || attempt.output?.session_id}
                          <Button size="sm" variant="secondary" disabled={chatBusyKey !== null} onclick={() => openStepChat(attempt)}>
                            {#if chatBusyKey === `step:${attempt.step_run_id}`}
                              <LoaderCircle class="mr-1.5 h-3.5 w-3.5 animate-spin" />
                            {:else}
                              <MessageSquarePlus class="mr-1.5 h-3.5 w-3.5" />
                            {/if}
                            Chat
                          </Button>
                        {/if}
                        {#if attempt.output?.session_id || attempt.session_id}
                          <Button size="sm" variant="secondary" onclick={() => openSessionLogs(attempt)}>Logs</Button>
                        {/if}
                        {#if isEditable}
                          <Button size="sm" variant="secondary" onclick={() => startRevisionForStep(selectedStepGroup.stepName)}>Revise</Button>
                        {/if}
                        <Tooltip text={displayStepStatusHint(attempt)}>
                          <span class="cursor-help rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider {statusColors[visibleStatus] ?? 'border-slate-600 text-slate-400'}">{visibleStatus}</span>
                        </Tooltip>
                      </div>
                    </div>

                    {#if outcomeStatus !== 'success'}
                      <div class="mt-4 rounded-2xl border border-sky-500/20 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
                        <p class="font-medium uppercase tracking-wide text-[11px] text-sky-300">Outcome marker</p>
                        <p class="mt-1">This attempt completed but reported <span class="font-semibold uppercase">{outcomeStatus}</span>{#if outcomeReason}: {outcomeReason}{/if}</p>
                      </div>
                    {/if}

                    {#if gateEvaluation(attempt)}
                      <details class="mt-4 rounded-2xl border border-amber-500/20 bg-amber-500/10 px-4 py-3" open>
                        <summary class="flex cursor-pointer list-none flex-wrap items-center justify-between gap-3">
                          <span class="text-xs uppercase tracking-[0.25em] text-amber-200">Gate evaluation</span>
                          <span class="flex flex-wrap items-center gap-2">
                            {#each gateSummaryRows(attempt) as row}
                              <span class="rounded-full border border-amber-400/30 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-100">
                                {row.label}: <span class="font-mono">{row.value}</span>
                              </span>
                            {/each}
                            <ChevronDown class="h-4 w-4 shrink-0 text-amber-200/70" />
                          </span>
                        </summary>
                        {#if gateConditionRows(attempt).length > 0}
                          <div class="mt-3 space-y-3 text-xs">
                            {#each gateConditionRows(attempt) as row}
                              <div class="rounded-xl border border-amber-400/20 bg-slate-950/60 px-3 py-2">
                                <div class="flex flex-wrap items-center justify-between gap-2">
                                  <code class="break-all text-amber-100">{row.expression}</code>
                                  <span class={`rounded-full border px-2 py-0.5 uppercase tracking-wider ${row.passed ? 'border-emerald-400/40 text-emerald-200' : 'border-rose-400/40 text-rose-200'}`}>{row.passed ? 'passed' : 'failed'}</span>
                                </div>
                                <dl class="mt-2 grid gap-2 sm:grid-cols-3">
                                  <div><dt class="text-slate-500">Operator</dt><dd class="mt-1 font-mono text-slate-200">{row.operator}</dd></div>
                                  <div><dt class="text-slate-500">Actual values</dt><dd class="mt-1 truncate font-mono text-slate-200" title={row.actual}>{row.actual}</dd></div>
                                  <div><dt class="text-slate-500">Expected / thresholds</dt><dd class="mt-1 truncate font-mono text-slate-200" title={row.expected}>{row.expected}</dd></div>
                                </dl>
                                {#if row.error}<p class="mt-2 text-rose-200">{row.error}</p>{/if}
                              </div>
                            {/each}
                          </div>
                        {:else}
                          <p class="mt-3 text-sm text-amber-100">No condition expressions were recorded for this gate.</p>
                        {/if}
                      </details>
                    {/if}

                    {#if stepSpecRows(selectedStepGroup, attempt).length > 0}
                      <details class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                        <summary class="flex cursor-pointer list-none flex-wrap items-center justify-between gap-3">
                          <span class="text-xs uppercase tracking-[0.25em] text-slate-500">Current workflow spec</span>
                          <span class="flex flex-wrap items-center gap-2">
                            {#each stepSpecSummaryRows(selectedStepGroup) as row}
                              <span class="rounded-full border border-slate-700 bg-slate-900/70 px-2 py-0.5 text-[11px] text-slate-300">
                                {row.label}: <span class="font-mono text-slate-100">{row.value}</span>
                              </span>
                            {/each}
                            <ChevronDown class="h-4 w-4 shrink-0 text-slate-500" />
                          </span>
                        </summary>
                        <p class="mt-3 text-xs text-slate-500">Reflects the current workflow template, while runtime rows show this attempt's recorded execution target.</p>
                        <dl class="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                          {#each stepSpecRows(selectedStepGroup, attempt) as row}
                            <div class="min-w-0 rounded-lg border border-slate-800/70 bg-slate-900/40 px-2.5 py-2">
                              <dt class="text-slate-500">{row.label}</dt>
                              <dd class="mt-1 truncate font-mono text-slate-300" title={row.value}>{row.value}</dd>
                            </div>
                          {/each}
                        </dl>
                      </details>
                    {/if}

                    <details class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                      <summary class="flex cursor-pointer list-none flex-wrap items-center justify-between gap-3">
                        <span class="text-xs uppercase tracking-[0.25em] text-slate-500">Runtime and model</span>
                        <span class="flex flex-wrap items-center gap-2">
                          {#each runtimeCompactRows(attempt) as row}
                            <span class="rounded-full border border-slate-700 bg-slate-900/70 px-2 py-0.5 text-[11px] text-slate-300">
                              {row.label}: <span class="font-mono text-slate-100">{row.value}</span>
                            </span>
                          {/each}
                          <ChevronDown class="h-4 w-4 shrink-0 text-slate-500" />
                        </span>
                      </summary>
                      {#if runtimeRows(attempt).length > 0}
                        <dl class="mt-3 grid gap-2 text-xs sm:grid-cols-2">
                          {#each runtimeRows(attempt) as row}
                            <div class="min-w-0 rounded-lg border border-slate-800/70 bg-slate-900/40 px-2.5 py-2">
                              <dt class="text-slate-500">{row.label}</dt>
                              <dd class="mt-1 truncate font-mono text-slate-300" title={row.value}>{row.value}</dd>
                            </div>
                          {/each}
                        </dl>
                      {:else}
                        <p class="mt-3 text-sm text-amber-200">{runtimeMissingMessage(attempt)}</p>
                      {/if}
                    </details>

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

                    {#if attempt.deliverables.length > 0}
                      {@const deliverable = latestDeliverable(attempt)}
                      <div class="mt-4 rounded-2xl border border-sky-500/20 bg-sky-500/5 px-4 py-4">
                        <div class="flex flex-wrap items-center justify-between gap-3">
                          <div>
                            <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Deliverable</p>
                            {#if deliverable?.title}
                              <p class="mt-1 text-sm font-medium text-white">{deliverable.title}</p>
                            {/if}
                          </div>
                          <div class="flex flex-wrap gap-2 text-[11px] uppercase tracking-wide text-slate-300">
                            {#each attempt.deliverables as item}
                              <span class={`rounded-full border px-2.5 py-1 ${item.status === 'delivered' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200' : item.status === 'approved' ? 'border-sky-500/30 bg-sky-500/10 text-sky-200' : item.status === 'rejected' ? 'border-sky-500/30 bg-sky-500/10 text-sky-200' : 'border-slate-700 bg-slate-900/80 text-slate-300'}`}>
                                v{item.version} {item.status}
                              </span>
                            {/each}
                          </div>
                        </div>
                        <div class="mt-4 flex flex-wrap gap-2 text-xs text-slate-400">
                          {#if deliverable?.format}
                            <span class="rounded-full border border-slate-700 bg-slate-950/80 px-2.5 py-1">Format {deliverable.format}</span>
                          {/if}
                          {#if deliverable?.content}
                            <span class="rounded-full border border-slate-700 bg-slate-950/80 px-2.5 py-1">{deliverable.content.length.toLocaleString()} chars</span>
                          {/if}
                        </div>
                        <p class="mt-4 text-sm text-slate-400">Deliverable content is hidden here so large outputs do not overwhelm the task view. Open the full output below to inspect it.</p>
                      </div>
                    {/if}

                    {#if activeStepTodos(attempt).length > 0}
                      {@const todos = activeStepTodos(attempt)}
                      <div class="mt-4 rounded-xl border border-slate-800/60 bg-slate-900/40">
                        <div class="border-b border-slate-800/60 px-3 py-1.5 text-xs font-medium text-slate-400">
                          Open todos
                          <span class="text-slate-500"> · {todos.length}</span>
                        </div>
                        <ul class="divide-y divide-slate-800/40">
                          {#each todos as todo}
                            <li class="flex items-center gap-2 px-3 py-1.5 text-sm text-slate-200">
                              <span
                                class={`inline-block h-2 w-2 shrink-0 rounded-full ${todoStatusDot(todo.status)}`}
                                aria-label={todo.status.replace('_', ' ')}
                                title={todo.status.replace('_', ' ')}
                              ></span>
                              <span class="min-w-0 flex-1 truncate">{todo.content}</span>
                              {#if todo.priority !== 'medium'}
                                <span class={`shrink-0 text-xs ${todoPriorityClass(todo.priority)}`}>{todo.priority}</span>
                              {/if}
                            </li>
                          {/each}
                        </ul>
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
                      {:else if outcomeStatus === 'success' && !stepError && !attempt.evaluation}
                        <p class="mt-3 text-sm text-slate-400">No extra completion metadata was recorded for this attempt.</p>
                      {/if}

                      {#if hasRecordedStepOutput(attempt)}
                        <div class="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-800 pt-3">
                          <Button size="sm" variant="secondary" onclick={() => openOutputModal(attempt)}>Show full output</Button>
                          <span class="text-xs text-slate-500">Includes deliverable versions, completion metadata, and any recorded reasoning.</span>
                        </div>
                      {/if}
                    </div>

                    {#if attempt.evaluation}
                      {@const evalDecision = String(attempt.evaluation.decision ?? '')}
                      {@const evalReasoning = String(attempt.evaluation.reasoning ?? '')}
                      {@const evalColor = evalDecision === 'approved' || evalDecision === 'approve' ? 'text-emerald-400' : evalDecision === 'revise' ? 'text-sky-400' : evalDecision === 'failed' || evalDecision === 'reject' ? 'text-rose-400' : 'text-sky-400'}
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
         </Card>

        </div>

       <div class="space-y-4 lg:hidden">
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
                {#if task.created_by_agent_id}
                  <div class="flex justify-between gap-3"><dt class="text-slate-500">Creator agent</dt><dd class="inline-flex items-center gap-2"><AgentAvatar name={agentName(task.created_by_agent_id)} avatarUrl={creatorAgent?.avatar_url ?? null} class="h-5 w-5 rounded-lg" />{agentName(task.created_by_agent_id)}</dd></div>
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
              {#if finalTaskResultTitle(task) || finalTaskResultFormat(task)}
                <div class="mt-4 flex flex-wrap gap-2 text-xs text-slate-400">
                  {#if finalTaskResultTitle(task)}
                    <span class="rounded-full border border-slate-700 bg-slate-950/80 px-2.5 py-1">{finalTaskResultTitle(task)}</span>
                  {/if}
                  {#if finalTaskResultFormat(task)}
                    <span class="rounded-full border border-slate-700 bg-slate-950/80 px-2.5 py-1">Format {finalTaskResultFormat(task)}</span>
                  {/if}
                </div>
              {/if}
              {#if currentWorkflow()?.lifecycle === 'ephemeral'}
                <Button class="mt-4" size="sm" variant="secondary" onclick={() => void promoteWorkflowFromTask()}>Promote workflow</Button>
              {/if}
              {#if hasFinalTaskOutput(task)}
                <p class="mt-4 text-xs text-slate-500">The finalized deliverable stays in the full step output modal to keep this summary lightweight.</p>
              {/if}
            </div>
          </div>
        </details>
      </div>

      <!-- Sidebar -->
      <div class="min-w-0 hidden space-y-5 lg:block">
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
            {#if task.created_by_agent_id}
              <div class="flex justify-between gap-3">
                <dt class="text-slate-500">Creator agent</dt>
                <dd class="inline-flex items-center gap-2 text-slate-300"><AgentAvatar name={agentName(task.created_by_agent_id)} avatarUrl={creatorAgent?.avatar_url ?? null} class="h-5 w-5 rounded-lg" />{agentName(task.created_by_agent_id)}</dd>
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
                  <dd class="font-mono text-sky-300">{stats.evalRevisions}</dd>
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
                  <dd class="font-mono text-sky-300">{stats.totalLoops}</dd>
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
          {#if finalTaskResultTitle(task) || finalTaskResultFormat(task)}
            <div class="mt-4 flex flex-wrap gap-2 text-xs text-slate-400">
              {#if finalTaskResultTitle(task)}
                <span class="rounded-full border border-slate-700 bg-slate-950/80 px-2.5 py-1">{finalTaskResultTitle(task)}</span>
              {/if}
              {#if finalTaskResultFormat(task)}
                <span class="rounded-full border border-slate-700 bg-slate-950/80 px-2.5 py-1">Format {finalTaskResultFormat(task)}</span>
              {/if}
            </div>
          {/if}
          {#if currentWorkflow()?.lifecycle === 'ephemeral'}
            <Button class="mt-4" size="sm" variant="secondary" onclick={() => void promoteWorkflowFromTask()}>Promote workflow</Button>
          {/if}
          {#if hasFinalTaskOutput(task)}
            <p class="mt-4 text-xs text-slate-500">The finalized deliverable stays in the full step output modal to keep this summary lightweight.</p>
          {/if}
        </Card>
      </div>
    </div>
  </section>

  {#if mobileStepDetailOpen && selectedStepGroup}
    <Sheet open={mobileStepDetailOpen} onClose={closeMobileStepDetail} side="bottom" label={`Step detail for ${selectedStepGroup.stepName}`} maxHeight="88dvh">
      {#snippet header()}
        <div class="flex min-w-0 items-start justify-between gap-3">
          <div class="min-w-0 flex-1">
            <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Step detail</p>
            <h2 class="mt-1 break-words text-lg font-semibold text-white">{selectedStepGroup.stepName}</h2>
            <p class="mt-1 text-sm text-slate-400">{selectedStepGroup.stepType === 'gate' ? 'Gate step' : 'Execution step'} with {attemptCountForGroup(selectedStepGroup)} attempt{attemptCountForGroup(selectedStepGroup) === 1 ? '' : 's'}.</p>
          </div>
          <Button class="shrink-0" size="sm" variant="secondary" onclick={closeMobileStepDetail}>Close</Button>
        </div>
      {/snippet}

      {#snippet children()}
        {#if selectedStepGroup.latest}
          {@const attempt = selectedAttempt ?? selectedStepGroup.latest}
          {@const summary = stepOutputSummary(attempt)}
          {@const claims = stepOutputClaims(attempt)}
          {@const visibleStatus = displayStepStatus(attempt)}
          {#if selectedStepGroup.attempts.length > 1}
            <div class="-mx-1 mb-3 flex flex-wrap items-center gap-2 overflow-x-auto px-1 pb-1">
              <span class="text-xs uppercase tracking-[0.2em] text-slate-500">Attempts</span>
              {#each selectedStepGroup.attempts as run (run.step_run_id)}
                {@const isSelected = attempt.step_run_id === run.step_run_id}
                {@const isLatestRun = selectedStepGroup.latest?.step_run_id === run.step_run_id}
                {@const status = displayStepStatus(run)}
                <button
                  type="button"
                  class={`inline-flex shrink-0 items-center gap-2 rounded-full border px-3 py-1.5 text-xs transition ${isSelected ? 'border-sky-400/60 bg-sky-500/10 text-sky-100' : 'border-slate-700 bg-slate-950/70 text-slate-300 hover:border-slate-600 hover:text-white'}`}
                  onclick={() => isLatestRun ? clearAttemptOverride(selectedStepGroup.stepName) : selectAttempt(selectedStepGroup.stepName, run.step_run_id)}
                  aria-pressed={isSelected}
                >
                  <span class="font-mono text-[11px]">{attemptLabel(run)}</span>
                  {#if stepTryLabel(run)}<span class="text-[10px] text-slate-500">{stepTryLabel(run)}</span>{/if}
                  {#if isLatestRun}<span class="rounded-full border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-sky-200">latest</span>{/if}
                  <span class="rounded-full border px-1.5 py-0.5 text-[10px] uppercase tracking-wider {statusColors[status] ?? 'border-slate-600 text-slate-400'}">{status}</span>
                </button>
              {/each}
            </div>
          {/if}
          <div class="rounded-3xl border border-slate-800 bg-slate-900/60 p-4">
            <div class="flex items-center justify-between gap-3">
              <span class="rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wider {statusColors[visibleStatus] ?? 'border-slate-600 text-slate-400'}">{visibleStatus}</span>
              <div class="flex items-center gap-2">
                {#if attempt.output?.session_id || attempt.session_id}
                  <Button size="sm" variant="secondary" disabled={chatBusyKey !== null} onclick={() => openStepChat(attempt)}>
                    {#if chatBusyKey === `step:${attempt.step_run_id}`}
                      <LoaderCircle class="mr-1.5 h-3.5 w-3.5 animate-spin" />
                    {:else}
                      <MessageSquarePlus class="mr-1.5 h-3.5 w-3.5" />
                    {/if}
                    Chat
                  </Button>
                  <Button size="sm" variant="secondary" onclick={() => openSessionLogs(attempt)}>Logs</Button>
                {/if}
              </div>
            </div>
            {#if summary}
              <div class="prose prose-sm prose-invert mt-4 max-w-none text-slate-300">{@html renderMarkdown(summary)}</div>
            {/if}
            {#if stepSpecRows(selectedStepGroup, attempt).length > 0}
              <details class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
                <summary class="flex cursor-pointer list-none flex-wrap items-center justify-between gap-3">
                  <span class="text-xs uppercase tracking-[0.25em] text-slate-500">Current workflow spec</span>
                  <span class="flex flex-wrap items-center gap-2">
                    {#each stepSpecSummaryRows(selectedStepGroup) as row}
                      <span class="rounded-full border border-slate-700 bg-slate-900/70 px-2 py-0.5 text-[11px] text-slate-300">
                        {row.label}: <span class="font-mono text-slate-100">{row.value}</span>
                      </span>
                    {/each}
                    <ChevronDown class="h-4 w-4 shrink-0 text-slate-500" />
                  </span>
                </summary>
                <dl class="mt-3 grid gap-2 text-xs">
                  {#each stepSpecRows(selectedStepGroup, attempt) as row}
                    <div class="min-w-0 rounded-lg border border-slate-800/70 bg-slate-900/40 px-2.5 py-2">
                      <dt class="text-slate-500">{row.label}</dt>
                      <dd class="mt-1 truncate font-mono text-slate-300" title={row.value}>{row.value}</dd>
                    </div>
                  {/each}
                </dl>
              </details>
            {/if}
            <details class="mt-4 rounded-2xl border border-slate-800 bg-slate-950/70 px-4 py-3">
              <summary class="flex cursor-pointer list-none flex-wrap items-center justify-between gap-3">
                <span class="text-xs uppercase tracking-[0.25em] text-slate-500">Runtime and model</span>
                <span class="flex flex-wrap items-center gap-2">
                  {#each runtimeCompactRows(attempt) as row}
                    <span class="rounded-full border border-slate-700 bg-slate-900/70 px-2 py-0.5 text-[11px] text-slate-300">
                      {row.label}: <span class="font-mono text-slate-100">{row.value}</span>
                    </span>
                  {/each}
                  <ChevronDown class="h-4 w-4 shrink-0 text-slate-500" />
                </span>
              </summary>
              {#if runtimeSummaryRows(attempt).length > 0}
                <dl class="mt-3 grid gap-2 text-xs">
                  {#each runtimeSummaryRows(attempt) as row}
                    <div class="min-w-0 rounded-lg border border-slate-800/70 bg-slate-900/40 px-2.5 py-2">
                      <dt class="text-slate-500">{row.label}</dt>
                      <dd class="mt-1 truncate font-mono text-slate-300" title={row.value}>{row.value}</dd>
                    </div>
                  {/each}
                </dl>
                {#if runtimeDebugRows(attempt).length > 0}
                  <details class="mt-3 rounded-xl border border-slate-800/70 bg-slate-900/30 px-3 py-2">
                    <summary class="flex cursor-pointer list-none items-center justify-between gap-3 text-xs font-medium text-slate-300">
                      Debug runtime details
                      <ChevronDown class="h-4 w-4 shrink-0 text-slate-500" />
                    </summary>
                    <dl class="mt-3 grid gap-2 text-xs">
                      {#each runtimeDebugRows(attempt) as row}
                        <div class="min-w-0 rounded-lg border border-slate-800/70 bg-slate-950/50 px-2.5 py-2">
                          <dt class="text-slate-500">{row.label}</dt>
                          <dd class="mt-1 truncate font-mono text-slate-300" title={row.value}>{row.value}</dd>
                        </div>
                      {/each}
                    </dl>
                  </details>
                {/if}
              {:else}
                <p class="mt-3 text-sm text-amber-200">{runtimeMissingMessage(attempt)}</p>
              {/if}
            </details>
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
              {#if hasRecordedStepOutput(attempt)}
                <Button class="mt-4" size="sm" variant="secondary" onclick={() => openOutputModal(attempt)}>Show full output</Button>
              {/if}
            </div>
          </div>
        {:else}
          <div class="rounded-3xl border border-dashed border-slate-700 px-4 py-8 text-center text-sm text-slate-400">This step has not produced an attempt yet.</div>
        {/if}
      {/snippet}
    </Sheet>
  {/if}

  {#if configModalOpen}
    <BlockingDialog open={configModalOpen} onClose={closeConfigModal} label="Task configuration" titleId="task-config-title" panelClass="max-w-4xl">
      {#snippet header()}
        <div class="flex items-start justify-between gap-4">
          <div>
            <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Task configuration</p>
            <h2 class="mt-1 text-xl font-semibold text-white" id="task-config-title">{task?.title ?? 'Task configuration'}</h2>
            <p class="mt-2 text-sm text-slate-400">Configuration is secondary to live execution, so edits live here while the main page stays focused on workflow progress.</p>
          </div>
          <Button size="sm" variant="secondary" onclick={closeConfigModal}>Close</Button>
        </div>
      {/snippet}

      {#snippet children()}
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Title</span>
            <Input bind:value={editForm.title} disabled={!isEditable} />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Priority</span>
            <Input bind:value={editForm.priority} type="number" disabled={!isEditable} />
          </label>
          <div class="space-y-2 text-sm font-medium text-slate-200">
            <span>Agent</span>
            <AgentSelect
              agents={agents.filter((a) => a.agent_type === 'primary')}
              value={editForm.agent_id}
              onchange={(next) => { editForm.agent_id = next; }}
              disabled={!isEditable}
            />
          </div>
          <AgentProfileSelect
            agents={agents.filter((a) => a.agent_type === 'primary')}
            agentId={editForm.agent_id}
            bind:value={editForm.agent_profile_id}
            disabled={!isEditable}
          />
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Workflow</span>
            <select bind:value={editForm.workflow_id} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
              <option value="">Auto{editForm.project_id ? ' (project-aware)' : ''}</option>
              {#each (projectWorkflowOptionsLoaded ? projectWorkflowOptions : workflows) as workflow}
                <option value={workflow.workflow_id}>{workflow.name}</option>
              {/each}
            </select>
            {#if editForm.project_id && projectWorkflowOptionsLoaded && projectWorkflowOptions.length === 0}
              <span class="block text-xs text-slate-500">No workflows are bound to this project. Pick "Auto" or bind a workflow on the project page.</span>
            {/if}
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Project</span>
            <select bind:value={editForm.project_id} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
              <option value="">None</option>
              {#each projects as project}
                <option value={project.project_id}>{project.name}</option>
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
              {#if (task?.source_type === 'chat' || task?.source_type === 'agent') && task.source_ref}
                <option value="same_conversation">Same conversation</option>
              {/if}
              <option value="preferred_channel">Preferred channel</option>
              <option value="specific_conversation">Specific conversation</option>
              <option value="latest_active_for_agent">Latest active</option>
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

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Interaction policy</span>
          <select bind:value={editForm.interaction_mode_override} disabled={!isEditable} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100 disabled:opacity-50">
            <option value="">Workflow default</option>
            <option value="step_requests">Allow planning questions</option>
            <option value="explicit_gates">Gates only</option>
            <option value="none">Fully autonomous</option>
          </select>
          <span class="block text-xs text-slate-500">Fully autonomous disables dynamic clarification questions for this task.</span>
        </label>

        <div class="mt-4">
          <SessionPolicyEditor
            bind:allowText={editForm.allow_policy_text}
            bind:denyText={editForm.deny_policy_text}
            disabled={!isEditable}
            title="Intaris session policies"
          />
        </div>

        <div class="mt-6 rounded-3xl border border-slate-800 bg-slate-950/40 p-4">
          <div class="flex items-center justify-between gap-3">
            <div>
              <p class="text-xs uppercase tracking-[0.25em] text-slate-500">Dependencies</p>
              <p class="mt-1 text-sm text-slate-400">Only direct dependencies are shown in the live workflow. Manage them here.</p>
            </div>
          </div>
          <div class="mt-4 space-y-3">
            {#each task?.dependencies ?? [] as dependency}
              <div class="flex items-center justify-between gap-3 rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3">
                <button class="min-w-0 break-words text-left text-sm text-slate-200 hover:text-white" onclick={() => goto(`/tasks/${dependency.depends_on}`)} type="button">{allTasks.find((candidate) => candidate.task_id === dependency.depends_on)?.title ?? dependency.depends_on}</button>
                {#if isEditable}
                  <Button size="sm" variant="danger" onclick={() => removeDependency(dependency.depends_on)}>Remove</Button>
                {/if}
              </div>
            {/each}
            {#if (task?.dependencies?.length ?? 0) === 0}
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

      {/snippet}

      {#snippet footer()}
        <div class="flex flex-col-reverse justify-end gap-3 sm:flex-row">
          <Button variant="secondary" onclick={closeConfigModal}>Close</Button>
          <Button disabled={saving || !isEditable} onclick={async () => { if (await saveTask()) closeConfigModal(); }}>{saving ? 'Saving...' : 'Save task'}</Button>
        </div>
      {/snippet}
    </BlockingDialog>
  {/if}

  <Sheet open={taskActionsOpen} onClose={() => (taskActionsOpen = false)} side="bottom" label="Task actions">
    {#snippet children()}
      <div class="space-y-2">
        <Button class="w-full justify-center" variant="secondary" onclick={() => { taskActionsOpen = false; configModalOpen = true; }} disabled={!isEditable}>
          Configure task
        </Button>
        {#if isRerunnable}
          <Button class="w-full justify-center" onclick={() => { taskActionsOpen = false; void rerunTask(); }} disabled={rerunBusy}>
            Re-run task
          </Button>
        {/if}
        {#if isCancellable}
          <Button class="w-full justify-center" variant="danger" onclick={() => { taskActionsOpen = false; void cancelTask(); }}>
            Cancel task
          </Button>
        {/if}
      </div>
    {/snippet}
  </Sheet>

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
    {#key `${sessionDrawer.conversationId}:${sessionDrawer.sessionId}`}
      <SessionLogsDrawer
         conversationId={sessionDrawer.conversationId}
         sessionId={sessionDrawer.sessionId}
         stepRunId={sessionDrawer.stepRunId}
         taskId={task?.task_id ?? null}
        stepRun={sessionDrawerStepRun}
        stepName={sessionDrawer.stepName}
        agent={sessionDrawer.agent}
        backLabel={sessionDrawerBackStack[sessionDrawerBackStack.length - 1]?.stepName ?? 'Parent session'}
        onBack={sessionDrawerBackStack.length > 0 ? goBackSessionLogs : undefined}
        onViewSession={openSessionLogsById}
        onclose={closeSessionLogs}
      />
    {/key}
  {/if}
{:else}
  <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error || 'Task not found.'}</p>
{/if}
