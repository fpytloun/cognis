<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import LoadingState from '$lib/components/LoadingState.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { api } from '$lib/api/client';
  import type { Conversation } from '$lib/types/api';

  let loading = true;
  let conversations: Conversation[] = [];
  let error = '';

  onMount(() => {
    void (async () => {
      try {
        conversations = (await api.conversations.list()).items;
        const target = conversations[0]?.conversation_id;
        await goto(target ? `/chat/${target}` : '/chat/new', { replaceState: true });
      } catch (caughtError) {
        error = caughtError instanceof Error ? caughtError.message : 'Unable to load conversations.';
      } finally {
        loading = false;
      }
    })();
  });
</script>

<svelte:head>
  <title>Chat · Cognis</title>
</svelte:head>

{#if loading}
  <LoadingState label="Opening chat" description="Looking up your most recent conversation." />
{:else if error}
  <section class="rounded-3xl border border-rose-500/30 bg-rose-500/10 px-6 py-10 text-center text-sm text-rose-100">
    <p>{error}</p>
    <div class="mt-4">
      <Button onclick={() => goto('/chat/new')}>Start a new conversation</Button>
    </div>
  </section>
{/if}
