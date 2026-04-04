<script lang="ts">
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import ImageLightbox from '$lib/components/ImageLightbox.svelte';
  import type { Agent } from '$lib/types/api';

  let { agent, onClose } = $props<{
    agent: Agent;
    onClose: () => void;
  }>();

  let showLightbox = $state(false);

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="fixed inset-0 z-40" onclick={onClose}></div>

<div class="absolute left-0 top-full z-50 mt-2 w-72 rounded-2xl border border-slate-700 bg-slate-900 p-4 shadow-2xl">
  <div class="flex items-start gap-3">
    {#if agent.avatar_url}
      <button type="button" class="shrink-0 cursor-pointer" onclick={() => { showLightbox = true; }}>
        <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={agent.avatar_url} class="h-14 w-14" />
      </button>
    {:else}
      <AgentAvatar name={agent.display_name ?? agent.name} avatarUrl={null} class="h-14 w-14 shrink-0" />
    {/if}
    <div class="min-w-0">
      <p class="truncate text-sm font-semibold text-slate-100">{agent.display_name ?? agent.name}</p>
      {#if agent.agent_type}
        <p class="text-xs text-slate-500">{agent.agent_type}</p>
      {/if}
    </div>
  </div>
  {#if agent.description}
    <p class="mt-3 text-sm leading-relaxed text-slate-300">{agent.description}</p>
  {/if}
</div>

{#if showLightbox && agent.avatar_url}
  <ImageLightbox src={agent.avatar_url} alt="{agent.name} avatar" onClose={() => { showLightbox = false; }} />
{/if}
