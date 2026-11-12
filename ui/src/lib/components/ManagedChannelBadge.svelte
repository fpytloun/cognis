<script lang="ts">
  import { managedChannelIdentity } from '$lib/managed-channel';
  import type { Conversation } from '$lib/types/api';

  let { conversation }: { conversation: Conversation } = $props();
  const identity = $derived(managedChannelIdentity(conversation));
</script>

{#if identity}
  <span class="block truncate text-xs font-medium text-accent" title={`${identity.channelLabel} channel · ${identity.participant}`} data-testid="managed-channel-identity">
    {identity.channelLabel} channel · {identity.participant}
  </span>
{/if}
