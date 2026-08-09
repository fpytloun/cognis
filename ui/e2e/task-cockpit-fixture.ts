import type { Page, Request } from '@playwright/test';

export type CockpitStatus = 'draft' | 'running' | 'paused' | 'completed';

const NOW = '2026-07-30T12:00:00Z';
export const TASK_ID = 'task-stage39-cockpit';
export const HEAVY_STEP_RUN_ID = 'run-fetch-2';
const RUNNING_FETCH_STEP_RUN_ID = 'run-fetch-1';

function projectionSteps(status: CockpitStatus): Record<string, unknown>[] {
  const draft = status === 'draft';
  const running = status === 'running';
  const paused = status === 'paused';
  const completed = status === 'completed';
  return [
    step('scope', 'run', draft ? 'pending' : 'completed', {
      summary: draft ? null : 'Objective and constraints established.'
    }),
    step('fetch', 'tool_call', running ? 'running' : draft ? 'pending' : 'completed', {
      attempt_count: draft ? 0 : running ? 1 : 2,
      duration_seconds: draft ? null : running ? 1.7 : 3.4,
      has_output: paused || completed,
      has_logs: paused || completed,
      step_run_id: draft ? null : running ? RUNNING_FETCH_STEP_RUN_ID : HEAVY_STEP_RUN_ID,
      metadata: { execution_kind: 'deterministic', tool_name: 'builtin:web_fetch' }
    }),
    step('route', 'condition', paused || completed ? 'completed' : 'pending', {
      summary: paused || completed ? 'Matched the review branch.' : null,
      metadata: { execution_kind: 'deterministic', deterministic_state: paused || completed ? 'true_branch' : 'pending' }
    }),
    step('no_changes', 'complete', paused || completed ? 'skipped' : 'pending', {
      skip_reason: paused || completed ? 'condition:route:false' : null,
      metadata: { execution_kind: 'deterministic' }
    }),
    step('approve', 'gate', paused ? 'waiting' : completed ? 'completed' : 'pending', {
      action_required: paused,
      pause_type: paused ? 'gate' : null,
      summary: paused ? 'Human approval is required.' : completed ? 'Release approved.' : null
    }),
    step('finish', 'complete', completed ? 'completed' : 'pending', {
      summary: completed ? 'Release approved.' : null,
      metadata: { execution_kind: 'deterministic' }
    })
  ];
}

function step(
  name: string,
  type: string,
  status: string,
  overrides: Record<string, unknown> = {}
): Record<string, unknown> {
  return {
    name,
    type,
    status,
    attempt_count: status === 'pending' ? 0 : 1,
    max_attempts: 3,
    started_at: status === 'pending' ? null : NOW,
    completed_at: status === 'completed' || status === 'skipped' ? NOW : null,
    duration_seconds: status === 'pending' ? null : 1.2,
    action_required: false,
    pause_type: null,
    summary: null,
    error: null,
    has_output: false,
    has_logs: false,
    has_deliverable: false,
    skip_reason: null,
    step_run_id: null,
    output_url: null,
    logs_url: null,
    deliverables_url: null,
    metadata: {},
    ...overrides
  };
}

function projectedRun(
  name: string,
  type: string,
  status: string,
  id: string,
  attempt: number
): Record<string, unknown> {
  return {
    step_run_id: id,
    task_id: TASK_ID,
    step_name: name,
    step_type: type,
    status,
    attempt,
    attempt_number: 1,
    superseded_by_step_run_id: null,
    agent_id: 'agent-stage39',
    workspace_root: null,
    working_directory: null,
    conversation_id: name === 'fetch' ? 'conv-fetch' : null,
    session_id: name === 'fetch' ? 'sess-fetch' : null,
    intaris_session_id: null,
    deliverable_id: null,
    require_deliverable: false,
    output: null,
    evaluation: null,
    runtime_info: null,
    deliverables: [],
    todos: [],
    started_at: NOW,
    completed_at: status === 'completed' ? NOW : null,
    updated_at: NOW,
    duration_seconds: 1.2,
    accumulated_duration_seconds: 1.2,
    latest_attempt_duration_seconds: 1.2,
    is_projection: true
  };
}

function projectedRuns(status: CockpitStatus): Record<string, unknown>[] {
  return projectionSteps(status)
    .filter((item) => item.status !== 'pending' && item.status !== 'skipped')
    .map((item) => projectedRun(
      String(item.name),
      String(item.type),
      item.status === 'waiting' ? 'paused' : String(item.status),
      String(item.step_run_id ?? `run-${String(item.name)}-1`),
      Number(item.attempt_count)
    ));
}

