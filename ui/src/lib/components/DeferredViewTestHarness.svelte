<script lang="ts">
  import DeferredView from './DeferredView.svelte';
  let { load, message = 'Ready', onCancel }: {
    load: () => Promise<{ ready: boolean }>;
    message?: string;
    onCancel?: () => void;
  } = $props();
</script>

<DeferredView {load} {onCancel}>
  {#snippet children(value)}
    <p>{value.ready ? message : 'Not ready'}</p>
  {/snippet}
</DeferredView>
