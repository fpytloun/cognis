<script lang="ts">
  import { onMount } from 'svelte';
  import WorkspaceWindow from '../../src/lib/components/dashboard/WorkspaceWindow.svelte';
  import CompactConversationChat from '../../src/lib/components/chat-v2/CompactConversationChat.svelte';
  import ConversationModalWorkspace from '../../src/lib/components/dashboard/ConversationModalWorkspace.svelte';
  import PinnedConversationPanel from '../../src/lib/components/chat/PinnedConversationPanel.svelte';
  import type { PinnedPanel } from '../../src/lib/components/chat/pinned-panel.svelte';
  import type { Agent, BackgroundWorkItem, Conversation } from '../../src/lib/types/api';
  import type { RetainedConversation } from '../../src/lib/dashboard/conversation-workspace';
  import SessionLogsDrawer from '../../src/lib/components/tasks/SessionLogsDrawer.svelte';
  import ChildChatView from '../../src/lib/components/chat-v2/ChildChatView.svelte';
  import TimelineOngoingWorkDrawer from '../../src/lib/components/timeline/TimelineOngoingWorkDrawer.svelte';
  import SharedInspectorTabs from '../../src/lib/components/inspector/SharedInspectorTabs.svelte';
  import ConversationInfoDrawer from '../../src/lib/components/ConversationInfoDrawer.svelte';
  import { DEFAULT_USER_PREFERENCES } from '../../src/lib/user-preferences';
  import type { WorkstreamRef } from '../../src/lib/chat-v2/types';
  import type { StepRun } from '../../src/lib/types/api';
  import { WorkspaceManager, serializeWorkspaceWindows } from '../../src/lib/dashboard/workspace';
  import { ConversationWorkspace } from '../../src/lib/dashboard/conversation-workspace';
  import { conversationTimelineScope } from '../../src/lib/chat-v2/types';

  const count = Number(new URLSearchParams(location.search).get('windows') ?? 1);
  const surface = new URLSearchParams(location.search).get('surface');
  let logsOpen = $state(false);
  let childInspectorOpen = $state(false);
  const childNode: WorkstreamRef = {
    key: 'acceptance-delegate', root_key: 'acceptance-root', parent_key: 'acceptance-root',
    kind: 'delegate', edge_kind: 'delegate', ordinal: 1, session_id: 'acceptance-delegate',
    event_store_session_id: 'acceptance-delegate', title: 'Read-only bounded public-source research',
    agent_id: 'acceptance-agent', status: 'running', execution_state: 'running',
    current: true, superseded: false, activity_state: 'active',
  };
  const pinnedEntry: RetainedConversation = {
    window: {
      key: 'conversation:acceptance-conversation-3', kind: 'conversation', entityId: 'acceptance-conversation-3',
      resolvedConversationId: 'acceptance-conversation-3', agentId: 'acceptance-agent', title: 'Synthetic maintenance chat',
      canonicalHref: '/chat/acceptance-conversation-3', rect: { x: 0, y: 0, width: 1100, height: 800 },
      minimized: false, zOrder: 1, switcherOrder: 0, restoreRect: null, refreshToken: 0, focusRevision: 0,
    },
    projectId: null, layouts: {},
  };
  const pinnedGroup = {
    id: 'acceptance-agent', entries: [pinnedEntry], lastSelectedKey: pinnedEntry.window.key, activityAt: 0,
    state: 'idle' as const, agent: { agent_id: 'acceptance-agent', name: 'Demo agent' } as Agent,
  };
  const pinnedPanel: PinnedPanel = {
    groups: [pinnedGroup], selectedKey: pinnedEntry.window.key, selected: pinnedEntry,
    selectedId: 'acceptance-conversation-3', group: pinnedGroup,
    conversation: {
      conversation_id: 'acceptance-conversation-3', agent_id: 'acceptance-agent', active_session_id: 'acceptance-session-3',
      title: 'Synthetic maintenance chat', has_active_turn: false,
    } as Conversation,
    picker: false, resolving: false, error: '', creating: false,
    activity: () => 'idle', refresh: async () => {}, observe: () => {}, close: () => {},
    select: async () => {}, openAgent: async () => {}, openConversation: async () => {}, unpin: async () => {},
    newConversation: async () => {}, command: async () => {}, loaded: () => {}, unpinAgent: async () => {},
  };
  const attemptCount = new URLSearchParams(location.search).get('attempts') === '5' ? 5 : 3;
  const latestStatus = new URLSearchParams(location.search).get('status') ?? 'running';
  const footerFixture = new URLSearchParams(location.search).has('footer');
  let managedTurnState = $state('running');
  let childOpen = $state(true);
  let drawerOpenedSession = $state('');
  let inspectorFixtureOpen = $state(true);
  const inspectorFixturePresentation = new URLSearchParams(location.search).get('presentation') as 'overlay' | 'focus' | 'pinned' | null;
  const drawerLongTitle = 'Inspect every stage of this deliberately long verification workflow without wrapping or hiding its complete description and final acceptance criteria';
  const drawerTodos = Array.from({ length: 8 }, (_, index) => ({
    content: `Todo ${index + 1}: ${drawerLongTitle}`, status: 'in_progress' as const, priority: 'normal' as const,
  }));
  const drawerWork: BackgroundWorkItem[] = [
    { work_id: 'drawer-managed', kind: 'managed_conversation', title: `Managed subsession: ${drawerLongTitle}`, status: 'running', controller_conversation_id: 'drawer-controller', target_conversation_id: 'drawer-managed', session_id: 'drawer-managed-session', agent_id: 'agent-1', todos: [] },
    { work_id: 'drawer-delegate', kind: 'delegated_session', title: `Delegate subsession: ${drawerLongTitle}`, status: 'running', controller_conversation_id: 'drawer-controller', session_id: 'drawer-delegate-session', agent_id: 'agent-1', todos: [] },
    { work_id: 'drawer-command', kind: 'background_command', title: `Command: ${drawerLongTitle}`, status: 'running', controller_conversation_id: 'drawer-controller', agent_id: 'agent-1', todos: [] },
    { work_id: 'drawer-task', kind: 'task', title: `Task: ${drawerLongTitle}`, status: 'running', controller_conversation_id: 'drawer-controller', task_id: 'drawer-task', agent_id: 'agent-1', todos: [] },
    { work_id: 'drawer-unlinked', kind: 'delegated_session', title: `Unavailable subsession: ${drawerLongTitle}`, status: 'running', controller_conversation_id: 'drawer-controller', agent_id: 'agent-1', todos: [] },
  ];
  const managedTarget = $derived({
    conversation_id: 'acceptance-managed-child', title: 'Subsession with ongoing verification',
    has_active_turn: managedTurnState === 'running',
    managed_agent: { channel: 'agent_work', conversation_state: 'open', turn_state: managedTurnState,
      controller_conversation_id: 'acceptance-conversation-3' },
  } as Conversation);
  const childWork: BackgroundWorkItem[] = Array.from({ length: new URLSearchParams(location.search).has('crowded') ? 24 : 1 }, (_, index) => ({
    work_id: index ? `acceptance-verification-command-${index}` : 'acceptance-verification-command',
    kind: 'background_command', title: index ? `Verification command ${index + 1}` : 'Verification command',
    controller_conversation_id: 'acceptance-conversation-3', agent_id: 'acceptance-agent',
    session_id: 'acceptance-delegate', status: 'running', todos: [],
  }));
  const attempts = Array.from({ length: attemptCount }, (_, index) => index + 1).map(attempt => ({
    step_run_id: `acceptance-step-${attempt}`, task_id: 'acceptance-task', step_name: 'execute',
    conversation_id: `acceptance-conversation-${attempt}`, session_id: `acceptance-session-${attempt}`,
    agent_id: 'acceptance-agent', status: attempt === attemptCount ? latestStatus : 'failed', attempt: 1,
    attempt_number: attempt, todos: [],
  } as unknown as StepRun));
  const originalScopePath = new URLSearchParams(location.search).get('scope') === 'original';
  const scopes = new Map(Array.from({ length: count }, (_, index) => {
    const scope = conversationTimelineScope(`drag-chat-${index}`);
    return [scope.key, scope] as const;
  }));
  const manager = new WorkspaceManager('desktop', { x: 12, y: 12, width: 1500, height: 940 });
  const retained = new ConversationWorkspace();
  const counters = { publications: 0, retainedPublications: 0, writes: 0, json: 0 };
  onMount(() => {
    retained.setOwner('drag-acceptance@cognis.test', localStorage);
    for (let index = 0; index < count; index += 1) {
      manager.open({
        kind: 'conversation', entityId: `drag-chat-${index}`, title: `Synthetic chat ${index}`,
        canonicalHref: `/chat/drag-chat-${index}`,
      });
    }
    localStorage.setItem('drag-acceptance-legacy', serializeWorkspaceWindows(manager.snapshot()));
    void retained.reduce({ type: 'migrate', key: 'drag-acceptance-legacy', projectId: null });
    // Exercise the real retained-layout and dashboard persistence consumers.
    const stopBind = retained.bind(manager, null, 'drag-acceptance-legacy');
    const stopRetained = retained.subscribe(() => counters.retainedPublications++);
    const stopManager = manager.subscribe(() => {
      counters.publications++;
      localStorage.setItem('drag-acceptance-legacy', serializeWorkspaceWindows(manager.persistentSnapshot()));
    });
    const originalSetItem = Storage.prototype.setItem;
    const originalStringify = JSON.stringify;
    Storage.prototype.setItem = function(key, value) {
      counters.writes++;
      return originalSetItem.call(this, key, value);
    };
    JSON.stringify = function(...args: Parameters<typeof JSON.stringify>) {
      counters.json++;
      return originalStringify(...args);
    } as typeof JSON.stringify;
    Object.assign(window, {
      dragAcceptance: {
        reset: () => { for (const key of Object.keys(counters) as Array<keyof typeof counters>) counters[key] = 0; },
        counts: () => ({ ...counters }),
        snapshot: () => manager.snapshot(),
      },
    });
    return () => {
      Storage.prototype.setItem = originalSetItem;
      JSON.stringify = originalStringify;
      stopManager(); stopRetained(); stopBind();
    };
  });
