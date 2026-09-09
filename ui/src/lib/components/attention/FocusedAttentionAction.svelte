<script lang="ts">
  import { onDestroy } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import { isAttentionActionDetailActionable } from '$lib/attention/actions';
  import { wsClient } from '$lib/ws/client';
  import type { AttentionActionDetail, AttentionActionResolvePayload } from '$lib/types/api';
  import AttentionActionRenderer from './AttentionActionRenderer.svelte';

  let {
    actionId,
    onSettled,
    onUnavailable,
    onDismiss,
  } = $props<{
    actionId: string;
    onSettled: (action: AttentionActionDetail) => void | Promise<void>;
    onUnavailable?: (actionId: string) => void | Promise<void>;
    onDismiss?: () => void;
  }>();

  let action = $state<AttentionActionDetail | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let error = $state<string | null>(null);
  let generation = 0;
  let submissionId: string | null = null;
  let unavailableTimer: ReturnType<typeof setTimeout> | null = null;
  let destroyed = false;

  function createSubmissionId(): string {
    return `attention-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  async function confirmUnavailable(message: string, requestGeneration: number): Promise<void> {
    error = message;
    await onUnavailable?.(actionId);
    if (destroyed || requestGeneration !== generation) return;
    if (onDismiss) {
      if (unavailableTimer) clearTimeout(unavailableTimer);
      unavailableTimer = setTimeout(onDismiss, 900);
    }
  }

  async function refresh(): Promise<void> {
    const currentId = actionId;
    const requestGeneration = ++generation;
    if (unavailableTimer) {
      clearTimeout(unavailableTimer);
      unavailableTimer = null;
    }
    try {
      const next = await api.attentionActions.get(currentId);
      if (requestGeneration !== generation || currentId !== actionId) return;
      action = next;
      error = null;
      if (['resolved', 'expired', 'orphaned'].includes(next.status)) {
        await onSettled(next);
      } else if (!isAttentionActionDetailActionable(next)) {
        await confirmUnavailable('This action is no longer available.', requestGeneration);
      }
    } catch (caught) {
      if (requestGeneration !== generation || currentId !== actionId) return;
      const apiError = asApiError(caught);
      if (apiError.status === 404) {
        await confirmUnavailable('This action is no longer available.', requestGeneration);
      } else {
        error = apiError.message;
      }
    } finally {
      if (requestGeneration === generation) loading = false;
    }
  }

  async function submit(
    payload: Omit<AttentionActionResolvePayload, 'expected_revision' | 'submission_id'>
  ): Promise<void> {
    if (!action || busy) return;
    const currentId = action.action_id;
    const requestGeneration = generation;
    busy = true;
    error = null;
    submissionId ??= createSubmissionId();
    try {
      const response = await api.attentionActions.resolve(action.action_id, {
        ...payload,
        expected_revision: action.revision,
        submission_id: submissionId,
      });
      if (
        destroyed
        || requestGeneration !== generation
        || currentId !== actionId
      ) return;
      if (response.status === 'resolved') {
        await onSettled({ ...action, ...response, availability: 'resolved', can_resolve: false });
        return;
      }
      await refresh();
    } catch (caught) {
      if (
        destroyed
        || requestGeneration !== generation
        || currentId !== actionId
      ) return;
      const apiError = asApiError(caught);
      error = apiError.message;
      if ([404, 409].includes(apiError.status)) await refresh();
    } finally {
      if (!destroyed && requestGeneration === generation && currentId === actionId) {
        busy = false;
      }
    }
  }

  $effect(() => {
    action = null;
    loading = true;
    error = null;
    submissionId = null;
    void refresh();
  });

  const unsubscribe = wsClient.subscribe((event) => {
    const candidate = event as typeof event & {
      notification_id?: string;
      conversation_id?: string;
      task_id?: string;
      reason?: string;
    };
    if (
      candidate.notification_id === actionId
      || (
        candidate.type === 'scope_invalidated'
        && candidate.reason === 'notification_state_changed'
        && action
        && (
          candidate.conversation_id === action.source.conversation_id
          || candidate.task_id === action.source.task_id
        )
      )
    ) void refresh();
  });

  onDestroy(() => {
    destroyed = true;
    generation += 1;
    if (unavailableTimer) clearTimeout(unavailableTimer);
    unsubscribe();
  });
</script>

{#if loading && !action}
  <p class="p-5 text-sm text-slate-400" role="status">Loading request…</p>
{:else if action}
  <AttentionActionRenderer {action} {busy} {error} onSubmit={submit} {onDismiss} />
{:else}
  <div class="p-5">
    <p class="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-100" role="alert">{error ?? 'This request is unavailable.'}</p>
    <button
      type="button"
      class="mt-3 rounded-xl border border-slate-600 px-3 py-2 text-sm"
      onclick={() => void refresh()}
      onpointerdown={(event) => event.stopPropagation()}
    >Retry</button>
    {#if onDismiss}
      <button
        type="button"
        class="ml-2 mt-3 min-h-11 rounded-xl border border-slate-600 px-3 py-2 text-sm"
        onclick={onDismiss}
      >Close</button>
    {/if}
  </div>
{/if}
