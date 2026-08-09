<script lang="ts">
  import type { KnowledgebaseDocumentModel } from '$lib/types/api';
  let { doc, metadataSchema = {} }: { doc: KnowledgebaseDocumentModel; metadataSchema?: Record<string, unknown> } = $props();
  const fields = $derived((metadataSchema.fields && typeof metadataSchema.fields === 'object' ? metadataSchema.fields : {}) as Record<string, { display?: boolean }>);
  const entries = $derived(Object.entries(doc.metadata).filter(([key]) => key !== 'source_path' && fields[key]?.display !== false));
</script>
<aside class="rounded-xl border border-slate-800/80 bg-slate-950/40 p-3 text-sm" data-testid="knowledge-document-metadata">
  <h4 class="font-semibold text-white">Details</h4>
  <dl class="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
    <dt class="text-slate-500">Path</dt><dd class="break-all text-slate-300">{doc.source_path ?? doc.display_name}</dd>
    <dt class="text-slate-500">Type</dt><dd class="text-slate-300">{doc.mime_type ?? 'Unknown'}</dd>
    <dt class="text-slate-500">Status</dt><dd class="text-slate-300">{doc.status}</dd>
    <dt class="text-slate-500">Chunks</dt><dd class="text-slate-300">{doc.chunk_count}</dd>
  </dl>
  {#if entries.length}
    <div class="mt-3 border-t border-slate-800 pt-3">
      {#each entries as [key, value] (key)}
        <div class="mb-2"><p class="text-xs text-slate-500">{key}</p>
          {#if Array.isArray(value)}<div class="flex flex-wrap gap-1">{#each value as item}<span class="rounded-full bg-slate-800 px-2 py-0.5 text-xs">{String(item)}</span>{/each}</div>
          {:else if value && typeof value === 'object'}<details><summary class="cursor-pointer text-xs">Structured value</summary><pre class="mt-1 overflow-x-auto whitespace-pre-wrap text-xs">{JSON.stringify(value, null, 2)}</pre></details>
          {:else}<p class="text-xs text-slate-300">{String(value)}</p>{/if}
        </div>
      {/each}
    </div>
  {/if}
</aside>
