import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const childSource = readFileSync(resolve('src/lib/components/chat-v2/ChildChatView.svelte'), 'utf8');
const parentLinkSource = readFileSync(resolve('src/lib/components/chat-v2/ParentChatLink.svelte'), 'utf8');
const headerSource = readFileSync(resolve('src/lib/components/chat-v2/SessionLogHeader.svelte'), 'utf8');
const infoButtonSource = readFileSync(resolve('src/lib/components/session/SessionDetailsButton.svelte'), 'utf8');
const appCss = readFileSync(resolve('src/app.css'), 'utf8');
const pageSource = readFileSync(resolve('src/routes/(app)/chat/[conversationId]/+page.svelte'), 'utf8');
const layoutSource = readFileSync(resolve('src/routes/(app)/+layout.svelte'), 'utf8');

describe('ChildChatView shell ownership', () => {
  it('uses the shared scoped timeline, compact header, and info control', () => {
    expect(childSource).toContain('<ScopedChatV2Timeline');
    expect(childSource).toContain('<TimelineOngoingWorkDrawer');
    expect(childSource).toContain('childViewScope(view)');
    expect(childSource).not.toContain('Following latest');
    expect(childSource).toContain('testId="child-header-inspector"');
    expect(childSource).toContain('<SessionDetailsButton');
    expect(childSource).toContain('<SessionLogHeader');
    expect(childSource).not.toContain('aria-label="Info"');
    expect(childSource).toContain('onTodosChange={handleTodosChange}');
    expect(childSource).not.toContain('onTodosChange={(next)');
  });

  it('keeps one Inspector toggle, lifecycle actions in Session, and desktop PWA window opening', () => {
    const headerTestId = pageSource.indexOf('data-testid="chat-header"');
    const header = pageSource.slice(
      pageSource.lastIndexOf('<div', headerTestId),
      pageSource.indexOf('{#if chatSearchOpen}'),
    );
    expect(header).toContain('{#if !isWindowMode && canOpenAuxiliaryWindow}');
    expect(header).not.toContain('!isStandalonePwa && !isWindowMode');
    expect(header).not.toContain('data-testid="chat-header-work"');
    expect(header).not.toContain('>Archive<');
    expect(header).not.toContain('>Delete<');
    expect(pageSource).toContain('onArchive={canManageInspectorConversationLifecycle ? archiveConversation : undefined}');
    expect(pageSource).toContain('onDelete={canManageInspectorConversationLifecycle ? deleteConversation : undefined}');
  });

  it('mounts only in the middle grid column and leaves Inspector independent', () => {
    expect(pageSource).toContain('data-testid="child-middle-column"');
    expect(pageSource).toContain("inspectorPinned ? 'relative col-start-1 row-start-1 row-end-4' : 'absolute inset-0'");
    expect(pageSource.indexOf('<ConversationInfoDrawer')).toBeLessThan(
      pageSource.indexOf('data-testid="child-middle-column"'),
    );
    expect(pageSource).not.toContain('Sub-session drawer overlay');
    expect(pageSource).not.toContain('absolute inset-0 z-30');
  });

  it('keeps the parent timeline mounted but hidden and inert in child mode', () => {
    const headerTestId = pageSource.indexOf('data-testid="chat-header"');
    expect(pageSource.lastIndexOf('{#if !childView}', headerTestId)).toBeGreaterThan(-1);
    expect(pageSource).toContain('data-testid="parent-chat-surface"');
    expect(pageSource).toContain('class:contents={!childView}');
    expect(pageSource).toContain('class:hidden={Boolean(childView)}');
    expect(pageSource).toContain('inert={childView ? true : undefined}');
    expect(pageSource).toContain("aria-hidden={childView ? 'true' : undefined}");
    expect(pageSource).not.toContain('{#if !childView}\n      <!-- Message area + composer -->');
    const messageBranchStart = pageSource.indexOf('data-testid="parent-chat-surface"');
    const messageBranchEnd = pageSource.indexOf('{#if childView && childWorkstream}', messageBranchStart);
    const footerChrome = pageSource.indexOf('<div bind:this={footerChromeEl} class="shrink-0 space-y-3">');
    expect(footerChrome).toBeGreaterThan(messageBranchStart);
    expect(footerChrome).toBeLessThan(messageBranchEnd);
    expect(pageSource).toContain('if (node.key === node.root_key)');
    expect(pageSource).toContain('closeSubSessionPanel();');
    expect(pageSource).toContain('{runtimeActiveSessionIds}');
    // The handler binds the session ID at render time via an IIFE so a
    // delayed/async event cannot misattribute to a different, later-selected
    // child once the user has navigated away.
    expect(pageSource).toContain('onRuntimeActiveChange={((boundSessionId: string) => (active: boolean)');
    expect(pageSource).toContain('isTerminalSessionStatus(currentNode?.status)');
    expect(pageSource).toContain('updateRuntimeActiveSessions(');
    expect(pageSource).toContain('if (!timelineEl || childView) return;');
    expect(pageSource).toContain(
      'if (!timelineEl || childView || programmaticScroll || timelineWindowPending || !timelineInitialPositionSettled) return;',
    );
    expect(pageSource).toContain('parentSurfaceScrollTop = timelineEl?.scrollTop');
    expect(pageSource).toContain('writeProgrammaticScrollTop(parentSurfaceScrollTop);');
    expect(pageSource).toContain('scrollToBottom();');
    expect(pageSource).toContain('transitionGeneration !== parentSurfaceTransitionGeneration');
  });

  it('blocks synthetic top backfill until the initial parent position settles', () => {
    const scrollHandler = pageSource.slice(
      pageSource.indexOf('function handleTimelineScroll'),
      pageSource.indexOf('function jumpToBottom'),
    );
    expect(scrollHandler).toContain('timelineInitialPositionSettled');
    expect(scrollHandler.indexOf('timelineInitialPositionSettled')).toBeLessThan(
      scrollHandler.indexOf('void loadOlder();'),
    );
    expect(pageSource).toContain('timelineInitialPositionSettled = false;');
    expect(pageSource).toContain('timelineInitialPositionSettled = true;');
  });

  it('keeps timeline content visible while initial scroll restoration settles', () => {
    const timelineMarkup = pageSource.slice(
      pageSource.indexOf('<!-- Timeline -->'),
      pageSource.indexOf('<div bind:this={footerChromeEl}'),
    );
    expect(timelineMarkup).not.toContain("? 'invisible' : ''");
    expect(timelineMarkup).toContain('interactionEnabled={timelineInitialPositionSettled}');
  });

  it('dismisses a transient Inspector before mounting the child and disables idle right-edge capture', () => {
    const navigation = pageSource.slice(
      pageSource.indexOf('function handleViewSession'),
      pageSource.indexOf('function navigateToStructuralParent'),
    );
    expect(navigation).toContain('if (navigation.closeInspectorOverlay && headerInfoOpen)');
    expect(navigation.indexOf('closeHeaderInfo(false);')).toBeLessThan(
      navigation.indexOf('childView = nextChildView;'),
    );
    expect(pageSource).toContain("edge: 'right',");
    expect(pageSource).toContain('disabled: !mobileListOpen,');
    expect(pageSource).toContain('data-edge-swipe-surface="true"');
  });

  it('cancels focused overview requests before child close and root back navigation', () => {
    const rootBack = pageSource.slice(
      pageSource.indexOf('function navigateToStructuralParent'),
      pageSource.indexOf('function closeSubSessionPanel'),
    );
    const close = pageSource.slice(
      pageSource.indexOf('function closeSubSessionPanel'),
      pageSource.indexOf('async function runChildManagedAction'),
    );

    expect(rootBack.indexOf('backChildViewToRoot(cancelActivityOverviewLoad')).toBeGreaterThanOrEqual(0);
    expect(rootBack.indexOf('backChildViewToRoot(cancelActivityOverviewLoad')).toBeLessThan(
      rootBack.indexOf('focusedSessionId = null;'),
    );
    expect(close.indexOf('closeChildViewToRoot(cancelActivityOverviewLoad')).toBeGreaterThanOrEqual(0);
    expect(close.indexOf('closeChildViewToRoot(cancelActivityOverviewLoad')).toBeLessThan(
      close.indexOf('childView = null;'),
    );
  });

  it('drops only the departed session from the runtime-active set on navigation and close, never the whole set', () => {
    const navigation = pageSource.slice(
      pageSource.indexOf('function handleViewSession'),
      pageSource.indexOf('function navigateToStructuralParent'),
    );
    const close = pageSource.slice(
      pageSource.indexOf('function closeSubSessionPanel'),
      pageSource.indexOf('async function runChildManagedAction'),
    );
    expect(navigation).toContain('const leavingSessionId = childView?.sessionId;');
    expect(navigation).toContain('next.delete(leavingSessionId);');
    expect(navigation).not.toMatch(/runtimeActiveChildSessionIds = new Set\(\);/);
    expect(close).toContain('next.delete(childView.sessionId);');
    expect(close).not.toMatch(/runtimeActiveChildSessionIds = new Set\(\);/);
  });

  it('routes child focus through the visibility-gated overview demand loader', () => {
    const focus = pageSource.slice(
      pageSource.indexOf('function focusInspectorSession'),
      pageSource.indexOf('$effect(() => {', pageSource.indexOf('function focusInspectorSession')),
    );
    expect(focus).toContain('tick().then(() => loadVisibleActivityOverview())');
    expect(focus).not.toContain('tick().then(() => loadActivityOverview())');
  });

  it('reserves exact Activity Overview aggregation for explicit refreshes', () => {
    const overviewLoaders = pageSource.slice(
      pageSource.indexOf('async function loadRootActivityOverview'),
      pageSource.indexOf('function cancelActivityOverviewDemand'),
    );
    expect(overviewLoaders.match(/exactLoader:/g)).toHaveLength(3);
    expect(overviewLoaders.match(/\.\.\.\(force \? \{/g)).toHaveLength(3);
  });

  it('keeps stale-runtime recovery on canonical Chat v2 sync', () => {
    const guardStart = pageSource.indexOf('staleRuntimeGuardTimer = window.setInterval');
    const guard = pageSource.slice(
      guardStart,
      pageSource.indexOf('scheduleHistorySectionDayRefresh', guardStart),
    );
    expect(guard).toContain('recoverChatV2Canonical(currentConversation.conversation_id)');
    expect(guard).not.toContain("refreshConversationView('stale-runtime-ttl')");
  });

  it('refreshes overview only from scoped post-materialization invalidation', () => {
    const invalidationEffect = pageSource.slice(
      pageSource.indexOf('const invalidateOverview = (event: Event): void =>'),
      pageSource.indexOf('function toggleWorkView'),
    );
    const socketHandler = pageSource.slice(
      pageSource.indexOf('function handleSocketEvent'),
      pageSource.indexOf('function handleRealtimeDelivery'),
    );
    expect(invalidationEffect).toContain('invalidateActivityOverview(');
    expect(invalidationEffect).toContain('detail?.workRevision');
    expect(socketHandler).not.toContain('eventHasCompletedToolResult');
    expect(socketHandler).not.toContain('invalidateActivityOverview(');
  });

  it('refreshes a focused inspector scope without requiring child-view mode', () => {
    const scheduler = pageSource.slice(
      pageSource.indexOf('function scheduleTreeRefresh'),
      pageSource.indexOf('function cancelScheduledTreeRefresh'),
    );
    expect(scheduler).toContain('treeReconciliation.invalidate();');
    expect(scheduler).not.toContain('if (childView)');
  });

  it('awaits every rendered overview source before reporting child reconciliation success', () => {
    const rootLoader = pageSource.slice(
      pageSource.indexOf('async function loadRootActivityOverview'),
      pageSource.indexOf('function cancelRootOverviewLoad'),
    );
    const visibleLoader = pageSource.slice(
      pageSource.indexOf('async function loadVisibleActivityOverview'),
      pageSource.indexOf('onDestroy(cancelActivityOverviewLoad)'),
    );
    expect(rootLoader).toContain('Promise<boolean>');
    expect(rootLoader).toContain('rootApplied = await promoteRootOverview(');
    expect(rootLoader).toContain('return rootApplied;');
    expect(visibleLoader).toContain('return reconcileRenderedOverviewSources({');
    expect(visibleLoader).toContain('loadRoot: () => loadRootActivityOverview(force)');
    expect(visibleLoader).toContain('loadFocused: () => loadActivityOverview(force)');
    expect(visibleLoader).not.toContain('void loadRootActivityOverview(force)');
  });

  it('keeps hidden clients stale without issuing overview requests', () => {
    const invalidationEffect = pageSource.slice(
      pageSource.indexOf('const invalidateOverview = (event: Event): void =>'),
      pageSource.indexOf('function toggleWorkView'),
    );
    expect(invalidationEffect).toContain('if (document.hidden) return;');
    expect(invalidationEffect.indexOf('if (document.hidden) return;')).toBeLessThan(
      invalidationEffect.indexOf('scheduleTreeRefresh();'),
    );
    const scheduler = pageSource.slice(
      pageSource.indexOf('function scheduleTreeRefresh'),
      pageSource.indexOf('function cancelScheduledTreeRefresh'),
    );
    expect(scheduler).toContain('if (document.hidden) return;');
  });

  it('catches up root and focused overview scopes after visibility returns', () => {
    const visibilityHandler = pageSource.slice(
      pageSource.indexOf('visibilityHandler = () =>'),
      pageSource.indexOf('focusHandler = () =>'),
    );
    expect(visibilityHandler).toContain('if (!document.hidden)');
    expect(visibilityHandler).toContain('scheduleTreeRefresh();');
  });

  it('forces visible tree reconciliation after reconnect', () => {
    const reconnect = pageSource.slice(
      pageSource.indexOf("if (event.type === 'reconnected')"),
      pageSource.indexOf("if (event.type === 'workflow_step_question'"),
    );
    expect(reconnect).toContain('invalidateWorkScope(');
    expect(reconnect).toContain('scheduleTreeRefresh();');
  });

  it('hydrates exact focused identity without Intaris details when the graph is missing it', () => {
    const focus = pageSource.slice(
      pageSource.indexOf('function focusInspectorSession'),
      pageSource.indexOf('$effect(() => {', pageSource.indexOf('function focusInspectorSession')),
    );
    expect(pageSource).toContain('new FocusedSessionIdentityLoader(');
    expect(pageSource).toContain('api.sessions.detail(sessionId, { signal })');
    expect(focus).toContain('focusedSessionIdentityLoader.resolve(canonicalSessionId)');
    expect(focus).not.toContain('intarisDetail');
  });

  it('renders pending and actual-error states without a layout-shifting refresh label', () => {
    const overviewPanel = pageSource.slice(
      pageSource.indexOf("{#if headerInfoMode === 'overview' || headerInfoMode === 'context'}"),
      pageSource.indexOf("{:else if headerInfoMode === 'work'}"),
    );
    expect(overviewPanel).toContain('overviewReadPresentation.loading');
    expect(overviewPanel).not.toContain('Refreshing activity overview');
    expect(overviewPanel).toContain('overviewReadPresentation.error');
    expect(overviewPanel).not.toContain("?? 'Unable to load activity overview.'");
  });

  it('cancels all page-owned overview demand on mode transitions and inspector close', () => {
    const cancelDemand = pageSource.slice(
      pageSource.indexOf('function cancelActivityOverviewDemand'),
      pageSource.indexOf('function loadVisibleActivityOverview'),
    );
    const openWork = pageSource.slice(
      pageSource.indexOf('function openInspectorWork'),
      pageSource.indexOf('function focusInspectorSession'),
    );
    const closeInspector = pageSource.slice(
      pageSource.indexOf('function closeHeaderInfo'),
      pageSource.indexOf('$effect(() => {', pageSource.indexOf('function closeHeaderInfo')),
    );
    const tabChange = pageSource.slice(
      pageSource.indexOf('testIdPrefix="conversation-info"'),
      pageSource.indexOf('/>', pageSource.indexOf('testIdPrefix="conversation-info"')),
    );

    expect(cancelDemand).toContain('cancelScheduledTreeRefresh();');
    expect(cancelDemand).toContain('cancelRootOverviewLoad();');
    expect(cancelDemand).toContain('cancelActivityOverviewLoad();');
    expect(openWork.indexOf('cancelActivityOverviewDemand();')).toBeLessThan(
      openWork.indexOf("conversationInfoDrawer.mode = 'work';"),
    );
    expect(closeInspector.indexOf('cancelActivityOverviewDemand();')).toBeLessThan(
      closeInspector.indexOf('conversationInfoDrawer.close();'),
    );
    expect(tabChange).toContain("if (id !== 'overview') cancelActivityOverviewDemand();");
    expect(pageSource).toContain('else {\n                cancelActivityOverviewDemand();\n              }');
  });
});

describe('ChildChatView PWA-safe theme and layout', () => {
  it('shares the main chat surface instead of a mismatched dark overlay', () => {
    expect(childSource).not.toContain('bg-slate-950/95');
    expect(childSource).toMatch(/data-testid="child-chat-view"[^>]*>/s);
    expect(childSource).toContain('bg-transparent');
  });

  it('clears the bottom protected area on the root surface', () => {
    expect(childSource).toContain('padding-bottom: var(--app-bottom-control-inset);');
  });

  it('inherits the route-owned top safe area while protecting horizontal edges', () => {
    const header = headerSource;
    expect(header).not.toContain('env(safe-area-inset-top)');
    expect(header).toContain('env(safe-area-inset-left)');
    expect(header).toContain('env(safe-area-inset-right)');
    expect(header).toContain('app-keyboard-stable-header chat-header-shell');
    expect(layoutSource).toContain('app-chat-shell-safe');
    expect(layoutSource).toContain('app-chat-mobile-safe-top');
    expect(header).not.toContain('app-overlay-inner');
  });

  it('gives Back, Inspector, and Close 40-44px touch targets on narrow/coarse screens with compact desktop sizes', () => {
    const header = childSource.slice(childSource.indexOf('<SessionLogHeader'), childSource.indexOf('</SessionLogHeader>'));
    expect(header).toContain('<ParentChatLink');
    expect(parentLinkSource).toContain('touch-target-compact');
    expect(infoButtonSource).toContain('touch-target-compact');
    expect(header).toContain('touch-target-compact');
    expect(appCss).toMatch(/\.touch-target-compact\s*\{[^}]*height: 44px/s);
    expect(appCss).toMatch(/\.touch-target-compact\s*\{[^}]*height: 2rem/s);
  });

  it('keeps only Search and Info actions and puts the avatar in the metadata line', () => {
    const header = childSource.slice(childSource.indexOf('<SessionLogHeader'), childSource.indexOf('</SessionLogHeader>'));
    expect(header).not.toContain('Copy canonical session ID');
    expect(header).not.toContain('ExternalLink');
    const metadata = header.slice(header.indexOf('{#snippet metadata()}'), header.indexOf('{#snippet actions()}'));
    expect(metadata).toContain('<AgentAvatar');
    expect(metadata).toContain('class="h-4 w-4 shrink-0"');
    expect(header).not.toContain('view.sessionId.slice');
  });

  it('never overlaps header actions: title stays min-width-constrained and actions stay shrink-0', () => {
    const header = headerSource;
    expect(header).toContain('<h2 class="truncate text-sm font-semibold text-foreground">{title}</h2>');
    expect(header).toContain('class="min-w-0 flex-1"');
    expect(header).toContain('class="flex shrink-0 items-center gap-1"');
  });
});
