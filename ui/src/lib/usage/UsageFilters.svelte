<script lang="ts">
  import type { UsageDimension, UsageQuery, UsageView } from './model';
  import { localDate } from './local-time';
  let { query, facets, onquery, onfacet }: { query: UsageQuery; facets?: UsageView['facets']; onquery: (query: UsageQuery) => void; onfacet?: (field: UsageDimension) => void } = $props();
  const dimensions: UsageDimension[] = ['provider', 'model', 'effort', 'agent', 'user', 'purpose', 'project'];
  const labels: Record<UsageDimension, string> = { provider: 'Provider', model: 'Model', effort: 'Effort', agent: 'Agent', user: 'User', purpose: 'Purpose', project: 'Project' };
  const advanced = $derived<UsageDimension[]>(['effort', 'agent', ...(query.mode !== 'personal' ? ['user' as const] : []), 'purpose', ...(query.projectScope ? [] : ['project' as const])]);
  const activeFilters = $derived(dimensions.filter((field) => Boolean(query.filters[field]) && !(field === 'project' && query.projectScope)));
  const advancedActive = $derived(advanced.filter((field) => Boolean(query.filters[field])).length);

  // Calendar days in the browser's time zone.
  const iso = localDate;
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  function preset(key: 'today' | '7d' | '30d' | 'month'): { start: string; end: string } {
    const today = new Date();
    const end = iso(today);
    if (key === 'today') return { start: end, end };
    if (key === 'month') return { start: `${end.slice(0, 8)}01`, end };
    const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - (key === '7d' ? 6 : 29));
    return { start: iso(start), end };
  }
  const presets = [
    { key: 'today', label: 'Today' }, { key: '7d', label: '7 days' }, { key: '30d', label: '30 days' }, { key: 'month', label: 'This month' }
  ] as const;
  const activePreset = $derived(presets.find((item) => {
    const range = preset(item.key);
    return range.start === query.filters.start && range.end === query.filters.end;
  })?.key ?? null);
  function update(field: keyof UsageQuery['filters'], value: string) { onquery({ ...query, filters: { ...query.filters, [field]: value } }); }
  function range(key: (typeof presets)[number]['key']) { onquery({ ...query, filters: { ...query.filters, ...preset(key) } }); }
