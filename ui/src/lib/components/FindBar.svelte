<script lang="ts">
  import { on } from 'svelte/events';
  import { mountFindBar } from '$lib/find';
  import '$lib/theme/find-toolbar.css';
  import Search from 'lucide-svelte/icons/search';
  import ArrowUp from 'lucide-svelte/icons/arrow-up';
  import ArrowDown from 'lucide-svelte/icons/arrow-down';
  import X from 'lucide-svelte/icons/x';
  import Button from './ui/Button.svelte';
  import Input from './ui/Input.svelte';

  let {
    query = $bindable(''), inputElement = $bindable<HTMLInputElement | null>(null),
    label, count = '', error = '', disabled = false, navigationDisabled = false,
    onInput, onSubmit, onNext, onPrevious, onClose,
  }: {
    query: string; inputElement?: HTMLInputElement | null; label: string; count?: string; error?: string;
    disabled?: boolean; navigationDisabled?: boolean;
    onInput?: () => void; onSubmit: () => void; onNext: () => void;
    onPrevious: () => void; onClose: () => void;
  } = $props();

  function keys(node: HTMLFormElement) {
    return { destroy: on(node, 'keydown', event => {
      if (event.key === 'Escape') {
        event.preventDefault(); event.stopImmediatePropagation(); onClose();
      } else if ((event.metaKey !== event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'f') {
        event.preventDefault(); event.stopImmediatePropagation(); inputElement?.focus(); inputElement?.select();
      } else if (event.key === 'Enter' && event.target === inputElement && event.shiftKey) {
        event.preventDefault(); onPrevious();
      }
    }) };
  }
</script>

<form role="search" aria-label={label} class="find-bar" data-find-bar
  use:mountFindBar use:keys onsubmit={event => { event.preventDefault(); onSubmit(); }}>
  <div class="find-field">
    <Search size={16} class="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-quiet" />
    <Input bind:element={inputElement} bind:value={query} type="text" role="searchbox" aria-label={label}
      placeholder={label} class="min-w-0 pl-8 pr-24 py-1 h-11 min-h-11 md:h-8 md:min-h-8"
      {disabled} maxlength={4096} oninput={onInput} />
    <span role={error ? 'alert' : 'status'} class="find-count text-xs tabular-nums text-quiet" title={error || count}>{error ? 'Search failed' : count}</span>
  </div>
  <Button variant="ghost" size="icon" type="button" class="find-action" disabled={navigationDisabled} onclick={onPrevious} aria-label="Previous match" title="Previous match (Shift+Enter)"><ArrowUp size={16} /></Button>
  <Button variant="ghost" size="icon" type="button" class="find-action" disabled={navigationDisabled} onclick={onNext} aria-label="Next match" title="Next match (Enter)"><ArrowDown size={16} /></Button>
  <Button variant="ghost" size="icon" type="button" class="find-action" onclick={onClose} aria-label="Close search" title="Close search (Escape)"><X size={16} /></Button>
</form>

<style>
  .find-bar { display: flex; align-items: center; gap: .125rem; width: 22rem; max-width: 100%; min-width: 0; margin-left: auto; color: rgb(var(--color-foreground)); }
  .find-field { position: relative; flex: 1; min-width: 0; }
  .find-count { position: absolute; right: .5rem; top: 50%; transform: translateY(-50%); width: 5.5rem; text-align: right; white-space: nowrap; pointer-events: none; }
  .find-bar :global(.find-action) { width: 2rem; min-width: 2rem; height: 2rem; min-height: 2rem; padding: 0; }
  @media (pointer: coarse) {
    .find-bar :global(.find-action) { width: 2.75rem; min-width: 2.75rem; height: 2.75rem; min-height: 2.75rem; }
  }
  @media (max-width: 639px) { .find-bar { width: 100%; } }
  @media print { .find-bar { display: none; } }
</style>
