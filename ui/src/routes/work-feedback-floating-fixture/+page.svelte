<script lang="ts">
  import { dev } from '$app/environment';
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth';
  let Fixture = $state<typeof import('$lib/components/feedback/FeedbackFloatingFixture.svelte').default>();
  onMount(() => {
    if (import.meta.env.DEV && dev) void (async () => {
      await auth.bootstrap();
      Fixture = (await import('$lib/components/feedback/FeedbackFloatingFixture.svelte')).default;
    })();
  });
</script>
{#if dev && Fixture}<Fixture />{:else}<p>Development-only floating feedback fixture.</p>{/if}
