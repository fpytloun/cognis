<script lang="ts">
  import { onMount } from 'svelte';
  import ScopedChatV2Timeline from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
  import { fixtureScopes, ScopedFixtureController } from '$lib/chat-v2/scoped-fixture';
  import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
  import type { StepRun } from '$lib/types/api';

  const controller = new ScopedFixtureController();
  const fixturePreferences = {
    ...DEFAULT_USER_PREFERENCES,
    chat: {
      ...DEFAULT_USER_PREFERENCES.chat,
      group_tool_calls: false,
      show_internal_tool_calls: true,
    },
  };
  onMount(() => {
    (window as typeof window & { __scopedFixtureController?: ScopedFixtureController }).__scopedFixtureController = controller;
    return () => delete (window as typeof window & { __scopedFixtureController?: ScopedFixtureController }).__scopedFixtureController;
  });
  let selected = $state('parent');
  let scope = $derived(fixtureScopes[selected] ?? fixtureScopes.parent);
  let activityStatus = $state('running');
  let taskStepRun = $state<StepRun>({
    step_run_id: 'fixture-step',
    task_id: 'fixture-task',
    step_name: 'Fixture task step',
    step_type: 'agent',
    status: 'running',
    attempt: 1,
    attempt_number: 1,
    superseded_by_step_run_id: null,
    agent_id: 'fixture-agent',
    workspace_root: null,
    working_directory: null,
    conversation_id: 'fixture-conversation',
    session_id: 'fixture-step-session',
    intaris_session_id: 'fixture-step-session',
    deliverable_id: null,
    require_deliverable: true,
    output: null,
    evaluation: null,
    runtime_info: null,
    deliverables: [],
    todos: [],
    started_at: '2026-01-01T00:00:00Z',
    completed_at: null,
    updated_at: '2026-01-01T00:00:00Z',
  });
  const currentStepRun = $derived(scope.kind === 'task_step' ? taskStepRun : null);

  function select(next: string): void {
    selected = next;
  }

  function resetCursor(): void {
    controller.triggerCursorReset();
  }

  function emitStaleFrame(): void {
    controller.triggerStaleFrame(scope);
  }

  function emitCrossScopeFrame(): void {
    controller.triggerCrossScopeFrame(scope);
  }

  function reconnect(): void {
    controller.realtime.disconnect();
    controller.realtime.reAuthenticate();
  }

  function setEvaluating(): void {
    activityStatus = 'evaluating';
    taskStepRun = { ...taskStepRun, status: 'evaluating', updated_at: '2026-01-01T00:01:00Z' };
  }

  function setApproved(): void {
    activityStatus = 'approved';
    taskStepRun = {
      ...taskStepRun,
      status: 'approved',
      deliverable_id: 'fixture-deliverable',
      deliverables: [{
        deliverable_id: 'fixture-deliverable',
        step_run_id: 'fixture-step',
        version: 2,
        attempt_number: 1,
        format: 'rich',
        title: 'Fixture outcome',
        content: 'Fixture fallback',
        rich_payload: { blocks: [{ type: 'markdown', content: '# Fixture rich outcome' }] },
        status: 'approved',
        target: null,
        outputs: {},
        created_at: '2026-01-01T00:02:00Z',
        updated_at: '2026-01-01T00:02:00Z',
        evaluator_feedback: 'Looks correct',
      }],
      evaluation: { decision: 'approved', reasoning: 'Verified outcome', feedback: 'Looks correct' },
      output: { summary: 'Fixture summary', claims: ['Claim one'], outcome: { status: 'success' } },
      completed_at: '2026-01-01T00:02:00Z',
      updated_at: '2026-01-01T00:02:00Z',
    };
  }

  function setFailed(): void {
    activityStatus = 'failed';
    taskStepRun = {
      ...taskStepRun,
      status: 'failed',
      output: { error: 'Fixture terminal failure' },
      completed_at: '2026-01-01T00:03:00Z',
      updated_at: '2026-01-01T00:03:00Z',
    };
  }

