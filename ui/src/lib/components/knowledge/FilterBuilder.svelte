<script lang="ts">
  import { onDestroy } from 'svelte';
  import Plus from 'lucide-svelte/icons/plus';
  import X from 'lucide-svelte/icons/x';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { api } from '$lib/api/client';
  import { coerceFilterValue, fieldOptionsFromSchema, operatorsForField, type MetadataFieldOption } from '$lib/knowledge/filters';
  import type { KnowledgebaseFacetValue, KnowledgebaseFilter, KnowledgebaseFilterOp } from '$lib/types/api';

  let { knowledgebaseId, metadataSchema, filters, revision, onChange }: {
    knowledgebaseId: string; metadataSchema: Record<string, unknown>; filters: KnowledgebaseFilter[];
    revision: string; onChange: (filters: KnowledgebaseFilter[]) => void;
  } = $props();
  const fieldOptions: MetadataFieldOption[] = $derived(fieldOptionsFromSchema(metadataSchema));
  interface DraftRow { id: string; field: string; op: KnowledgebaseFilterOp; raw: string; selected: string[]; }
  let nextId = 0;
  const fromFilter = (filter: KnowledgebaseFilter): DraftRow => ({
    id: `filter-${nextId++}`, field: filter.field, op: filter.op,
    raw: Array.isArray(filter.value) ? filter.value.join(', ') : String(filter.value ?? ''),
    selected: Array.isArray(filter.value) ? filter.value.map(String) : []
  });
  let drafts = $state<DraftRow[]>([]);
  let hydratedRevision = $state('');
  let facets = $state<Record<string, KnowledgebaseFacetValue[]>>({});
  let facetController: AbortController | null = null;
  onDestroy(() => facetController?.abort());
  $effect(() => {
    if (revision !== hydratedRevision) { hydratedRevision = revision; drafts = filters.map(fromFilter); }
  });
  function option(field: string) { return fieldOptions.find((item) => item.field === field); }
  function update(id: string, patch: Partial<DraftRow>) { drafts = drafts.map((row) => row.id === id ? { ...row, ...patch } : row); }
  function updateOperator(id: string, value: string) {
    const row = drafts.find((item) => item.id === id);
    if (!row) return;
    const op = value as KnowledgebaseFilterOp;
    if (op === 'in' || op === 'overlap') {
      update(id, { op, selected: row.selected.length ? row.selected : row.raw.split(',').map((part) => part.trim()).filter(Boolean) });
      return;
    }
    update(id, { op, raw: row.selected[0] ?? row.raw.split(',')[0]?.trim() ?? '', selected: [] });
  }
  function addRow() {
    const first = fieldOptions[0];
    drafts = [...drafts, { id: `filter-${nextId++}`, field: first?.field ?? '', op: operatorsForField(first?.schema)[0] ?? 'eq', raw: '', selected: [] }];
  }
  async function loadFacets(field: string) {
    if (!field || facets[field]) return;
    facetController?.abort(); const controller = new AbortController(); facetController = controller;
    try {
      const response = await api.knowledgebases.facets(
        knowledgebaseId,
        { fields: [field], filters, limit_per_field: 50 },
        { signal: controller.signal }
      );
      if (!controller.signal.aborted) facets = { ...facets, [field]: response.fields[0]?.values ?? [] };
    } catch { /* free-text remains available */ }
  }
  function values(row: DraftRow): unknown[] {
    const schema = option(row.field)?.schema;
    return schema?.enum ?? schema?.items?.enum ?? facets[row.field]?.map((item) => item.value) ?? [];
  }
  function apply() {
    const next = drafts.flatMap((row) => {
      const type = String(option(row.field)?.schema.type ?? 'string');
      const choiceEditor = values(row).length > 0 && (row.op === 'in' || row.op === 'overlap');
      const value = choiceEditor && row.selected.length
        ? row.selected.map((item) => coerceFilterValue('eq', item, type))
        : coerceFilterValue(row.op, row.raw, type);
      const complete = row.field && (row.selected.length > 0 || row.raw.trim() !== '');
      return complete ? [{ field: row.field, op: row.op, value }] : [];
    });
    facets = {};
    onChange(next);
  }
  function clear() { drafts = []; facets = {}; onChange([]); }
