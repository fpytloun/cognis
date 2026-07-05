import { asApiError } from '$lib/api/client';
import type { Agent, Conversation, CursorPage, StepRun, Task, TaskDetail, Workflow } from '$lib/types/api';

const RERUNNABLE_TASK_STATUSES = new Set(['paused', 'completed', 'failed', 'cancelled']);

type TaskDetailApi = {
  tasks: {
    detail(taskId: string): Promise<TaskDetail>;
    summary(taskId: string): Promise<TaskDetail>;
    stepSummaries(taskId: string): Promise<CursorPage<StepRun>>;
    list(params?: { limit?: number }): Promise<CursorPage<Task>>;
  };
  agents: {
    listAll(): Promise<Agent[]>;
  };
  workflows: {
    listAll(): Promise<Workflow[]>;
    detail(workflowId: string): Promise<Workflow>;
  };
  conversations: {
    list(
      cursor?: string | null,
      filters?: {
        contextType?: string | null;
        agentId?: string | null;
        status?: string | null;
        includeAgentDirect?: boolean | null;
      }
    ): Promise<CursorPage<Conversation>>;
    detail(conversationId: string): Promise<Conversation>;
  };
};

export type TaskPageData = {
  task: TaskDetail;
  agents: Agent[];
  workflows: Workflow[];
  conversations: Conversation[];
  allTasks: Task[];
  auxiliaryError: string;
};

export type TaskPageRefresh = {
  task: TaskDetail;
  allTasks: Task[];
  auxiliaryError: string;
};

export async function loadTaskPageData(api: TaskDetailApi, taskId: string): Promise<TaskPageData> {
  const summary = await api.tasks.summary(taskId);
  const stepRunsResult = await Promise.allSettled([api.tasks.stepSummaries(taskId)]);
  const task = {
    ...summary,
    step_runs:
      stepRunsResult[0].status === 'fulfilled' ? stepRunsResult[0].value.items : summary.step_runs
  };
  const sourceConversationPromise =
    task.source_ref && (task.source_type === 'chat' || task.source_type === 'agent')
      ? api.conversations.detail(task.source_ref)
      : Promise.resolve(null);
  const [agentsResult, workflowsResult, conversationsResult, sourceConversationResult] =
    await Promise.allSettled([
      api.agents.listAll(),
      api.workflows.listAll(),
      api.conversations.list(null, { status: 'all', includeAgentDirect: true }),
      sourceConversationPromise
    ]);
  const dependencyCandidatesResult = await Promise.allSettled([api.tasks.list({ limit: 100 })]);

  let workflows = workflowsResult.status === 'fulfilled' ? workflowsResult.value : [];
  if (task.workflow_id && !workflows.some((workflow) => workflow.workflow_id === task.workflow_id)) {
    try {
      workflows = [...workflows, await api.workflows.detail(task.workflow_id)];
    } catch {
      // Leave the auxiliary workflow list best-effort; task detail already loaded.
    }
  }

  return {
    task,
    agents: agentsResult.status === 'fulfilled' ? agentsResult.value : [],
    workflows,
    conversations: mergeConversationChoices(
      conversationsResult.status === 'fulfilled' ? conversationsResult.value.items : [],
      sourceConversationResult.status === 'fulfilled' ? sourceConversationResult.value : null
    ),
    allTasks:
      dependencyCandidatesResult[0].status === 'fulfilled'
        ? dependencyCandidatesResult[0].value.items
        : [],
    auxiliaryError:
      firstAuxiliaryError(
        agentsResult,
        workflowsResult,
        conversationsResult,
        sourceConversationResult,
        dependencyCandidatesResult[0],
        stepRunsResult[0]
      ) ?? ''
  };
}

function mergeConversationChoices(
  conversations: Conversation[],
  sourceConversation: Conversation | null
): Conversation[] {
  if (!sourceConversation) return conversations;
  if (
    conversations.some(
      (conversation) => conversation.conversation_id === sourceConversation.conversation_id
    )
  ) {
    return conversations;
  }
  return [sourceConversation, ...conversations];
}

export async function refreshTaskPageData(
  api: TaskDetailApi,
  taskId: string,
  currentTasks: Task[]
): Promise<TaskPageRefresh> {
  const summary = await api.tasks.summary(taskId);
  const stepRuns = await api.tasks.stepSummaries(taskId);
  const task = { ...summary, step_runs: stepRuns.items };
  return { task, allTasks: currentTasks, auxiliaryError: '' };
}

export function shouldClearTaskFromError(error: unknown): boolean {
  return asApiError(error).status === 404;
}

export function isTaskRerunnable(task: Pick<Task, 'status'> | null | undefined): boolean {
  if (!task) return false;
  return RERUNNABLE_TASK_STATUSES.has(task.status);
}

function firstAuxiliaryError(
  ...results: Array<PromiseSettledResult<unknown>>
): string | null {
  for (const result of results) {
    if (result.status === 'rejected') {
      return asApiError(result.reason).message;
    }
  }
  return null;
}
