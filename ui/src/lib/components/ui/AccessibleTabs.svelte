<script lang="ts">
  export interface AccessibleTab {
    id: string;
    label: string;
    count?: number;
    suffix?: string;
  }

  let {
    tabs,
    activeId,
    idPrefix,
    ariaLabel,
    onChange,
    sticky = false,
    edgeFade = true,
    testIdPrefix = 'tab',
  } = $props<{
    tabs: AccessibleTab[];
    activeId: string;
    idPrefix: string;
    ariaLabel: string;
    onChange: (id: string) => void;
    sticky?: boolean;
    edgeFade?: boolean;
    testIdPrefix?: string;
  }>();

  function selectAt(index: number): void {
    const tab = tabs[index];
    if (!tab) return;
    onChange(tab.id);
    requestAnimationFrame(() => document.getElementById(`${idPrefix}-tab-${tab.id}`)?.focus());
  }

  function onKeydown(event: KeyboardEvent, index: number): void {
    let next = index;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    else return;
    event.preventDefault();
    selectAt(next);
  }
</script>

<div class={`${sticky ? 'sticky top-0 z-20 bg-slate-950/95 backdrop-blur' : ''} relative border-b border-slate-800`}>
  <div class="tabs-scroll overflow-x-auto [scrollbar-width:none]" role="tablist" aria-label={ariaLabel}>
    <div class="flex min-w-max gap-1 pr-8">
      {#each tabs as tab, index (tab.id)}
        <button
          id={`${idPrefix}-tab-${tab.id}`}
          type="button"
          role="tab"
          aria-selected={activeId === tab.id}
          aria-controls={`${idPrefix}-panel-${tab.id}`}
          tabindex={activeId === tab.id ? 0 : -1}
          class={`inline-flex min-h-10 items-center gap-2 rounded-t-lg border-b-2 px-3 text-xs font-medium transition ${activeId === tab.id ? 'border-sky-300 bg-sky-500/10 text-sky-100' : 'border-transparent text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'}`}
          onclick={() => onChange(tab.id)}
          onkeydown={(event) => onKeydown(event, index)}
          data-testid={`${testIdPrefix}-${tab.id}`}
        >
          {tab.label}
          {#if tab.count !== undefined}
            <span class="rounded-full bg-slate-800 px-1.5 py-0.5 text-[10px] tabular-nums">{tab.count}</span>
          {/if}
          {#if tab.suffix}<span class="font-mono text-[10px]">{tab.suffix}</span>{/if}
        </button>
      {/each}
    </div>
  </div>
  {#if edgeFade}
    <div class="pointer-events-none absolute inset-y-0 right-0 w-8 bg-gradient-to-l from-slate-950 to-transparent" aria-hidden="true" data-testid="tabs-edge-fade"></div>
  {/if}
</div>

<style>
  .tabs-scroll::-webkit-scrollbar { display: none; width: 0; height: 0; }
</style>
