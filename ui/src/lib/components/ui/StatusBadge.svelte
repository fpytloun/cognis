<script lang="ts">
  import Badge from './Badge.svelte';
  import { statusPresentation, type StatusPresentation } from '$lib/status';
  let { status, presentation: override = null }: { status: string; presentation?: StatusPresentation | null } = $props();
  const presentation = $derived(override ?? statusPresentation(status));
</script>
<Badge class={presentation.className}><span aria-hidden="true" class="mr-1">{presentation.icon}</span>{presentation.label}</Badge>
