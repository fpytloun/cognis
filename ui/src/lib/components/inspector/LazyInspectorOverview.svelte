<script module lang="ts">
  import type InspectorOverview from '$lib/components/InspectorOverview.svelte';
  let request: Promise<typeof InspectorOverview> | null = null;
  function loadOverview(): Promise<typeof InspectorOverview> {
    return request ??= import('$lib/components/InspectorOverview.svelte')
      .then(module => module.default)
      .catch(error => { request = null; throw error; });
  }
</script>

<script lang="ts">
  import { onMount, type ComponentProps } from 'svelte';
  let props: ComponentProps<typeof InspectorOverview> = $props();
  let Overview = $state.raw<typeof InspectorOverview | null>(null);
  let error = $state(false);
  let mounted = false;
  async function load(): Promise<void> {
    error = false;
    try {
      const component = await loadOverview();
      if (mounted) Overview = component;
    } catch {
      if (mounted) error = true;
    }
  }
  onMount(() => {
    mounted = true;
    void load();
    return () => { mounted = false; };
  });
</script>

{#if Overview}
  <Overview {...props} />
{:else if error}
  <div role="alert" class="text-sm text-secondary">
    <p>Could not load the activity overview.</p>
    <button type="button" class="text-accent" onclick={load}>Try again</button>
  </div>
{:else}
  <p role="status" class="text-sm text-muted">Loading activity overview…</p>
{/if}
