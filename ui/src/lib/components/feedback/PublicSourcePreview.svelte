<script lang="ts">
  import { onMount, tick } from 'svelte';
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import ArtifactPreviewContent from '$lib/components/ArtifactPreviewContent.svelte';
  import { previewKind, type AttachmentPreviewKind } from '$lib/attachments/preview';
  import type { PublicFeedbackClient, PublicFeedbackContent } from '$lib/feedback/public-api';
  import PublicDocumentControls from './PublicDocumentControls.svelte';
  import FeedbackWorkspace from './FeedbackWorkspace.svelte';
  import FeedbackIndicator from './FeedbackIndicator.svelte';
  import Button from '../ui/Button.svelte';
  import CompactHeaderActions from '../CompactHeaderActions.svelte';
  import Search from 'lucide-svelte/icons/search';
  import { requestFind } from '$lib/find';
  import { isCompactViewport } from '$lib/stores/viewport';
  import { normalizeThemeFamily } from '$lib/theme/catalog';
  import { fetchHostedAppearance, loadViewerTheme, resolveViewerTheme, viewerTheme, type ViewerAppearance } from '../rich/viewer-theme';
  let { content, mediaUrl, guard, contentRef, client }: {
    content: PublicFeedbackContent; mediaUrl: string | null;
    contentRef?: string; client?: PublicFeedbackClient;
    guard: { signal: AbortSignal; assertCurrent: () => void; validate: () => Promise<void> };
  } = $props();
  const kind = $derived.by((): AttachmentPreviewKind | 'image' | null => {
    if (content.mime_type.startsWith('image/')) return 'image';
    if (['markdown', 'mermaid', 'csv', 'pdf', 'html'].includes(content.format)) return content.format as AttachmentPreviewKind;
    return previewKind(content) ?? 'text';
  });
  const htmlSource = $derived(kind === 'html' && content.content !== null ? { text: content.content, guard } : null);
  let accountAppearance = $state<ViewerAppearance | null>(null);
  let systemDark = $state(false);
  let refresh = $state(0);
  let root = $state<HTMLDivElement>();
  const searchable = $derived(['text', 'markdown', 'mermaid', 'csv', 'html', 'pdf'].includes(kind ?? ''));
  function searchDocument(event?: MouseEvent) {
    const details = event?.currentTarget instanceof Element ? event.currentTarget.closest('details') : null;
    if (details) { details.open = false; details.querySelector('summary')?.focus(); }
    const scope = root?.querySelector<HTMLElement>('[data-find-scope]');
    if (scope) requestFind(scope);
  }
  const family = $derived(accountAppearance?.theme_family ?? normalizeThemeFamily(content.theme_family));
  const appearance = $derived(resolveViewerTheme($viewerTheme, 'standalone', null, systemDark, accountAppearance));
  onMount(() => {
    loadViewerTheme();
    const media = matchMedia('(prefers-color-scheme: dark)');
    const update = () => { systemDark = media.matches; };
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  });
  $effect(() => {
    void refresh;
    const abort = new AbortController();
    void fetchHostedAppearance(abort.signal).then(value => {
      if (!abort.signal.aborted) accountAppearance = value;
    }).catch(() => { /* A private preference lookup must not block public feedback. */ });
    return () => abort.abort();
  });
  $effect(() => {
    const theme = appearance;
    void family;
    const node = root;
    if (!node) return;
    const html = document.documentElement;
    const previousBackground = html.style.getPropertyValue('--app-bottom-chrome-bg');
    const previousScheme = html.style.colorScheme;
    const metas = ['theme-color', 'color-scheme'].map(name => {
      const existing = document.querySelector<HTMLMetaElement>(`meta[name="${name}"]`);
      const meta = existing ?? document.createElement('meta');
      const previous = meta.content;
      if (!existing) { meta.name = name; document.head.append(meta); }
      return { meta, previous, existing };
    });
    let disposed = false;
    void tick().then(() => {
      if (disposed) return;
      const header = node.querySelector('[data-testid="rich-viewer-chrome"], header') ?? node;
      const color = getComputedStyle(header).backgroundColor;
      html.style.setProperty('--app-bottom-chrome-bg', color);
      html.style.colorScheme = theme;
      metas[0].meta.content = color;
      metas[1].meta.content = theme;
    });
    return () => {
      disposed = true;
      if (previousBackground) html.style.setProperty('--app-bottom-chrome-bg', previousBackground);
      else html.style.removeProperty('--app-bottom-chrome-bg');
      html.style.colorScheme = previousScheme;
      for (const { meta, previous, existing } of metas) {
        if (existing) meta.content = previous; else meta.remove();
      }
    };
  });
</script>
<svelte:window onfocus={() => refresh += 1} />
{#snippet controls()}
  {#if client && contentRef}<PublicDocumentControls {client} {contentRef} {content} {guard} />{/if}
{/snippet}
<div bind:this={root} class="theme-palette h-full min-h-0 bg-canvas text-foreground" data-theme-family={family} data-resolved-theme={appearance}>
{#if content.format === 'rich' && content.rich_payload}
  <RichDeliverable payload={content.rich_payload} content={content.content ?? ''} title={content.filename} surface="standalone"
    publicationThemeFamily={normalizeThemeFamily(content.theme_family)} {accountAppearance}
    mediaUrlFor={() => ''} audioControls={controls} />
{:else}
  <div data-find-container class="flex h-full min-h-0 flex-col">
    <header data-find-toolbar class="flex min-h-12 shrink-0 items-center gap-3 border-b border-separator bg-surface px-3 pt-[env(safe-area-inset-top)]">
      <h1 class="min-w-0 flex-1 truncate text-sm font-medium">{content.filename}</h1>
      <div data-find-actions class="flex shrink-0 items-center gap-2">
        <FeedbackIndicator />
        <CompactHeaderActions alwaysShowMenu={searchable}>
          {#snippet children(menu: boolean)}
            {#if menu && searchable}<Button variant="ghost" size="inline" data-menu-close onclick={searchDocument}><Search size={18} />Search in document</Button>{/if}
            {#if !menu || $isCompactViewport}{@render controls()}{/if}
          {/snippet}
        </CompactHeaderActions>
      </div>
    </header>
    <div class="min-h-0 flex-1">
      <FeedbackWorkspace>
        <ArtifactPreviewContent {kind} filename={content.filename} mime={content.mime_type}
          content={content.content ?? ''} {mediaUrl} richPayload={content.rich_payload} {...{ htmlSource }}
          appearanceSurface="standalone" publicationThemeFamily={normalizeThemeFamily(content.theme_family)} {accountAppearance} />
      </FeedbackWorkspace>
    </div>
  </div>
{/if}
</div>
