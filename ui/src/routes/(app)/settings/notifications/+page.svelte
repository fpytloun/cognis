<script lang="ts">
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  onMount(() => {
    void goto('/settings?tab=notifications', { replaceState: true });
  });
</script>

<svelte:head>
  <title>Settings · notifications · Cognis</title>
  <meta http-equiv="refresh" content="0; url=/settings?tab=notifications" />
</svelte:head>

<div class="flex min-h-[40vh] items-center justify-center p-8 text-sm text-slate-400">
  Opening settings...
</div>
