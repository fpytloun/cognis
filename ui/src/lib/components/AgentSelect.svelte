<script lang="ts">
  import Check from 'lucide-svelte/icons/check';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';

  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import type { Agent } from '$lib/types/api';

  /**
   * Accessible agent picker that can render an avatar alongside each option.
   *
   * Native ``<option>`` cannot render HTML, so this component replaces the
   * native ``<select>`` with a button + listbox popover. It is intentionally
   * small: we only need single-select with avatars.
   */

  interface Props {
    agents: Agent[];
    value: string;
    onchange: (value: string) => void;
    allowAll?: boolean;
    allValue?: string;
    allLabel?: string;
    emptyLabel?: string;
    label?: string | null;
    disabled?: boolean;
    class?: string;
    placeholder?: string;
    id?: string;
  }

  let {
    agents,
    value,
    onchange,
    allowAll = false,
    allValue = 'all',
    allLabel = 'All agents',
    emptyLabel = 'No agents available',
    label = null,
    disabled = false,
    class: className = '',
    placeholder = 'Select an agent',
    id,
  }: Props = $props();

  let open = $state(false);
  let root = $state<HTMLDivElement | null>(null);
  let triggerId = $derived(id ?? `agent-select-${Math.random().toString(36).slice(2, 8)}`);

  const current = $derived(agents.find((agent) => agent.agent_id === value) ?? null);
  const hasSelection = $derived(current !== null || (allowAll && value === allValue));

  function toggle(): void {
    if (disabled) return;
    open = !open;
  }

  function selectValue(next: string): void {
    onchange(next);
    open = false;
  }

  function onKey(event: KeyboardEvent): void {
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      open = false;
    }
  }

  function onDocumentPointerDown(event: PointerEvent): void {
    if (!open) return;
    if (root && event.target instanceof Node && !root.contains(event.target)) {
      open = false;
    }
  }

  $effect(() => {
    if (!open) return;
    if (typeof document === 'undefined') return;
    document.addEventListener('pointerdown', onDocumentPointerDown, true);
    return () => document.removeEventListener('pointerdown', onDocumentPointerDown, true);
  });

  function agentLabel(agent: Agent): string {
    return agent.display_name ?? agent.name;
  }
</script>

<svelte:window onkeydown={onKey} />

<div bind:this={root} class={`relative ${className}`}>
  {#if label}
    <label class="mb-1 block text-xs font-medium uppercase tracking-widest text-slate-500" for={triggerId}>{label}</label>
  {/if}
  <button
    aria-expanded={open}
    aria-haspopup="listbox"
    class="flex w-full items-center justify-between gap-2 rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-left text-sm text-slate-100 transition hover:border-slate-600 disabled:cursor-not-allowed disabled:opacity-60"
    {disabled}
    id={triggerId}
    onclick={toggle}
    type="button"
  >
    <span class="flex min-w-0 flex-1 items-center gap-2">
      {#if current}
        <AgentAvatar name={agentLabel(current)} avatarUrl={current.avatar_url ?? null} class="h-6 w-6 rounded-lg text-[10px]" />
        <span class="truncate">{agentLabel(current)}</span>
      {:else if allowAll && value === allValue}
        <span class="truncate">{allLabel}</span>
      {:else}
        <span class="truncate text-slate-400">{agents.length === 0 ? emptyLabel : placeholder}</span>
      {/if}
    </span>
    <ChevronDown class="h-4 w-4 shrink-0 text-slate-400" />
  </button>

  {#if open}
    <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
    <ul
      role="listbox"
      aria-labelledby={triggerId}
      class="absolute left-0 right-0 z-50 mt-1 max-h-72 overflow-auto rounded-xl border border-slate-700 bg-slate-950 p-1 shadow-xl"
    >
      {#if allowAll}
        {@const selected = value === allValue}
        <li role="none">
          <button
            aria-selected={selected}
            role="option"
            class={`flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-sm transition ${selected ? 'bg-sky-500/15 text-white' : 'text-slate-200 hover:bg-slate-900'}`}
            onclick={() => selectValue(allValue)}
            type="button"
          >
            <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-slate-700 bg-slate-900 text-[11px] font-semibold text-slate-300">All</span>
            <span class="flex-1 truncate">{allLabel}</span>
            {#if selected}
              <Check class="h-4 w-4 text-sky-300" />
            {/if}
          </button>
        </li>
      {/if}

      {#if agents.length === 0 && !allowAll}
        <li class="px-2 py-2 text-sm text-slate-400" role="option" aria-disabled="true" aria-selected="false">{emptyLabel}</li>
      {/if}

      {#each agents as agent (agent.agent_id)}
        {@const selected = agent.agent_id === value}
        <li role="none">
          <button
            aria-selected={selected}
            role="option"
            class={`flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-sm transition ${selected ? 'bg-sky-500/15 text-white' : 'text-slate-200 hover:bg-slate-900'}`}
            onclick={() => selectValue(agent.agent_id)}
            type="button"
          >
            <AgentAvatar name={agentLabel(agent)} avatarUrl={agent.avatar_url ?? null} class="h-7 w-7 rounded-lg text-[11px]" />
            <span class="flex-1 truncate">{agentLabel(agent)}</span>
            {#if selected}
              <Check class="h-4 w-4 text-sky-300" />
            {/if}
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</div>