</script>

<div class="flex flex-col gap-3" data-testid="knowledge-filter-builder">
  {#each drafts as row (row.id)}
    {@const schema = option(row.field)?.schema}
    {@const choices = values(row)}
    <fieldset class="grid gap-2 rounded-xl border border-slate-800 p-3 sm:grid-cols-[1fr_1fr_2fr_auto]">
      <legend class="sr-only">Filter</legend>
      <label class="text-xs text-slate-400">Field<select aria-label="Filter field" class="mt-1 w-full rounded-lg bg-slate-950 p-2" value={row.field}
        onchange={(e) => { const field = e.currentTarget.value; const nextSchema = option(field)?.schema; update(row.id, { field, op: operatorsForField(nextSchema)[0] ?? 'eq', raw: '', selected: [] }); void loadFacets(field); }}>
        {#each fieldOptions as item}<option value={item.field}>{item.field}</option>{/each}</select></label>
      <label class="text-xs text-slate-400">Operator<select aria-label="Filter operator" class="mt-1 w-full rounded-lg bg-slate-950 p-2" value={row.op}
        onchange={(e) => updateOperator(row.id, e.currentTarget.value)}>
        {#each operatorsForField(schema) as op}<option value={op}>{op}</option>{/each}</select></label>
      <label class="text-xs text-slate-400">Value
        {#if schema?.type === 'boolean'}
          <select aria-label="Filter value" class="mt-1 w-full rounded-lg bg-slate-950 p-2" value={row.raw} onchange={(e) => update(row.id, { raw: e.currentTarget.value })}><option value="">Select…</option><option value="true">True</option><option value="false">False</option></select>
        {:else if choices.length > 0 && (row.op === 'in' || row.op === 'overlap')}
          <select aria-label="Filter values" multiple class="mt-1 min-h-24 w-full rounded-lg bg-slate-950 p-2" onchange={(e) => {
            const selected = Array.from(e.currentTarget.selectedOptions).map((o) => o.value);
            update(row.id, { selected, raw: selected.join(', ') });
          }}>
            {#each choices as choice}<option value={String(choice)} selected={row.selected.includes(String(choice))}>{String(choice)}{facets[row.field]?.find((v) => v.value === choice) ? ` (${facets[row.field].find((v) => v.value === choice)?.count})` : ''}</option>{/each}
          </select>
        {:else if choices.length > 0}
          <select aria-label="Filter value" class="mt-1 w-full rounded-lg bg-slate-950 p-2" value={row.raw} onchange={(e) => update(row.id, { raw: e.currentTarget.value })}><option value="">Select…</option>{#each choices as choice}<option value={String(choice)}>{String(choice)}</option>{/each}</select>
        {:else if row.op === 'between'}
          <div class="mt-1 grid grid-cols-2 gap-2"><Input aria-label="Minimum value" value={row.raw.split(',')[0] ?? ''} oninput={(e) => update(row.id, { raw: `${e.currentTarget.value},${row.raw.split(',')[1] ?? ''}` })}/><Input aria-label="Maximum value" value={row.raw.split(',')[1] ?? ''} oninput={(e) => update(row.id, { raw: `${row.raw.split(',')[0] ?? ''},${e.currentTarget.value}` })}/></div>
        {:else}
          <Input aria-label="Filter value" class="mt-1" type={schema?.type === 'number' || schema?.type === 'integer' ? 'number' : schema?.type === 'date' ? 'date' : schema?.type === 'datetime' ? 'datetime-local' : 'text'} value={row.raw} onfocus={() => void loadFacets(row.field)} oninput={(e) => update(row.id, { raw: e.currentTarget.value })}/>
        {/if}
      </label>
      <Button size="icon" variant="ghost" class="self-end" onclick={() => drafts = drafts.filter((item) => item.id !== row.id)} aria-label="Remove filter"><X class="h-4 w-4"/></Button>
    </fieldset>
  {/each}
  <div class="flex flex-wrap gap-2"><Button size="sm" variant="ghost" onclick={addRow}><Plus class="mr-1 h-4 w-4"/>Add filter</Button><Button size="sm" onclick={apply}>Apply filters</Button><Button size="sm" variant="secondary" onclick={clear}>Clear</Button></div>
</div>