function detailedRun(run: Record<string, unknown>): Record<string, unknown> {
  const terminalOutput = run.step_run_id === HEAVY_STEP_RUN_ID
    ? {
        summary: 'Fetched release evidence',
        content: 'All required checks passed. Artifact digest: sha256:verified-stage39.',
        outputs: { checks: 12, failures: 0 }
      }
    : null;
  return {
    ...run,
    is_projection: false,
    output: terminalOutput,
    runtime_info: {
      execution_kind: run.step_type === 'run' || run.step_type === 'gate' ? 'agent' : 'deterministic',
      deterministic_state: run.status
    }
  };
}

function task(status: CockpitStatus): Record<string, unknown> {
  const draft = status === 'draft';
  const paused = status === 'paused';
  const running = status === 'running';
  const terminal = status === 'completed';
  const currentStep = paused ? 'approve' : running ? 'fetch' : terminal ? null : 'scope';
  const steps = projectionSteps(status);
  return {
    task_id: TASK_ID,
    title: 'Release safety review',
    description: 'Verify the deterministic release workflow and approve the production handoff.',
    expected_output: 'An evidence-backed go/no-go decision with complete audit context.',
    status,
    priority: 8,
    created_by: 'admin@cognis-e2e.localdev.me',
    agent_id: 'agent-stage39',
    agent_profile_id: null,
    created_by_agent_id: null,
    source_type: 'manual',
    source_ref: null,
    delivery: { mode: 'latest_active_for_agent', target: null },
    completion_mode_family: 'direct',
    allow_silent_completion: true,
    interaction_mode_override: 'explicit_gates',
    session_policy: { allow_policies: [], deny_policies: [] },
    workflow_id: 'wf-stage39',
    project_id: null,
    attempt_number: 1,
    workspace_root: null,
    working_directory: null,
    workflow_state: {
      current_step_index: currentStep === 'approve' ? 4 : running ? 1 : terminal ? 6 : 0,
      step_outputs: {},
      loop_iterations: { deterministic_jumps: 1 },
      status,
      skipped_steps: paused || terminal ? ['no_changes'] : []
    },
    queue_name: 'default',
    scheduled_for: null,
    created_at: NOW,
    started_at: draft ? null : NOW,
    completed_at: terminal ? NOW : null,
    updated_at: NOW,
    result_summary: terminal ? 'Release approved.' : null,
    result_data: terminal ? { final_deliverable_id: 'dlv-stage39-final' } : null,
    applied_completion_mode: null,
    applied_completion_reason: null,
    dependencies: [],
    step_runs: [],
    workflow_run: {
      task_id: TASK_ID,
      workflow_id: 'wf-stage39',
      project_id: null,
      attempt_number: 1,
      workflow_state: null,
      current_step_name: currentStep,
      pending_pause: paused ? pendingPause() : null
    },
    pending_pause: paused ? pendingPause() : null,
    workflow_projection: {
      workflow_id: 'wf-stage39',
      workflow_version: 7,
      workflow_digest: 'sha256:stage39-cockpit-fixture',
      current_phase_id: paused ? 'review' : running ? 'investigate' : terminal ? null : 'prepare',
      current_step_name: currentStep,
      phases: [
        { id: 'prepare', title: 'Prepare', description: 'Frame the release decision.', status: draft ? 'pending' : 'completed', steps: [steps[0]] },
        { id: 'investigate', title: 'Investigate', description: 'Fetch evidence and route deterministically.', status: paused ? 'completed' : running ? 'active' : terminal ? 'completed' : 'pending', steps: steps.slice(1, 4) },
        { id: 'review', title: 'Review', description: 'Human approval and terminal completion.', status: paused ? 'waiting' : terminal ? 'completed' : 'pending', steps: steps.slice(4) }
      ]
    },
    progress: {
      todos: [
        { content: 'Collect deterministic evidence', status: terminal ? 'completed' : 'in_progress' },
        { content: 'Obtain human approval', status: terminal ? 'completed' : 'pending' }
      ],
      work_items: [{
        kind: 'managed_conversation',
        work_id: 'managed-stage39-review',
        step_name: 'fetch',
        step_run_id: HEAVY_STEP_RUN_ID,
        title: 'Independent evidence review',
        agent_id: 'agent-reviewer',
        status: terminal ? 'completed' : 'running',
        result_summary: terminal ? 'Evidence verified.' : null,
        error: null,
        todos: [{ content: 'Verify artifact digest', status: terminal ? 'completed' : 'in_progress' }],
        conversation_id: 'conv-managed-review',
        session_id: 'sess-managed-review',
        started_at: NOW,
        updated_at: NOW
      }],
      active_count: terminal ? 0 : 1,
      completed_count: terminal ? 1 : 0,
      truncated: false
    }
  };
}