</script>

<svelte:head><title>Scoped Chat V2 Fixture</title></svelte:head>

<main data-testid="scoped-chat-v2-fixture" class="min-h-screen bg-slate-950 p-4 text-slate-100">
  <h1 class="mb-2 text-xl font-semibold">Scoped ChatV2 fixture</h1>
  <p data-testid="active-scope" class="mb-4 text-sm text-slate-400">{scope.label} · {scope.key}</p>
  <div class="mb-4 flex flex-wrap gap-2" role="toolbar" aria-label="Scope controls">
    <button type="button" data-testid="scope-parent" onclick={() => select('parent')}>Parent</button>
    <button type="button" data-testid="scope-child" onclick={() => select('child')}>View child</button>
    <button type="button" data-testid="scope-grandchild" onclick={() => select('grandchild')}>View grandchild</button>
    <button type="button" data-testid="scope-task-step" onclick={() => select('task-step')}>Task step</button>
    <button type="button" data-testid="scope-missing" onclick={() => select('missing')}>Missing stream</button>
    <button type="button" data-testid="emit-active-frame" onclick={() => controller.triggerActiveFrame(scope)}>Emit active frame</button>
     <button type="button" data-testid="emit-stale-frame" onclick={emitStaleFrame}>Emit stale frame</button>
     <button type="button" data-testid="emit-cross-scope-frame" onclick={emitCrossScopeFrame}>Emit cross-scope frame</button>
      <button type="button" data-testid="hold-refresh-snapshot" onclick={() => controller.holdNextRefreshSnapshot()}>Hold next snapshot</button>
      <button type="button" data-testid="resolve-refresh-snapshot" onclick={() => controller.resolveHeldRefreshSnapshot()}>Resolve snapshot</button>
      <button type="button" data-testid="reset-cursor" onclick={resetCursor}>Reset cursor</button>
       <button type="button" data-testid="reconnect" onclick={reconnect}>Reconnect</button>
      <button type="button" data-testid="set-evaluating" onclick={setEvaluating}>Set evaluating</button>
      <button type="button" data-testid="set-approved" onclick={setApproved}>Set approved</button>
      <button type="button" data-testid="set-failed" onclick={setFailed}>Set failed</button>
      <button type="button" data-testid="back-parent" onclick={() => select(selected === 'grandchild' ? 'child' : 'parent')}>Back</button>
  </div>
  <div class="flex h-[32rem] overflow-hidden rounded-xl border border-slate-800" data-testid="scoped-timeline-shell">
    <ScopedChatV2Timeline
      {scope}
      api={controller.api}
      realtime={controller.realtime}
      preferences={fixturePreferences}
      {activityStatus}
      stepRun={currentStepRun}
      emptyLabel="No scoped events."
    />
  </div>
  <section class="mt-4 grid gap-3 md:grid-cols-2" data-testid="concurrent-timelines">
    <div class="h-48 overflow-hidden rounded-xl border border-slate-800" data-testid="concurrent-parent">
      <ScopedChatV2Timeline scope={fixtureScopes.parent} api={controller.api} realtime={controller.realtime} compact emptyLabel="No parent events." />
    </div>
    <div class="h-48 overflow-hidden rounded-xl border border-slate-800" data-testid="concurrent-child">
      <ScopedChatV2Timeline scope={fixtureScopes.child} api={controller.api} realtime={controller.realtime} compact emptyLabel="No child events." />
    </div>
  </section>
  <output data-testid="active-subscriptions">{controller.activeSubscriptions.join(',')}</output>
</main>

<style>
  button {
    border: 1px solid rgb(71 85 105);
    border-radius: 0.5rem;
    padding: 0.45rem 0.7rem;
    font-size: 0.8rem;
  }
  button:hover { background: rgb(30 41 59); }
</style>