</script>

{#if surface}
  <main class="flex h-dvh flex-col bg-canvas text-foreground">
    <header class="flex h-14 shrink-0 items-center justify-between border-b border-separator px-4">
      <h1 class="text-sm font-semibold">Unified chat acceptance · synthetic data</h1>
      {#if surface === 'task'}<button class="rounded border border-control-border px-3 py-2 text-sm" onclick={() => { logsOpen = true; }}>Open step logs</button>{/if}
    </header>
  {#if surface === 'ongoing'}
    <section class="flex min-h-0 flex-1 flex-col" data-testid="ongoing-drawer-fixture">
      <div class="min-h-0 flex-1 overflow-y-auto p-3">Synthetic transcript above the ongoing work footer.</div>
      <footer class="shrink-0 space-y-3 p-3">
        <div data-testid="drawer-under-test">
          <TimelineOngoingWorkDrawer open={true} todos={drawerTodos} work={drawerWork} onViewSession={id => { drawerOpenedSession = id; }} />
        </div>
        <div class="rounded-2xl border border-separator p-3" data-testid="drawer-composer">Message agent</div>
        <output data-testid="drawer-opened-session" class="sr-only">{drawerOpenedSession}</output>
      </footer>
    </section>
  {:else if surface === 'inspector'}
    <div class="grid min-h-0 flex-1 grid-cols-[1fr_auto]">
      {#if inspectorFixtureOpen}
        <ConversationInfoDrawer open={true} presentation={inspectorFixturePresentation ?? 'overlay'} width={360}
          onWidthChange={() => {}} onWidthCommit={() => {}} onFocusChange={() => {}}
          onClose={() => { inspectorFixtureOpen = false; }}>
          {#snippet header()}
            <div class="min-w-0 flex-1">
              <SharedInspectorTabs scope={{ key: 'conversation:drawer-controller', kind: 'conversation', conversation_id: 'drawer-controller' }} sessionId="drawer-session" headerOnly idPrefix="drawer-inspector" />
            </div>
          {/snippet}
          <p>Synthetic inspector contents.</p>
        </ConversationInfoDrawer>
      {/if}
    </div>
  {:else if surface === 'child' && childOpen}
      <ChildChatView
        view={footerFixture
          ? { kind: 'managed', conversationId: 'acceptance-managed-child', sessionId: 'acceptance-delegate', controllerRootConversationId: 'acceptance-conversation-3', nodeKey: childNode.key }
          : { kind: 'delegate', conversationId: 'acceptance-conversation-3', sessionId: 'acceptance-delegate', controllerRootConversationId: 'acceptance-conversation-3', nodeKey: childNode.key }}
        node={footerFixture ? { ...childNode, kind: 'managed', execution_state: managedTurnState === 'running' ? 'running' : 'idle' } : childNode}
        agent={{ agent_id: 'acceptance-agent', name: 'Research delegate' } as Agent}
        work={footerFixture ? childWork : []} managedConversation={footerFixture ? managedTarget : null}
        onManagedStop={() => { managedTurnState = 'idle'; }} onManagedSend={() => {}} onManagedTakeControl={() => {}}
        preferences={DEFAULT_USER_PREFERENCES} inspectorOpen={childInspectorOpen}
        onBack={() => { location.search = '?surface=task&attempts=5&status=failed'; }}
        onClose={() => { if (footerFixture) childOpen = false; else location.search = '?surface=task'; }} onViewSession={() => {}}
        onToggleInspector={() => { childInspectorOpen = !childInspectorOpen; }}
      />
    {:else if surface !== 'task'}
      {#if surface === 'pinned'}
        <PinnedConversationPanel panel={pinnedPanel} onMinimize={() => {}} />
      {:else}
        <ConversationModalWorkspace conversationId="acceptance-conversation-3" sessionId="acceptance-session-3" />
      {/if}
    {:else if logsOpen}
      {@const latest = attempts[attempts.length - 1]}
      <SessionLogsDrawer conversationId={latest.conversation_id!} sessionId={latest.session_id!}
        taskId="acceptance-task" stepRunId={latest.step_run_id} stepName="execute"
        stepRun={latest} {attempts} onclose={() => { logsOpen = false; }} />
    {/if}
  </main>
{:else}
{#each $manager as windowState (windowState.key)}
  {@const scope = scopes.get(windowState.key)!}
  <WorkspaceWindow window={windowState} {manager}>
    {#if originalScopePath}
      <!-- Preserve the original failing prop path for the sync owner's regression. -->
      <CompactConversationChat conversationId={windowState.entityId} embedded />
    {:else}
      <CompactConversationChat conversationId={scope.conversation_id!} timelineScope={scope} embedded />
    {/if}
  </WorkspaceWindow>
{/each}
{/if}
