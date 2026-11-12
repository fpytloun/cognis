<script lang="ts">
  import { tick, untrack, type Snippet } from 'svelte';
  import { presentationAuthority, type FeedbackPresentationPort } from '$lib/feedback/presentation';
  import type { FeedbackController } from '$lib/feedback/state';
  import type { FeedbackAnchorCandidate, FeedbackTargetGeometry } from '$lib/feedback/types';
  import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
  import { feedbackSourceState, targetSummary } from '$lib/feedback/target-summary';
  import { surfacePortal } from '$lib/feedback/surface-portal';
  import { inlinePortal } from '$lib/feedback/inline-portal';
  import { feedbackOverlayBounds, type FeedbackOverlayBounds } from '$lib/feedback/overlay-bounds';
  import { avatarClusters, contentObstacles, floatingPlacement, targetPaint, MARKER_HIT, MARKER_SIZE,
    type AvatarCluster, type FloatingOffset } from '$lib/feedback/floating-layout';
  import FeedbackComments from './FeedbackComments.svelte';
  import TargetQuote from './TargetQuote.svelte';
  import Button from '$lib/components/ui/Button.svelte';

  // docked: a host sidebar shows the comment list, so this overlay keeps only selection, composer and previews.
  let { controller, port: supplied, comments, docked = false }: ({ controller: FeedbackController; port?: never } | { controller?: never; port: FeedbackPresentationPort }) & { comments?: Snippet; docked?: boolean } = $props();
  const port = $derived(supplied ?? controller!.presentation());
  const snapshot = $derived(port.state), session = $derived(port.session.state);
  const authority = $derived($snapshot.source ? presentationAuthority(port, $snapshot) : '');
  const preview = $derived($snapshot.threads.find(thread => thread.thread_id === $session.preview));
  const thread = $derived($snapshot.threads.find(thread => thread.thread_id === $snapshot.selected));
  const previewTarget = $derived(preview ? targetSummary({ locator: preview.anchor.locator, source: preview.anchor.source,
    sourceTitle: feedbackSourceState(preview.anchor, $snapshot.source) === 'current' ? $snapshot.source?.title ?? null : null }) : null);
  // A save clears the live candidate before the composer closes; keep describing and attaching
  // to the comment's own target instead of inventing a whole-source target meanwhile.
  let retained = $state.raw<FeedbackAnchorCandidate | null>(null);
  $effect(() => {
    const candidate = $snapshot.candidate, composing = $session.composer;
    if (candidate) retained = candidate; else if (!composing) retained = null;
  });
  const composed = $derived($snapshot.candidate ?? ($session.composer && ($snapshot.saving || $snapshot.pending_body) ? retained : null));
  const draftTarget = $derived(composed ? targetSummary({ locator: composed.locator, source: $snapshot.source?.source ?? null, sourceTitle: $snapshot.source?.title ?? null }) : null);
  const sourceStatus = $derived(!$snapshot.source && ($snapshot.open || $snapshot.mode !== 'view') && ($snapshot.loading || !!$snapshot.error));
  const listSurface = $derived(!!$session.surface && $session.surface !== 'compose' && !$session.choices.length);
  const showing = $derived(!!(($session.surface && !(docked && listSurface)) || $session.target || preview || sourceStatus));
  const selectionOnly = $derived(!!$session.target && !$session.composer && !sourceStatus);
  const inline = $derived($snapshot.source?.source.kind === 'work_diff' && !selectionOnly);
  const editorLocator = $derived($session.composer ? composed?.locator : preview?.anchor.locator ?? thread?.anchor.locator);
  let geometry = $state<FeedbackTargetGeometry | null>(null), avatars = $state<AvatarCluster[]>([]);
  const paint = $derived(targetPaint(geometry?.rects ?? []));
  let placement: { key: string; offset: FloatingOffset | null } = { key: '', offset: null };
  let left = $state(8), top = $state(80), width = $state(344), maxHeight = $state(700), narrow = $state(false);
  const SELECTION_ICON = 28;
  let slotted = $state(false);
  let textarea = $state<HTMLTextAreaElement>(), card = $state<HTMLElement>(), constraints = $state<HTMLElement>();
  let active = $state(false), accepting = $state(false), targetError = $state('');
  let usable = $state<FeedbackOverlayBounds>({ left: 8, top: 8, right: 8, bottom: 8, width: 0, height: 0 });
  let lastComposing = false;
  const compact = $derived(narrow && usable.height < 500);
  $effect(() => { const key = authority; untrack(() => port.session.authorize(key)); });
  $effect(() => { const owner = port.session; return () => untrack(() => owner.invalidate()); });
  $effect(() => {
    const composing = active && $session.composer;
    if (composing && !lastComposing) void tick().then(() => {
      if (active && $session.composer) textarea?.focus({ preventScroll: !inline });
    });
    lastComposing = composing;
  });
  $effect(() => {
    const owner = port, target = $session.target, candidate = composed, source = $session.bounds;
    const composing = $session.composer, selected = thread, hover = preview, surface = $session.surface, threads = $snapshot.threads;
    const boundary = constraints, element = card;
    if (!active) return;
    let frame = 0;
    function update() {
      const viewport = window.visualViewport;
      const visual = { left: viewport?.offsetLeft ?? 0, top: viewport?.offsetTop ?? 0,
        right: (viewport?.offsetLeft ?? 0) + (viewport?.width ?? window.innerWidth),
        bottom: (viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight) };
      const box = boundary?.getBoundingClientRect();
      const app = feedbackOverlayBounds(box?.width && box.height ? box : visual, visual);
      // A short inline source is not the floating host's height. Keep the canonical application
      // viewport, narrowing to the source's scroll viewport when it can hold the card, so chrome
      // stays beside the source instead of over sidebars or the message composer.
      const horizontal = source && source.width >= 344, vertical = source && source.height >= 240;
      const bounds = {
        ...app,
        left: horizontal ? Math.max(app.left, source.left) : app.left,
        right: horizontal ? Math.min(app.right, source.left + source.width) : app.right,
        top: vertical ? Math.max(app.top, source.top) : app.top,
        bottom: vertical ? Math.min(app.bottom, source.top + source.height) : app.bottom,
      };
      usable = { ...bounds, width: Math.max(0, bounds.right - bounds.left), height: Math.max(0, bounds.bottom - bounds.top) };
      narrow = usable.width < 720;
      geometry = target?.geometry() ?? (candidate && composing ? owner.geometry(candidate)
        : hover || surface === 'thread' && selected ? owner.geometry({ locator: (hover ?? selected)!.anchor.locator }) : null);
      // On phones the comment list is a near full-height sheet; selection and composer stay compact.
      maxHeight = Math.max(0, narrow ? usable.height * (surface && surface !== 'compose' ? .9 : .65) : usable.height);
      // Measuring visible content is a layout read; do it at most once and only when needed.
      let measured: ReturnType<typeof contentObstacles> | null = null;
      const content = () => measured ??= contentObstacles(app);
      if (element) {
        // The add-comment icon sits at the bottom-right end of the selection, beside its last line,
        // so native selection menus (shown above or below the line) do not cover it.
        const last = target && !composing ? geometry?.rects.at(-1) : null;
        // Renderers with a native control slot (diff line gutter) keep the icon in that slot.
        const slot = target && !composing ? target.controlGeometry?.()?.rects[0] : null;
        slotted = !!slot && slot.width > 0 && slot.height > 0 && slot.left >= usable.left && slot.top >= usable.top
          && slot.left + slot.width <= usable.right && slot.top + slot.height <= usable.bottom;
        if (slot && slotted) {
          left = slot.left; top = slot.top; width = Math.min(SELECTION_ICON, slot.width);
          maxHeight = Math.min(SELECTION_ICON, slot.height);
        } else if (last) {
          const beside = last.left + last.width + 4 + SELECTION_ICON <= usable.right;
          left = beside ? last.left + last.width + 4 : Math.max(usable.left, usable.right - SELECTION_ICON);
          top = Math.max(usable.top, Math.min(usable.bottom - SELECTION_ICON,
            beside ? last.top + last.height - SELECTION_ICON * 0.7 : last.top + last.height + 4));
          width = SELECTION_ICON; maxHeight = SELECTION_ICON;
        } else {
        const obstacles = content();
        const key = composing ? 'compose' : target ? 'target' : hover ? `preview:${hover.thread_id}` : `${surface ?? ''}:${selected?.thread_id ?? ''}`;
        if (placement.key !== key) placement = { key, offset: null };
        const placed = floatingPlacement(usable, geometry?.rects ?? [], {
          width: Math.min(target && !composing ? 44 : surface && surface !== 'compose' ? 384 : 344, usable.width),
          height: Math.min(target && !composing ? 44 : element.offsetHeight || 260, maxHeight),
        }, narrow && !(target && !composing), obstacles.ink, placement.offset, obstacles.surfaces);
        placement.offset = placed.offset;
        left = placed.left; top = placed.top; width = placed.width;
        }
      }
      const markerBounds = source ? {
        left: Math.max(app.left, source.left), top: Math.max(app.top, source.top),
        right: Math.min(app.right, source.left + source.width), bottom: Math.min(app.bottom, source.top + source.height),
        width: Math.max(0, Math.min(app.right, source.left + source.width) - Math.max(app.left, source.left)),
        height: Math.max(0, Math.min(app.bottom, source.top + source.height) - Math.max(app.top, source.top)),
      } : app;
      const markerTargets = threads.flatMap(value => {
        if (value.state !== 'open' || feedbackSourceState(value.anchor, $snapshot.source) !== 'current') return [];
        const rects = owner.geometry({ locator: value.anchor.locator })?.rects ?? [];
        const author = value.entries[0]?.author;
        return rects.length ? [{ id: value.thread_id, author: author?.label ?? 'Reviewer', guest: author?.kind === 'guest_unverified',
          label: value.entries[0]?.body ?? 'Comment', rects }] : [];
      });
      avatars = markerTargets.length ? avatarClusters(markerTargets, markerBounds, content().ink) : [];
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(() => { frame = 0; update(); }); }
    untrack(update);
    const observer = new ResizeObserver(schedule);
    if (element) observer.observe(element);
    if (boundary) observer.observe(boundary);
    const chrome = new MutationObserver(schedule);
    chrome.observe(document.documentElement, { attributes: true, attributeFilter: ['style'] });
    window.addEventListener('scroll', schedule, true); window.addEventListener('resize', schedule);
    viewportListen('addEventListener');
    function viewportListen(method: 'addEventListener' | 'removeEventListener') {
      window.visualViewport?.[method]('resize', schedule); window.visualViewport?.[method]('scroll', schedule);
    }
    return () => {
      if (frame) cancelAnimationFrame(frame);
      observer.disconnect(); chrome.disconnect(); window.removeEventListener('scroll', schedule, true); window.removeEventListener('resize', schedule);
      viewportListen('removeEventListener');
    };
  });
  function openAvatar(avatar: AvatarCluster) {
    if (avatar.ids.length > 1) port.session.chooseThreads(avatar.ids);
    else openThread(avatar.ids[0]);
  }
  function openThread(id: string) {
    // The docked list shows the thread; open it first so its sidebar tab is current.
    if (docked) port.openRail('threads');
    port.select(id);
  }
  function hoverMove(event: PointerEvent) {
    if (!$session.preview) return;
    if (event.buttons) { port.session.preview(null); return; }
    const avatar = avatars.find(value => value.ids.includes($session.preview!)), box = card?.getBoundingClientRect();
    if (!avatar || !box) { port.session.preview(null); return; }
    // The corridor is observational, never an overlay that intercepts selection.
    if (event.clientX < Math.min(avatar.left, box.left) - 8 || event.clientX > Math.max(avatar.left + 44, box.right) + 8
      || event.clientY < Math.min(avatar.top, box.top) - 8 || event.clientY > Math.max(avatar.top + 44, box.bottom) + 8) port.session.preview(null);
  }
  async function add() {
    const target = $session.target;
    if (!target || accepting || target.surface_id !== surfaceIdentity(port.binding())) return;
    accepting = true; targetError = '';
    try {
      await target.validate?.();
      if ($session.target === target && target.surface_id === surfaceIdentity(port.binding())) port.accept(target.candidate);
    } catch (caught) { if ($session.target === target) targetError = caught instanceof Error ? caught.message : 'Select the target again.'; }
    finally { accepting = false; }
  }
  async function save() {
    await port.save();
    if (!$snapshot.draft && !$snapshot.pending_body) {
      if ($snapshot.mode === 'draw' || $snapshot.mode === 'area') { port.setMode('comment'); port.session.setTool('text'); }
      // Saving ends the gesture; never open the new thread automatically. An open list comes back.
      port.select(''); port.session.close();
      returnToList();
    }
  }
  /** With the comment list open, a finished or cancelled comment returns to it (docked lists never left). */
  function returnToList() { if ($snapshot.open && !docked) port.openRail('threads'); }
  function close() {
    if (sourceStatus) port.setMode('view');
    // Cancel discards; an unconfirmed send stays recoverable until it is retried.
    if ($session.composer && !$snapshot.saving && !$snapshot.pending_body) void port.discard();
    // Cancelling a comment keeps an open list open; closing the list closes everything.
    port.binding()?.onInteractionChange(false);
    const composing = $session.composer;
    if (!composing) port.closeRail();
    port.session.close(); port.select('');
    if (composing) returnToList();
  }
