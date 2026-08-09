<script lang="ts">
  import Maximize2 from 'lucide-svelte/icons/maximize-2';
  import Minimize2 from 'lucide-svelte/icons/minimize-2';
  import X from 'lucide-svelte/icons/x';
  import { onMount, tick, untrack } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import ActivityAvatar from '$lib/components/ActivityAvatar.svelte';
  import AttentionPanel from '$lib/components/task-cockpit/AttentionPanel.svelte';
  import TaskControlChat from '$lib/components/task-cockpit/TaskControlChat.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import WorkView from '$lib/components/work/WorkView.svelte';
  import { conversationTimelineScope } from '$lib/chat-v2/types';
  import { conversationActivityState } from '$lib/conversation-activity';
  import { addToast } from '$lib/stores/toasts';
  import { overlayStack, registerOverlay } from '$lib/stores/overlays';
   import { isTextInputTarget, taskAgentDock, taskDockWorkKey } from '$lib/stores/taskAgentDock.svelte';
  import type { Agent, BackgroundWorkItem, CognisWebSocketEvent, Conversation, QuestionSetAnswer, TaskControlChatResponse, TaskDetail } from '$lib/types/api';
  import { wsClient } from '$lib/ws/client';

  let {
    task,
    agent,
    onGate,
    onQuestion
  } = $props<{
    task: TaskDetail;
    agent: Agent | null;
    onGate: (action: string, instruction: string) => boolean | void | Promise<boolean | void>;
    onQuestion: (answers: QuestionSetAnswer[]) => boolean | void | Promise<boolean | void>;
  }>();
  let chat = $state<TaskControlChatResponse | null>(null);
  let chatLoading = $state(false);
  let chatError = $state<string | null>(null);
  let conversation = $state<Conversation | null>(null);
  let backgroundWork = $state<BackgroundWorkItem[]>([]);
  let runtimeActive = $state<boolean | null>(null);
  let decisionBusy = $state(false);
  let panel = $state<HTMLElement | null>(null);
  let launcher = $state<HTMLButtonElement | null>(null);
  let tabButtons = $state<Array<HTMLButtonElement | null>>([]);
  let mobileViewport = $state(false);
  let previousFocus: HTMLElement | null = null;
  let overlayId = $state<string | null>(null);
  let unregisterOverlay: (() => void) | null = null;
  const inertBackground = new Map<HTMLElement, boolean>();
  let boundTaskId = '';
  let generation = 0;
  let readState: {
    generation: number;
    conversationId: string;
    again: boolean;
  } | null = null;
  let sidebarState: {
    generation: number;
    conversationId: string;
    promise: Promise<void>;
    again: boolean;
  } | null = null;
  let observedDockState = taskAgentDock.state;

  const agentName = $derived(agent?.display_name ?? agent?.name ?? task?.agent_id ?? 'Task agent');
  const avatarState = $derived(conversationActivityState(conversation, {
    open: taskAgentDock.state !== 'minimized',
    runtimeActive: runtimeActive ?? undefined,
    backgroundWork,
  }));
  const effectiveAvatarState = $derived(chatError
    ? {
        ...avatarState,
        active: false,
        background: false,
        attention: false,
        unread: false,
        error: true,
        tone: 'rose' as const,
        label: 'Task agent connection failed',
      }
    : avatarState);
  const chatScope = $derived(chat ? conversationTimelineScope(chat.conversation_id) : null);
  const workScope = $derived(taskAgentDock.workScope ?? chatScope);
  const modal = $derived(mobileViewport || taskAgentDock.state === 'fullscreen');
  const otherBlockingOverlay = $derived.by(() => {
    const top = $overlayStack.at(-1);
    return Boolean(
      top?.kind === 'blocking'
      && top.id !== overlayId
      && typeof document !== 'undefined'
      && document.querySelector('[data-blocking-overlay]')
    );
  });
  const tabs: Array<{ id: 'chat' | 'work'; label: string }> = [
    { id: 'chat', label: 'Chat' },
    { id: 'work', label: 'Work' }
  ];

  function resetForTask(nextTaskId: string): void {
    if (boundTaskId === nextTaskId) return;
    boundTaskId = nextTaskId;
    generation += 1;
    chat = null;
    conversation = null;
    backgroundWork = [];
    runtimeActive = null;
    readState = null;
    sidebarState = null;
    chatLoading = false;
    chatError = null;
    taskAgentDock.reset();
  }

  async function ensureChat(): Promise<void> {
    if (chat || chatLoading) return;
    const currentTaskId = task.task_id;
    const current = generation;
    chatLoading = true;
    chatError = null;
    try {
      const nextChat = await api.tasks.controlChat(currentTaskId);
      if (current === generation && currentTaskId === task.task_id) {
        chat = nextChat;
        try {
          const nextConversation = await api.conversations.detail(
            nextChat.conversation_id,
            { includeState: false },
          );
          if (current === generation && currentTaskId === task.task_id) {
            conversation = nextConversation;
            await markConversationReadIfOpen();
          }
        } catch {
          // The native timeline still works if the conversation projection refresh fails.
        }
        void refreshBackgroundWork();
      }
    } catch (error) {
      if (current === generation) {
        chatError = asApiError(error).message || 'Could not open the task agent.';
        addToast(chatError, 'error');
      }
    } finally {
      if (current === generation) chatLoading = false;
    }
  }

  async function respondToGate(action: string, instruction: string): Promise<void> {
    if (decisionBusy) return;
    decisionBusy = true;
    try {
      const resolved = await onGate(action, instruction);
      if (resolved === false) return;
      addToast(action === 'continue' || action === 'approve' ? 'Task approved.' : 'Revision requested.', 'success');
    } catch (error) {
      addToast(asApiError(error).message, 'error');
    } finally {
      decisionBusy = false;
    }
  }

  async function respondToQuestion(answers: QuestionSetAnswer[]): Promise<void> {
    if (decisionBusy) return;
    decisionBusy = true;
    try {
      const resolved = await onQuestion(answers);
      if (resolved === false) return;
      addToast('Response sent.', 'success');
    } catch (error) {
      addToast(asApiError(error).message, 'error');
    } finally {
      decisionBusy = false;
    }
  }

  async function openDock(): Promise<void> {
    previousFocus = launcher;
    taskAgentDock.open();
    await markConversationReadIfOpen();
  }

  async function markConversationReadIfOpen(): Promise<void> {
    const current = conversation;
    const currentGeneration = generation;
    if (
      current?.has_unread
      && taskAgentDock.state !== 'minimized'
      && readState?.generation === currentGeneration
      && readState.conversationId === current.conversation_id
    ) {
      readState.again = true;
      return;
    }
    if (
      !current?.has_unread
      || taskAgentDock.state === 'minimized'
    ) return;
    const state = {
      generation: currentGeneration,
      conversationId: current.conversation_id,
      again: false,
    };
    readState = state;
    conversation = { ...current, has_unread: false };
    try {
      await api.conversations.markRead(current.conversation_id);
    } catch (error) {
      if (
        generation === state.generation
        && conversation?.conversation_id === state.conversationId
      ) {
        conversation = { ...conversation, has_unread: true };
        addToast(asApiError(error).message, 'error');
      }
    } finally {
      if (readState !== state) return;
      readState = null;
      if (
        state.again
        && generation === state.generation
        && conversation?.conversation_id === state.conversationId
      ) {
        if (!conversation.has_unread) {
          conversation = { ...conversation, has_unread: true };
        }
        void markConversationReadIfOpen();
      }
    }
  }

  async function refreshBackgroundWork(): Promise<void> {
    const currentConversationId = conversation?.conversation_id;
    if (!currentConversationId) return;
    const currentGeneration = generation;
    if (
      sidebarState?.generation === currentGeneration
      && sidebarState.conversationId === currentConversationId
    ) {
      sidebarState.again = true;
      return sidebarState.promise;
    }
    const state = {
      generation: currentGeneration,
      conversationId: currentConversationId,
      promise: Promise.resolve(),
      again: false,
    };
    state.promise = api.conversations.sidebar()
      .then((sidebar) => {
        if (
          generation === state.generation
          && conversation?.conversation_id === state.conversationId
        ) {
          backgroundWork = sidebar.background_work.items;
        }
      })
      .catch(() => undefined)
      .finally(() => {
        if (sidebarState !== state) return;
        sidebarState = null;
        if (
          state.again
          && generation === state.generation
          && conversation?.conversation_id === state.conversationId
        ) {
          void refreshBackgroundWork();
        }
      });
    sidebarState = state;
    return state.promise;
  }

  function handleConversationEvent(event: CognisWebSocketEvent): void {
    if (!conversation || !('conversation_id' in event) || event.conversation_id !== conversation.conversation_id) return;
    if (event.type === 'conversation_updated') {
      conversation = {
        ...conversation,
        ...(event.has_unread !== undefined ? { has_unread: event.has_unread } : {}),
        ...(event.has_active_turn !== undefined ? { has_active_turn: event.has_active_turn } : {}),
        ...(event.active_session_status !== undefined ? { active_session_status: event.active_session_status } : {}),
        ...(event.active_session_completion_reason !== undefined
          ? { active_session_completion_reason: event.active_session_completion_reason }
          : {}),
        ...(event.pending_notification_types !== undefined
          ? { pending_notification_types: event.pending_notification_types }
          : {}),
      };
      if (event.has_unread) void markConversationReadIfOpen();
      void refreshBackgroundWork();
    } else if (event.type === 'conversation_runtime_snapshot' && event.has_active_turn !== undefined) {
      if (taskAgentDock.state === 'minimized' || taskAgentDock.tab !== 'chat') return;
      runtimeActive = event.has_active_turn;
      void refreshBackgroundWork();
    } else if (
      event.type === 'delegation_started'
      || event.type === 'delegation_completed'
      || event.type === 'delegation_failed'
    ) {
      void refreshBackgroundWork();
    }
  }

  function handleRuntimeActiveChange(
    active: boolean,
    expectedChat: TaskControlChatResponse,
  ): void {
    if (
      chat !== expectedChat
      || conversation?.conversation_id !== expectedChat.conversation_id
      || taskAgentDock.state === 'minimized'
      || taskAgentDock.tab !== 'chat'
    ) return;
    runtimeActive = active;
  }

  function minimize(): void {
    runtimeActive = false;
    taskAgentDock.minimize();
    void tick().then(() => {
      window.requestAnimationFrame(() => {
        const target = previousFocus?.isConnected && previousFocus !== document.body
          ? previousFocus
          : document.querySelector<HTMLButtonElement>('[data-testid="task-agent-dock-launcher"]') ?? launcher;
        target?.focus();
        window.requestAnimationFrame(() => target?.focus());
        previousFocus = null;
      });
    });
  }

  function selectTab(tab: 'chat' | 'work'): void {
    if (tab !== 'chat') runtimeActive = false;
    taskAgentDock.tab = tab;
  }

  function focusableElements(): HTMLElement[] {
    if (!panel) return [];
    return Array.from(panel.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    ));
  }

  function restoreBackgroundInert(): void {
    inertBackground.forEach((wasInert, element) => {
      element.inert = wasInert;
    });
    inertBackground.clear();
  }

  function setBackgroundInert(): void {
    restoreBackgroundInert();
    if (!panel || !modal || taskAgentDock.state === 'minimized') return;
    let current: HTMLElement | null = panel;
    while (current?.parentElement) {
      const parent: HTMLElement = current.parentElement;
      for (const sibling of Array.from(parent.children)) {
        if (
          !(sibling instanceof HTMLElement)
          || sibling === current
          || sibling.contains(panel)
          || sibling.matches('[data-task-agent-modal]')
        ) continue;
        inertBackground.set(sibling, Boolean(sibling.inert));
        sibling.inert = true;
      }
      current = parent === document.body ? null : parent;
    }
  }

  function focusPanel(): void {
    panel?.focus();
  }

  function handlePanelKeydown(event: KeyboardEvent): void {
    if (!modal || event.key !== 'Tab') return;
    const focusables = focusableElements();
    if (focusables.length === 0) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleTabKeydown(event: KeyboardEvent, index: number): void {
    let next = index;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    else return;
    event.preventDefault();
    selectTab(tabs[next].id);
    void tick().then(() => tabButtons[next]?.focus());
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (otherBlockingOverlay || isTextInputTarget(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key.toLowerCase() === 'a') {
      event.preventDefault();
      if (taskAgentDock.state === 'minimized') void openDock();
      else minimize();
    } else if (event.key === 'Escape' && taskAgentDock.state !== 'minimized') {
      event.preventDefault();
      minimize();
    }
  }

  $effect(() => {
    resetForTask(task.task_id);
    untrack(() => void ensureChat());
  });

  $effect(() => {
    const state = taskAgentDock.state;
    if (state !== observedDockState) {
      if (state === 'minimized') runtimeActive = false;
      observedDockState = state;
    }
    if (state === 'minimized' || otherBlockingOverlay) return;
    untrack(() => void markConversationReadIfOpen());
    if (!previousFocus) {
      previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    }
    untrack(() => {
      void ensureChat();
      void tick().then(focusPanel);
    });
  });

  $effect(() => {
    const shouldRegister = taskAgentDock.state !== 'minimized' && modal;
    if (shouldRegister && !overlayId) {
      const handle = registerOverlay({ kind: 'fullscreen', blocksChrome: true });
      overlayId = handle.id;
      unregisterOverlay = handle.unregister;
      document.body.classList.add('task-agent-dock-modal');
    } else if (!shouldRegister && overlayId) {
      unregisterOverlay?.();
      unregisterOverlay = null;
      overlayId = null;
      document.body.classList.remove('task-agent-dock-modal');
    }
  });

  onMount(() => {
    const unsubscribe = wsClient.subscribe((event) => {
      if (event.type !== 'chat_v2_frame') handleConversationEvent(event);
    });
    const media = typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 1023px)') : null;
    const syncViewport = (): void => { mobileViewport = media?.matches ?? window.innerWidth <= 1023; };
    syncViewport();
    media?.addEventListener('change', syncViewport);
    if (!media) window.addEventListener('resize', syncViewport);
    return () => {
      generation += 1;
      runtimeActive = false;
      unsubscribe();
      media?.removeEventListener('change', syncViewport);
      if (!media) window.removeEventListener('resize', syncViewport);
      unregisterOverlay?.();
      restoreBackgroundInert();
      document.body.classList.remove('task-agent-dock-modal');
    };
  });

  $effect(() => {
    panel;
    modal;
    taskAgentDock.state;
    untrack(setBackgroundInert);
  });
</script>

<svelte:window onkeydown={handleKeydown} />

{#if !otherBlockingOverlay && taskAgentDock.state === 'minimized'}
  <button
    bind:this={launcher}
    type="button"
    class={`task-agent-fab fixed z-[60] flex h-[52px] w-[52px] items-center justify-center rounded-2xl border bg-slate-900 shadow-2xl transition hover:-translate-y-0.5 motion-reduce:transition-none motion-reduce:hover:translate-y-0 ${effectiveAvatarState.attention || effectiveAvatarState.unread || effectiveAvatarState.error ? 'border-amber-400 ring-2 ring-amber-400/20' : 'border-slate-700'}`}
    aria-label={`Open ${agentName} for task ${task.title}. ${effectiveAvatarState.label}`}
    aria-haspopup="dialog"
    data-testid="task-agent-dock-launcher"
    onclick={openDock}
  >
    <ActivityAvatar
      name={agentName}
      avatarUrl={agent?.avatar_url ?? null}
      state={effectiveAvatarState}
      class="h-11 w-11 rounded-xl"
    />
  </button>
{/if}

{#if !otherBlockingOverlay && taskAgentDock.state !== 'minimized'}
  {#if modal}
    <button class="fixed inset-0 z-[89] bg-slate-950/75 backdrop-blur-sm" data-task-agent-modal type="button" aria-label="Minimize task agent" onclick={minimize}></button>
  {/if}
  <div
    bind:this={panel}
    tabindex="-1"
    role="dialog"
    aria-modal={modal}
    aria-label={`Agent — ${task.title}`}
    class:task-agent-fullscreen={taskAgentDock.state === 'fullscreen'}
    class="task-agent-panel fixed z-[90] flex flex-col overflow-hidden border border-slate-700 bg-slate-950 shadow-2xl outline-none"
    data-testid="task-agent-dock"
    onkeydown={handlePanelKeydown}
  >
    <header class="task-agent-header flex shrink-0 items-center justify-between gap-3 border-b border-slate-800 px-3 py-3">
      <div class="flex min-w-0 items-center gap-3">
        <ActivityAvatar
          name={agentName}
          avatarUrl={agent?.avatar_url ?? null}
          state={effectiveAvatarState}
          class="h-9 w-9 rounded-xl"
        />
        <div class="min-w-0"><p class="truncate text-sm font-semibold text-white">{agentName}</p><p class="truncate text-xs text-slate-400">Scoped to: {task.title}</p></div>
      </div>
      <div class="flex shrink-0 gap-1">
        {#if taskAgentDock.state === 'open'}<Button size="sm" variant="ghost" aria-label="Expand agent to full screen" onclick={() => taskAgentDock.expand()}><Maximize2 class="h-4 w-4" /></Button>{/if}
        {#if taskAgentDock.state === 'fullscreen'}<Button size="sm" variant="ghost" onclick={() => taskAgentDock.open()}>Return to task</Button>{/if}
        <Button size="sm" variant="ghost" aria-label="Minimize agent dock" onclick={minimize}>
          {#if taskAgentDock.state === 'fullscreen'}<Minimize2 class="h-4 w-4" />{:else}<X class="h-4 w-4" />{/if}
        </Button>
      </div>
    </header>

    <div class="flex shrink-0 border-b border-slate-800 p-1" role="tablist" aria-label="Agent Dock views">
      {#each tabs as item, index}
        <button
          bind:this={tabButtons[index]}
          id={`task-agent-tab-${item.id}`}
          type="button"
          role="tab"
          aria-selected={taskAgentDock.tab === item.id}
          aria-controls={`task-agent-panel-${item.id}`}
          tabindex={taskAgentDock.tab === item.id ? 0 : -1}
          class={`min-h-11 flex-1 rounded-lg px-3 text-sm ${taskAgentDock.tab === item.id ? 'bg-sky-500/10 text-sky-100' : 'text-slate-400 hover:text-white'}`}
          onclick={() => selectTab(item.id)}
          onkeydown={(event) => handleTabKeydown(event, index)}
        >{item.label}</button>
      {/each}
    </div>

    {#if task.pending_pause && task.status === 'paused'}
      <div class="max-h-[45%] shrink-0 overflow-y-auto border-b border-slate-800 p-3">
        <AttentionPanel pause={task.pending_pause} compact busy={decisionBusy} onGate={respondToGate} onQuestion={respondToQuestion} />
      </div>
    {/if}

    <div
      id={`task-agent-panel-${taskAgentDock.tab}`}
      role="tabpanel"
      aria-labelledby={`task-agent-tab-${taskAgentDock.tab}`}
      class="min-h-0 flex-1"
    >
      {#if chatLoading}
        <p class="p-4 text-sm text-slate-400">Loading the task agent…</p>
      {:else if chatError}
        <div class="m-4 rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-100" role="alert">
          <p>{chatError}</p>
          <Button class="mt-3" size="sm" variant="secondary" onclick={() => void ensureChat()}>Try again</Button>
        </div>
      {:else if taskAgentDock.tab === 'chat' && chat}
        {@const activeChat = chat}
        <TaskControlChat
          chat={activeChat}
          {agent}
          onSent={ensureChat}
          onRuntimeActiveChange={(active) => handleRuntimeActiveChange(active, activeChat)}
        />
       {:else if taskAgentDock.tab === 'work' && workScope}
         <div class="h-full overflow-y-auto p-4" data-testid="task-agent-work">
            {#key taskDockWorkKey(workScope, taskAgentDock.workCategory, taskAgentDock.workSessionId)}
             <WorkView
               scope={workScope}
               initialTab={taskAgentDock.workCategory}
               forceInitialTab
               sessionId={taskAgentDock.workSessionId ?? undefined}
               onClearSessionFilter={() => { taskAgentDock.openWork(workScope, taskAgentDock.workCategory === 'results' ? 'deliverables' : taskAgentDock.workCategory); }}
             />
           {/key}
         </div>
      {/if}
    </div>
    <span class="sr-only" aria-live="polite">Agent docked. Scoped to {task.title}.</span>
  </div>
{/if}

<style>
  .task-agent-fab { right: max(1rem, env(safe-area-inset-right)); bottom: calc(5rem + env(safe-area-inset-bottom)); }
  .task-agent-panel { inset-block: 1rem; right: max(1rem, env(safe-area-inset-right)); width: min(420px, 92vw); border-radius: 1rem; }
  .task-agent-fullscreen { inset: 0; width: 100%; border-radius: 0; }
  .task-agent-fullscreen .task-agent-header { padding-top: max(0.75rem, env(safe-area-inset-top)); }
  @media (max-width: 1023px) {
    .task-agent-panel:not(.task-agent-fullscreen) { inset: auto 0 0; width: 100%; height: min(88dvh, 760px); border-radius: 1rem 1rem 0 0; padding-bottom: env(safe-area-inset-bottom); }
  }
  :global(body.task-agent-dock-modal nav[aria-label='Primary']) { display: none; }
</style>
