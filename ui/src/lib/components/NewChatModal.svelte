<script lang="ts">
  import { onMount } from 'svelte';

  import type { Agent } from '$lib/types/api';
  import AgentSelect from '$lib/components/AgentSelect.svelte';
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

<div class="app-viewport-overlay z-[90] flex items-center justify-center overflow-y-auto overscroll-contain bg-slate-950/80 px-4 py-6 backdrop-blur" role="presentation">
  <button class="absolute inset-0" onclick={() => !busy && oncancel()} type="button" aria-label="Close new chat dialog"></button>
  <div bind:this={container} class="relative z-10 max-h-full w-full max-w-lg overflow-y-auto rounded-3xl border border-slate-800 bg-slate-950 p-6 shadow-card overscroll-contain" role="dialog" aria-modal="true" aria-labelledby="new-chat-title">
    <p class="text-sm uppercase tracking-[0.25em] text-slate-400">Conversation setup</p>
    <h2 class="mt-3 text-xl font-semibold text-white" id="new-chat-title">{title}</h2>
    <p class="mt-3 text-sm leading-6 text-slate-300">{description}</p>

    {#if error}
      <p class="mt-4 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">{error}</p>
    {/if}

    <div class="mt-5">
      <AgentSelect
        label="Primary agent"
        agents={primaryAgents}
        value={selectedAgentId}
        onchange={(next) => { selectedAgentId = next; }}
        disabled={busy || primaryAgents.length === 0}
        emptyLabel="No active primary agents"
        placeholder="Select an agent"
      />
    </div>

    <div class="mt-6 flex flex-wrap justify-end gap-3">
      <Button variant="secondary" disabled={busy} onclick={oncancel}>Cancel</Button>
      <Button disabled={busy || !selectedAgentId} onclick={onconfirm}>{busy ? 'Creating...' : confirmLabel}</Button>
    </div>
  </div>
</div>
