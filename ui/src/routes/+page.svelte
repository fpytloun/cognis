<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  import LoadingState from '$lib/components/LoadingState.svelte';
  import { auth } from '$lib/stores/auth';

  onMount(() => {
    void auth.bootstrap().then(async () => {
      const state = auth.getSnapshot();
      await goto(state.status === 'authenticated' ? '/chat' : '/login', { replaceState: true });
    });
  });
</script>

<svelte:head>
  <title>Cognis</title>
</svelte:head>

<div class="mx-auto flex min-h-screen max-w-5xl items-center justify-center px-6 py-16">
  <LoadingState label="Opening Cognis" description="Checking your session and routing you to the right workspace." />
</div>