function taskWork(stepRunId: string, status: CockpitStatus): Record<string, unknown> {
  const completed = status === 'completed';
  const rootWorkstream = {
    key: 'session:root-architect',
    kind: 'root',
    root_key: 'session:root-architect',
    edge_kind: 'root',
    ordinal: 0,
    conversation_id: 'conv-task-chat',
    session_id: 'root-architect',
    event_store_session_id: 'store-root-architect',
    title: 'Release architecture',
    agent_id: 'architect',
    status: completed ? 'completed' : 'running',
    current: !completed,
    superseded: false
  };
  const childWorkstream = {
    ...rootWorkstream,
    key: 'session:child-implementation',
    kind: 'delegate',
    parent_key: rootWorkstream.key,
    edge_kind: 'delegate',
    ordinal: 1,
    session_id: 'child-implementation',
    event_store_session_id: 'store-child-implementation',
    title: 'Release implementation',
    agent_id: 'worker',
    status: completed ? 'completed' : 'running',
    current: !completed
  };
  const workstreams = completed
    ? [
        rootWorkstream,
        childWorkstream,
        ...Array.from({ length: 126 }, (_, index) => ({
          ...childWorkstream,
          key: `session:support-${index}`,
          ordinal: index + 2,
          session_id: `support-${index}`,
          event_store_session_id: `store-support-${index}`,
          title: `Supporting stream ${index}`
        }))
      ]
    : [rootWorkstream, childWorkstream];
  return {
    schema_version: 2,
    projection_version: 'stage39-task-work',
    scope: { key: `task_step:${stepRunId}`, kind: 'task_step', step_run_id: stepRunId, conversation_id: 'conv-fetch', session_id: 'sess-fetch' },
    mutations: [
      {
        id: `mutation-${stepRunId}`,
        call_id: `call-write-${stepRunId}`,
        sort_key: '1',
        tool_name: 'apply_patch',
        display_name: 'Apply release manifest patch',
        category: 'filesystem',
        operation_kind: 'file_write',
        status: 'complete',
        arguments: {},
        paths: ['cognis/release/manifest.yaml'],
        file_diffs: [{ path: 'cognis/release/manifest.yaml', diff: '@@ -1 +1 @@\n-candidate\n+approved' }],
        diffs_truncated: false,
        source_workstream: childWorkstream
      },
      {
        id: `mutation-record-${stepRunId}`,
        call_id: `call-record-${stepRunId}`,
        sort_key: '3',
        tool_name: 'manage_agents',
        display_name: 'Update release agent',
        category: 'agent_management',
        operation_kind: 'update',
        status: 'complete',
        arguments: {},
        paths: [],
        file_diffs: [],
        diffs_truncated: false,
        source_workstream: rootWorkstream
      }
    ],
    commands: [{
      id: `command-${stepRunId}`,
      call_id: `call-test-${stepRunId}`,
      sort_key: '2',
      tool_name: 'bash',
      arguments: { command: 'uv run pytest tests/release -q', workdir: 'cognis' },
      command: 'uv run pytest tests/release -q',
      workdir: '/workspace',
      status: 'complete',
      preview: '12 passed',
      preview_truncated: false,
      has_full_output: true,
      duration_ms: 842,
      source_workstream: childWorkstream
    }],
    artifacts: [{ artifact_id: 'artifact-stage39', filename: 'release-report.txt', mime_type: 'text/plain', size_bytes: 128, source_workstream: childWorkstream }],
    final_deliverable: completed ? {
      deliverable_id: stepRunId === HEAVY_STEP_RUN_ID ? 'dlv-stage39-final' : `dlv-intermediate-${stepRunId}`,
      format: 'markdown',
      title: stepRunId === HEAVY_STEP_RUN_ID ? 'Release decision' : 'Intermediate evidence',
      content: stepRunId === HEAVY_STEP_RUN_ID
        ? '# Release approved\n\nAll deterministic checks passed. The release is approved.'
        : '# Intermediate evidence\n\nThis is not the canonical task result.',
      render_metadata: {},
      export_metadata: {},
      source_workstream: rootWorkstream
    } : null,
    deliverables: completed ? [{
      deliverable_id: stepRunId === HEAVY_STEP_RUN_ID ? 'dlv-stage39-final' : `dlv-intermediate-${stepRunId}`,
      format: 'markdown',
      title: stepRunId === HEAVY_STEP_RUN_ID ? 'Release decision' : 'Intermediate evidence',
      content: stepRunId === HEAVY_STEP_RUN_ID
        ? '# Release approved\n\nAll deterministic checks passed. The release is approved.'
        : '# Intermediate evidence\n\nThis is not the canonical task result.',
      render_metadata: {},
      export_metadata: {},
      source_workstream: rootWorkstream
    }, {
      deliverable_id: 'dlv-child-supporting',
      format: 'markdown',
      title: 'Implementation evidence',
      content: '# Supporting implementation\n\nChild checks passed.',
      render_metadata: {},
      export_metadata: {},
      source_workstream: childWorkstream
    }] : [],
    workstreams,
    graph_fingerprint: completed ? 'graph-128' : 'graph-running',
    graph_truncated: completed,
    summary: { mutations: 1, commands: 1, changed_files: 1, artifacts: 1, deliverables: completed ? 2 : 0, additions: 1, deletions: 1 },
    has_more_before: false,
    before_cursor: null,
    server_time: NOW
  };
}

