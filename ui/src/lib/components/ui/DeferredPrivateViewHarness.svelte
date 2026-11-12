<script lang="ts">
  import DeferredPrivateView from './DeferredPrivateView.svelte';
  import type { DeferredDefinition } from './deferred-definition';
  let { definition, source = 'first', visible = true, modal = false, onClose }: {
    definition: DeferredDefinition<{ name: string }>; source?: string; visible?: boolean; modal?: boolean; onClose?: () => void;
  } = $props();
</script>
{#if visible}<DeferredPrivateView {definition} viewProps={{ name: source }} label="test view" {modal} {onClose} />{/if}
