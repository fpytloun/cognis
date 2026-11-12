<script lang="ts">
  import type { Snippet } from 'svelte';
  import { provideFeedbackSurface } from '$lib/feedback/context';
  import type { FeedbackMode, FeedbackSurfaceBinding, FeedbackVisibleArea } from '$lib/feedback/types';
  import type { FeedbackSourceLayout } from '$lib/feedback/presenter-session';
  let { binding, commentCount, onOpen, onClose = () => undefined, onCommenting, onRequestCommentAccess, onPrepare, onWhole, onMode, onResume, onShow, railOpen = false, onDockChange, workspaceOwned = false, children, panel, open = false, available, layout = null, onBoundsChange }: {
    binding: FeedbackSurfaceBinding | null; onOpen: (view?: 'threads' | 'review' | 'history' | 'access') => void; onClose?: () => void; onCommenting?: (enabled: boolean) => void; children: Snippet; panel?: Snippet; open?: boolean; available?: boolean;
    commentCount?: number | null;
    onRequestCommentAccess?: () => void;
    layout?: FeedbackSourceLayout | null;
    onBoundsChange?: (bounds: FeedbackVisibleArea | null) => void;
    onPrepare?: () => void | Promise<void>; onWhole?: () => void; onMode?: (mode: FeedbackMode) => void;
    workspaceOwned?: boolean;
    onResume?: () => void;
    onShow?: () => void; railOpen?: boolean; onDockChange?: (docked: boolean) => void;
  } = $props();
  let docks = $state(0);
  $effect(() => { onDockChange?.(docks > 0); });
  let workspaces = $state(0);
  let indicators = $state<symbol[]>([]);
  provideFeedbackSurface({ get binding() { return binding; }, get opened() { return open; },
    get commentCount() { return commentCount; },
    get panel() { return panel; }, get available() { return available ?? Boolean(binding); }, get layout() { return layout; }, open: view => onOpen(view),
    commenting: enabled => onCommenting?.(enabled),
    get requestCommentAccess() { return onRequestCommentAccess; },
    get prepare() { return onPrepare; }, get whole() { return onWhole; }, get setMode() { return onMode; },
    get workspaceOwned() { return workspaceOwned; },
    get resume() { return onResume; },
    get reportBounds() { return onBoundsChange; },
    get railOpen() { return railOpen; },
    show: () => (onShow ?? (() => onOpen('threads')))(),
    close: () => onClose(),
    registerDock: () => { docks++; return () => { docks--; }; },
    // The most recently mounted control (an opened full view) owns commenting for the source.
    get indicatorOwner() { return indicators.at(-1); },
    registerIndicator: id => { indicators = [...indicators, id]; return () => { indicators = indicators.filter(value => value !== id); }; },
    registerWorkspace: () => { workspaces++; return () => { workspaces--; }; } });
</script>
{@render children()}
