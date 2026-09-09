<script lang="ts">
  import type { SlashCommandSuggestion } from '$lib/types/api';

  let {
    suggestions,
    selectedIndex,
    listboxId,
    onSelect,
  } = $props<{
    suggestions: SlashCommandSuggestion[];
    selectedIndex: number;
    listboxId: string;
    onSelect: (event: MouseEvent, index: number) => void;
  }>();
</script>

<div
  id={listboxId}
  class="mb-1 max-h-[40vh] overflow-y-auto overscroll-contain rounded-xl border border-slate-700 bg-slate-900/95 py-1 text-sm shadow-lg"
  role="listbox"
  aria-label="Slash command suggestions"
>
  {#each suggestions as suggestion, index}
    <button
      id={`${listboxId}-option-${index}`}
      class="flex w-full items-center gap-3 px-3 py-1.5 text-left text-xs transition {index === selectedIndex ? 'bg-slate-700/60 text-slate-100' : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'}"
      onmousedown={(event) => event.preventDefault()}
      onclick={(event) => onSelect(event, index)}
      type="button"
      role="option"
      aria-selected={index === selectedIndex}
    >
      <span class="min-w-0 font-mono font-medium text-sky-400">
        {suggestion.kind === 'parameter' ? suggestion.value : suggestion.command}
      </span>
      <span class="min-w-0 flex-1 truncate opacity-70">
        {#if suggestion.kind === 'parameter' && suggestion.label !== suggestion.value}
          {suggestion.label}
          {#if suggestion.description}<span class="opacity-60"> — {suggestion.description}</span>{/if}
        {:else}
          {suggestion.description}
        {/if}
      </span>
      {#if suggestion.badges.length > 0}
        <span class="flex shrink-0 gap-1">
          {#each suggestion.badges as badge}
            <span class="rounded-full border border-slate-600 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-slate-300">{badge}</span>
          {/each}
        </span>
      {/if}
    </button>
  {/each}
</div>
