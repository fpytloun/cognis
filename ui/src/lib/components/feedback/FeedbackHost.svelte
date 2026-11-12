<script lang="ts">
  import { onMount, tick, untrack, type Snippet } from 'svelte';
  import { getFeedbackContext } from '$lib/feedback/context';
  import { documentCoordination } from '$lib/document-coordination';
  import { getDocumentPeopleLoader, getDocumentSharing, provideDocumentSharing } from '$lib/document-sharing-context';
  import type { FeedbackController } from '$lib/feedback/state';
  import type { FeedbackContext, FeedbackSourceRef } from '$lib/feedback/types';
  import FeedbackSourceProvider from './FeedbackSourceProvider.svelte';
  import { loadFeedbackController } from '$lib/feedback/controller-loader';
  import FeedbackChromeMount from './FeedbackChromeMount.svelte';
  import { panelChrome, inlineChrome } from './panel-chrome';
  import { getCommentWorkspace } from '$lib/feedback/comment-workspace';
  import { feedbackApi } from '$lib/feedback/api';
  import { commentStatus, needsReviewer } from '$lib/feedback/thread-status';
  let presentationGuard = $state<(() => void) | undefined>();
  let { source, context, children, standalone = false, onBusyChange }: { source: FeedbackSourceRef | null; context?: FeedbackContext; children: Snippet; standalone?: boolean; onBusyChange?: (busy: boolean) => void } = $props();
  const origin = getFeedbackContext();
  const parentWorkspace = getCommentWorkspace();
  const workspace = $derived(source?.kind === 'document' ? undefined : parentWorkspace);
  let controller = $state<FeedbackController | null>(null);
  let initError = $state('');
  let opening = $state(false);
  let activeKey = '';
  let openGeneration = 0;
  let alive = true;
  let mounted = $state(false);
  let commentCount = $state<number | null | undefined>();
  let docked = $state(false);
  // Document badges are read independently of the lazy annotation controller.
  // Transcript/work hosts stay lazy: a chat can contain hundreds of them.
  $effect(() => {
    if (!mounted || source?.kind !== 'document' || standalone) return;
    void loadedThreads;
    const requestedSource = source;
    const requestedContext = context ?? origin.context;
    const abort = new AbortController();
    let request = 0;
    commentCount = null;
    async function refreshCount() {
      const generation = ++request;
      try {
        const lease = documentCoordination().capture();
        const signal = AbortSignal.any([abort.signal, lease.signal]);
        const resolved = await feedbackApi.resolve(requestedSource, requestedContext, signal);
        lease.assertCurrent();
        let total = 0;
        let cursor: string | undefined;
        if (resolved.capabilities.read) {
          do {
            const page = await feedbackApi.threads(resolved.subject_id, cursor, signal);
            lease.assertCurrent();
            // The badge counts what waits on the reviewer: unsent, answered or failed comments.
            total += page.items.filter(thread => !thread.hidden && needsReviewer(commentStatus(thread))).length;
            cursor = page.next_cursor ?? undefined;
          } while (cursor && !signal.aborted && generation === request);
        }
        if (!signal.aborted && generation === request) commentCount = total;
      } catch {
        // A badge must not retain private counts after failed revalidation.
        if (!abort.signal.aborted && generation === request) commentCount = null;
      }
    }
    const invalidate = documentCoordination().onInvalidated(() => {
      request++; abort.abort(); commentCount = null;
    });
    const refresh = () => { void refreshCount(); };
    window.addEventListener('feedback-changed', refresh);
    refresh();
    return () => {
      request++; abort.abort(); invalidate();
      window.removeEventListener('feedback-changed', refresh);
      commentCount = undefined;
    };
  });
  const snapshot = $derived(controller?.state);
  const loadedThreads = $derived($snapshot?.threads);
  const presenter = $derived(controller?.presenter.state);
  $effect(() => { if (controller) return workspace?.register(controller); });
  // A document inside a chat follows the chat's comment mode (comments shown), so its tables and
  // blocks get the crosshair picker without a separate toggle.
  const parentState = parentWorkspace?.state;
  let followedChatMode = false;
  $effect(() => {
    if (source?.kind !== 'document' || !parentState) return;
    const enabled = $parentState!.enabled;
    untrack(() => {
      if (enabled) { followedChatMode = true; void open('threads', true, false, true); }
      else if (followedChatMode) {
        // Supersede a still-loading open from enabling, so it cannot switch comment mode back on.
        followedChatMode = false; openGeneration++; opening = false;
        controller?.presentation().setMode('view');
      }
    });
  });
  $effect(() => {
    if (source?.kind !== 'document' || !parentWorkspace) return;
    return parentWorkspace.registerDocumentLocator(source, async thread => {
      await open('threads', undefined, false, true);
      await tick();
      return controller ? controller.locate(thread) : false;
    });
  });
  $effect(() => {
    const inherited = context ?? origin.context;
    const key = JSON.stringify({ source, context: inherited });
    if (key === activeKey) return;
    activeKey = key;
    untrack(() => {
      controller?.dispose();
      openGeneration++;
      controller = null; initError = ''; opening = false;
    });
  });
  onMount(() => { mounted = true; return () => { alive = false; openGeneration++; controller?.dispose(); }; });
  const binding = $derived.by(() => { void $presenter; return controller && $snapshot ? controller.binding($snapshot) : null; });
  const parentSharing = getDocumentSharing();
  const peopleLoader = getDocumentPeopleLoader();
  const initialSource = untrack(() => source);
  if (!parentSharing || initialSource?.kind === 'document' && parentSharing.contentRef !== initialSource.content_ref) {
    provideDocumentSharing({
      get contentRef() { return source?.kind === 'document' ? source.content_ref : null; },
      get canManage() {
        return !standalone && source?.kind === 'document' && binding?.source.source.kind === 'document'
          && binding.source.source.content_ref === source.content_ref && binding.source.capabilities.disclose;
      },
      capture: () => documentCoordination().capture(),
      loadPeople: peopleLoader,
    });
  }
  $effect(() => { onBusyChange?.(Boolean($snapshot?.saving || $snapshot?.interacting || $snapshot?.draft.length
    || $presenter?.composer || $presenter?.target || $snapshot?.open && $snapshot.view === 'review')); });
  async function open(view: 'threads' | 'review' | 'history' | 'access' = 'threads', commenting?: boolean, whole = false, prepare = false) {
    if (!source) return;
    const generation = ++openGeneration;
    const requestedSource = source;
    const requestedContext = context ?? origin.context;
    const requestedStandalone = standalone;
    opening = !prepare; initError = '';
    try {
      const lease = requestedStandalone ? null : documentCoordination().capture();
      const state = await loadFeedbackController();
      if (!alive || generation !== openGeneration) return;
      lease?.assertCurrent();
      controller ??= new state.FeedbackController(requestedSource, requestedContext, requestedStandalone);
      presentationGuard = lease?.assertCurrent;
    } catch (caught) {
      if (!alive || generation !== openGeneration) return;
      initError = caught instanceof Error ? caught.message : 'Feedback is unavailable until the account is verified.';
      opening = !prepare; return;
    }
    initError = ''; opening = false;
    const current = controller;
    if (commenting === undefined && !prepare) current.presentation().openRail(view);
    else if (commenting !== undefined) current.patch({ mode: commenting ? 'comment' : 'view' });
    if (!$snapshot?.source && !$snapshot?.loading) await current.load();
    if (!alive || generation !== openGeneration) return;
    if (whole) current.binding($snapshot!)?.onAnchorCandidate({ locator: { kind: 'whole' } });
  }
  function close() { openGeneration++; opening = false; controller?.presentation().closeRail(); }
  /** The comment button: open the list with comment mode on, or close both. */
  async function show() {
    if ($snapshot?.open) { close(); return; }
    await open('threads');
    if (controller && $snapshot?.open && $snapshot.mode === 'view') controller.patch({ mode: 'comment' });
  }
  function commenting(enabled: boolean) { controller?.presenter.close(false); controller?.patch({ interacting: false }); void open('threads', enabled); }