function pendingPause(): Record<string, unknown> {
  return {
    pause_id: 'pause-approve',
    pause_type: 'gate',
    step_name: 'approve',
    question: 'Approve this release for production?',
    questions: [],
    options: [
      { label: 'Approve', action: 'approve' },
      { label: 'Reject', action: 'reject' }
    ],
    context: {},
    created_at: NOW
  };
}

const workflow = {
  workflow_id: 'wf-stage39',
  name: 'Release review',
  description: 'Stage 39 cockpit fixture',
  version: 7,
  criteria: '',
  tags: ['e2e'],
  interaction: { mode: 'explicit_gates' },
  defaults: {},
  steps: projectionSteps('paused').map((item) => ({ name: item.name, type: item.type })),
  presentation: {
    phases: [
      { id: 'prepare', title: 'Prepare', description: 'Frame the release decision.', step_names: ['scope'] },
      { id: 'investigate', title: 'Investigate', description: 'Fetch evidence and route deterministically.', step_names: ['fetch', 'route', 'no_changes'] },
      { id: 'review', title: 'Review', description: 'Human approval and terminal completion.', step_names: ['approve', 'finish'] }
    ]
  },
  is_system: false,
  owner_email: 'admin@cognis-e2e.localdev.me',
  lifecycle: 'persistent',
  archived_at: null,
  lineage: null,
  editable_fields: [],
  has_overrides: false,
  disabled: false,
  disableable: false,
  override_warnings: []
};

const taskChatConversation = {
  conversation_id: 'conv-task-chat',
  user_email: 'admin@cognis-e2e.localdev.me',
  agent_id: 'agent-stage39',
  agent_profile_id: null,
  project_id: null,
  title: 'Release safety review',
  title_source: 'task',
  context: {
    type: 'web',
    ref: TASK_ID,
    platform_data: { kind: 'task_control', task_id: TASK_ID },
    memory_labels: {}
  },
  active_session_id: 'sess-task-chat',
  active_executor_id: null,
  active_executor_assigned_at: null,
  active_executor_expires_at: null,
  active_executor_source: null,
  active_session_status: 'active',
  active_session_completion_reason: null,
  active_turn_chat_mode: null,
  active_turn_chat_mode_source: null,
  pending_notification_types: [],
  starred_at: null,
  status: 'active',
  last_message_at: NOW,
  last_read_at: NOW,
  has_unread: false,
  has_active_turn: false,
  managed_agent: null,
  created_at: NOW,
  updated_at: NOW,
  conversation_state: null
};

const taskChatConversationB = {
  ...taskChatConversation,
  conversation_id: 'conv-task-chat-b',
  title: 'Release safety review B',
  active_session_id: 'sess-task-chat-b',
  context: {
    ...taskChatConversation.context,
    ref: 'task-stage39-release-b',
    platform_data: { kind: 'task_control', task_id: 'task-stage39-release-b' }
  }
};

