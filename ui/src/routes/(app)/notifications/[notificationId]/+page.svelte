<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import CredentialRequestForm from '$lib/components/CredentialRequestForm.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import type { Notification } from '$lib/types/api';

  let loading = $state(true);
  let error = $state('');
  let notification = $state<Notification | null>(null);

  async function loadNotification(): Promise<void> {
    loading = true;
    error = '';
    try {
      const notificationId = $page.params.notificationId;
      if (!notificationId) throw new Error('Notification ID is missing.');
      notification = await api.notifications.get(notificationId);
    } catch (caughtError) {
      error = asApiError(caughtError).message;
    } finally {
      loading = false;
    }
  }

  async function handleResolved(): Promise<void> {
    await loadNotification();
  }

  onMount(() => {
    void loadNotification();
  });
</script>

<svelte:head>
  <title>Credential Request · Cognis</title>
</svelte:head>

<div class="mx-auto flex min-h-full w-full max-w-2xl flex-col justify-center px-4 py-8">
  {#if loading}
    <LoadingState label="Loading credential request..." />
  {:else if error}
    <Card class="p-6">
      <p class="text-sm text-rose-200">{error}</p>
      <Button class="mt-4" variant="secondary" onclick={() => goto('/chat')}>Back to Cognis</Button>
    </Card>
  {:else if !notification}
    <Card class="p-6">
      <p class="text-sm text-slate-300">Credential request not found.</p>
    </Card>
  {:else if notification.notification_type !== 'credential_request'}
    <Card class="p-6">
      <p class="text-sm text-slate-300">This notification is not a credential request.</p>
      <Button class="mt-4" variant="secondary" onclick={() => goto('/chat')}>Back to Cognis</Button>
    </Card>
  {:else if notification.status !== 'pending'}
    <Card class="p-6">
      <p class="text-xs font-semibold uppercase tracking-[0.25em] text-emerald-300">Resolved</p>
      <h1 class="mt-2 text-xl font-semibold text-white">Credential request is no longer pending</h1>
      <p class="mt-2 text-sm text-slate-400">Decision: {String(notification.resolution?.decision ?? 'resolved')}</p>
      <Button class="mt-4" onclick={() => notification?.task_id ? goto(`/tasks/${notification.task_id}`) : goto('/chat')}>Continue</Button>
    </Card>
  {:else}
    <CredentialRequestForm notification={notification} onResolved={handleResolved} />
  {/if}
</div>
