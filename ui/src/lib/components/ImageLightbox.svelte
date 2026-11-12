<script lang="ts">
  import { getContext, onMount } from 'svelte';
  import { readable, type Readable } from 'svelte/store';
  import { DELIVERABLE_SHARING_CONTEXT } from '$lib/deliverable-permissions';
  import { api } from '$lib/api/client';
  import type { ArtifactRevisionSummary } from '$lib/types/api';

  import { portal } from '$lib/actions/portal';
  import { isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import ChevronLeft from 'lucide-svelte/icons/chevron-left';
  import ChevronRight from 'lucide-svelte/icons/chevron-right';
  import AttachmentSaveButton from './AttachmentSaveButton.svelte';
  import CompactHeaderActions from './CompactHeaderActions.svelte';
  import DocumentFavoriteButton from './DocumentFavoriteButton.svelte';
  import DocumentLinkActions from './rich/DocumentLinkActions.svelte';
  import type { ShareLinkOptions } from './rich/share-options';
  import X from 'lucide-svelte/icons/x';
  import ImageAnnotations from './feedback/annotations/ImageAnnotations.svelte';
  import FeedbackIndicator from './feedback/FeedbackIndicator.svelte';
  import FeedbackWorkspace from './feedback/FeedbackWorkspace.svelte';
  import { getFeedbackSurface } from '$lib/feedback/context';
  const feedback = getFeedbackSurface();

  /**
   * Full-screen image lightbox.
   *
   * - Click the backdrop or the close button to dismiss.
   * - Press Escape to dismiss.
    * - The shared file-save control keeps downloads inside the app.
   * - Locks the body scroll while open so touchmove on the backdrop on
   *   iOS Safari doesn't rubber-band the page behind the lightbox.
   *   Without the lock the user can drag the page underneath, and the
   *   backdrop-blur layer appears to shift / lose its fixed position,
   *   and the toolbar scrolls out of the visible area.
   * - Toolbar is absolutely positioned inside the app-shell-bounded
   *   fixed viewport so it stays visible regardless of image aspect ratio.
   */

  type LightboxImage = {
    src: string;
    alt?: string;
    filename?: string | null;
    artifact_id?: string;
    /** Revisions of this document; more than one shows a revision menu. */
    revisions?: number;
  };

  let { src, alt = 'Image', filename = null, images = null, index = 0, onIndexChange = undefined, onClose } = $props<{
    src: string;
    alt?: string;
    filename?: string | null;
    images?: LightboxImage[] | null;
    index?: number;
    onIndexChange?: (index: number) => void;
    onClose: () => void;
  }>();

  let overlayId = $state<string | null>(null);
  let currentIndex = $state(0);
  let scale = $state(1);
  let translateX = $state(0);
  let translateY = $state(0);
  let dragging = $state(false);

  type Point = { x: number; y: number };

  const pointers = new Map<number, Point>();
  let gestureMoved = false;
  let singleStart: Point | null = null;
  let dragStartTranslateX = 0;
  let dragStartTranslateY = 0;
  let pinchStartDistance = 0;
  let pinchStartScale = 1;
  let pinchStartMidpoint: Point = { x: 0, y: 0 };
  let pinchStartTranslateX = 0;
  let pinchStartTranslateY = 0;

  const items = $derived.by((): LightboxImage[] => (images && images.length > 0 ? images : [{ src, alt, filename }]));
  // An older revision picked from the menu replaces only the image currently shown.
  let revisionView = $state<{ index: number; artifact_id: string; src: string } | null>(null);
  const listed = $derived(items[Math.min(currentIndex, items.length - 1)] ?? { src, alt, filename });
  const active = $derived(revisionView && revisionView.index === currentIndex
    ? { ...listed, src: revisionView.src, artifact_id: revisionView.artifact_id } : listed);
  const revisionSwitcher = () => import('./RevisionSwitcher.svelte');
  let revisions = $state<ArtifactRevisionSummary[]>([]);
  let revisionError = $state('');
  let revisionGeneration = 0;
  $effect(() => {
    const id = listed.artifact_id;
    let disposed = false;
    revisionGeneration++;
    revisionView = null;
    revisionError = '';
    revisions = [];
    if (id) {
      void Promise.resolve().then(() => api.artifacts.revisions(id))
        .then(history => { if (!disposed) revisions = history.revisions; })
        .catch(() => {});
    }
    return () => { disposed = true; revisionGeneration++; };
  });
  async function showRevision(id: string) {
    const index = currentIndex;
    const generation = ++revisionGeneration;
    revisionError = '';
    if (id === listed.artifact_id) { revisionView = null; return; }
    try {
      const { url } = await api.artifacts.signedUrl(id, 3600, 'download');
      if (generation === revisionGeneration && index === currentIndex) {
        revisionView = { index, artifact_id: id, src: url }; resetTransform();
      }
    } catch {
      if (generation === revisionGeneration) revisionError = 'That revision could not be loaded.';
    }
  }
  const canNavigate = $derived(items.length > 1);
  const favoriteId = $derived('artifact_id' in active ? active.artifact_id : null);
  const sharingPermission = getContext<Readable<boolean>>(DELIVERABLE_SHARING_CONTEXT) ?? readable(true);
  async function createPublicLink(options: ShareLinkOptions): Promise<string> {
    if (!favoriteId) throw new Error('This image has no saved artifact.');
    return (await api.artifactDocuments.publicLink('artifact', favoriteId, { ...options, mode: 'download' })).url;
  }

  $effect(() => {
    if (index >= 0 && index < items.length && index !== currentIndex) {
      currentIndex = index;
      resetTransform();
    } else if (currentIndex >= items.length) {
      currentIndex = Math.max(0, items.length - 1);
      resetTransform();
    }
  });

  onMount(() => {
    const handle = registerOverlay({ kind: 'fullscreen', blocksChrome: true });
    overlayId = handle.id;
    return () => {
      handle.unregister();
      overlayId = null;
    };
  });

  function handleKeydown(event: KeyboardEvent): void {
    if (!isTopOverlay(overlayId)) {
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }
    if (canNavigate && event.key === 'ArrowLeft') {
      event.preventDefault();
      navigate(-1);
      return;
    }
    if (canNavigate && event.key === 'ArrowRight') {
      event.preventDefault();
      navigate(1);
    }
  }

  function clamp(value: number, min: number, max: number): number {
    return Math.min(max, Math.max(min, value));
  }

  function distance(a: Point, b: Point): number {
    return Math.hypot(a.x - b.x, a.y - b.y);
  }

  function midpoint(a: Point, b: Point): Point {
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  }

  function resetTransform(): void {
    scale = 1;
    translateX = 0;
    translateY = 0;
  }

  function navigate(delta: number): void {
    if (!canNavigate) return;
    const next = (currentIndex + delta + items.length) % items.length;
    currentIndex = next;
    revisionView = null;
    onIndexChange?.(next);
    resetTransform();
  }

  function beginPinch(): void {
    const [first, second] = Array.from(pointers.values());
    if (!first || !second) return;
    pinchStartDistance = Math.max(1, distance(first, second));
    pinchStartScale = scale;
    pinchStartMidpoint = midpoint(first, second);
    pinchStartTranslateX = translateX;
    pinchStartTranslateY = translateY;
  }

  function handlePointerDown(event: PointerEvent): void {
    if (feedback?.binding?.mode !== 'view' && feedback?.binding) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    dragging = true;
    gestureMoved = false;

    if (pointers.size === 1) {
      singleStart = { x: event.clientX, y: event.clientY };
      dragStartTranslateX = translateX;
      dragStartTranslateY = translateY;
    } else if (pointers.size === 2) {
      beginPinch();
    }
  }

  function handlePointerMove(event: PointerEvent): void {
    if (!pointers.has(event.pointerId)) return;
    event.preventDefault();
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (pointers.size >= 2) {
      const [first, second] = Array.from(pointers.values());
      if (!first || !second) return;
      const nextMidpoint = midpoint(first, second);
      const nextScale = clamp(pinchStartScale * (distance(first, second) / pinchStartDistance), 1, 5);
      scale = nextScale;
      translateX = pinchStartTranslateX + (nextMidpoint.x - pinchStartMidpoint.x);
      translateY = pinchStartTranslateY + (nextMidpoint.y - pinchStartMidpoint.y);
      gestureMoved = true;
      return;
    }

    if (!singleStart) return;
    const dx = event.clientX - singleStart.x;
    const dy = event.clientY - singleStart.y;
    if (Math.hypot(dx, dy) > 6) gestureMoved = true;

    if (scale > 1) {
      translateX = dragStartTranslateX + dx;
      translateY = dragStartTranslateY + dy;
    } else if (canNavigate && Math.abs(dx) > Math.abs(dy)) {
      translateX = dx * 0.28;
    }
  }

  function handlePointerUp(event: PointerEvent): void {
    const point = pointers.has(event.pointerId) ? { x: event.clientX, y: event.clientY } : null;
    try {
      (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
    } catch {
      /* ignore */
    }

    if (point && singleStart && pointers.size === 1 && scale <= 1.02) {
      const dx = point.x - singleStart.x;
      const dy = point.y - singleStart.y;
      if (canNavigate && Math.abs(dx) > 64 && Math.abs(dx) > Math.abs(dy) * 1.25) {
        navigate(dx < 0 ? 1 : -1);
      } else {
        resetTransform();
      }
    }

    pointers.delete(event.pointerId);
    if (scale <= 1.02) resetTransform();

    if (pointers.size === 1) {
      const remaining = Array.from(pointers.values())[0];
      singleStart = remaining ? { ...remaining } : null;
      dragStartTranslateX = translateX;
      dragStartTranslateY = translateY;
    } else {
      singleStart = null;
      dragging = false;
    }
  }

  function handleStageClick(event: MouseEvent): void {
    if (gestureMoved) return;
    if (event.target === event.currentTarget) onClose();
  }

  function handleDoubleClick(event: MouseEvent): void {
    if (feedback?.binding && feedback.binding.mode !== 'view') return;
    event.stopPropagation();
    if (scale > 1) {
      resetTransform();
      return;
    }
    scale = 2;
    translateX = 0;
    translateY = 0;
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<!-- svelte-ignore a11y_interactive_supports_focus -->
<div
  use:portal
  role="dialog"
  aria-modal="true"
  aria-label={active.filename ?? active.alt ?? 'Image'}
  tabindex="-1"
  class="app-viewport-overlay app-safe-fullscreen z-[95] overflow-hidden overscroll-contain touch-none bg-scrim/95"
>
  <!--
    Safe top toolbar. It is part of the fullscreen shell layout instead of
    being absolutely pinned to top: 0, so iOS standalone chrome, notches,
    and the Cognis mobile shell cannot push the close control out of view.
  -->
  <div
    class="app-safe-fullscreen__toolbar flex items-center justify-between gap-2 rounded-2xl border border-separator bg-elevated/90 px-3 py-2.5 shadow-lg backdrop-blur sm:px-4 sm:py-3"
    onclick={(event) => event.stopPropagation()}
  >
    <div class="flex min-w-0 flex-1 items-center gap-2">
      <p class="min-w-0 truncate text-sm text-foreground">
        {active.filename ?? active.alt ?? 'Image'}{#if canNavigate}<span class="ml-2 text-quiet">{currentIndex + 1}/{items.length}</span>{/if}
      </p>
      {#if revisionError}<p role="alert" class="text-xs text-danger">{revisionError}</p>{/if}
      {#if favoriteId && !favoriteId.startsWith('local:')}<DocumentFavoriteButton kind="artifact" id={favoriteId} />{/if}
    </div>
    <div class="flex shrink-0 items-center gap-2">
      <FeedbackIndicator />
      {#if listed.artifact_id && revisions.length > 1}
        {#await revisionSwitcher() then { default: RevisionSwitcher }}
          {#key `${currentIndex}:${active.artifact_id}`}
            <RevisionSwitcher testid="image-revision" currentId={active.artifact_id ?? ''}
              label={`Revision ${revisions.find(revision => revision.artifact_id === active.artifact_id)?.revision ?? 1} of ${Math.max(...revisions.map(revision => revision.revision))}`}
              load={async () => revisions.map(revision => ({
                id: revision.artifact_id, revision: revision.revision, created_at: revision.created_at, is_latest: revision.is_latest,
                author_email: revision.author_email, author_agent_id: revision.author_agent_id,
              }))}
              onSelect={(revision) => void showRevision(revision.id)} />
          {/key}
        {/await}
      {/if}
      <CompactHeaderActions>
        {#snippet children(menu: boolean)}
          {#key active.src}
          <AttachmentSaveButton
            filename={active.filename ?? 'image'}
            resolveUrl={() => Promise.resolve(active.src)}
            class="document-icon-action"
            label={menu ? 'Download' : ''}
          />
          {/key}
          {#if favoriteId && !favoriteId.startsWith('local:')}
            <DocumentLinkActions internalUrl={`/library/artifact/${encodeURIComponent(favoriteId)}`} createLink={$sharingPermission ? createPublicLink : undefined} allowModeChoice={false} defaultMode="download" shareLabel={menu ? 'Share link' : ''} />
          {/if}
        {/snippet}
      </CompactHeaderActions>
      <button
        aria-label="Close image viewer"
        title="Close"
        type="button"
        onclick={onClose}
        class="document-icon-action"
      >
        <X class="h-5 w-5" />
      </button>
    </div>
  </div>

  <!-- Image area. Centered, fills the viewport. Tapping the image
       does not close the lightbox; tapping the surrounding backdrop
       does. -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <div
    class="app-safe-fullscreen__content flex items-center justify-center overflow-hidden rounded-[1.75rem] bg-scrim/30" data-image-stage
    onclick={handleStageClick}
    onpointerdown={handlePointerDown}
    onpointermove={handlePointerMove}
    onpointerup={handlePointerUp}
    onpointercancel={handlePointerUp}
    ondblclick={handleDoubleClick}
  >
    <FeedbackWorkspace contentClass="flex items-center justify-center">
    <!-- Keep one image element while comments load or change mode, so enabling comments never
         swaps and resizes it. The annotation layer itself appears only once the source binds. -->
    {#if feedback && (items.length === 1 || favoriteId && (!feedback.binding || feedback.binding.source.source.kind === 'document' && feedback.binding.source.source.content_ref === favoriteId))}
      <div data-feedback-image-transform class="max-h-full max-w-full"
        style={`transform: translate3d(${translateX}px, ${translateY}px, 0) scale(${scale}); transition: ${dragging ? 'none' : 'transform 180ms cubic-bezier(.32,.72,0,1)'};`}>
        <ImageAnnotations src={active.src} alt={active.alt ?? alt} />
      </div>
    {:else}
    {#if !active.src}<p class="text-sm text-quiet" role="status">Loading image…</p>{:else}
    <img
      src={active.src}
      alt={active.alt ?? 'Image'}
      class="max-h-full max-w-full select-none rounded-2xl object-contain shadow-2xl"
      draggable="false"
      style={`transform: translate3d(${translateX}px, ${translateY}px, 0) scale(${scale}); transition: ${dragging ? 'none' : 'transform 180ms cubic-bezier(.32,.72,0,1)'};`}
      onclick={(event) => event.stopPropagation()}
    />
    {/if}
    {/if}
    </FeedbackWorkspace>
  </div>

  {#if canNavigate}
    <button
      type="button"
      aria-label="Previous image"
      class="absolute top-1/2 hidden h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full bg-elevated/90 text-foreground shadow-lg backdrop-blur transition hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent sm:inline-flex"
      style="left: var(--app-overlay-inner-left);"
      onclick={(event) => { event.stopPropagation(); navigate(-1); }}
    >
      <ChevronLeft class="h-6 w-6" />
    </button>
    <button
      type="button"
      aria-label="Next image"
      class="absolute top-1/2 hidden h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full bg-elevated/90 text-foreground shadow-lg backdrop-blur transition hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent sm:inline-flex"
      style="right: var(--app-overlay-inner-right);"
      onclick={(event) => { event.stopPropagation(); navigate(1); }}
    >
      <ChevronRight class="h-6 w-6" />
    </button>
  {/if}

  <div class="app-safe-fullscreen__toolbar flex justify-center sm:hidden" onclick={(event) => event.stopPropagation()}>
    <button
      type="button"
      class="inline-flex min-h-11 items-center justify-center rounded-full border border-control-border bg-elevated/95 px-6 text-sm font-medium text-foreground shadow-lg backdrop-blur transition hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      onclick={onClose}
    >
      Close image
    </button>
  </div>
</div>