const taskChatSession = {
  session_id: 'sess-task-chat',
  conversation_id: 'conv-task-chat',
  parent_session_id: null,
  previous_session_id: null,
  user_email: 'admin@cognis-e2e.localdev.me',
  agent_id: 'agent-stage39',
  agent_profile_id: null,
  delegation_mode: null,
  delegation_task: null,
  status: 'active',
  intaris_session_id: 'intaris-task-chat',
  mnemory_session_id: null,
  started_at: NOW,
  idle_since: NOW,
  completed_at: null,
  completion_reason: null,
  result_summary: null,
  result_content: null,
  result_anchors: null,
  result_sections: null,
  updated_at: NOW
};

const taskChatSessionB = {
  ...taskChatSession,
  session_id: 'sess-task-chat-b',
  conversation_id: 'conv-task-chat-b',
  intaris_session_id: 'intaris-task-chat-b'
};

const taskChatTimeline = [
  {
    id: 'message:task-control-user',
    kind: 'message',
    sort_key: '0000:000000000000001:000000:02:000000000',
    source_refs: [{ store: 'intaris', session_id: 'sess-task-chat', seq: 1, event_type: 'user_message' }],
    stable: true,
    role: 'user',
    content: 'What is blocking this release?',
    message_id: 'msg-task-control-user',
    attachments: [],
    partial: false
  },
  {
    id: 'message:task-control-assistant',
    kind: 'message',
    sort_key: '0000:000000000000002:000000:02:000000000',
    source_refs: [{ store: 'intaris', session_id: 'sess-task-chat', seq: 2, event_type: 'assistant_message' }],
    stable: true,
    role: 'assistant',
    content: 'The release is waiting for your approval. Deterministic evidence passed, and no implementation error remains.',
    message_id: 'msg-task-control-assistant',
    attachments: [],
    partial: false
  }
];

export interface CockpitFixture {
  setStatus(status: CockpitStatus): void;
  heavyRequests(): number;
  actionRequests(): string[];
  navigationRequests(): string[];
  detailResponses(): Array<Record<string, unknown>>;
  unmockedRequests(): string[];
}