</script>
{#snippet panel()}
  {#if initError}<p role="alert" class="p-3 text-sm text-danger">{initError}</p><button type="button" class="min-h-11 p-3 text-accent" onclick={() => open()}>Retry feedback</button>
  {:else if controller}<FeedbackChromeMount definition={panelChrome} viewProps={{ controller }} guard={presentationGuard} onClose={close} />{:else}<p role="status" class="p-3 text-sm">Loading feedback…</p><button type="button" class="min-h-11 p-3" onclick={close}>Close comments</button>{/if}
{/snippet}
<FeedbackSourceProvider {binding} {panel} {commentCount} workspaceOwned={!!workspace} onShow={show} railOpen={Boolean($snapshot?.open)} onDockChange={value => { docked = value; }} layout={$presenter?.layout} onBoundsChange={controller ? controller.presenter.setBounds.bind(controller.presenter) : undefined} available={Boolean(source)} open={opening || Boolean($presenter?.surface)} onOpen={open} onClose={close} onCommenting={commenting}
  onPrepare={() => open('threads', undefined, false, true)}
  onWhole={() => void open('threads', true, true, true)}
  onMode={mode => { controller?.presentation().setMode(mode); controller?.presenter.setTool(mode === 'draw' ? 'draw' : mode === 'area' ? 'area' : 'text'); }}>
  {@render children()}
</FeedbackSourceProvider>
{#if controller && !workspace}<FeedbackChromeMount definition={inlineChrome} viewProps={{ controller, comments: panel, docked }} guard={presentationGuard} />{/if}
<!-- A docked sidebar already shows the loading panel. -->
{#if !docked}
{#if opening && !controller}<section aria-label="Feedback" class="fixed bottom-4 right-4 z-[2147483103] max-h-[70dvh] w-80 max-w-[calc(100vw-32px)] overflow-auto rounded-xl border border-separator bg-surface p-3 shadow-xl">{@render panel()}</section>{/if}
{/if}
