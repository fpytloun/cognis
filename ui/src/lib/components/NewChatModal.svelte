<script lang="ts">
  import { onMount } from 'svelte';

  import type { Agent } from '$lib/types/api';
  import Button from '$lib/components/ui/Button.svelte';

  let {
    agents,
    selectedAgentId = $bindable(''),
    title = 'New chat',
    description = 'Choose the primary agent for the new conversation.',
    confirmLabel = 'Create conversation',
    busy = false,
    error = '',
    oncancel,
    onconfirm,
  } = $props<{
    agents: Agent[];
    selectedAgentId?: string;
    title?: string;
    description?: string;
    confirmLabel?: string;
    busy?: boolean;
    error?: string;
    oncancel: () => void;
    onconfirm: () => void;
  }>();

  let container: HTMLDivElement | null = null;
  let previousFocus: HTMLElement | null = null;

  const primaryAgents = $derived(agents.filter((agent: Agent) => agent.agent_type === 'primary' && agent.status === 'active'));

  function focusableElements(): HTMLElement[] {
    if (!container) return [];
    return Array.from(
      container.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
    ).filter((element) => !element.hasAttribute('disabled'));
  }

  function trapFocus(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault();
      if (!busy) oncancel();
      return;
    }
    if (event.key !== 'Tab') return;
    const elements = focusableElements();
    if (elements.length === 0) return;
    const first = elements[0];
    const last = elements[elements.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  onMount(() => {
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    queueMicrotask(() => {
      focusableElements()[0]?.focus();
    });
    document.addEventListener('keydown', trapFocus);
    return () => {
      document.removeEventListener('keydown', trapFocus);
      queueMicrotask(() => previousFocus?.focus());
    };
  });
</script>

<div class="fixed inset-0 z-[90] flex items-center justify-center bg-slate-950/80 px-4 py-6 backdrop-blur" role="presentation">
  <button class="absolute inset-0" onclick={() => !busy && oncancel()} type="button" aria-label="Close new chat dialog"></button>
  <div bind:this={container} class="relative z-10 w-full max-w-lg rounded-3xl border border-slate-800 bg-slate-950 p-6 shadow-card" role="dialog" aria-modal="true" aria-labelledby="new-chat-title">
    <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Conversation setup</p>
    <h2 class="mt-3 text-xl font-semibold text-white" id="new-chat-title">{title}</h2>
    <p class="mt-3 text-sm leading-6 text-slate-300">{description}</p>

    {#if error}
      <p class="mt-4 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <label class="mt-5 block space-y-2">
      <span class="text-xs font-medium uppercase tracking-widest text-slate-500">Primary agent</span>
      <select
        bind:value={selectedAgentId}
        class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100"
        disabled={busy || primaryAgents.length === 0}
      >
        {#if primaryAgents.length === 0}
          <option value="">No active primary agents</option>
        {/if}
        {#each primaryAgents as agent}
          <option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>
        {/each}
      </select>
    </label>

    <div class="mt-6 flex flex-wrap justify-end gap-3">
      <Button variant="secondary" disabled={busy} onclick={oncancel}>Cancel</Button>
      <Button disabled={busy || !selectedAgentId} onclick={onconfirm}>{busy ? 'Creating...' : confirmLabel}</Button>
    </div>
  </div>
</div>
