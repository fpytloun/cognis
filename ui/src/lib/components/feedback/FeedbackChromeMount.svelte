<script lang="ts" generics="P extends Record<string, unknown>">
  import DeferredPrivateView from '../ui/DeferredPrivateView.svelte';
  import type { DeferredDefinition } from '../ui/deferred-definition';
  let { definition, viewProps, guard, onClose }: {
    definition: DeferredDefinition<P>; viewProps: P; guard?: () => void; onClose?: () => void;
  } = $props();
</script>
<DeferredPrivateView {definition} {viewProps} label="feedback controls" {guard} {onClose} />