</script>
{#snippet select(field: UsageDimension)}
  <label class="field"><span>{labels[field]}</span><select aria-label={`Filter ${field}`} value={query.filters[field]} onfocus={() => onfacet?.(field)} onchange={(event) => update(field, event.currentTarget.value)}>
    <option value="">All</option>
    {#if query.filters[field] && !facets?.[field]?.some((item) => item.value === query.filters[field])}<option value={query.filters[field]}>{query.filters[field]}</option>{/if}
    {#each facets?.[field] ?? [] as item}<option value={item.value}>{item.label}</option>{/each}
  </select></label>
{/snippet}
<section class="filters" aria-label="Usage filters">
  <div class="row">
    <div class="segmented" role="radiogroup" aria-label="Date range">
      {#each presets as item}
        <button type="button" role="radio" aria-checked={activePreset === item.key} onclick={() => range(item.key)}>{item.label}</button>
      {/each}
    </div>
    <div class="dates">
      <input type="date" aria-label="From date" value={query.filters.start} max={query.filters.end || undefined} onchange={(event) => update('start', event.currentTarget.value)}/>
      <span aria-hidden="true">–</span>
      <input type="date" aria-label="Through date" value={query.filters.end} min={query.filters.start || undefined} onchange={(event) => update('end', event.currentTarget.value)}/>
      <span class="tz" title={zone}>{zone.split('/').pop()?.replaceAll('_', ' ')}</span>
    </div>
    <div class="selects">
      {#if query.projectScope}<div class="field"><span>Project</span><span class="scope-fixed project-scope-value">{query.projectScope.label} · fixed scope</span></div>{/if}
      {@render select('provider')}{@render select('model')}
    </div>
    <details class="more">
      <summary>More filters{advancedActive ? ` (${advancedActive})` : ''}</summary>
      <div class="advanced">
        {#each advanced as field}{@render select(field)}{/each}
      </div>
    </details>
  </div>
  {#if activeFilters.length}
    <div class="chips" aria-label="Active usage filters">
      {#each activeFilters as field}
        <button type="button" aria-label={`Clear ${field} filter`} onclick={() => update(field, '')}><span>{labels[field]}: <strong>{query.filters[field]}</strong></span><span aria-hidden="true">×</span></button>
      {/each}
      {#if activeFilters.length > 1}<button type="button" class="clear" onclick={() => onquery({ ...query, filters: { ...query.filters, ...Object.fromEntries(activeFilters.map((field) => [field, ''])) } })}>Clear all</button>{/if}
    </div>
  {/if}
</section>
<style>
  .filters { margin: 0 0 14px; }
  .row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 14px; }
  .segmented { display: inline-flex; padding: 2px; border: 1px solid rgb(var(--color-control-border)); border-radius: 8px; background: rgb(var(--color-inset, var(--color-surface))); }
  .segmented button { min-height: 30px; padding: 2px 11px; border: 0; border-radius: 6px; background: transparent; color: rgb(var(--color-secondary)); font: inherit; font-size: 12px; font-weight: 550; cursor: pointer; white-space: nowrap; }
  .segmented button[aria-checked="true"] { background: rgb(var(--color-surface)); color: rgb(var(--color-foreground)); box-shadow: 0 1px 2px rgb(0 0 0 / .08); }
  .dates { display: flex; align-items: center; gap: 6px; color: rgb(var(--color-secondary)); font-size: 12px; }
  .tz { font-size: 10.5px; font-weight: 600; letter-spacing: .04em; }
  .selects { display: flex; gap: 10px; flex-wrap: wrap; }
  .field { display: flex; align-items: center; gap: 6px; font-size: 12px; color: rgb(var(--color-secondary)); min-width: 0; }
  input, select, .scope-fixed { min-height: 32px; min-width: 0; max-width: 220px; padding: 4px 8px; border: 1px solid rgb(var(--color-control-border)); border-radius: 7px; background: rgb(var(--color-surface)); color: rgb(var(--color-foreground)); font: inherit; font-size: 12px; }
  .scope-fixed { display: inline-flex; align-items: center; }
  .more { position: relative; }
  .more summary { min-height: 32px; display: flex; align-items: center; padding: 0 4px; font-size: 12px; font-weight: 550; color: rgb(var(--color-accent)); cursor: pointer; list-style: none; }
  .more summary::-webkit-details-marker { display: none; }
  .advanced { display: flex; flex-wrap: wrap; gap: 10px 14px; padding: 10px 0 2px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .chips button { display: inline-flex; align-items: center; gap: 10px; min-height: 28px; max-width: 100%; padding: 2px 10px; border: 1px solid rgb(var(--color-accent) / .25); border-radius: 999px; background: rgb(var(--color-accent) / .07); color: rgb(var(--color-accent)); font: inherit; font-size: 11.5px; cursor: pointer; }
  .chips button span:first-child { overflow-wrap: anywhere; }
  .chips .clear { border-color: transparent; background: transparent; color: rgb(var(--color-secondary)); }
  :is(input, select, summary, button):focus-visible { outline: 2px solid rgb(var(--color-accent)); outline-offset: 2px; }
  @media (max-width: 767px), (pointer: coarse) {
    .segmented button, input, select, .more summary, .chips button { min-height: 44px; }
    .row { align-items: stretch; }
    .segmented { width: 100%; } .segmented button { flex: 1; }
    .dates { width: 100%; } .dates input { flex: 1; max-width: none; }
    .selects, .advanced { width: 100%; } .field { flex: 1 1 140px; } .field select { flex: 1; max-width: none; }
  }
</style>
