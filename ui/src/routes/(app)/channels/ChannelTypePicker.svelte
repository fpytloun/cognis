<script lang="ts">
  import MessagesSquare from 'lucide-svelte/icons/messages-square';

  import type { ChannelMeta } from '$lib/types/api';

  export let channelTypes: ChannelMeta[] = [];
  export let selectedType: ChannelMeta | null = null;
  export let onSelect: (meta: ChannelMeta) => void;
</script>

<div class="grid gap-3 sm:grid-cols-2">
  {#each channelTypes as meta (meta.channel_type)}
    <button
      class={`rounded-2xl border p-4 text-left transition ${selectedType?.channel_type === meta.channel_type ? 'border-emerald-400 bg-emerald-500/10' : 'border-slate-700 bg-slate-950/60 hover:border-slate-500'}`}
      onclick={() => onSelect(meta)}
      type="button"
      aria-label={`Select ${meta.label}`}
    >
      <div class="flex items-center gap-2 text-white">
        <MessagesSquare class="h-4 w-4 text-amber-300" />
        <span class="font-medium">{meta.label}</span>
      </div>
      <p class="mt-2 text-sm text-slate-400">{meta.description}</p>
    </button>
  {/each}
</div>
