<script lang="ts" generics="T">
  import { onMount, type Snippet } from 'svelte';

  let { load, children, label = 'Loading view…', onCancel, overlay = false }: {
    load: () => Promise<T>;
    children: Snippet<[T]>;
    label?: string;
    onCancel?: () => void;
    overlay?: boolean;
  } = $props();
  let value = $state.raw<T | undefined>(undefined);
  let failed = $state(false);
  let loading = $state(false);
  let mounted = false;

  async function retry(): Promise<void> {
    if (loading) return;
    loading = true;
    failed = false;
    try {
      const result = await load();
      if (mounted) value = result;
    } catch {
      if (mounted) failed = true;
    } finally {
      if (mounted) loading = false;
    }
  }
  onMount(() => {
    mounted = true;
    void retry();
    return () => { mounted = false; };
  });
  function keydown(event: KeyboardEvent): void {
    if (value === undefined && onCancel && event.key === 'Escape' && !event.defaultPrevented) {
      event.preventDefault();
      onCancel();
    }
  }
</script>

<svelte:window onkeydown={keydown} />
{#if value !== undefined}
  {@render children(value)}
{:else}
  <div class={overlay
    ? 'fixed inset-0 z-50 grid place-content-center gap-3 bg-base/90 p-3 text-sm text-muted'
    : 'flex items-center gap-3 p-3 text-sm text-muted'}>
    {#if failed}
      <span role="alert">Could not load this view.</span>
      <button type="button" class="underline" onclick={() => void retry()}>Retry</button>
    {:else}
      <span role="status">{label}</span>
    {/if}
    {#if onCancel}<button type="button" class="underline" onclick={onCancel}>Close</button>{/if}
  </div>
{/if}