export async function installTaskCockpitFixture(page: Page): Promise<CockpitFixture> {
  let status: CockpitStatus = 'paused';
  let heavyRequestCount = 0;
  const actionRequests: string[] = [];
  const navigationRequests: string[] = [];
  const detailResponses: Array<Record<string, unknown>> = [];
  const unmockedRequests: string[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const json = (body: unknown) => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body)
    });

    if (path === '/api/v1/system/diagnostics') {
      return json({
        readiness: {
          mnemory_reachable: true,
          intaris_reachable: true,
          llm_provider_configured: true,
          executor_tools_configured: true,
          agent_created: true,
          chat_ready: true
        },
        ui: {},
        database: {},
        config: {}
      });
    }
    if (path === `/api/v1/tasks/${TASK_ID}/summary`) return json(task(status));
    if (path === '/api/v1/deliverables/dlv-stage39-final') {
      return json({
        deliverable_id: 'dlv-stage39-final',
        step_run_id: HEAVY_STEP_RUN_ID,
        conversation_id: 'conv-fetch',
        session_id: 'sess-fetch',
        turn_id: 'turn-final',
        version: 1,
        attempt_number: 1,
        content: '# Release approved\n\nAll deterministic checks passed. The release is approved.',
        format: 'markdown',
        title: 'Release decision',
        target: 'none',
        outputs: {},
        rich_payload: null,
        validation_warnings: [],
        render_metadata: {},
        export_metadata: {},
        status: 'approved',
        evaluator_feedback: null,
        created_at: NOW,
        updated_at: NOW
      });
    }
    if (path === '/api/v1/deliverables/dlv-child-supporting') {
      return json({
        deliverable_id: 'dlv-child-supporting',
        step_run_id: HEAVY_STEP_RUN_ID,
        conversation_id: 'conv-task-chat',
        session_id: 'child-implementation',
        turn_id: 'turn-child',
        version: 1,
        attempt_number: 1,
        content: '# Supporting implementation\n\nChild checks passed.',
        format: 'markdown',
        title: 'Implementation evidence',
        target: 'none',
        outputs: {},
        rich_payload: null,
        validation_warnings: [],
        render_metadata: {},
        export_metadata: {},
        status: 'approved',
        evaluator_feedback: null,
        created_at: NOW,
        updated_at: NOW
      });
    }
    if (path.match(/^\/api\/v1\/chat\/v2\/task-steps\/[^/]+\/work$/)) {
      const stepRunId = path.split('/')[6] ?? HEAVY_STEP_RUN_ID;
      return json(taskWork(stepRunId, status));
    }
    if (path === '/api/v1/chat/v2/conversations/conv-task-chat/work') {
      const projection = taskWork(HEAVY_STEP_RUN_ID, 'completed');
      if (url.searchParams.get('before') === 'work-older-1') {
        const commands = Array.from({ length: 100 }, (_, index) => ({
          id: `older-command-${index}`,
          call_id: `older-call-${index}`,
          sort_key: String(index).padStart(6, '0'),
          tool_name: 'bash',
          arguments: { command: `older command ${index}`, workdir: 'cognis' },
          command: `older command ${index}`,
          workdir: 'cognis',
          status: 'complete',
          preview: `older output ${index}`,
          preview_truncated: false,
          has_full_output: false,
        }));
        return json({
          ...projection,
          mutations: [],
          commands,
          artifacts: [],
          deliverables: [],
          final_deliverable: null,
          summary: { mutations: 0, commands: 100, changed_files: 0, artifacts: 0, deliverables: 0, additions: 0, deletions: 0, omitted_files: 0 },
          has_more_before: false,
          before_cursor: null,
          scope: { key: 'conversation:conv-task-chat', kind: 'conversation', conversation_id: 'conv-task-chat' },
        });
      }
      return json({
        ...projection,
        has_more_before: true,
        before_cursor: 'work-older-1',
        scope: { key: 'conversation:conv-task-chat', kind: 'conversation', conversation_id: 'conv-task-chat' }
      });
    }
    if (path === `/api/v1/tasks/${TASK_ID}/steps/summary`) {
      return json({
        items: projectedRuns(status),
        next_cursor: null,
        has_more: false
      });
    }
    if (path.startsWith('/api/v1/step-runs/')) {
      const pathParts = path.split('/');
      const stepRunId = pathParts[pathParts.length - 1] ?? '';
      const run = projectedRuns(status).find((candidate) => candidate.step_run_id === stepRunId);
      if (run) {
        if (stepRunId === HEAVY_STEP_RUN_ID) heavyRequestCount += 1;
        const response = detailedRun(run);
        detailResponses.push(response);
        return json(response);
      }
    }
    if (path.startsWith(`/api/v1/tasks/${TASK_ID}/steps/`) && path.endsWith('/summary')) {
      return json({ items: [], next_cursor: null, has_more: false });
    }
    if (path === `/api/v1/tasks/${TASK_ID}/comments`) return json([]);
    if (path === '/api/v1/agents') {
      return json({
        items: [{
          agent_id: 'agent-stage39',
          name: 'stage39',
          display_name: 'Stage 39 Agent',
          avatar_url: null,
          avatar_image_id: null
        }],
        next_cursor: null,
        has_more: false
      });
    }
    if (path === '/api/v1/agents/agent-stage39') {
      return json({
        agent_id: 'agent-stage39',
        name: 'stage39',
        display_name: 'Stage 39 Agent',
        avatar_url: null,
        avatar_image_id: null
      });
    }
    if (path === '/api/v1/workflows/step-profiles') return json([]);
    if (path === '/api/v1/workflows') return json({ items: [workflow], next_cursor: null, has_more: false });
    if (path === '/api/v1/workflows/wf-stage39') return json(workflow);
    if (path === '/api/v1/conversations') return json({ items: [], next_cursor: null, has_more: false });
    if (path === '/api/v1/conversations/open' && method === 'POST') return json(taskChatConversation);
    if (path === '/api/v1/tasks') return json({ items: [task(status)], next_cursor: null, has_more: false });
    if (path === '/api/v1/projects') return json([]);
    if (path === '/api/v1/notifications') return json([]);
    if (path === `/api/v1/tasks/${TASK_ID}/control-chat`) {
      navigationRequests.push(`${method} ${path}`);
      return json({
        conversation_id: 'conv-task-chat',
        session_id: 'sess-task-chat',
        task_id: TASK_ID,
        agent_id: 'agent-stage39',
        agent_profile_id: null,
        task_status: status,
        attempt_number: 1
      });
    }
    if (path === '/api/v1/conversations/conv-task-chat') return json(taskChatConversation);
    if (path === '/api/v1/conversations/conv-task-chat-b') return json(taskChatConversationB);
    if (path === '/api/v1/conversations/conv-task-chat-b/opened' && method === 'POST') return json({ ok: true });
    if (path === '/api/v1/conversations/conv-task-chat-b/read' && method === 'POST') return json({ ok: true });
    if (path === '/api/v1/conversations/conv-task-chat-b/queue') return json({ items: [], queued_count: 0 });
    if (path === '/api/v1/conversations/conv-task-chat-b/sessions') return json([taskChatSessionB]);
    if (path === '/api/v1/sessions/sess-task-chat-b/intaris') {
      return json({
        session_id: 'sess-task-chat-b',
        intaris_session_id: 'intaris-task-chat-b',
        status: 'active',
        total_calls: 0,
        approved_count: 0,
        denied_count: 0,
        escalated_count: 0,
        context_usage: {
          prompt_tokens: 2400,
          max_context_tokens: 16000,
          percentage: 15,
          model: 'stage39-model-b',
          reasoning_effort: 'medium',
          agent_profile_id: null,
          provider_id: 'stage39-provider',
          effective_prompt_budget: 12000
        },
        last_generation: null
      });
    }
    if (path === '/api/v1/chat/v2/conversations/conv-task-chat-b/snapshot') {
      return json({
        schema_version: 2,
        projection_version: 'stage39-cockpit-e2e-b',
        scope: { key: 'conversation:conv-task-chat-b', kind: 'conversation', conversation_id: 'conv-task-chat-b' },
        conversation: taskChatConversationB,
        timeline: { items: taskChatTimeline, has_more_before: false, before_cursor: null },
        runtime: {
          active_session_id: 'sess-task-chat-b',
          active_turn_id: null,
          active_turn_state: null,
          has_active_turn: false,
          volatile_items: []
        },
        cursor: 'cursor:conv-task-chat-b',
        server_time: NOW
      });
    }
    if (path === '/api/v1/chat/v2/conversations/conv-task-chat-b/sync') {
      const cursor = url.searchParams.get('cursor') ?? 'cursor:conv-task-chat-b';
      return json({
        schema_version: 2,
        projection_version: 'stage39-cockpit-e2e-b',
        scope: { key: 'conversation:conv-task-chat-b', kind: 'conversation', conversation_id: 'conv-task-chat-b' },
        conversation_id: 'conv-task-chat-b',
        cursor_before: cursor,
        cursor_after: cursor,
        ops: [],
        runtime: null,
        reset_required: false,
        reset_reason: null,
        has_more: false,
        server_time: NOW
      });
    }
    if (path === '/api/v1/conversations/conv-task-chat/opened' && method === 'POST') {
      return json({ ok: true });
    }
    if (path === '/api/v1/conversations/conv-task-chat/read' && method === 'POST') {
      return json({ ok: true });
    }
    if (path === '/api/v1/conversations/conv-task-chat/queue') {
      return json({ items: [], queued_count: 0 });
    }
    if (
      path.match(/^\/api\/v1\/chat\/v2\/conversations\/conv-task-chat\/messages\/[^/]+$/)
      && method === 'PUT'
    ) {
      const parts = path.split('/');
      const clientTxnId = parts[parts.length - 1] ?? '';
      const payload = request.postDataJSON() as { client_message_id?: string };
      return json({
        status: 'accepted',
        client_txn_id: clientTxnId,
        client_message_id: payload.client_message_id ?? clientTxnId,
        conversation_id: 'conv-task-chat',
        message_id: 'message-task-control',
        queue_id: null,
        cursor: 'cursor:conv-task-chat',
        server_time: NOW
      });
    }
    if (path === '/api/v1/conversations/conv-task-chat/sessions') return json([taskChatSession]);
    if (path === '/api/v1/sessions/sess-task-chat/intaris') {
      return json({
        session_id: 'sess-task-chat',
        intaris_session_id: 'intaris-task-chat',
        title: 'Task control: Release safety review',
        intention: 'Understand and safely manage the release task.',
        summary: 'The task is paused at the production approval gate.',
        status: 'active',
        total_calls: 1,
        approved_count: 1,
        denied_count: 0,
        escalated_count: 0,
        context_usage: {
          prompt_tokens: 3200,
          max_context_tokens: 16000,
          percentage: 20,
          model: 'stage39-model',
          reasoning_effort: 'medium',
          agent_profile_id: null,
          provider_id: 'stage39-provider',
          effective_prompt_budget: 12000
        },
        token_usage: null,
        last_generation: null
      });
    }
    if (path === '/api/v1/chat/v2/conversations/conv-task-chat/snapshot') {
      return json({
        schema_version: 2,
        projection_version: 'stage39-cockpit-e2e',
        scope: {
          key: 'conversation:conv-task-chat',
          kind: 'conversation',
          conversation_id: 'conv-task-chat'
        },
        conversation: taskChatConversation,
        timeline: { items: taskChatTimeline, has_more_before: false, before_cursor: null },
        state: {
          state_version: 1,
          snapshot_generated_at: NOW,
          capabilities: [],
          active_turn: {},
          pending: {},
          active_session: {
            session_id: 'sess-task-chat',
            status: 'active',
            completion_reason: null,
            todos: []
          },
        },
        queue: { messages: [], queued_count: 0 },
        runtime: {
          runtime_epoch: 'stage39-cockpit-e2e',
          runtime_revision: 1,
          generated_at: NOW,
          has_active_turn: false,
          volatile_items: []
        },
        cursor: 'cursor:conv-task-chat',
        server_time: NOW
      });
    }
    if (path === '/api/v1/chat/v2/conversations/conv-task-chat/sync') {
      const cursor = url.searchParams.get('cursor') ?? 'cursor:conv-task-chat';
      return json({
        schema_version: 2,
        projection_version: 'stage39-cockpit-e2e',
        scope: {
          key: 'conversation:conv-task-chat',
          kind: 'conversation',
          conversation_id: 'conv-task-chat'
        },
        conversation_id: 'conv-task-chat',
        cursor_before: cursor,
        cursor_after: cursor,
        ops: [],
        runtime: null,
        reset_required: false,
        reset_reason: null,
        has_more: false,
        server_time: NOW
      });
    }
    if (path === '/api/v1/user-preferences') {
      return json({
        display: { theme: 'dark', language: 'en' },
        chat: {
          show_thinking_blocks: true,
          group_tool_calls: true,
          keep_assistant_messages_separate: false,
          show_internal_tool_calls: false
        }
      });
    }
    if (path === '/api/v1/search/health') return json({ enabled: false });
    if (path === '/api/v1/conversations/sidebar') {
      const isTaskFilter = url.searchParams.get('status') === 'task';
      return json({
        agents: [],
        agent_direct_chats: [],
        conversations: {
          items: isTaskFilter ? [taskChatConversation, taskChatConversationB] : [],
          next_cursor: null,
          has_more: false
        },
        context_types: ['web'],
        removed_conversation_ids: [],
        full_resync_required: false,
        sync_timestamp: NOW,
        background_work: { items: [], active_count: 0, truncated: false, generated_at: NOW }
      });
    }
    if (path === `/api/v1/tasks/${TASK_ID}/rerun` && method === 'POST') {
      actionRequests.push(`${method} ${path}`);
      return json({ ok: true, source_task_id: TASK_ID, task_id: TASK_ID, status, created_new: false });
    }
    if (path === `/api/v1/tasks/${TASK_ID}/gate-response` && method === 'POST') {
      actionRequests.push(`${method} ${path}`);
      status = 'running';
      return json({ ok: true, task_id: TASK_ID, status });
    }
    if (path.match(new RegExp(`/api/v1/tasks/${TASK_ID}/(submit|pause|resume|cancel)$`)) && method === 'POST') {
      actionRequests.push(`${method} ${path}`);
      return json({ ok: true, task_id: TASK_ID, status });
    }
    if (path === `/api/v1/tasks/${TASK_ID}` && method === 'GET') return json(task(status));
    if (path === `/api/v1/tasks/${TASK_ID}` && method === 'PATCH') return json(task(status));

    unmockedRequests.push(`${method} ${path}`);
    return route.fulfill({
      status: 404,
      contentType: 'application/json',
      body: JSON.stringify({ detail: `Unmocked cockpit fixture API: ${method} ${path}` })
    });
  });
  return {
    setStatus(nextStatus) {
      status = nextStatus;
    },
    heavyRequests() {
      return heavyRequestCount;
    },
    actionRequests() {
      return [...actionRequests];
    },
    navigationRequests() {
      return [...navigationRequests];
    },
    detailResponses() {
      return [...detailResponses];
    },
    unmockedRequests() {
      return [...unmockedRequests];
    }
  };
}

export function isCockpitApiRequest(request: Request): boolean {
  return request.url().includes('/api/v1/');
}
