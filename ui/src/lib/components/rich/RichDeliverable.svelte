<script lang="ts">
  import DocumentFind from '../DocumentFind.svelte';
  let documentFind: DocumentFind | undefined;
  async function searchDocument() {
    inlineExpanded = true;
    await tick();
    documentFind?.requestOpen();
  }
  import { afterUpdate, onDestroy, onMount, tick } from 'svelte';
  import type { Snippet } from 'svelte';
  import { renderInlineMarkdown, renderMarkdown } from '$lib/markdown';
  import { portal } from '$lib/actions/portal';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import {
    blockTitle,
    blockText,
    normalizeRichDeliverable,
    privateDeliverableMediaUrl,
    resolveRichMedia,
    richCanvas,
    richDensity,
    richPresentation,
    type RichMediaUrlFor,
  } from '$lib/rich-deliverable';
  import RichBlockList from './RichBlockList.svelte';
  import FeedbackWorkspace from '../feedback/FeedbackWorkspace.svelte';
  // Loaded after startup so the standalone viewer's initial bundle stays within budget.
  let FeedbackIndicator: typeof import('../feedback/FeedbackIndicator.svelte').default | null = null;
  void import('../feedback/FeedbackIndicator.svelte').then((module) => { FeedbackIndicator = module.default; });
  import { annotateRichPayload } from '$lib/feedback/annotations/rich';
  import './rich-fonts.css';
  import RichToc from './RichToc.svelte';
  import RichViewerChrome from './RichViewerChrome.svelte';
  import DownloadMenu from './DownloadMenu.svelte';
  import ViewerIcon from './ViewerIcon.svelte';
  import ShareMenu from './ShareMenuLoader.svelte';
  import DocumentLinkActions from './DocumentLinkActions.svelte';
  import type { ShareLinkOptions } from './share-options';
  import DocumentActions from './DocumentActions.svelte';
  import X from 'lucide-svelte/icons/x';
  import { loadViewerTheme, observeViewerTheme, resolveViewerTheme, viewerTheme, type ViewerAppearance } from './viewer-theme';
  import {
    buildCitationRegistry,
    buildTocItems,
    decorateBlocks,
    namespaceTocItems,
    publicationOptions,
  } from './publication';
  import { createPublicationContext } from './publication-context';
  import { allocateRichInstanceNamespace } from './instance-namespace';
  import DeferredView from '../DeferredView.svelte';
  import type { DocumentAudioClient } from '$lib/document-audio';

  export let payload: unknown;
  export let content = '';
  export let title = '';
  export let surface: 'embedded' | 'standalone';
  export let exportMode = false;
  export let closePreview: (() => void) | null = null;
  export let expiredLink = false;
  export let compact = false;
  export let collapsedByDefault = false;
  export let inlineDisclosure = false;
  export let standaloneUrl = '';
  export let pdfUrl = '';
  export let htmlUrl = '';
  export let markdownUrl = '';
  export let shareLinkCallback: ((options: ShareLinkOptions) => Promise<string>) | null = null;
  export let instanceId = '';
  export let embeddedDocumentUrl = '';
  export let audioControls: Snippet | undefined = undefined;
  export let audioClient: DocumentAudioClient | undefined = undefined;
  export let audioEnabled = true;
  export let accountAppearance: ViewerAppearance | null = null;
  // Public preview hosts keep embedded geometry but resolve appearance as external readers.
  export let appearanceSurface: 'embedded' | 'standalone' | null = null;
  export let publicationThemeFamily: ViewerAppearance['theme_family'] | null = null;
  export let fullscreenOnly = false;
  export let onCloseFullscreen: (() => void) | undefined = undefined;
  export let internalUrl = '';
  export let hosted = false;
  export let conversational = false;
  export let footer: Snippet | undefined = undefined;
  export let extraActions: Snippet<[boolean?]> | undefined = undefined;
  export let revisionControls: Snippet | undefined = undefined;
  export let mediaUrlFor: RichMediaUrlFor = (mediaKey: string) => instanceId
    ? privateDeliverableMediaUrl(instanceId, mediaKey)
    : '';

  const instanceNamespace = allocateRichInstanceNamespace(instanceId);
  const publicationContext = createPublicationContext();
  $: normalizedPayload = normalizeRichDeliverable(annotateRichPayload(payload));
  $: authorizedMediaUrlFor = (mediaKey: string) =>
    normalizedPayload.media_manifest?.[mediaKey] ? mediaUrlFor(mediaKey) : '';
  $: normalized = resolveRichMedia(normalizedPayload, authorizedMediaUrlFor);
  $: metadata = normalized.metadata;
  $: presentation = richPresentation(metadata);
  $: density = richDensity(metadata, normalized.blocks);
  $: canvas = richCanvas(metadata);
  // Payload-level `title` is only a fallback for payloads authored with the
  // title inline; the host-supplied `title` prop (the deliverable's own
  // stored title/label) always wins when set.
  $: effectiveTitle = title || normalized.title || '';
  $: subtitle = typeof metadata.subtitle === 'string' ? metadata.subtitle : '';
  $: eyebrow = typeof metadata.eyebrow === 'string' ? metadata.eyebrow : '';
  $: badges = Array.isArray(metadata.badges) ? metadata.badges.map(String).filter(Boolean) : [];
  $: options = publicationOptions(metadata, normalized.blocks);
  $: localHeadingItems = buildTocItems(normalized.blocks, 4);
  $: headingItems = namespaceTocItems(localHeadingItems, instanceNamespace);
  $: internalLinkTargets = headingItems.reduce<Record<string, string>>((targets, item) => {
    targets[item.requestedAnchor] ??= item.anchor;
    return targets;
  }, {});
  $: tocItems = [
    ...(heroOwnsIdentity && !headingItems.some(item => item.label === 'Overview') ? [{ anchor: `${instanceNamespace}-overview`, requestedAnchor: 'overview', label: 'Overview', level: 2 as const, block: normalized.blocks[0] }] : []),
    ...headingItems.filter((item) => item.level <= options.tocDepth).map(item => ({
      ...item,
      label: item.block.type === 'section' && typeof item.block.eyebrow === 'string' && /^\d+$/.test(item.block.eyebrow)
        ? `${Number(item.block.eyebrow)}  ${item.label}` : item.label,
    })),
  ];
  $: decoratedBlocks = decorateBlocks(normalized.blocks, headingItems, options, instanceNamespace);
  $: heroTitle = normalized.blocks[0]?.type === 'hero' ? blockTitle(normalized.blocks[0]) : '';
  $: heroOwnsIdentity = Boolean(heroTitle);
  // Dashboard documents use their first section header as the presentation
  // title. Keep the host toolbar for actions, but do not duplicate that title
  // above the authored dashboard canvas.
  $: dashboardSectionOwnsIdentity = presentation === 'dashboard'
    && normalized.blocks[0]?.type === 'section_header'
    && Boolean(blockTitle(normalized.blocks[0]));
  $: markdownOwnsIdentity = normalized.blocks[0]?.type === 'markdown'
    && /^#\s+\S/.test(blockText(normalized.blocks[0]).trimStart());
  $: payloadOwnsIdentity = heroOwnsIdentity || dashboardSectionOwnsIdentity || markdownOwnsIdentity;
  $: documentBlocks = decoratedBlocks.map((block, index) => index === 0
    && ((block.type === 'hero' && heroOwnsIdentity) || (block.type === 'section_header' && dashboardSectionOwnsIdentity))
    ? { ...block, __document_heading_level: documentHeadingLevel, ...(heroOwnsIdentity ? { __publication_anchor: `${instanceNamespace}-overview` } : {}) }
    : block);
  $: publicationContext.set(buildCitationRegistry(normalized.blocks, normalized.sources, instanceNamespace));
  $: showToc = options.showToc;
  $: documentHeadingLevel = surface === 'embedded' && !fullOpen ? 2 : 1;
  $: chatFlow = conversational && surface === 'embedded' && !exportMode;
  export let fullOpen = false;
  let inlineExpanded = !collapsedByDefault;
  let tocOpen = false;
  let tocChosen = false;
  let readingProgress = 0;
  let copied = false;
  let copyError = '';
  let copyResetTimer: ReturnType<typeof setTimeout> | null = null;
  let systemDark = true;
  let appTheme: string | null = null;
  let appFamily: string | null = null;
  $: appearanceContext = appearanceSurface ?? surface;
  $: viewerFamily = appearanceContext === 'embedded' ? appFamily
    : (exportMode ? null : accountAppearance?.theme_family) ?? publicationThemeFamily;
  $: resolvedViewerTheme = resolveViewerTheme($viewerTheme, appearanceContext, appTheme, systemDark, exportMode ? null : accountAppearance);
  let root: HTMLDivElement;
  let modalPanel: HTMLDivElement;
  let closeButton: HTMLButtonElement;
  export let lastFocusedElement: HTMLElement | null = null;
  let overlayId: string | null = null;
  let unregisterOverlay: (() => void) | null = null;
  let mermaidRenderQueued = false;
  let mermaidRenderRunning = false;
  let mermaidRenderPending = false;
  let mermaidGeneration = 0;
  let mermaidDisposed = false;
  let inlineTocWide = false;
  let rootResizeObserver: ResizeObserver | null = null;
  let collapseIdentity = '';

  $: { documentBlocks; resolvedViewerTheme; fullOpen; inlineExpanded; invalidateMermaidRender(); }

  function invalidateMermaidRender() {
    mermaidGeneration += 1;
  }

  $: {
    const nextCollapseIdentity = `${instanceId}\u0000${title}`;
    if (nextCollapseIdentity !== collapseIdentity) {
      collapseIdentity = nextCollapseIdentity;
      inlineExpanded = !collapsedByDefault;
      tocOpen = false;
      tocChosen = false;
    }
  }

  function replaceFragment(fragment: string) {
    history.replaceState(history.state, '', fragment);
  }

  function navigateToc(item: (typeof tocItems)[number]) {
    const visibleRoot = fullOpen ? modalPanel : root;
    const target = visibleRoot?.querySelector<HTMLElement>(`#${item.anchor}`);
    target?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    target?.focus?.({ preventScroll: true });
    requestAnimationFrame(() => target?.focus?.({ preventScroll: true }));
    replaceFragment(`#${item.anchor}`);
  }

  async function openContextualToc() {
    if (showToc) { tocChosen = true; tocOpen = true; }
    await tick();
    const visibleRoot = fullOpen ? modalPanel : root;
    visibleRoot?.querySelector<HTMLButtonElement>('aside.rich-toc button[aria-label="Close table of contents"]')?.focus({ preventScroll: true });
  }

  function closeToc() { tocChosen = true; tocOpen = false; }

  function toggleInlineDocument() {
    inlineExpanded = !inlineExpanded;
    if (!inlineExpanded) tocOpen = false;
  }

  /**
   * The full-view modal is portaled to `document.body` (see `use:portal`
   * below) so it can escape the `isolation: isolate` stacking context on
   * `.rich-deliverable`. That means it is no longer a DOM descendant of
   * `root` once open, so any logic that used to find its content via
   * `root.querySelectorAll(...)` needs to also scan `modalPanel`.
   */
  function scopedRoots(): HTMLDivElement[] {
    return [root, modalPanel].filter((el): el is HTMLDivElement => Boolean(el));
  }

  function rewriteInternalLinks() {
    for (const scope of scopedRoots()) {
      for (const link of scope.querySelectorAll<HTMLAnchorElement>('a[href^="#"]')) {
        const fragment = link.getAttribute('href')?.slice(1) ?? '';
        const target = internalLinkTargets[fragment];
        if (target) link.setAttribute('href', `#${target}`);
      }
    }
  }

  function handleRootClick(event: MouseEvent) {
    const link = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>('a[href^="#"]') : null;
    if (!link) return;
    const scope = scopedRoots().find((candidate) => candidate.contains(link));
    if (!scope) return;
    const target = scope.querySelector<HTMLElement>(link.getAttribute('href') ?? '');
    if (!target) return;
    event.preventDefault();
    target.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    target.focus({ preventScroll: true });
    replaceFragment(link.getAttribute('href') ?? '');
  }

  /** Mirrors `root`'s click-to-navigate handling for the portaled modal
   * panel, since it is no longer a descendant of `root` once open. */
  function bindPanelInteractions(node: HTMLDivElement): { destroy(): void } {
    node.addEventListener('click', handleRootClick, true);
    const stopThemeObservation = observeViewerTheme(node, refreshMermaidTheme);
    return {
      destroy(): void {
        node.removeEventListener('click', handleRootClick, true);
        stopThemeObservation();
      },
    };
  }

  async function copyFallback() {
    copied = false;
    copyError = '';
    try {
      if (!navigator.clipboard) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(content || JSON.stringify(normalized, null, 2));
      copied = true;
      if (copyResetTimer) clearTimeout(copyResetTimer);
      copyResetTimer = setTimeout(() => copied = false, 1500);
    } catch {
      copyError = 'Could not copy document. Check clipboard permissions and try again.';
    }
  }

  async function renderMermaidFallbacks() {
    const generation = mermaidGeneration;
    await tick();
    if (mermaidDisposed || generation !== mermaidGeneration) return;
    const scopes = scopedRoots();
    const nodes = scopes.flatMap((scope) => Array.from(scope.querySelectorAll<HTMLElement>('pre[data-mermaid-source]')));
    if (nodes.length === 0) return;
    const sources = new Map(nodes.map((node) => [node, node.textContent]));
    const isCurrent = (node: HTMLElement) => !mermaidDisposed && generation === mermaidGeneration
      && node.isConnected && sources.get(node) === node.textContent
      && scopes.some((scope) => scopedRoots().includes(scope) && scope.contains(node));
    try {
      const { renderMermaidPresentation } = await import('./mermaid-presentation');
      if (!nodes.some(isCurrent)) return;
      await renderMermaidPresentation(nodes, fullOpen ? modalPanel : root, instanceNamespace, isCurrent, (node, presentation) => {
        if (isCurrent(node)) node.replaceWith(presentation);
      });
    } catch {
      // Mermaid is optional at runtime; source fallback remains usable.
    }
  }

  function scheduleMermaidRender() {
    if (mermaidDisposed) return;
    if (mermaidRenderRunning) {
      mermaidRenderPending = true;
      return;
    }
    if (mermaidRenderQueued) return;
    mermaidRenderQueued = true;
    queueMicrotask(async () => {
      mermaidRenderQueued = false;
      if (mermaidDisposed) return;
      mermaidRenderRunning = true;
      try {
        await renderMermaidFallbacks();
      } finally {
        mermaidRenderRunning = false;
        if (mermaidRenderPending) {
          mermaidRenderPending = false;
          scheduleMermaidRender();
        }
      }
    });
  }

  function refreshMermaidTheme() {
    if (mermaidDisposed) return;
    invalidateMermaidRender();
    for (const scope of scopedRoots()) {
      for (const wrapper of scope.querySelectorAll<HTMLElement>('.rich-mermaid[data-mermaid-source]')) {
        const source = document.createElement('pre');
        source.dataset.mermaidSource = wrapper.dataset.mermaidSource;
        source.dataset.mermaidId = wrapper.dataset.mermaidId;
        source.textContent = wrapper.dataset.mermaidSource ?? '';
        wrapper.replaceWith(source);
      }
    }
    scheduleMermaidRender();
  }

  onMount(() => {
    loadViewerTheme();
    if (fullscreenOnly || fullOpen) void openFullView();
    const query = window.matchMedia?.('(prefers-color-scheme: dark)');
    systemDark = query?.matches ?? true;
    const change = (event: MediaQueryListEvent) => systemDark = event.matches;
    query?.addEventListener('change', change);
    const updateAppTheme = () => {
      appTheme = document.documentElement.getAttribute('data-resolved-theme');
      appFamily = document.documentElement.getAttribute('data-theme-family');
    };
    updateAppTheme();
    const stopThemeObservation = observeViewerTheme(root, () => {
      updateAppTheme();
      refreshMermaidTheme();
    });
    root.addEventListener('click', handleRootClick, true);
    root.addEventListener('rich-toc-request', openContextualToc);
    rewriteInternalLinks();
    scheduleMermaidRender();
    if (typeof ResizeObserver !== 'undefined') {
      rootResizeObserver = new ResizeObserver(([entry]) => {
        const wasWide = inlineTocWide;
        inlineTocWide = (entry?.contentRect.width ?? 0) >= 960;
        if (chatFlow && wasWide && !inlineTocWide) {
          tocOpen = false;
          tocChosen = false;
        } else if (!tocChosen) tocOpen = inlineTocWide;
      });
      rootResizeObserver.observe(root);
    }
    return () => {
      query?.removeEventListener('change', change);
      stopThemeObservation();
    };
  });
  afterUpdate(() => {
    rewriteInternalLinks();
    scheduleMermaidRender();
  });
  onDestroy(() => {
    mermaidDisposed = true;
    invalidateMermaidRender();
    mermaidRenderPending = false;
    root?.removeEventListener('click', handleRootClick, true);
    root?.removeEventListener('rich-toc-request', openContextualToc);
    unregisterOverlay?.();
    rootResizeObserver?.disconnect();
    if (copyResetTimer) clearTimeout(copyResetTimer);
  });
  $: if (fullOpen) scheduleMermaidRender();

  $: if (fullOpen && !unregisterOverlay) {
    const handle = registerOverlay({ kind: 'fullscreen', blocksChrome: true, root: () => modalPanel });
    overlayId = handle.id;
    unregisterOverlay = handle.unregister;
  }

  $: if (!fullOpen && unregisterOverlay) {
    unregisterOverlay();
    unregisterOverlay = null;
    overlayId = null;
  }

  async function openFullView() {
    // A keyed revision replacement reopens an existing fullscreen view.
    if (!fullOpen) {
      lastFocusedElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    }
    tocOpen = window.innerWidth >= 960;
    fullOpen = true;
    await tick();
    closeButton?.focus();
  }

  async function closeFullView() {
    fullOpen = false;
    const restoreTarget = lastFocusedElement;
    lastFocusedElement = null;
    await tick();
    const target = restoreTarget?.isConnected ? restoreTarget : root?.querySelector<HTMLElement>('[aria-label="Open fullscreen"]');
    target?.focus({ preventScroll: true });
    onCloseFullscreen?.();
  }

  function focusableModalElements(): HTMLElement[] {
    const nodes = modalPanel?.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    ) ?? [];
    const visible = Array.from(nodes).filter((node) => node.offsetParent !== null
      || (node === closeButton && !node.closest('[data-find-toolbar][data-find-open]')));
    // Keep the dialog panel in the focus cycle for single-action modals.
    return modalPanel && modalPanel.tabIndex >= 0 ? [modalPanel, ...visible] : visible;
  }

  function handleKeydown(event: KeyboardEvent) {
    if (!fullOpen || !isTopOverlay(overlayId)) return;
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'f') return;
    if (event.key === 'Escape' && event.target instanceof Element && event.target.closest('[data-find-bar]')) return;
    if (event.key === 'Escape' && event.target instanceof Element && event.target.closest('.download-menu[open], .chat-document-more[open], .revision-switcher[open]')) return;
    event.stopPropagation();
    if (event.key === 'Escape') {
      closeFullView();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = focusableModalElements();
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
</script>

<div
  bind:this={root}
  inert={fullOpen}
  aria-hidden={fullOpen ? 'true' : undefined}
  class:compact
  class:embedded={surface === 'embedded'}
  class:hosted
  class:conversational={chatFlow}
  class="rich-deliverable"
  class:theme-palette={Boolean(viewerFamily)}
  data-theme-family={viewerFamily}
  data-resolved-theme={viewerFamily ? resolvedViewerTheme : undefined}
  class:pulse={presentation === 'pulse'}
  class:dashboard={presentation === 'dashboard'}
  data-presentation={presentation}
  data-rich-density={density}
  data-rich-canvas={canvas}
  data-rich-instance={instanceNamespace}
  data-has-contextual-toc={showToc ? 'true' : undefined}
  data-inline-toc-layout={inlineTocWide ? 'sidebar' : 'drawer'}
  data-testid="rich-deliverable"
  data-find-container={hosted ? undefined : ''}
  data-viewer-theme={resolvedViewerTheme}
>
  {#if !fullscreenOnly && surface === 'standalone'}
    <RichViewerChrome {exportMode} title={payloadOwnsIdentity ? blockTitle(normalized.blocks[0]) || effectiveTitle : effectiveTitle} identity={metadata.viewer_identity} customControls onCopy={copyFallback} progress={readingProgress} showProgress={showToc} onClose={closePreview}>
      <DocumentActions onSearch={embeddedDocumentUrl ? undefined : searchDocument} copyDocument={copyFallback} {copied} {pdfUrl} {htmlUrl} {markdownUrl} {internalUrl} createLink={shareLinkCallback} {extraActions} {revisionControls} audioControls={documentControls} />
    </RichViewerChrome>
  {/if}

  {#if !fullscreenOnly && !hosted && !chatFlow}
  <header data-find-toolbar={surface === 'standalone' ? undefined : ''} class="rich-toolbar" class:actions-only={(payloadOwnsIdentity && inlineExpanded) || surface === 'standalone'} data-testid="rich-deliverable-toolbar">
    {#if surface !== 'standalone' && (!payloadOwnsIdentity || !inlineExpanded)}
    <div>
      {#if eyebrow}<span class="rich-eyebrow">{@html renderInlineMarkdown(eyebrow)}</span>{/if}
      <!-- Documents inside an app page sit below the page title. -->
      <svelte:element this={documentHeadingLevel === 1 ? 'h1' : 'h2'} class="document-toolbar-title">{@html renderInlineMarkdown(effectiveTitle || 'Deliverable')}</svelte:element>
      {#if subtitle}<p>{@html renderInlineMarkdown(subtitle)}</p>{/if}
      {#if badges.length > 0}
        <div class="rich-badges">{#each badges as badge}<span>{@html renderInlineMarkdown(badge)}</span>{/each}</div>
      {/if}
    </div>
    {/if}
    {#if surface === 'embedded'}{@render documentActions()}{/if}
  </header>
  {/if}
  {#snippet documentAudio()}
    {#if !exportMode && audioEnabled}
      {#if audioControls}{@render audioControls()}
      {:else if /^(dlv|art|att|doc|img)_[0-9a-f]{32}$/.test(instanceId)}
        <DeferredView load={() => import('../DeliverableAudio.svelte')} label="Loading audio controls…">
          {#snippet children(loaded)}
            <loaded.default sourceRef={instanceId} client={audioClient} rich />
          {/snippet}
        </DeferredView>
      {/if}
    {/if}
  {/snippet}
  {#snippet documentControls()}
    <!-- Inline chat/Work documents share the conversation's commenting; only opened views get comment mode. -->
    {#if !exportMode && surface !== 'embedded' && FeedbackIndicator}<FeedbackIndicator />{/if}
    {@render documentAudio()}
  {/snippet}
  {#snippet fullViewControls()}
    {#if !exportMode && FeedbackIndicator}<FeedbackIndicator />{/if}
    {@render documentAudio()}
  {/snippet}
  {#snippet documentBody()}
    <div class="rich-body" data-testid="rich-deliverable-body">
      {#if exportMode || (surface === 'standalone' && embeddedDocumentUrl)}
        {@render documentContents()}
      {:else}
        <DocumentFind bind:this={documentFind} primary={hosted || fullOpen || surface === 'standalone'} identity={normalized}>
          {@render documentContents()}
        </DocumentFind>
      {/if}
    </div>
  {/snippet}
  {#snippet documentContents()}
      {#if expiredLink}<p class="preview-notice" role="status"><strong>Shared link expired</strong> — viewing through your authenticated owner access.</p>{/if}
      {#if (surface === 'standalone' || chatFlow || fullOpen) && !payloadOwnsIdentity}
        {#if eyebrow}<span class="rich-eyebrow">{@html renderInlineMarkdown(eyebrow)}</span>{/if}
        <svelte:element this={documentHeadingLevel === 1 ? 'h1' : 'h2'} class="document-title">{@html renderInlineMarkdown(effectiveTitle || 'Deliverable')}</svelte:element>
        {#if subtitle}<p class="document-lede">{@html renderInlineMarkdown(subtitle)}</p>{/if}
        {#if badges.length > 0}
          <div class="rich-badges">{#each badges as badge}<span>{@html renderInlineMarkdown(badge)}</span>{/each}</div>
        {/if}
      {/if}
      {#if surface === 'standalone' && embeddedDocumentUrl}
        <iframe src={embeddedDocumentUrl} title="Artifact preview" sandbox="allow-scripts" referrerpolicy="no-referrer" class="artifact-document-frame"></iframe>
      {:else if normalized.blocks.length > 0}
        <FeedbackWorkspace><RichBlockList blocks={documentBlocks} sources={normalized.sources} mediaUrlFor={authorizedMediaUrlFor} /></FeedbackWorkspace>
      {:else}
        <div class="rich-fallback">{@html renderMarkdown(content)}</div>
      {/if}
  {/snippet}
  {#snippet documentActions()}
    <DocumentActions onSearch={searchDocument} openFullView={surface === 'embedded' ? openFullView : undefined} copyDocument={copyFallback} {copied}
      {pdfUrl} {htmlUrl} {markdownUrl} {internalUrl} standaloneUrl={surface === 'embedded' ? standaloneUrl : ''} createLink={shareLinkCallback}
      collapsible={collapsedByDefault} {inlineDisclosure} expanded={inlineExpanded} controlsId={`${instanceNamespace}-inline-document`} toggleExpanded={toggleInlineDocument}
      {extraActions} {revisionControls} audioControls={documentControls} />
  {/snippet}
  {#if !fullscreenOnly && !chatFlow && showToc && (!inlineTocWide || !tocOpen)}
    <div class="rich-hosted-navigation">
      <button class="document-icon-action" type="button" aria-label="Open table of contents" title="Open table of contents" aria-expanded={tocOpen} on:click={openContextualToc}><ViewerIcon name="collapse" /></button>
    </div>
  {/if}
  {#if copyError}<p class="rich-action-error" role="alert">{copyError}</p>{/if}

  {#if !fullscreenOnly && !fullOpen && inlineExpanded}
  <div
    id={`${instanceNamespace}-inline-document`}
    class:has-toc={showToc && tocOpen}
    class:inline-toc-sidebar={showToc && inlineTocWide && tocOpen}
    class:chat-toc-enabled={chatFlow && showToc}
    class="rich-document rich-inline-document"
    data-testid="rich-deliverable-inline-document"
  >
    {#if showToc}
      <RichToc
        items={tocItems}
        onNavigate={navigateToc}
        bind:open={tocOpen}
        onClose={closeToc}
        bind:progress={readingProgress}
        layout={inlineTocWide ? 'sidebar' : 'drawer'}
        visible={inlineTocWide ? tocOpen : true}
        dismissibleSidebar={true}
        side={chatFlow ? 'right' : 'left'}
        theme={resolvedViewerTheme}
        family={viewerFamily}
        documentRoot={() => root}
      />
    {/if}

    {@render documentBody()}
    {#if chatFlow && showToc && (!inlineTocWide || !tocOpen)}
      <div class="chat-toc-rail">
        <button class="copy-icon-button" type="button" aria-label="Open table of contents" title="Contents"
          aria-expanded={tocOpen} on:click={openContextualToc}><ViewerIcon name="collapse" /></button>
      </div>
    {/if}
  </div>
  {/if}

  {#if !fullscreenOnly && chatFlow && !hosted}
    {#if !inlineExpanded}<svelte:element this={documentHeadingLevel === 1 ? 'h1' : 'h2'} class="chat-document-summary">{effectiveTitle || 'Deliverable'}</svelte:element>{/if}
    <footer class="chat-document-footer" data-find-toolbar data-testid="chat-document-footer">
      {@render footer?.()}
      {@render documentActions()}
    </footer>
  {/if}

  {#if fullOpen}
    <div
      class="rich-full"
      class:pulse={presentation === 'pulse'}
      class:dashboard={presentation === 'dashboard'}
      use:portal
      role="dialog"
      aria-modal="true"
      tabindex="-1"
      aria-label={effectiveTitle || 'Document full view'}
      data-testid="rich-deliverable-full-view"
      data-find-container
      data-presentation={presentation}
      data-rich-density={density}
      data-rich-canvas={canvas}
      data-viewer-theme={resolvedViewerTheme}
      class:theme-palette={Boolean(viewerFamily)}
      data-theme-family={viewerFamily}
      data-resolved-theme={viewerFamily ? resolvedViewerTheme : undefined}
      on:keydown|capture={handleKeydown}
    >
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <div class="rich-full-panel" bind:this={modalPanel} tabindex="0" use:bindPanelInteractions>
          <RichViewerChrome {surface} title={payloadOwnsIdentity ? blockTitle(normalized.blocks[0]) || effectiveTitle : effectiveTitle} identity={metadata.viewer_identity} customControls onCopy={copyFallback} progress={readingProgress} showProgress={showToc}>
            <DocumentActions onSearch={searchDocument} copyDocument={copyFallback} {copied} {pdfUrl} {htmlUrl} {markdownUrl} {internalUrl} createLink={shareLinkCallback} {extraActions} {revisionControls} audioControls={fullViewControls} />
            <button class="document-icon-action" bind:this={closeButton} type="button" aria-label="Close" title="Close full view" on:click={closeFullView}><X aria-hidden="true" /></button>
         </RichViewerChrome>
         {#if copyError}<p class="rich-action-error" role="alert">{copyError}</p>{/if}
        <div class="rich-full-body">
              <div class:has-toc={showToc && tocOpen} class="rich-document">
                {#if showToc}
                    <RichToc items={tocItems} onNavigate={navigateToc} bind:open={tocOpen} bind:progress={readingProgress} onClose={closeToc} layout="auto" visible={tocOpen} side="right" dismissibleSidebar={true} theme={resolvedViewerTheme} family={viewerFamily} documentRoot={() => modalPanel} />
                {/if}

            {@render documentBody()}
          </div>
        </div>
        {#if showToc && !tocOpen}
          <button class="full-toc-trigger document-icon-action" type="button" aria-label="Open table of contents" title="Contents" aria-expanded={tocOpen} on:click={openContextualToc}><ViewerIcon name="collapse" /></button>
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .artifact-document-frame { display: block; width: 100%; min-height: 75dvh; border: 0; background: white; }
  .rich-deliverable.hosted {
    border: 0; border-radius: 0; box-shadow: none; background: transparent;
  }
  .rich-hosted-navigation { display: flex; justify-content: flex-end; padding: .5rem 1rem; }
  .rich-deliverable {
    position: relative;
    isolation: isolate;
    container: rich-inline / inline-size;
    width: 100%;
    min-width: 0;
    max-width: 100%;
    overflow: clip;
    border: 1px solid var(--rich-line);
    border-radius: .35rem;
    background: var(--rich-surface);
  }

  /* The card clips its content; open action menus must escape the clip. */
  .rich-deliverable:has(:global(.download-menu[open])),
  .rich-deliverable:has(:global(.chat-document-more[open])),
  .rich-deliverable:has(:global(.share-menu .share-panel)) {
    overflow: visible;
    z-index: 5;
  }

  .full-toc-trigger { position: fixed; right: max(1rem, env(safe-area-inset-right)); top: calc(env(safe-area-inset-top) + 5.5rem); z-index: 5; min-width: 44px; min-height: 44px; background: var(--rich-surface-raised); }
  .rich-full button.document-icon-action { width: var(--rich-action-size, 44px); min-width: var(--rich-action-size, 44px); height: var(--rich-action-size, 44px); padding: 0; border-radius: 6px; }

  /* Action size for child-component triggers (download, share), which
     cannot be reached by this component's scoped button rules. Mirrors the
     button sizes below for each surface. */
  .rich-deliverable {
    --rich-action-size: 2.75rem;
    --rich-action-radius: 7px;
  }

  .rich-deliverable.pulse {
    --rich-action-size: 2.25rem;
    --rich-action-radius: 999px;
  }

  .rich-deliverable.compact {
    border-radius: 1.25rem;
  }

  .rich-deliverable.embedded {
    border-radius: .35rem;
  }

  .rich-deliverable.pulse {
    border-color: rgb(148 163 184 / 0.18);
    border-radius: .35rem;
    background: var(--rich-surface);
    box-shadow: 0 18px 55px rgb(2 6 23 / .2);
  }

  .rich-deliverable.pulse .rich-toolbar {
    min-height: 2.9rem;
    padding: .35rem .65rem;
    border-bottom-color: rgb(148 163 184 / .2);
    background: transparent;
  }

  .rich-deliverable.pulse .rich-toolbar.actions-only {
    padding-block: .25rem;
  }


  /* .rich-full is portaled to document.body (see use:portal) to escape the
     isolated stacking context above, so it carries its own .pulse class
     (set alongside .rich-deliverable.pulse) instead of relying on
     .rich-deliverable as an ancestor -- every pulse rule above that visually
     matters for full-view needs an explicit `.rich-full.pulse` companion,
     since `.rich-deliverable.pulse ...` selectors never match inside the
     portaled `.rich-full` tree. `.rich-orb`/`.rich-toolbar`/`.rich-actions`
     don't need one: those elements only exist in the non-full-view
     document, not inside `.rich-full`'s own header/body markup. `.rich-body`
     does: the same `.rich-body` class is reused inside full-view's
     `.rich-full-body`, and previously fell back to the generic full-width
     layout there instead of pulse's narrower editorial column, which was
     the most visible source of full-view looking different from the
     standalone page for Pulse presentations. */
  .rich-full.pulse .rich-full-panel {
    background: var(--rich-surface);
  }


  /* Dashboard presentation: flat, editorial, and dense -- restrained
     shadows/gradients (no ambient orbs, a plain surface instead of the
     default's layered gradient shell), strong section dividers (inherited
     from `--rich-line` borders already used throughout), and tabular
     numeric treatment (see `.rich-deliverable.dashboard :is(strong, .rich-metric strong)`
     rules in rich-blocks.css). Kept minimal here: only the outer shell and
     orb suppression are dashboard-specific chrome; everything else reuses
     the same generic primitives every other presentation shares. */
   .rich-deliverable.dashboard {
     border-color: transparent;
     border-radius: 0;
      background: var(--rich-surface);
     box-shadow: none;
   }

   .rich-full.dashboard .rich-full-panel {
      background: var(--rich-surface);
     box-shadow: none;
   }


   .rich-deliverable.dashboard .rich-toolbar {
     min-height: 2.25rem;
     padding: .3rem .55rem;
     border-bottom-color: color-mix(in srgb, var(--rich-line) 72%, transparent);
     background: transparent;
     backdrop-filter: none;
   }

   .rich-deliverable.dashboard .rich-toolbar.actions-only {
     padding-block: .2rem;
   }


  /* Constrain reading content, never the full-view canvas. Embedded width
     remains owned by the chat host. */
  .rich-deliverable[data-rich-canvas='wide']:not(.embedded) .rich-body {
    width: min(100%, 104rem);
    margin: 0 auto;
  }

  /* Deliverables default to dark regardless of OS preference until Cognis
     ships app-wide theming (no per-deliverable toggle exists). This used to
     hardcode a light pulse background/color purely from
     `prefers-color-scheme: light`, with no `data-resolved-theme` check at
     all -- so pulse rendered light on any light-OS machine with no way
     back to dark. Removed; a future explicit `data-resolved-theme="light"`
     variant can be added here when app-wide theming lands. */

  .rich-toolbar {
    display: flex;
    gap: 1rem;
    align-items: flex-start;
    justify-content: space-between;
    padding: clamp(1.1rem, 2.5vw, 1.7rem);
    border-bottom: 1px solid var(--rich-line);
    background: var(--rich-surface);
  }

  .rich-toolbar.actions-only {
    justify-content: flex-end;
    padding-block: 0.65rem;
  }

  .rich-toolbar > div {
    min-width: 0;
  }

  .rich-deliverable.embedded .rich-toolbar {
    padding: clamp(0.85rem, 1.8vw, 1.15rem);
  }

  .rich-toolbar .document-toolbar-title {
    display: block;
    margin: 0.15rem 0 0;
    color: var(--rich-text);
    font-size: clamp(2rem, 4vw, 3.6rem);
    letter-spacing: -0.045em;
    line-height: 1.05;
  }

  .rich-deliverable.embedded .rich-toolbar .document-toolbar-title {
    font-size: clamp(1.08rem, 2.1vw, 1.48rem);
    line-height: 1.14;
  }

  .rich-toolbar p {
    max-width: 54rem;
    margin: 0.55rem 0 0;
    color: var(--rich-text-secondary);
    line-height: 1.6;
  }

  .rich-deliverable.embedded .rich-toolbar p {
    margin-top: 0.42rem;
    line-height: 1.5;
  }

  .rich-eyebrow {
    color: var(--rich-accent-soft);
    font-size: 0.72rem;
    font-weight: 850;
    letter-spacing: 0.18em;
    text-transform: uppercase;
  }

  .rich-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    margin-top: 0.85rem;
  }

  .rich-deliverable.embedded .rich-badges {
    margin-top: 0.6rem;
  }

  .rich-badges span {
    border: 1px solid color-mix(in srgb, var(--rich-accent) 22%, transparent);
    border-radius: 999px;
    background: color-mix(in srgb, var(--rich-accent) 10%, var(--rich-surface-wash));
    color: var(--rich-accent-soft);
    padding: 0.28rem 0.58rem;
    font-size: 0.74rem;
    font-weight: 750;
  }

  .rich-full button,
  .rich-deliverable:not(.embedded) button {
    width: 2.75rem;
    min-width: 2.75rem;
    height: 2.75rem;
    justify-content: center;
    padding: 0;
  }

  button {
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--rich-line);
    border-radius: 0.85rem;
    background: color-mix(in srgb, var(--rich-surface-raised) 88%, transparent);
    color: var(--rich-text-secondary);
    padding: 0.48rem 0.72rem;
    font-size: 0.82rem;
    font-weight: 700;
    text-decoration: none;
    transition: border-color 150ms ease, background 150ms ease, transform 150ms ease;
  }

  button:hover {
    border-color: color-mix(in srgb, var(--rich-accent) 45%, transparent);
    background: color-mix(in srgb, var(--rich-accent) 14%, var(--rich-surface-raised));
    color: var(--rich-text);
    transform: translateY(-1px);
  }

  .rich-action-error {
    margin: 0;
    border-bottom: 1px solid var(--rich-line);
    color: var(--rich-tone-danger-fg);
    padding: 0.65rem 1rem;
    font-size: 0.82rem;
  }

  .rich-document {
    display: block;
    min-width: 0;
    max-width: 100%;
  }

  /* Inline TOC layout follows this component's measured container, not the
     viewport. Narrow embeddings use the drawer and one readable body column. */
  .rich-inline-document.inline-toc-sidebar {
    display: grid;
    grid-template-columns: minmax(11rem, 14rem) minmax(0, 1fr);
    gap: clamp(0.85rem, 2vw, 1.25rem);
    align-items: start;
    padding: clamp(0.85rem, 2vw, 1.2rem);
  }

  .rich-inline-document.inline-toc-sidebar .rich-body {
    min-width: 0;
    padding: 0;
  }

  .rich-body {
    min-width: 0;
    max-width: 100%;
    padding: clamp(1rem, 2.5vw, 1.7rem);
  }

  .rich-deliverable.embedded .rich-body {
    padding: clamp(0.85rem, 1.9vw, 1.2rem);
  }

  .rich-fallback {
    color: var(--rich-text-secondary);
    line-height: 1.7;
  }

  .rich-full {
    position: fixed;
    inset: 0;
    z-index: 2147483000;
    display: grid;
    place-items: start center;
    overflow: auto;
    padding: 0;
    background: var(--rich-surface);
  }

  .rich-full-panel {
    width: 100%;
    min-width: 0;
    min-height: 100vh;
    min-height: 100dvh;
    background: var(--rich-surface);
  }

  .rich-full-body {
    padding: clamp(1rem, 3vw, 2rem);
  }

  .rich-full-body .rich-document.has-toc {
    padding: 0;
  }

  /* Fullscreen keeps the established viewport-driven TOC contract because
     its portal is no longer inside the inline container. Keep this threshold
     aligned with RichToc's automatic sidebar mode. */
  @media (min-width: 1024px) {
    .rich-full-body .rich-document.has-toc {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(11rem, 14rem);
      gap: clamp(0.85rem, 2vw, 1.25rem);
      align-items: start;
    }

    .rich-full-body .rich-document.has-toc .rich-body {
      grid-column: 1;
      grid-row: 1;
      min-width: 0;
      padding: 0;
    }
    .rich-full-body .rich-document.has-toc :global(aside.rich-toc) { grid-column: 2; grid-row: 1; }
  }

  @media (max-width: 760px) {
    .rich-deliverable {
      border-radius: 1rem;
    }

    .rich-toolbar {
      flex-direction: column;
      gap: 0.8rem;
      padding: 0.9rem;
    }

    .rich-toolbar.actions-only {
      flex-direction: row;
      padding-block: .45rem;
    }

    .rich-toolbar .document-toolbar-title {
      font-size: clamp(1.2rem, 6.5vw, 1.65rem);
      line-height: 1.16;
      overflow-wrap: anywhere;
    }

    .rich-toolbar p {
      font-size: 0.9rem;
      line-height: 1.5;
    }

    .rich-eyebrow {
      font-size: 0.68rem;
    }

    button {
      border-radius: 0.8rem;
      padding: 0.45rem 0.62rem;
      font-size: 0.74rem;
      white-space: normal;
    }

    .rich-body,
    .rich-full-body {
      padding: 0.75rem;
    }

    .rich-full {
      padding: 0;
    }

    .rich-full-panel {
      border-radius: 0;
    }
  }
  .rich-deliverable:not(.embedded) .rich-toolbar.actions-only { display: none; }
  .rich-deliverable:not(.embedded) .rich-document,
  .rich-full-body { max-width: 100rem; margin-inline: auto; padding: clamp(1rem, 3vw, 3rem); }
  .rich-body { width: 100%; margin-inline: auto; }
  .document-title { color: var(--rich-text); font-size: clamp(1.875rem, 3vw, 2.75rem); line-height: 1.14; letter-spacing: -.035em; overflow-wrap: anywhere; margin: 0 0 24px; }
  .document-lede { color: var(--rich-text-secondary); max-width: 68ch; font-size: 1.2rem; line-height: 1.7; }
</style>
