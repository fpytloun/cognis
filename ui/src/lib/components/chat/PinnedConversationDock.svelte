<script lang="ts">
  import { onMount, tick, untrack } from 'svelte';
  import { page } from '$app/stores';
  import { scale } from 'svelte/transition';
  import { flip } from 'svelte/animate';
  import GripVertical from 'lucide-svelte/icons/grip-vertical';
  import PanelRightClose from 'lucide-svelte/icons/panel-right-close';
  import PanelRightOpen from 'lucide-svelte/icons/panel-right-open';
  import ActivityAvatar from '$lib/components/ActivityAvatar.svelte';
  import WorkspaceVeil from '$lib/components/dashboard/WorkspaceVeil.svelte';
  import { conversationWorkspace } from '$lib/dashboard/conversation-workspace';
  import { aggregatePinnedActivity, pinnedActivityRank, PINNED_AVATAR_GAP, pinnedDockGeometry, pinnedAvatarPositions, pinnedThrow, pinnedEdgeGesture, pinnedBlockRows, isPinnedWorkspaceRoute, pinnedAvatarTuckOffset } from '$lib/dashboard/pinned-dock';
  import { blockingOverlayActive } from '$lib/stores/overlays';
  import { loadUserPreferences, userPreferences } from '$lib/stores/userPreferences';
  import { createPinnedPanel } from './pinned-panel.svelte';
  import PinnedConversationPanel from './PinnedConversationPanel.svelte';
  import { postNative } from '$lib/native/host';

  let { nativeRail = false }: { nativeRail?: boolean } = $props();
  let nativeSelected = $state<string | null>(null), nativeLeft = $state(false);
  let nativeHover = $state<string | null>(null), nativeRevision = -1;

  let root = $state<HTMLElement | null>(null);
  let width = $state(0), height = $state(0), shellTop = $state(0), avatarSize = $state(48);
  const railPadding = $derived(avatarSize / 4);
  let reducedMotion = $state(false), dragging = $state(false), expanded = $state(false), expandedUp = $state(false);
  let expandedOrder = $state<string[]>([]);
  let throwing = $state(false), hovered = $state(false), keyboardFocus = $state(false), suppressReveal = $state(false);
  let nudgeAgent = $state<string | null>(null), nudgeRevision = $state(0);
  const activitySignatures = new Map<string, string>();
  let drag: { start: number; screenStart: number; startX: number; y: number; moved: boolean; pointerId: number;
    lastY: number; lastX: number; lastTime: number; velocity: number; vx: number; dx: number } | null = null;
  let suppressClick = false, lastPointerType = '';
  const blockMode = $derived(!$userPreferences.display.pinned_agents_collapsed);
  const orderByActivity = $derived($userPreferences.display.pinned_agents_order_by_activity);
  const blockRows = $derived(Math.min($conversationWorkspace.agents.length, pinnedBlockRows(height, avatarSize)));
  const blockSpan = $derived(Math.max(0, blockRows - 1) * (avatarSize + PINNED_AVATAR_GAP));
  const visible = $derived((nativeRail || width >= 768)
    && !(isPinnedWorkspaceRoute($page.url.pathname) && $userPreferences.display.dashboard_workspace_windows)
    && !$page.url.searchParams.has('window') && $conversationWorkspace.agents.length > 0);
  const geometry = $derived(nativeRail
    ? { ...pinnedDockGeometry(width, height, 0, 0), y: 0, top: 0, bottom: Math.max(0, height - avatarSize), expandUp: false }
    : pinnedDockGeometry(width, height, $conversationWorkspace.position, shellTop, blockMode ? blockSpan : 0));
  const panel = createPinnedPanel({ visible: () => visible && !nativeRail, projectId: () => $page.params.projectId ?? null,
    onClose: () => { expanded = false; } });
  const groups = $derived(panel.groups);
  const selectedKey = $derived(nativeRail ? nativeSelected : panel.selectedKey);
  const selectedGroup = $derived(panel.group);
  const overall = $derived(aggregatePinnedActivity(groups.map((group) => group.state)));
  const orderedGroups = $derived(orderByActivity ? [...groups].sort((a, b) =>
    pinnedActivityRank(b.state) - pinnedActivityRank(a.state) || b.activityAt - a.activityAt) : groups);
  const avatarGroups = $derived(nativeRail || blockMode ? orderedGroups : expanded
    ? [...groups].sort((a, b) => expandedOrder.indexOf(a.id) - expandedOrder.indexOf(b.id)) : orderedGroups);
  const avatarPositions = $derived(pinnedAvatarPositions(groups.length, geometry.y, geometry.top, geometry.bottom, expandedUp, expanded, avatarSize));
  const duration = $derived(reducedMotion ? 0 : 150);
  const tucked = $derived($conversationWorkspace.tucked);
  const visuallyTucked = $derived(tucked && (nativeRail || !expanded) && !selectedKey && (suppressReveal || !(hovered || keyboardFocus)));
  const workspaceOwner = $derived($conversationWorkspace.owner), pathname = $derived($page.url.pathname);
  $effect(() => { if (workspaceOwner) void loadUserPreferences(workspaceOwner); });
  $effect(() => {
    if (nativeRail && groups.length === 0) postNative('rail', { action: 'empty' });
    else if (nativeRail && avatarSize > 0) postNative('rail', {
      action: 'size', width: avatarSize * 4 / 3 + railPadding * 2,
      height: Math.max(avatarSize, groups.length * (avatarSize + PINNED_AVATAR_GAP) - PINNED_AVATAR_GAP) + railPadding * 2
    });
  });
  function collapse(restoreFocus = false) {
    if (nativeRail) postNative('dismiss', {});
    panel.close(); expanded = false;
    if (restoreFocus) void tick().then(() => root?.querySelector<HTMLButtonElement>('[data-testid="pinned-chat-launcher"]')?.focus());
  }
  function prepareExpansion() { expandedUp = geometry.expandUp; expandedOrder = avatarGroups.map((group) => group.id); }
  function launch(group: (typeof groups)[number]) {
    if (suppressClick) { suppressClick = false; return; }
    if (nativeRail) { postNative('rail', { action: 'open', agentId: group.id }); return; }
    if (!blockMode && groups.length > 1 && !expanded) { prepareExpansion(); expanded = true; }
    else if (selectedGroup?.id === group.id) collapse();
    else {
      if (!expanded) prepareExpansion();
      expanded = groups.length > 1;
      void panel.openAgent(group.id);
      void tick().then(() => root?.querySelector<HTMLButtonElement>('[data-testid="pinned-chat-minimize"]')?.focus());
    }
  }
  function pointerDown(event: PointerEvent) {
    if (event.button !== 0 || drag || $blockingOverlayActive) return;
    if (event.pointerType === 'mouse') event.preventDefault();
    if (!nativeRail) (event.currentTarget as HTMLElement).focus({ preventScroll: true });
    suppressClick = false; lastPointerType = event.pointerType; throwing = false;
    drag = { start: event.clientY, screenStart: event.screenY, startX: event.clientX, y: geometry.y, moved: false, pointerId: event.pointerId,
      lastY: event.clientY, lastX: event.clientX, lastTime: event.timeStamp, velocity: 0, vx: 0, dx: 0 };
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
  }
  function pointerMove(event: PointerEvent) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const delta = event.clientY - drag.start, elapsed = event.timeStamp - drag.lastTime;
    if (elapsed > 0) { drag.velocity = (event.clientY - drag.lastY) / elapsed; drag.vx = (event.clientX - drag.lastX) / elapsed; }
    drag.dx = event.clientX - drag.startX;
    drag.lastY = event.clientY; drag.lastX = event.clientX; drag.lastTime = event.timeStamp;
    if (Math.abs(delta) > 5 || Math.abs(drag.dx) > 5) drag.moved = true;
    if (!drag.moved) return;
    dragging = true;
    if (nativeRail) {
      if (!suppressClick) postNative('rail', { action: 'drag-start' });
      suppressClick = true;
      postNative('rail', { action: 'drag-move' });
      return;
    }
    const range = geometry.bottom - geometry.top;
    conversationWorkspace.setPosition(range > 0 ? (drag.y + delta - geometry.top) / range : 0);
  }
  function pointerUp(event: PointerEvent, activate?: () => void) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const gesture = drag;
    suppressClick = drag.moved; drag = null; dragging = false;
    if (nativeRail) {
      if (gesture.moved) postNative('rail', { action: 'drag-end' });
      else if (event.type === 'pointerup') activate?.();
      return;
    }
    if (event.type === 'pointerup' && gesture.moved) {
      const fresh = event.timeStamp - gesture.lastTime < 100;
      const edge = pinnedEdgeGesture(gesture.dx, event.clientY - gesture.start, gesture.vx, fresh);
      if (edge === 'tuck') tuck();
      else if (edge === 'reveal') { conversationWorkspace.setTucked(false); suppressReveal = false; }
      else {
        const destination = pinnedThrow(geometry.y, fresh ? gesture.velocity : 0, geometry.top, geometry.bottom);
        const range = geometry.bottom - geometry.top;
        throwing = true;
        conversationWorkspace.setPosition(range > 0 ? (destination - geometry.top) / range : 0);
      }
    }
    if (event.type === 'pointerup' && event.pointerType === 'touch' && !suppressClick) activate?.();
  }
  function cancelDrag(): void {
    if (nativeRail && drag?.moved) postNative('rail', { action: 'drag-end' });
    drag = null; dragging = false; suppressClick = false; throwing = false;
  }
  function tuck(): void {
    void conversationWorkspace.execute({ type: 'tucked', value: true });
    suppressReveal = true; keyboardFocus = false;
    collapse();
  }
  function toggleTuck(): void {
    if (tucked) {
      void conversationWorkspace.execute({ type: 'tucked', value: false });
      suppressReveal = false;
    } else tuck();
  }
  function leaveDock(): void {
    hovered = false; suppressReveal = false;
  }
  function dismissBackdrop(): void {
    // Removing the full-screen backdrop need not emit a pointer-leave event.
    // Focus restoration must not retain the chat's keyboard-reveal state either.
    leaveDock();
    keyboardFocus = false;
    collapse(true);
  }
  function handleKey(event: KeyboardEvent): void {
    if (!visible || $blockingOverlayActive || event.defaultPrevented) return;
    if (root?.contains(event.target as Node)) keyboardFocus = true;
    if (event.key === 'Escape' && (selectedKey || expanded)) { event.preventDefault(); collapse(true); }
    if (event.altKey && ['ArrowUp', 'ArrowDown'].includes(event.key) && root?.contains(event.target as Node)) {
      if (nativeRail) {
        event.preventDefault();
        postNative('rail', { action: 'step', delta: event.key === 'ArrowUp' ? -32 : 32 });
        return;
      }
      event.preventDefault(); conversationWorkspace.setPosition($conversationWorkspace.position + (event.key === 'ArrowUp' ? -0.05 : 0.05));
    }
  }
  function outside(event: PointerEvent) {
    if (selectedKey) return;
    if (visible && (selectedKey || expanded) && !$blockingOverlayActive && !root?.contains(event.target as Node)) collapse();
  }
  $effect(() => {
    if (!visible) untrack(() => collapse());
    if (!visible || $blockingOverlayActive) untrack(cancelDrag);
  });
  $effect(() => {
    void pathname; void workspaceOwner;
    // Reset only for navigation/account changes, not reactive reads inside close().
    untrack(() => {
      hovered = false; keyboardFocus = false; suppressReveal = false;
      activitySignatures.clear(); collapse();
    });
  });
  $effect(() => {
    const ids = new Set(groups.map((group) => group.id));
    for (const id of activitySignatures.keys()) if (!ids.has(id)) activitySignatures.delete(id);
    for (const group of groups) {
      const signature = `${group.activityAt}:${group.state.error}:${group.state.attention}:${group.state.unread}`;
      const previous = activitySignatures.get(group.id); activitySignatures.set(group.id, signature);
      if (previous && previous !== signature && (group.state.error || group.state.attention || group.state.unread))
        untrack(() => { nudgeAgent = group.id; nudgeRevision++; });
    }
  });
  onMount(() => {
    const measure = () => {
      width = window.innerWidth; height = window.innerHeight;
      shellTop = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--app-shell-top-offset')) || 0;
      reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      avatarSize = parseFloat(getComputedStyle(document.documentElement).fontSize) * 3;
    };
    measure(); window.addEventListener('resize', measure);
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)'); motion.addEventListener('change', measure);
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('pointerup', pointerUp); document.addEventListener('pointercancel', pointerUp);
    document.addEventListener('lostpointercapture', pointerUp); window.addEventListener('blur', cancelDrag);
    document.addEventListener('visibilitychange', panel.observe); window.addEventListener('focus', panel.observe);
    const receiveRail = (event: Event) => {
      const state = (event as CustomEvent).detail;
      if (typeof state?.revision === 'number') {
        if (state.revision <= nativeRevision) return;
        nativeRevision = state.revision;
      }
      nativeSelected = typeof state?.selectedAgentId === 'string' ? state.selectedAgentId : null;
      nativeLeft = state?.side === 'left';
      if (typeof state?.pointerInside === 'boolean') {
        hovered = state.pointerInside;
        if (!hovered) { nativeHover = null; suppressReveal = false; }
        else if (typeof state.pointerX === 'number' && typeof state.pointerY === 'number') {
          nativeHover = Array.from(root?.querySelectorAll<HTMLElement>('[data-pinned-agent]') ?? []).find((button) => {
            const rect = button.getBoundingClientRect();
            return state.pointerX >= rect.left && state.pointerX < rect.right
              && state.pointerY >= rect.top && state.pointerY < rect.bottom;
          })?.dataset.pinnedAgent ?? null;
        }
      }
      if (state?.focused === false) {
        keyboardFocus = false;
        if (root?.contains(document.activeElement)) (document.activeElement as HTMLElement)?.blur();
      }
      if (state?.focused === true && !hovered) suppressReveal = false;
    };
    window.addEventListener('cognis-rail', receiveRail);
    if (nativeRail) window.addEventListener('cognis-rail-tuck', toggleTuck);
    if (nativeRail) postNative('rail', { action: 'ready' });
    return () => {
      cancelDrag(); window.removeEventListener('resize', measure); motion.removeEventListener('change', measure);
      document.removeEventListener('pointerdown', outside, true); document.removeEventListener('pointerup', pointerUp);
      document.removeEventListener('pointercancel', pointerUp); document.removeEventListener('lostpointercapture', pointerUp);
      window.removeEventListener('blur', cancelDrag); document.removeEventListener('visibilitychange', panel.observe);
      window.removeEventListener('focus', panel.observe);
      window.removeEventListener('cognis-rail', receiveRail);
      window.removeEventListener('cognis-rail-tuck', toggleTuck);
    };
  });
