<script lang="ts">
  import { tick, untrack } from 'svelte';
  import ChatSearchBar from '$lib/components/ChatSearchBar.svelte';
  import { findLocalChatMatches, type ChatSearchResult, type SearchTimelineItem } from '$lib/chat-search';

  let {
    scopeKey, items, onSelect,
    query = $bindable(''), matchedIds = $bindable<Set<string>>(new Set()),
    selectedId = $bindable(''),
  } = $props<{
    scopeKey: string;
    items: SearchTimelineItem[];
    onSelect: (id: string) => void | Promise<void>;
    query?: string;
    matchedIds?: Set<string>;
    selectedId?: string;
  }>();

  let open = $state(false);
  let input = $state<HTMLInputElement | null>(null);
  let opener: HTMLElement | null = null;
  const emptyMatches = new Set<string>();
  const results = $derived<ChatSearchResult[]>(findLocalChatMatches(items, query).map(local => ({
    source: 'local', local, targetId: local.id,
  })));
  const currentIndex = $derived(Math.max(0, results.findIndex(result => result.targetId === selectedId)));
  // Snapshot refreshes must not reset an unchanged search scope.
  const identity = $derived(scopeKey);

  $effect(() => {
    matchedIds = results.length ? new Set(results.map(result => result.targetId)) : emptyMatches;
    selectedId = results[currentIndex]?.targetId ?? '';
  });
  $effect(() => {
    if (!identity) return;
    untrack(() => close(false));
  });

  export async function openSearch(returnFocusTo?: HTMLElement): Promise<HTMLInputElement | null> {
    if (!open) opener = returnFocusTo ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    open = true;
    await tick();
    input?.focus();
    return input;
  }

  function close(restoreFocus = true): void {
    const returnFocusTo = restoreFocus && open ? opener : null;
    open = false;
    query = '';
    selectedId = '';
    opener = null;
    void tick().then(() => { if (returnFocusTo?.isConnected) returnFocusTo.focus({ preventScroll: true }); });
  }

  function setQuery(value: string): void {
    query = value;
    selectedId = '';
  }

  async function select(index: number): Promise<void> {
    if (!results.length) return;
    const selectionScope = scopeKey;
    const targetId = results[(index + results.length) % results.length].targetId;
    selectedId = targetId;
    await tick();
    if (scopeKey === selectionScope && results.some(result => result.targetId === targetId)) await onSelect(targetId);
  }
</script>

{#if open}
  <div class="relative z-20 h-0 min-w-0 shrink-0">
  <ChatSearchBar bind:query={() => query, setQuery} bind:inputElement={input}
    placeholder="Search loaded subsession history"
    {results} selectedIndex={currentIndex} onSubmit={() => void select(0)}
    onPrevious={() => void select(currentIndex - 1)}
    onNext={() => void select(currentIndex + 1)}
    onClose={() => close()}
  />
  </div>
{/if}
