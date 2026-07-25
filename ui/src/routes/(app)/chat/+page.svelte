<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { api } from '$lib/api/client';
  import {
    CHAT_STORAGE_KEYS,
    dedupeLastOpenedConversationEntries,
    lastOpenedConversationEntry,
    lastOpenedConversationStorageKey,
    parseLastOpenedConversationEntry,
    serializeLastOpenedConversationEntry,
  } from '$lib/chat-page';
  import type { Agent, Conversation, LastOpenedConversationCandidate } from '$lib/types/api';

  let loading = true;
  let error = '';
  let noAgents = false;
  let loadingSlow = false;
  let loadingSlowTimer: ReturnType<typeof setTimeout> | null = null;

  function storedAgentIds(value: string | null): string[] {
    if (!value || value === 'all') return [];
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) {
        return parsed.filter((item): item is string => typeof item === 'string' && item.length > 0);
      }
    } catch {
      // Backwards-compatible single selected-agent storage.
    }
    return [value];
  }

  function getSelectedAgentId(agents: Agent[]): string {
    const primary = agents.filter((a) => a.agent_type === 'primary');
    if (typeof window !== 'undefined') {
      const stored = window.localStorage.getItem(CHAT_STORAGE_KEYS.selectedAgent);
      const selected = storedAgentIds(stored);
      if (selected.length === 1 && primary.some((a) => a.agent_id === selected[0] && a.status === 'active')) {
        return selected[0];
      }
    }
    return primary.find((a) => a.status === 'active')?.agent_id ?? primary[0]?.agent_id ?? '';
  }

  /**
   * Read the global last-opened entry from localStorage (survives PWA relaunches).
   * This is the conversation-first restore path: we prefer the last-opened
   * conversation's agent over the UI-selected agent so PWA cold-starts always
   * land on the genuinely last-active chat.
   */
  function globalLastOpenedEntry(): LastOpenedConversationCandidate | null {
    if (typeof window === 'undefined') return null;
    return parseLastOpenedConversationEntry(
      window.localStorage.getItem(CHAT_STORAGE_KEYS.lastOpenedConversation),
    );
  }

  function lastOpenedConversationCandidates(selectedAgentId: string): LastOpenedConversationCandidate[] {
    if (typeof window === 'undefined') return [];
    const agentKey = lastOpenedConversationStorageKey(selectedAgentId);
    return dedupeLastOpenedConversationEntries([
      window.sessionStorage.getItem(agentKey),
      window.localStorage.getItem(agentKey),
      window.sessionStorage.getItem(CHAT_STORAGE_KEYS.lastOpenedConversation),
      window.localStorage.getItem(CHAT_STORAGE_KEYS.lastOpenedConversation),
    ].map(parseLastOpenedConversationEntry).filter((value): value is LastOpenedConversationCandidate => value !== null));
  }

  function rememberOpenedConversation(conversation: Conversation): void {
    if (typeof window === 'undefined') return;
    const entry = serializeLastOpenedConversationEntry(lastOpenedConversationEntry(conversation));
    window.sessionStorage.setItem(CHAT_STORAGE_KEYS.lastOpenedConversation, entry);
    window.sessionStorage.setItem(lastOpenedConversationStorageKey(conversation.agent_id), entry);
    window.localStorage.setItem(CHAT_STORAGE_KEYS.lastOpenedConversation, entry);
    window.localStorage.setItem(lastOpenedConversationStorageKey(conversation.agent_id), entry);
  }

  function clearLoadingSlowTimer(): void {
    if (loadingSlowTimer !== null) {
      clearTimeout(loadingSlowTimer);
      loadingSlowTimer = null;
    }
  }

  function startLoadingSlowTimer(): void {
    clearLoadingSlowTimer();
    loadingSlow = false;
    loadingSlowTimer = setTimeout(() => {
      loadingSlowTimer = null;
      loadingSlow = true;
    }, 10_000);
  }

  async function openDefaultConversation(): Promise<void> {
    loading = true;
    error = '';
    noAgents = false;
    startLoadingSlowTimer();
    try {
      const agents = await api.agents.listAll();

      // Conversation-first restore: use the last-opened conversation's agent
      // as the primary agent for the open request. This ensures PWA cold-starts
      // (where sessionStorage is empty) always restore the genuinely last-active
      // chat rather than defaulting to the first primary agent's latest conversation.
      const globalEntry = globalLastOpenedEntry();
      const globalAgentId = globalEntry?.agent_id ?? null;
      const primary = agents.filter((a) => a.agent_type === 'primary');

      // Resolve the agent to request: prefer the last-opened conversation's
      // agent if it is still active, otherwise fall back to the UI selection.
      const resolvedAgentId = (
        globalAgentId && primary.some((a) => a.agent_id === globalAgentId && a.status === 'active')
          ? globalAgentId
          : getSelectedAgentId(agents)
      );

      if (!resolvedAgentId) {
        noAgents = true;
        loading = false;
        return;
      }

      const candidates = lastOpenedConversationCandidates(resolvedAgentId);
      const conversation = await api.conversations.open({
        agent_id: resolvedAgentId,
        context_type: 'web',
        include_state: false,
        candidate_conversations: candidates,
        candidate_conversation_ids: candidates.map((candidate) => candidate.conversation_id),
      });
      rememberOpenedConversation(conversation);
      await goto(`/chat/${conversation.conversation_id}`, { replaceState: true });
    } catch (caughtError) {
      error = caughtError instanceof Error ? caughtError.message : 'Unable to load conversations.';
    } finally {
      loading = false;
      clearLoadingSlowTimer();
    }
  }

  onMount(() => {
    void openDefaultConversation();
    return () => {
      clearLoadingSlowTimer();
    };
  });
</script>

<svelte:head>
  <title>Chat · Cognis</title>
</svelte:head>

{#if loading}
  {#if loadingSlow}
    <section class="rounded-3xl border border-sky-500/30 bg-sky-500/10 px-6 py-10 text-center text-sm text-sky-100">
      <p class="font-medium">Chat is still opening.</p>
      <p class="mt-2 text-sky-50/80">Resolving your default conversation is taking longer than expected.</p>
      <div class="mt-4 flex justify-center gap-3">
        <Button variant="secondary" onclick={() => void openDefaultConversation()}>Retry</Button>
        <Button onclick={() => goto('/chat/new')}>Start a new conversation</Button>
      </div>
    </section>
  {:else}
    <LoadingState label="Opening chat" description="Resolving your default conversation." />
  {/if}
{:else if noAgents}
  <section class="rounded-3xl border border-sky-500/30 bg-sky-500/10 px-6 py-10 text-center text-sm text-sky-100">
    <p>Create an agent first before starting a conversation.</p>
    <div class="mt-4 flex justify-center gap-3">
      <Button onclick={() => goto('/agents')}>Open agents</Button>
    </div>
  </section>
{:else if error}
  <section class="rounded-3xl border border-rose-500/30 bg-rose-500/10 px-6 py-10 text-center text-sm text-rose-100">
    <p>{error}</p>
    <div class="mt-4 flex justify-center gap-3">
      <Button onclick={() => goto('/agents')}>Open agents</Button>
      <Button variant="secondary" onclick={() => goto('/chat/new')}>Start a new conversation</Button>
    </div>
  </section>
{/if}
