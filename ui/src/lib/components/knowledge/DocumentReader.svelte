<script lang="ts">
  import hljs from 'highlight.js/lib/common';
  import { parse as parseYaml } from 'yaml';

  import Download from 'lucide-svelte/icons/download';
  import FileText from 'lucide-svelte/icons/file-text';

  import Button from '$lib/components/ui/Button.svelte';
  import LoadingState from '$lib/components/LoadingState.svelte';
  import DocumentMetadataCard from '$lib/components/knowledge/DocumentMetadataCard.svelte';
  import { rewriteKnowledgeResourceHtml } from '$lib/knowledge/resources';
  import { classifyDocument, languageForHighlight } from '$lib/knowledge/render';
  import { renderMarkdownDocument, sanitizeHtml, type MarkdownHeading } from '$lib/markdown';
  import type { KnowledgebaseDocumentModel } from '$lib/types/api';

  let {
    doc,
    loading,
    error,
    content,
    downloadUrl,
    onDownload, knowledgebaseId, documents = [], metadataSchema = {}, requestedFragment = null, onOpenDocument
  }: {
    doc: KnowledgebaseDocumentModel;
    loading: boolean;
    error: string | null;
    content: { text: string; extractedText: boolean } | null;
    downloadUrl: string | null;
    onDownload: (() => void) | null;
    knowledgebaseId: string;
    documents?: KnowledgebaseDocumentModel[];
    metadataSchema?: Record<string, unknown>;
    requestedFragment?: string | null;
    onOpenDocument?: (docId: string, fragment?: string) => void;
  } = $props();

  const kind = $derived(classifyDocument(doc.mime_type, doc.display_name));

  const markdownDoc = $derived.by(() => {
    if (kind !== 'markdown' || !content) return null;
    const rendered = renderMarkdownDocument(content.text, `kb-doc-${doc.doc_id}`);
    return { ...rendered, html: rewriteKnowledgeResourceHtml(rendered.html, knowledgebaseId, doc, documents) };
  });

  const highlighted = $derived.by(() => {
    if (!content || (kind !== 'code' && kind !== 'json' && kind !== 'yaml' && kind !== 'xml')) return null;
    let text = content.text;
    if (kind === 'json') {
      try {
        text = JSON.stringify(JSON.parse(text), null, 2);
      } catch {
        // leave as-is if not valid JSON
      }
    } else if (kind === 'yaml') {
      try {
        parseYaml(text);
      } catch {
        // still render raw text; parsing is only a validity signal here
      }
    }
    const language = languageForHighlight(kind, doc.display_name);
    try {
      const result = language && hljs.getLanguage(language) ? hljs.highlight(text, { language }) : hljs.highlightAuto(text);
      return sanitizeHtml(result.value);
    } catch {
      return sanitizeHtml(text);
    }
  });

  let activeHeadingId = $state<string | null>(null);
  let handledFragmentKey = $state('');

  $effect(() => {
    if (!markdownDoc || !requestedFragment) return;
    const key = `${doc.doc_id}:${requestedFragment}`;
    if (handledFragmentKey === key) return;
    const heading = markdownDoc.headings.find(
      (candidate) => candidate.id === requestedFragment || candidate.id.endsWith(`-${requestedFragment}`)
    );
    if (!heading) return;
    handledFragmentKey = key;
    queueMicrotask(() => scrollToHeading(heading));
  });

  function scrollToHeading(heading: MarkdownHeading): void {
    activeHeadingId = heading.id;
    globalThis.document?.getElementById?.(heading.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function handleDownload(): void {
    if (downloadUrl) {
      window.open(downloadUrl, '_blank', 'noopener,noreferrer');
      return;
    }
    onDownload?.();
  }
  function handleReaderClick(event: MouseEvent): void {
    const anchor = (event.target as Element | null)?.closest('a');
    if (!anchor) return;
    const docId = anchor.getAttribute('data-kb-document-id');
    if (docId) {
      event.preventDefault();
      const encodedFragment = anchor.getAttribute('data-kb-document-fragment');
      let fragment: string | undefined;
      try { fragment = encodedFragment ? decodeURIComponent(encodedFragment) : undefined; } catch { fragment = undefined; }
      onOpenDocument?.(docId, fragment);
    }
    else if (anchor.hasAttribute('data-kb-resource-unavailable')) {
      event.preventDefault(); anchor.textContent = 'Resource unavailable'; anchor.setAttribute('role', 'status');
    }
  }
</script>

<div class="flex h-full min-h-0 flex-col" data-testid="knowledge-document-reader">
  <div class="flex shrink-0 items-center justify-between gap-3 border-b border-slate-800/80 px-4 py-3">
    <div class="flex items-center gap-2 overflow-hidden">
      <FileText class="h-4 w-4 shrink-0 text-slate-500" />
      <h3 class="truncate text-sm font-semibold text-white">{doc.display_name}</h3>
    </div>
    {#if downloadUrl || onDownload}
      <Button size="sm" variant="secondary" onclick={handleDownload}>
        <Download class="mr-1.5 h-3.5 w-3.5" /> Download
      </Button>
    {/if}
  </div>

  <div class="flex min-h-0 flex-1 overflow-hidden">
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -- delegated handling for rendered Markdown anchors -->
    <div class="min-w-0 flex-1 overflow-y-auto px-5 py-4" role="region" aria-label="Document content"
      onclick={handleReaderClick} onkeydown={() => {}}>
      {#if loading}
        <LoadingState label="Loading document…" description="Fetching content for preview." />
      {:else if error}
        <div class="rounded-2xl border border-rose-800/60 bg-rose-950/40 px-4 py-3 text-sm text-rose-300" role="alert">{error}</div>
      {:else if !content}
        <div class="flex flex-col items-center gap-3 rounded-2xl border border-dashed border-slate-800/80 px-6 py-12 text-center text-sm text-slate-400">
          <p>Preview isn't available for this file type yet.</p>
          {#if downloadUrl || onDownload}
            <p>Use Download to open it directly.</p>
          {/if}
        </div>
      {:else}
        {#if content.extractedText}
          <p class="mb-3 rounded-lg border border-slate-800/80 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
            Showing extracted text. Original formatting is not preserved.
          </p>
        {/if}
        {#if kind === 'markdown' && markdownDoc}
          <div class="prose prose-invert prose-sm max-w-none">{@html markdownDoc.html}</div>
        {:else if highlighted}
          <pre class="overflow-x-auto rounded-xl bg-slate-950/80 p-4 text-xs leading-relaxed"><code>{@html highlighted}</code></pre>
        {:else}
          <pre class="whitespace-pre-wrap break-words text-sm text-slate-200">{content.text}</pre>
        {/if}
      {/if}
    </div>

    <div class="hidden w-64 shrink-0 overflow-y-auto border-l border-slate-800/80 p-4 lg:block">
      <DocumentMetadataCard {doc} {metadataSchema} />
    {#if kind === 'markdown' && markdownDoc && markdownDoc.headings.length > 1}
      <nav class="mt-4" aria-label="Table of contents">
        <p class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Contents</p>
        <ul class="space-y-1 text-sm">
          {#each markdownDoc.headings as heading (heading.id)}
            <li style={`padding-left: ${(heading.level - 1) * 10}px`}>
              <button
                type="button"
                class={`text-left hover:text-sky-300 ${activeHeadingId === heading.id ? 'text-sky-300' : 'text-slate-400'}`}
                onclick={() => scrollToHeading(heading)}
              >
                {heading.text}
              </button>
            </li>
          {/each}
        </ul>
      </nav>
    {/if}
    </div>
  </div>
  <div class="border-t border-slate-800 p-3 lg:hidden"><details><summary class="cursor-pointer text-sm text-slate-300">Details</summary><div class="mt-2"><DocumentMetadataCard {doc} {metadataSchema} /></div></details></div>
</div>
