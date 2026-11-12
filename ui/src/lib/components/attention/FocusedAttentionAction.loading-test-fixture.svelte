<script lang="ts">
  import type { AttentionActionDetail } from '$lib/types/api';
  let { actionId } = $props<{
    actionId: string;
    onSettled: (action: AttentionActionDetail) => void | Promise<void>;
    onUnavailable?: (actionId: string) => void | Promise<void>;
    onDismiss?: () => void;
  }>();
  let draft = $state('');
</script>
<input aria-label={`Action draft ${actionId}`} bind:value={draft} />
