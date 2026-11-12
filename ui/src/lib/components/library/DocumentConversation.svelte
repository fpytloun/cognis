<script lang="ts">
  import { api } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import { wsClient, wsState } from '$lib/ws/client';
  import { conversationActivityState } from '$lib/conversation-activity';
  import { dashboardConversationPatch, dashboardRealtimeLoads, type DashboardConversationPatch } from '$lib/dashboard/realtime';
  import type { Agent, BackgroundWorkItem, Conversation } from '$lib/types/api';
  import type { LibraryItem } from '$lib/library';
  import ActivityAvatar from '$lib/components/ActivityAvatar.svelte';
  import ConversationLink from '$lib/components/ConversationLink.svelte';
  import Button from '$lib/components/ui/Button.svelte';

  let { item }: { item: LibraryItem } = $props();
  let conversation = $state<Conversation | null>(null);
  let agent = $state<Agent | null>(null);
  let background = $state<BackgroundWorkItem[]>([]);
  let error = $state('');
  let retry = $state(0);
  const activity = $derived(conversationActivityState(conversation, { backgroundWork: background }));

  $effect(() => {
    const id = item.conversation_id;
    const actor = $auth.status === 'authenticated' ? $auth.user?.email : null;
    void retry;
    let disposed = false;
    let generation = 0;
    let patches: Partial<DashboardConversationPatch> = {};
    conversation = null; agent = null; background = []; error = '';
    if (!id || !actor) return;
    async function refresh() {
      const current = ++generation;
      try {
        const [detail, work] = await Promise.all([
          api.conversations.detail(id!, { includeState: false }), api.conversations.backgroundWork(),
        ]);
        if (disposed || current !== generation) return;
        const merged = { ...detail, ...patches };
        const identity = await api.agents.detail(merged.agent_id);
        if (disposed || current !== generation) return;
        conversation = { ...detail, ...patches }; agent = identity; background = work.background_work.items; error = '';
        patches = {};
      } catch (caught) {
        if (disposed || current !== generation) return;
        conversation = null; agent = null; background = [];
        error = caught instanceof Error ? caught.message : 'Could not load conversation activity.';
      }
    }
    void refresh();
    const unsubscribe = wsClient.subscribe(event => {
      const target = 'conversation_id' in event ? event.conversation_id : null;
      if ((!target || target === id) && (
        dashboardRealtimeLoads(event.type, 'reason' in event ? event.reason : undefined).includes('conversations')
        || event.type === 'work_invalidated'
      )) void refresh();
      const patch = dashboardConversationPatch(event);
      if (patch?.conversation_id === id) {
        patches = { ...patches, ...patch };
        if (conversation) conversation = { ...conversation, ...patch };
      }
    });
    let connected: boolean | null = null;
    const stopSocket = wsState.subscribe(state => {
      if (state.status === 'connected' && connected === false) void refresh();
      connected = state.status === 'connected';
    });
    const visible = () => { if (document.visibilityState === 'visible') void refresh(); };
    window.addEventListener('focus', visible);
    document.addEventListener('visibilitychange', visible);
    return () => {
      disposed = true; unsubscribe(); stopSocket();
      window.removeEventListener('focus', visible);
      document.removeEventListener('visibilitychange', visible);
    };
  });
</script>

{#if item.conversation_id}
  <div class="space-y-2">
    <ConversationLink conversationId={item.conversation_id} class="flex min-w-0 items-center gap-3 rounded-lg p-1 text-accent hover:bg-surface-overlay">
      {#if conversation && agent}
        <ActivityAvatar name={agent.display_name || agent.name} avatarUrl={agent.avatar_url} state={activity} class="h-8 w-8" draggable={false} />
      {/if}
      <span class="min-w-0">
        <span class="block truncate text-sm font-medium">{conversation?.title || item.conversation_title || 'Open conversation'}</span>
        {#if conversation && agent}
          <span class="block truncate text-xs text-muted">{agent.display_name || agent.name}</span>
          <span class="block text-xs text-muted">{activity.label}</span>
        {/if}
      </span>
    </ConversationLink>
    {#if error}
      <p class="text-xs text-danger" role="alert">{error}</p>
      <Button variant="link" size="sm" onclick={() => retry++}>Retry conversation activity</Button>
    {:else if !conversation}
      <p class="text-xs text-muted">Loading conversation activity…</p>
    {/if}
  </div>
{/if}