</script>

<svelte:window onpointermove={hoverMove} onpointerdown={event => {
  if ($session.preview && !(event.target instanceof Element && event.target.closest('[data-feedback-avatar], [data-feedback-floating]'))) port.session.preview(null);
}} onkeydown={event => { if (active && event.key === 'Escape' && showing) { event.stopPropagation(); close(); } }} />
<div use:surfacePortal={value => active = value} class="contents" data-feedback-overlay hidden={!active} inert={!active} style:display={active ? undefined : 'none'}>
  <div bind:this={constraints} aria-hidden="true" class="pointer-events-none fixed" style:top="var(--app-floating-overlay-top)" style:bottom="var(--app-floating-overlay-bottom)" style:left="var(--app-floating-overlay-left)" style:right="var(--app-floating-overlay-right)"></div>
  <div class="pointer-events-none fixed z-[2147483098] overflow-hidden" aria-hidden="true" style:left={`${usable.left}px`} style:top={`${usable.top}px`} style:width={`${usable.width}px`} style:height={`${usable.height}px`}>
    {#each paint.rects as rect}<span data-feedback-target-highlight class="absolute rounded-[3px] bg-accent/20" class:ring-1={paint.block} class:ring-accent={paint.block} style:left={`${rect.left - usable.left}px`} style:top={`${rect.top - usable.top}px`} style:width={`${rect.width}px`} style:height={`${rect.height}px`}></span>{/each}
  </div>
  {#each $snapshot.source?.source.kind === 'work_diff' ? [] : avatars as avatar (avatar.ids.join('/'))}
    <button type="button" data-feedback-avatar class="fixed z-[2147483099] flex items-center justify-center rounded-full" style:left={`${avatar.left}px`} style:top={`${avatar.top}px`} style:width={`${MARKER_HIT}px`} style:height={`${MARKER_HIT}px`}
      aria-label={avatar.ids.length > 1 ? `${avatar.ids.length} comments at this target${avatar.guest ? ', includes Guest, unverified' : ''}` : `${avatar.author}${avatar.guest ? ', Guest, unverified' : ''}: ${avatar.label}`}
      onblur={event => { if (!(event.relatedTarget instanceof Element && event.relatedTarget.closest('[data-feedback-floating], [data-feedback-avatar]'))) port.session.preview(null); }}
      onclick={() => openAvatar(avatar)}>
      <span class="inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full bg-accent-subtle text-[10px] font-semibold leading-none text-accent transition-colors hover:bg-accent hover:text-on-accent" class:border-dashed={avatar.guest} style:width={`${MARKER_SIZE}px`} style:height={`${MARKER_SIZE}px`}>{#if avatar.ids.length > 1}{avatar.ids.length > 99 ? '99+' : avatar.ids.length}{:else}<svg class="h-3 w-3" viewBox="0 0 16 16" fill="none" stroke="currentColor" aria-hidden="true"><path d="M2 2h12v9H7l-5 3z" stroke-linejoin="round" /></svg>{/if}</span>
    </button>
  {/each}
  {#if showing}
    <section bind:this={card} use:inlinePortal={{ inline, active, host: () => port.editorHost?.(editorLocator) ?? null }} data-feedback-floating data-feedback-inline={inline || undefined} aria-label={$session.composer ? 'Add comment' : $session.target ? 'Selected target' : preview ? 'Comment preview' : 'Comments'}
      class="feedback-card fixed z-[2147483103] overflow-auto rounded-xl border border-separator bg-elevated text-sm text-foreground shadow-xl"
      class:p-3={!selectionOnly} class:rounded-full={selectionOnly}
      style:position={inline ? 'static' : undefined} style:box-shadow={inline ? 'none' : undefined}
      style:left={inline ? undefined : `${left}px`} style:top={inline ? undefined : `${top}px`} style:width={inline ? '100%' : `${width}px`} style:max-height={inline ? undefined : `${maxHeight}px`}>
      {#if sourceStatus}
        <div class="flex items-center justify-between gap-2">
          {#if $snapshot.loading}<p role="status" class="flex items-center gap-2 text-muted"><span class="h-3 w-3 animate-spin rounded-full border-2 border-accent/30 border-t-accent" aria-hidden="true"></span>Loading comments…</p>
          {:else}<p role="alert" class="text-danger">{$snapshot.error}</p>{/if}
          <button type="button" class="feedback-icon" aria-label="Close comments" onclick={close}>×</button>
        </div>
        {#if !$snapshot.loading}<button type="button" class="feedback-action text-accent" onclick={port.reload}>Retry loading comments</button>{/if}
      {:else if $session.target && !$session.composer}
        <button type="button" class="selection-comment flex items-center justify-center rounded-full text-muted hover:text-accent" style:width={slotted ? '100%' : `${SELECTION_ICON - 2}px`} style:height={slotted ? `${maxHeight - 2}px` : `${SELECTION_ICON - 2}px`} aria-label="Add comment" title="Add comment" disabled={accepting} onpointerdown={event => event.preventDefault()} onclick={add}>
          <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M21 15a3 3 0 0 1-3 3H8l-5 4V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3zM12 7v7m-3.5-3.5h7" stroke-linecap="round" stroke-linejoin="round" /></svg>
        </button>
        {#if targetError}<p role="alert" class="p-2 text-xs text-danger">{targetError}</p>{/if}
      {:else if $session.composer}
        <header><h2 class="font-semibold">Add comment</h2></header>
        {#if draftTarget}<div class="mt-1"><TargetQuote summary={draftTarget} /></div>{/if}
        <label class="mt-2 block"><span class="sr-only">Comment</span><textarea bind:this={textarea} placeholder="Write a comment…" class="min-h-20 w-full resize-y rounded-lg border border-separator bg-canvas px-2.5 py-2 text-sm placeholder:text-muted focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/30" style:min-height={compact ? '44px' : undefined} style:height={compact ? '56px' : undefined} value={$snapshot.draft} maxlength="16384" oninput={event => port.setDraft(event.currentTarget.value)}></textarea></label>
        {#if $snapshot.pending_body}<blockquote class="mt-1 whitespace-pre-wrap border-l-2 border-separator pl-2 text-sm text-muted" aria-label="Comment waiting to be sent">{$snapshot.pending_body}</blockquote>{/if}
        {#if $snapshot.error}<p role="alert" class="text-sm text-danger">{$snapshot.error}</p>{/if}
        <div class="mt-2 flex flex-wrap items-center gap-1">
          <Button variant="ghost" class="ml-auto" disabled={$snapshot.saving} onclick={close}>Cancel</Button>
          <Button disabled={$snapshot.saving || (!$snapshot.draft.trim() && !$snapshot.pending_body)} onclick={save}>{$snapshot.saving ? 'Sending…' : $snapshot.pending_body ? 'Retry original send' : 'Send'}</Button>
        </div>
      {:else if preview}
        {#if previewTarget}<TargetQuote summary={previewTarget} />{/if}
        <p class="mt-2 text-xs text-muted"><span class="font-medium text-foreground">{preview.entries[0]?.author.label}</span>{preview.entries[0]?.author.kind === 'guest_unverified' ? ' · Guest' : ''}{(preview.entries[0]?.version ?? 0) > 1 ? ' · Edited' : ''}</p>
        <p class="mt-0.5 max-h-40 overflow-auto whitespace-pre-wrap break-words">{preview.entries[0]?.body}</p>
        <Button variant="ghost" size="sm" class="-ml-3 mt-1 text-accent" onclick={() => openThread(preview.thread_id)}>{preview.entries.length > 1 ? `View ${preview.entries.length - 1} ${preview.entries.length === 2 ? 'reply' : 'replies'}` : 'Reply or resolve'}</Button>
      {:else if $session.choices.length}
        <header class="flex items-center justify-between gap-2"><h2 class="font-semibold">Comments at this target</h2><button type="button" class="feedback-icon" aria-label="Close comments" onclick={close}>×</button></header>
        <ul class="mt-1 divide-y divide-separator">
          {#each $snapshot.threads.filter(thread => $session.choices.includes(thread.thread_id)) as choice (choice.thread_id)}
            <li><Button variant="ghost" size="inline" class="block w-full rounded-md px-1 py-2 text-left hover:bg-surface-hover" onclick={() => openThread(choice.thread_id)}><span class="block text-xs text-muted"><span class="font-medium text-foreground">{choice.entries[0]?.author.label}</span>{choice.entries[0]?.author.kind === 'guest_unverified' ? ' · Guest' : ''}{choice.entries.length > 1 ? ` · ${choice.entries.length - 1} ${choice.entries.length === 2 ? 'reply' : 'replies'}` : ''}</span><span class="block line-clamp-2 break-words">{choice.entries[0]?.body}</span></Button></li>
          {/each}
        </ul>
      {:else}
        {#if comments}{@render comments()}{:else}<FeedbackComments {port} />{/if}
      {/if}
    </section>
  {/if}
</div>
