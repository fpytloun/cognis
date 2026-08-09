<script lang="ts">
  import { onDestroy, onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { confirmAction } from '$lib/stores/confirm';
  import type {
    KnowledgebaseModel,
    KnowledgebaseShareCandidate,
    KnowledgebaseShareModel
  } from '$lib/types/api';

  let { kb, disabled = false }: { kb: KnowledgebaseModel; disabled?: boolean } = $props();

  let shares = $state<KnowledgebaseShareModel[]>([]);
  let candidates = $state<KnowledgebaseShareCandidate[]>([]);
  let query = $state('');
  let loadingShares = $state(true);
  let searching = $state(false);
  let error = $state('');
  let pendingEmails = $state(new Set<string>());
  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let searchGeneration = 0;
  let searchController: AbortController | null = null;

  async function loadShares(): Promise<void> {
    loadingShares = true;
    error = '';
    try {
      shares = await api.knowledgebases.shares(kb.knowledgebase_id);
    } catch (err) {
      error = asApiError(err).message;
    } finally {
      loadingShares = false;
    }
  }

  onMount(loadShares);
  onDestroy(() => {
    if (searchTimer) clearTimeout(searchTimer);
    searchGeneration += 1;
    searchController?.abort();
  });

  function scheduleSearch(): void {
    if (searchTimer) clearTimeout(searchTimer);
    searchGeneration += 1;
    searchController?.abort();
    searchController = null;
    const trimmed = query.trim();
    if (trimmed.length < 2 || disabled) {
      candidates = [];
      searching = false;
      error = '';
      return;
    }
    const generation = searchGeneration;
    const controller = new AbortController();
    searchController = controller;
    searching = true;
    searchTimer = setTimeout(async () => {
      try {
        const result = await api.knowledgebases.shareCandidates(
          kb.knowledgebase_id,
          trimmed,
          { signal: controller.signal }
        );
        if (generation === searchGeneration) candidates = result;
      } catch (err) {
        if (generation === searchGeneration && (err as { name?: string }).name !== 'AbortError') {
          error = asApiError(err).message;
        }
      } finally {
        if (generation === searchGeneration) {
          searching = false;
          searchController = null;
        }
      }
    }, 250);
  }

  function setPending(email: string, pending: boolean): void {
    const next = new Set(pendingEmails);
    if (pending) next.add(email);
    else next.delete(email);
    pendingEmails = next;
  }

  async function grant(candidate: KnowledgebaseShareCandidate): Promise<void> {
    setPending(candidate.email, true);
    error = '';
    try {
      const share = await api.knowledgebases.grantShare(kb.knowledgebase_id, {
        user_email: candidate.email,
        permission: 'view'
      });
      shares = [...shares.filter((item) => item.user_email !== share.user_email), share];
      candidates = candidates.filter((item) => item.email !== candidate.email);
    } catch (err) {
      error = asApiError(err).message;
    } finally {
      setPending(candidate.email, false);
    }
  }

  async function revoke(share: KnowledgebaseShareModel): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Revoke shared access',
      message: `${share.user_name ?? share.user_email} will immediately lose read and query access.`,
      confirmLabel: 'Revoke',
      variant: 'danger'
    });
    if (!confirmed) return;
    setPending(share.user_email, true);
    error = '';
    try {
      await api.knowledgebases.revokeShare(kb.knowledgebase_id, share.user_email);
      shares = shares.filter((item) => item.user_email !== share.user_email);
    } catch (err) {
      error = asApiError(err).message;
    } finally {
      setPending(share.user_email, false);
    }
  }
</script>

<section class="flex flex-col gap-4" aria-labelledby="people-access-heading">
  <div>
    <h3 id="people-access-heading" class="text-sm font-semibold text-white">People</h3>
    <p class="mt-1 text-sm text-slate-400">
      Shared people can browse documents and use Search or Ask. They cannot upload, edit,
      reindex, manage jobs, change settings, assign agents, share onward, archive, or delete.
    </p>
  </div>

  {#if disabled}
    <p class="rounded-xl border border-amber-800/60 bg-amber-950/30 px-3 py-2 text-sm text-amber-200">
      Sharing changes are unavailable while this knowledgebase is archived.
    </p>
  {:else}
    <label class="flex flex-col gap-1 text-sm text-slate-300">
      Find a Cognis user
      <Input bind:value={query} oninput={scheduleSearch} placeholder="Type at least 2 characters…" data-testid="knowledge-share-search" />
    </label>
    {#if query.trim().length > 0 && query.trim().length < 2}
      <p class="text-xs text-slate-500">Enter at least 2 characters to search.</p>
    {:else if searching}
      <p class="text-sm text-slate-400" aria-live="polite">Searching users…</p>
    {:else if query.trim().length >= 2 && candidates.length === 0}
      <p class="text-sm text-slate-400">No eligible users found.</p>
    {:else if candidates.length > 0}
      <ul class="flex flex-col gap-2" data-testid="knowledge-share-candidates">
        {#each candidates as candidate (candidate.email)}
          <li class="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900/60 px-4 py-3">
            <div><p class="text-sm font-medium text-white">{candidate.name ?? candidate.email}</p><p class="text-xs text-slate-500">{candidate.email}</p></div>
            <Button
              size="sm"
              aria-label={`Grant access to ${candidate.name ?? candidate.email}`}
              disabled={pendingEmails.has(candidate.email)}
              onclick={() => grant(candidate)}
            >Grant access</Button>
          </li>
        {/each}
      </ul>
    {/if}
  {/if}

  {#if error}<div role="alert" class="rounded-xl border border-rose-800/60 bg-rose-950/30 px-3 py-2 text-sm text-rose-300">{error}</div>{/if}

  <div>
    <h4 class="text-sm font-medium text-slate-300">Current shares</h4>
    {#if loadingShares}
      <p class="mt-2 text-sm text-slate-400">Loading shared access…</p>
    {:else if shares.length === 0}
      <p class="mt-2 rounded-xl border border-dashed border-slate-800 px-4 py-5 text-center text-sm text-slate-400">Not shared with anyone yet.</p>
    {:else}
      <ul class="mt-2 flex flex-col gap-2">
        {#each shares as share (share.grant_id)}
          <li class="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900/60 px-4 py-3">
            <div><p class="text-sm font-medium text-white">{share.user_name ?? share.user_email}</p><p class="text-xs text-slate-500">{share.user_email} · Read/query</p></div>
            <Button
              size="sm"
              variant="ghost"
              aria-label={`Revoke access from ${share.user_name ?? share.user_email}`}
              disabled={disabled || pendingEmails.has(share.user_email)}
              onclick={() => revoke(share)}
            >Revoke</Button>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</section>
