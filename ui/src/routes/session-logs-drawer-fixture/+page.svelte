<script lang="ts">
  import SessionLogsDrawer from '$lib/components/tasks/SessionLogsDrawer.svelte';
  import type { StepRun } from '$lib/types/api';

  let open = $state(false);
  let stepRun = $state<StepRun>({
    step_run_id: 'fixture-step',
    task_id: 'fixture-task',
    step_name: 'Fixture step',
    step_type: 'agent',
    status: 'running',
    attempt: 1,
    attempt_number: 1,
    superseded_by_step_run_id: null,
    agent_id: 'fixture-agent',
    workspace_root: null,
    working_directory: null,
    conversation_id: 'fixture-conversation',
    session_id: 'fixture-session',
    intaris_session_id: 'fixture-session',
    deliverable_id: null,
    require_deliverable: false,
    output: null,
    evaluation: null,
    runtime_info: null,
    deliverables: [],
    todos: [],
    started_at: '2026-01-01T00:00:00Z',
    completed_at: null,
    updated_at: '2026-01-01T00:00:00Z',
  });
</script>

<main class="min-h-[200vh] bg-slate-950 p-6 text-slate-100" data-testid="drawer-fixture-page">
  <button data-testid="open-session-drawer" type="button" onclick={() => { open = true; }}>Open logs</button>
  <button
    data-testid="finish-session-step"
    type="button"
    onclick={() => { stepRun = { ...stepRun, status: 'completed', completed_at: '2026-01-01T00:01:00Z' }; }}
  >Finish step</button>
  <p class="mt-[120vh]" data-testid="page-bottom">Page bottom</p>
</main>

{#if open}
  <SessionLogsDrawer
    conversationId="fixture-conversation"
    sessionId="fixture-session"
    stepRunId="fixture-step"
    taskId="fixture-task"
    stepName="Fixture step"
    {stepRun}
    onclose={() => { open = false; }}
  />
{/if}
