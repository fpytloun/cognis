import { asApiError } from '$lib/api/client';
import type { Agent, Conversation, Task, TaskDetail, Workflow } from '$lib/types/api';

type TaskDetailApi = {
  tasks: {
    detail(taskId: string): Promise<TaskDetail>;
    listAll(): Promise<Task[]>;
  };
  agents: {
    listAll(): Promise<Agent[]>;
  };
  workflows: {
    listAll(): Promise<Workflow[]>;
  };
  conversations: {
    listAll(): Promise<Conversation[]>;
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
  const task = await api.tasks.detail(taskId);
  const [agentsResult, workflowsResult, conversationsResult, tasksResult] = await Promise.allSettled([
    api.agents.listAll(),
    api.workflows.listAll(),
    api.conversations.listAll(),
    api.tasks.listAll()
  ]);

  return {
    task,
    agents: agentsResult.status === 'fulfilled' ? agentsResult.value : [],
    workflows: workflowsResult.status === 'fulfilled' ? workflowsResult.value : [],
    conversations: conversationsResult.status === 'fulfilled' ? conversationsResult.value : [],
    allTasks: tasksResult.status === 'fulfilled' ? tasksResult.value : [],
    auxiliaryError:
      firstAuxiliaryError(agentsResult, workflowsResult, conversationsResult, tasksResult) ?? ''
  };
}

export async function refreshTaskPageData(
  api: TaskDetailApi,
  taskId: string,
  currentTasks: Task[]
): Promise<TaskPageRefresh> {
  const task = await api.tasks.detail(taskId);

  try {
    const allTasks = await api.tasks.listAll();
    return { task, allTasks, auxiliaryError: '' };
  } catch (error) {
    return {
      task,
      allTasks: currentTasks,
      auxiliaryError: asApiError(error).message
    };
  }
}

export function shouldClearTaskFromError(error: unknown): boolean {
  return asApiError(error).status === 404;
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
