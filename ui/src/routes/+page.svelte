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

<div class="mx-auto flex min-h-[100dvh] max-w-5xl items-center justify-center overflow-y-auto px-6 py-16" style="padding-left: max(1.5rem, env(safe-area-inset-left)); padding-right: max(1.5rem, env(safe-area-inset-right)); padding-top: calc(env(safe-area-inset-top) + 2rem); padding-bottom: calc(env(safe-area-inset-bottom) + 2rem);">
  <LoadingState label="Opening Cognis" description="Checking your session and routing you to the right workspace." />
</div>