</script>

<svelte:window onkeydown={handleKey} />
{#if visible}
  <div bind:this={root} class="pointer-events-none fixed inset-0 z-[70]" class:invisible={$blockingOverlayActive}
    class:native-left={nativeRail && nativeLeft}
    data-testid="pinned-conversation-dock" role="presentation"
    onpointerdown={() => { keyboardFocus = false; }}
    onfocusin={(event) => {
      if (nativeRail && event.target instanceof HTMLElement && event.target.matches(':focus-visible')) {
        keyboardFocus = true; suppressReveal = false;
      }
    }}
    onfocusout={(event) => { if (!root?.contains(event.relatedTarget as Node | null)) keyboardFocus = false; }}>
    {#if selectedKey && !nativeRail}
      <WorkspaceVeil testId="pinned-chat-veil" />
      <button type="button" tabindex="-1" aria-label="Dismiss pinned conversation"
        class="pointer-events-auto fixed inset-0 cursor-default border-0 bg-transparent p-0"
        style:outline="none" style:box-shadow="none" data-testid="pinned-chat-dismiss-backdrop"
        onpointerdown={(event) => { event.preventDefault(); event.stopPropagation(); }}
        onclick={(event) => { event.stopPropagation(); dismissBackdrop(); }}></button>
    {/if}
    <div class="fixed right-0 w-16" class:native-dock={nativeRail}
      style:--rail-padding={`${railPadding}px`}
      style:top={`${geometry.y}px`} style:height={`${nativeRail ? height : blockMode ? blockSpan + avatarSize : avatarSize}px`}
      style:transition={`top ${throwing && !dragging && !reducedMotion ? 450 : 0}ms cubic-bezier(0.15, 0.8, 0.2, 1)`}
      data-tucked={tucked}
      data-visually-tucked={visuallyTucked}
      data-testid={blockMode ? 'pinned-agent-block' : expanded && groups.length > 1 ? 'pinned-agent-strip' : 'pinned-agent-stack'}
      role="presentation"
      onpointerenter={(event) => { if (event.pointerType !== 'touch') hovered = true; }}
      onpointerleave={leaveDock}>
      <div class:block-scroll={blockMode && !nativeRail} class:contents={!blockMode && !nativeRail} class:native-column={nativeRail}
        class:pointer-events-auto={blockMode || nativeRail} style={nativeRail ? `gap: ${PINNED_AVATAR_GAP}px` : blockMode ? `grid-template-rows: repeat(${blockRows}, ${avatarSize}px); grid-auto-columns: 4rem; row-gap: ${PINNED_AVATAR_GAP}px;` : undefined}
        data-testid={blockMode ? 'pinned-agent-block-scroll' : undefined}>
        {#each avatarGroups as group, index (group.id)}
          {@const spread = nativeRail || blockMode || expanded}
          <button type="button" animate:flip={{ duration: blockMode && !reducedMotion && !dragging ? 220 : 0 }}
            class="dock-avatar group pointer-events-auto right-0 top-0 h-12 w-16 select-none rounded-l-full focus-visible:outline-none"
            class:relative={nativeRail || blockMode} class:absolute={!nativeRail && !blockMode} class:touch-none={nativeRail || !blockMode || groups.length <= blockRows}
            class:native-avatar={nativeRail}
            style:transform={nativeRail || blockMode ? undefined : `translate(${avatarPositions[index].x}px, ${avatarPositions[index].y}px)`}
            style:z-index={groups.length - index} style:opacity={spread || index < 3 ? 1 : 0}
            style:transition={`transform ${reducedMotion || dragging ? 0 : 240}ms cubic-bezier(0.2, 0.8, 0.2, 1), opacity ${duration}ms`}
            inert={!spread && index > 0} aria-hidden={!spread && index > 0}
            aria-label={!spread && groups.length > 1 ? `Open ${groups.length} pinned agents` : `${groups.length === 1 ? 'Open pinned chat' : 'Open pinned chats'} with ${group.agent?.display_name ?? group.agent?.name ?? 'agent'}`}
            aria-expanded={index === 0 ? expanded || !!selectedKey : undefined}
            title="Pinned conversations · drag vertically or Alt+↑/↓ to move" data-testid={index === 0 ? 'pinned-chat-launcher' : undefined}
            data-pinned-agent={group.id} data-selected={nativeRail ? nativeSelected === group.id : selectedGroup?.id === group.id}
            data-native-hover={nativeRail && nativeHover === group.id}
            data-keyboard-focus={keyboardFocus}
            onpointerenter={() => { if (nativeRail) nativeHover = group.id; }}
            onpointerleave={() => { if (nativeRail) nativeHover = null; }}
            onpointerdown={pointerDown} onpointermove={pointerMove} onpointerup={(event) => pointerUp(event, () => launch(group))}
            onpointercancel={pointerUp} onlostpointercapture={pointerUp}
            onclick={(event) => {
              if (nativeRail) { if (event.detail === 0) { suppressClick = false; launch(group); } }
              else if (event.detail === 0 || lastPointerType !== 'touch') launch(group);
            }}>
            <span class="avatar-face absolute left-0 top-0 grid h-12 w-12 place-items-center rounded-full border border-control-border bg-surface shadow-card"
               style:transform={visuallyTucked ? `translateX(${nativeRail && nativeLeft ? '-' : ''}${pinnedAvatarTuckOffset(avatarSize)}px)` : ''}
              style:transition={`transform ${dragging || reducedMotion ? 0 : 220}ms ease-out`} data-testid="pinned-avatar-face">
              {#key nudgeRevision}<span class="grid place-items-center" class:nudge={!reducedMotion && !dragging && nudgeAgent === group.id}>
                <ActivityAvatar name={group.agent?.display_name ?? group.agent?.name ?? 'Agent'} avatarUrl={group.agent?.avatar_url} draggable={false}
                  state={!spread && index === 0 ? overall : group.state}
                  class={visuallyTucked ? 'h-10 w-10 [&>span.absolute]:left-0 [&>span.absolute]:right-auto' : 'h-10 w-10'} />
              </span>{/key}
              {#if !spread && index === 0 && groups.length > 1}<span class={`absolute -bottom-1 ${visuallyTucked ? '-left-1' : '-right-1'} rounded-full border border-control-border bg-elevated px-1.5 text-[10px] text-foreground`}>{groups.length}</span>{/if}
            </span>
          </button>
        {/each}
      </div>
    </div>
    {#if selectedKey && !nativeRail}
      <div class="pointer-events-auto fixed right-20" style:top={`${geometry.panelTop}px`} style:width={`${geometry.panelWidth}px`}
        style:height={`${geometry.panelHeight}px`} transition:scale={{ start: 0.97, duration }}>
        <PinnedConversationPanel {panel} onMinimize={() => collapse(true)}>
          {#snippet controls()}
            <button type="button" aria-label="Move pinned chat vertically" title="Drag vertically · Alt+↑/↓" class="touch-none rounded-lg p-1.5 text-quiet hover:text-foreground"
              onpointerdown={pointerDown} onpointermove={pointerMove} onpointerup={pointerUp} onpointercancel={pointerUp} onlostpointercapture={pointerUp}><GripVertical class="h-4 w-4" /></button>
            <button type="button" class="rounded-lg p-2 text-muted hover:bg-surface-hover hover:text-foreground"
              aria-label={tucked ? 'Untuck pinned avatars' : 'Tuck pinned avatars'} aria-pressed={tucked}
              title={tucked ? 'Keep avatars fully on-screen' : 'Hide half of the avatar behind the right edge'} onclick={toggleTuck}>
              {#if tucked}<PanelRightOpen class="h-4 w-4" />{:else}<PanelRightClose class="h-4 w-4" />{/if}
            </button>
          {/snippet}
        </PinnedConversationPanel>
      </div>
    {/if}
  </div>
{/if}

<style>
  .dock-avatar:focus, .dock-avatar:focus-visible { outline: none; box-shadow: none; }
  .native-dock { width: 100%; }
  /* Padding belongs inside the scrollport so first/last and side glows survive
     native viewport clipping. Tucking deliberately crosses that outer viewport. */
  .native-column { display: flex; flex-direction: column; align-items: flex-end; padding: var(--rail-padding) 0; overflow-y: auto; overflow-x: hidden; height: 100%; scrollbar-width: none; }
  .native-left .native-column { align-items: flex-start; }
  .native-column > button { flex-shrink: 0; }
  .native-left .avatar-face { left: 1rem; }
  .dock-avatar:not(.native-avatar):hover .avatar-face,
  .dock-avatar:not(.native-avatar):focus-visible .avatar-face,
  .native-avatar[data-native-hover="true"] .avatar-face,
  .native-avatar:focus-visible[data-keyboard-focus="true"] .avatar-face,
  .dock-avatar[data-selected="true"] .avatar-face {
    border-color: rgb(var(--color-accent)); box-shadow: 0 0 0 2px rgb(var(--color-accent) / 0.65);
  }
  .block-scroll {
    display: grid; grid-auto-flow: column;
    height: calc(100% + 8px); width: calc(100% + 12px);
    margin: -4px 0 -4px -12px; padding: 4px 0 4px 12px;
    overflow-x: auto; overflow-y: hidden; overscroll-behavior-x: contain;
    scroll-snap-type: x mandatory; scroll-padding-left: 12px;
    scrollbar-width: none; touch-action: pan-x;
  }
  .block-scroll::-webkit-scrollbar { display: none; }
  .nudge { animation: pinned-nudge 400ms ease-out; }
  @keyframes pinned-nudge { 30% { transform: translateX(-4px); } 65% { transform: translateX(2px); } }
</style>
