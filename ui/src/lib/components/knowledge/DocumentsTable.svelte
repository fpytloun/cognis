<script lang="ts">
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';
  import Trash from 'lucide-svelte/icons/trash';

  import Button from '$lib/components/ui/Button.svelte';
  import { formatBytes, formatRelativeOrDate, documentStatusTone, statusToneClass } from '$lib/knowledge/format';
  import type { KnowledgebaseDocumentModel } from '$lib/types/api';

  let {
    documents,
    busyIds,
    canMutate = true,
    onReindex,
    onDetach
  }: {
    documents: KnowledgebaseDocumentModel[];
    busyIds: ReadonlySet<string>;
    canMutate?: boolean;
    onReindex: (doc: KnowledgebaseDocumentModel) => void;
    onDetach: (doc: KnowledgebaseDocumentModel) => void;
  } = $props();
</script>

<div class="overflow-x-auto rounded-2xl border border-slate-800/80">
  <table class="w-full min-w-[640px] text-left text-sm" data-testid="knowledge-documents-table">
    <thead class="border-b border-slate-800/80 text-xs uppercase tracking-wide text-slate-500">
      <tr>
        <th class="px-4 py-3">Document</th>
        <th class="px-4 py-3">Status</th>
        <th class="px-4 py-3">Size</th>
        <th class="px-4 py-3">Chunks</th>
        <th class="px-4 py-3">Updated</th>
        {#if canMutate}<th class="px-4 py-3 text-right">Actions</th>{/if}
      </tr>
    </thead>
    <tbody class="divide-y divide-slate-800/80">
      {#each documents as doc (doc.doc_id)}
        <tr>
          <td class="max-w-xs px-4 py-3">
            <span class="block truncate font-medium text-white" title={doc.source_path ?? doc.display_name}>{doc.display_name}</span>
            {#if doc.source_path}
              <span class="block truncate text-xs text-slate-500" title={doc.source_path}>{doc.source_path}</span>
            {/if}
            {#if doc.last_error}
              <span class="block truncate text-xs text-rose-300" title={doc.last_error}>{doc.last_error}</span>
            {/if}
          </td>
          <td class="px-4 py-3">
            <span class={`inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-medium capitalize ${statusToneClass(documentStatusTone(doc.status))}`}>
              {doc.status}
            </span>
          </td>
          <td class="px-4 py-3 text-slate-400">{formatBytes(doc.size_bytes)}</td>
          <td class="px-4 py-3 text-slate-400">{doc.chunk_count}</td>
          <td class="px-4 py-3 text-slate-400">{formatRelativeOrDate(doc.indexed_at ?? doc.attached_at)}</td>
          {#if canMutate}<td class="px-4 py-3 text-right">
            <div class="flex justify-end gap-2">
              <Button
                size="sm"
                variant="ghost"
                disabled={busyIds.has(doc.doc_id)}
                onclick={() => onReindex(doc)}
                aria-label={`Reindex ${doc.display_name}`}
              >
                <RefreshCw class="h-3.5 w-3.5" />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                class="text-rose-300 hover:text-rose-200"
                disabled={busyIds.has(doc.doc_id)}
                onclick={() => onDetach(doc)}
                aria-label={`Detach ${doc.display_name}`}
              >
                <Trash class="h-3.5 w-3.5" />
              </Button>
            </div>
          </td>{/if}
        </tr>
      {/each}
    </tbody>
  </table>
</div>
