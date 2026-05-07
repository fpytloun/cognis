<script lang="ts">
  import ArrowDown from 'lucide-svelte/icons/arrow-down';
  import ArrowUp from 'lucide-svelte/icons/arrow-up';
  import Search from 'lucide-svelte/icons/search';
  import X from 'lucide-svelte/icons/x';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { resultLabel, resultSnippet, type ChatSearchResult } from '$lib/chat-search';

  let {
    query = $bindable(''),
    results = [],
    selectedIndex = 0,
    loading = false,
    disabled = false,
    onSubmit,
    onClose,
    onNext,
    onPrevious,
    onSelect
  } = $props<{
    query: string;
    results?: ChatSearchResult[];
    selectedIndex?: number;
    loading?: boolean;
    disabled?: boolean;
    onSubmit: () => void;
    onClose: () => void;
    onNext: () => void;
    onPrevious: () => void;
    onSelect: (index: number) => void;
  }>();
</script>

<form
  class="border-b border-slate-800/80 bg-slate-950/70 px-2.5 py-2 sm:px-5"
  onsubmit={(event) => { event.preventDefault(); onSubmit(); }}
>
  <div class="flex items-center gap-2">
    <div class="relative min-w-0 flex-1">
      <Search class="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
      <Input bind:value={query} class="pl-9" placeholder="Search this conversation" disabled={disabled} />
    </div>
    <span class="hidden text-xs text-slate-500 sm:inline">
      {#if loading}
        Searching...
      {:else}
        {results.length ? `${selectedIndex + 1}/${results.length}` : '0 results'}
      {/if}
    </span>
    <Button size="sm" variant="secondary" type="button" disabled={results.length === 0} onclick={onPrevious} aria-label="Previous match">
      <ArrowUp class="h-4 w-4" />
    </Button>
    <Button size="sm" variant="secondary" type="button" disabled={results.length === 0} onclick={onNext} aria-label="Next match">
      <ArrowDown class="h-4 w-4" />
    </Button>
    <Button size="sm" variant="secondary" type="button" onclick={onClose} aria-label="Close search">
      <X class="h-4 w-4" />
    </Button>
  </div>
  {#if results.length > 0}
    <div class="mt-2 flex gap-2 overflow-x-auto pb-1">
      {#each results as result, index}
        <button
          type="button"
          class={`max-w-[18rem] shrink-0 rounded-xl border px-3 py-2 text-left text-xs transition ${index === selectedIndex ? 'border-sky-400/60 bg-sky-500/15 text-sky-50' : 'border-slate-800 bg-slate-900/70 text-slate-300 hover:border-slate-700'}`}
          onclick={() => onSelect(index)}
        >
          <span class="block font-semibold uppercase tracking-widest text-[10px] text-slate-500">{resultLabel(result)}</span>
          <span class="line-clamp-2 leading-5">{resultSnippet(result)}</span>
        </button>
      {/each}
    </div>
  {/if}
</form>
