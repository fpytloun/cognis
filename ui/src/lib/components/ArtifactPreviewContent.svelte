<script lang="ts">
  import { loadHighlighter } from '$lib/syntax/lazy-highlight';
  import { highlightJson, looksLikeJson, reindentJson } from '$lib/syntax/json';
  import { textFormat } from '$lib/artifact-text';
  import { previewLanguage, type AttachmentPreviewKind } from '$lib/attachments/preview';
  import RichDeliverable from './rich/RichDeliverable.svelte';
  import DocumentFind from './DocumentFind.svelte';
  import type { ViewerAppearance } from './rich/viewer-theme';
  import PdfAttachmentViewer from './PdfAttachmentViewer.svelte';
  import HtmlPreview from './HtmlPreview.svelte';
  import type { HtmlPreviewSource } from '$lib/attachments/html-preview';
  import ImageAnnotations from './feedback/annotations/ImageAnnotations.svelte';
  import TextAnnotations from './feedback/annotations/TextAnnotations.svelte';
  import { getFeedbackSurface } from '$lib/feedback/context';
  const feedback = getFeedbackSurface();

  let { kind, filename, mime = '', content = '', richPayload = null, truncated = false, mediaUrl = null, htmlSource = null, appearanceSurface = null, publicationThemeFamily = null, accountAppearance = null }: {
    kind: AttachmentPreviewKind | 'image' | null; filename: string; mime?: string;
    content?: string; richPayload?: unknown; truncated?: boolean; mediaUrl?: string | null;
    htmlSource?: HtmlPreviewSource | null;
    appearanceSurface?: 'embedded' | 'standalone' | null;
    publicationThemeFamily?: ViewerAppearance['theme_family'] | null;
    accountAppearance?: ViewerAppearance | null;
  } = $props();
  let hljs = $state<Awaited<ReturnType<typeof loadHighlighter>> | null>(null);
  $effect(() => {
    if (kind !== 'text') return;
    let cancelled = false;
    void loadHighlighter().then(value => { if (!cancelled) hljs = value; });
    return () => { cancelled = true; };
  });
  const highlighted = $derived.by(() => {
    // Whitespace-only indentation keeps the view identical with and without comments; comments
    // still anchor to the source text because they ignore whitespace differences.
    if (!truncated && textFormat(mime, filename) === 'json' && looksLikeJson(content)) {
      return highlightJson(reindentJson(content));
    }
    const language = previewLanguage(filename, mime);
    try {
      if (hljs && language && hljs.getLanguage(language)) return hljs.highlight(content, { language, ignoreIllegals: true }).value;
    } catch { /* Invalid source remains readable as escaped text. */ }
    return content.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  });
  const payload = $derived(
    ['markdown', 'mermaid', 'csv'].includes(kind ?? '')
      ? richPayload ?? (kind === 'markdown'
        ? { blocks: [{ type: 'markdown', content }] }
        : null)
      : null,
  );
</script>

{#if kind === 'image' && mediaUrl}
  <ImageAnnotations src={mediaUrl} alt={filename || 'Artifact'} />
{:else if kind === 'pdf' && mediaUrl}
  {#key mediaUrl}<PdfAttachmentViewer url={mediaUrl} />{/key}
{:else if kind === 'html' && htmlSource}
  <HtmlPreview source={htmlSource} />
{:else if kind === 'video' && mediaUrl}
  <!-- svelte-ignore a11y_media_has_caption: previews cannot synthesize a caption track -->
  <video class="h-full w-full bg-black object-contain" src={mediaUrl} controls playsinline></video>
{:else if kind === 'audio' && mediaUrl}
  <div class="flex h-full items-center justify-center p-3"><audio class="w-full max-w-xl" src={mediaUrl} controls></audio></div>
{:else if ['html', 'pdf', 'video', 'audio'].includes(kind ?? '')}
  <p class="p-4 text-sm text-danger">The document preview could not be loaded.</p>
{:else if payload}
  {#if truncated}<p class="p-3 text-xs text-warning">Preview is truncated to safe display limits. Download the complete file.</p>{/if}
  <RichDeliverable {content} {payload} surface="embedded" {appearanceSurface} {publicationThemeFamily} {accountAppearance} hosted title={filename} />
{:else if kind}
  {#if truncated}<p class="p-3 text-xs text-warning">Preview limited to the first 512 KB. Download the complete file.</p>{/if}
  <DocumentFind identity={content}>
    <TextAnnotations text={content}><pre class="min-h-full overflow-x-auto whitespace-pre-wrap break-words bg-inset p-5 text-sm leading-6 text-secondary"><code class="hljs">{@html highlighted}</code></pre></TextAnnotations>
  </DocumentFind>
{:else}
  <p class="p-8 text-sm text-quiet">No inline preview for this format. Use Download to open it.</p>
{/if}
