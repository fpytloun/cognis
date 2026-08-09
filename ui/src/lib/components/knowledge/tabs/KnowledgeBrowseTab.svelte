<script lang="ts">
  import { onDestroy } from 'svelte';
  import { asApiError } from '$lib/api/client';
  import DocumentReader from '$lib/components/knowledge/DocumentReader.svelte';
  import DocumentTree from '$lib/components/knowledge/DocumentTree.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import { resolveDocumentContent, resolveDownloadUrl, type ResolvedDocumentContent } from '$lib/knowledge/content';
  import { buildDocumentTree, collectFolderPaths, filterTree, type DocumentTreeFile, type DocumentTreeNode } from '$lib/knowledge/tree';
  import Library from 'lucide-svelte/icons/library';
  import Search from 'lucide-svelte/icons/search';
  import type { KnowledgebaseDocumentModel, KnowledgebaseModel } from '$lib/types/api';

  let {
    kb,
    documents,
    selectedDocumentId = null,
    selectedDocumentFragment = null,
    selectionRequestId = 0,
    onOpenDocument,
    missingDocument = false
  }: {
    kb: KnowledgebaseModel;
    documents: KnowledgebaseDocumentModel[];
    selectedDocumentId?: string | null;
    selectedDocumentFragment?: string | null;
    selectionRequestId?: number;
    onOpenDocument?: (docId: string, fragment?: string) => void;
    missingDocument?: boolean;
  } = $props();

  let treeQuery = $state('');
  let expanded = $state(new Set<string>());
  let selected = $state<DocumentTreeFile | null>(null);
  let mobileReaderOpen = $state(false);
  let contentLoading = $state(false);
  let contentError = $state<string | null>(null);
  let content = $state<ResolvedDocumentContent | null>(null);
  let downloadUrl = $state<string | null>(null);
  let handledSelectionRequestId = $state(-1);
  let requestGeneration = 0;
  let contentController: AbortController | null = null;

  const built = $derived(buildDocumentTree(documents));
  const visibleTree = $derived<DocumentTreeNode[]>(filterTree(built.tree, treeQuery));

  $effect(() => {
    if (built.tree.length > 0 && expanded.size === 0) {
      expanded = new Set(collectFolderPaths(built.tree));
    }
  });

  function toggleFolder(path: string): void {
    const next = new Set(expanded);
    if (next.has(path)) next.delete(path);
    else next.add(path);
    expanded = next;
  }

  async function selectFile(node: DocumentTreeNode): Promise<void> {
    if (node.kind !== 'file') return;
    contentController?.abort();
    const generation = ++requestGeneration;
    const controller = new AbortController();
    contentController = controller;
    selected = node;
    mobileReaderOpen =
      typeof window !== 'undefined' && window.matchMedia('(max-width: 767px)').matches;
    content = null;
    downloadUrl = null;
    contentError = null;
    contentLoading = true;
    try {
      const [resolvedContent, resolvedDownload] = await Promise.all([
        resolveDocumentContent(kb.knowledgebase_id, node.document, controller.signal).catch((err) => {
          if (generation === requestGeneration && (err as { name?: string }).name !== 'AbortError') {
            contentError = asApiError(err).message;
          }
          return null;
        }),
        resolveDownloadUrl(node.document).catch(() => null)
      ]);
      if (generation !== requestGeneration) return;
      content = resolvedContent;
      downloadUrl = resolvedDownload;
    } finally {
      if (generation === requestGeneration) {
        contentLoading = false;
        contentController = null;
      }
    }
  }

  $effect(() => {
    if (!selectedDocumentId || handledSelectionRequestId === selectionRequestId) return;
    const document = documents.find((candidate) => candidate.doc_id === selectedDocumentId);
    if (!document) return;
    handledSelectionRequestId = selectionRequestId;
    if (document.source_path) {
      const parts = document.source_path.split('/');
      expanded = new Set([...expanded, ...parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join('/'))]);
    }
    void selectFile({
      kind: 'file',
      name: document.display_name,
      path: document.source_path || document.display_name,
      document
    });
  });

  onDestroy(() => {
    requestGeneration += 1;
    contentController?.abort();
  });
</script>

<div class="flex h-[calc(100vh-260px)] min-h-[420px] gap-4">
  <div class="flex w-full flex-col rounded-2xl border border-slate-800/80 bg-slate-900/60 md:w-72 md:shrink-0">
    <div class="border-b border-slate-800/80 p-3">
      <div class="relative">
        <Search class="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
        <Input bind:value={treeQuery} placeholder="Filter documents…" class="pl-9" data-testid="knowledge-tree-filter" />
      </div>
    </div>
    <div class="min-h-0 flex-1 overflow-y-auto p-2">
      {#if documents.length === 0}
        <div class="flex flex-col items-center gap-2 px-4 py-10 text-center text-sm text-slate-400">
          <Library class="h-6 w-6 text-slate-600" />
          <p>No documents yet. Add some from the Documents tab.</p>
        </div>
      {:else if visibleTree.length === 0}
        <p class="px-3 py-6 text-center text-sm text-slate-400">No documents match "{treeQuery}".</p>
      {:else}
        <DocumentTree nodes={visibleTree} selectedPath={selected?.path ?? null} {expanded} onSelect={selectFile} onToggle={toggleFolder} />
      {/if}
    </div>
  </div>

  <div class="hidden min-h-0 flex-1 overflow-hidden rounded-2xl border border-slate-800/80 bg-slate-900/60 md:block">
    {#if selected}
      <DocumentReader doc={selected.document} loading={contentLoading} error={contentError} {content} {downloadUrl}
        onDownload={null} knowledgebaseId={kb.knowledgebase_id} {documents} metadataSchema={kb.metadata_schema}
        requestedFragment={selectedDocumentFragment} {onOpenDocument} />
    {:else if missingDocument}
      <div class="flex h-full flex-col items-center justify-center gap-2 text-center text-sm text-amber-300" role="status">
        <Library class="h-8 w-8 text-amber-500" /><p>The requested document is no longer available.</p>
      </div>
    {:else}
      <div class="flex h-full flex-col items-center justify-center gap-2 text-center text-sm text-slate-400">
        <Library class="h-8 w-8 text-slate-600" />
        <p>Select a document to preview it here.</p>
      </div>
    {/if}
  </div>
</div>

<Sheet
  open={mobileReaderOpen && selected !== null}
  onClose={() => (mobileReaderOpen = false)}
  side="bottom"
  maxHeight="92dvh"
  label={selected ? `Preview ${selected.name}` : 'Document preview'}
  class="md:hidden"
>
  {#snippet children()}
    {#if selected}
      <DocumentReader doc={selected.document} loading={contentLoading} error={contentError} {content} {downloadUrl}
        onDownload={null} knowledgebaseId={kb.knowledgebase_id} {documents} metadataSchema={kb.metadata_schema}
        requestedFragment={selectedDocumentFragment} {onOpenDocument} />
    {/if}
  {/snippet}
</Sheet>
