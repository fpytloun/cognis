<script lang="ts">
  import { onMount } from 'svelte';
  import { beforeNavigate } from '$app/navigation';
  import { get } from 'svelte/store';
  import DashboardEntityModal from '$lib/components/dashboard/DashboardEntityModal.svelte';
  import WorkspaceLayer from '$lib/components/dashboard/WorkspaceLayer.svelte';
  import { WorkspaceManager, serializeWorkspaceWindows, workspaceDeviceClass } from '$lib/dashboard/workspace';
  import { loadUserPreferences, userPreferences } from '$lib/stores/userPreferences';
  import { addToast } from '$lib/stores/toasts';
  import { workspaceOpenFailureMessage } from '$lib/dashboard/workspace-open-result';
  import type { Agent, Conversation } from '$lib/types/api';
  import { wsClient } from '$lib/ws/client';
  import { dashboardConversationPatch, dashboardRealtimeLoads } from '$lib/dashboard/realtime';
  import { DashboardConversationReadTracker } from '$lib/dashboard/conversation-read';
  import { api, asApiError } from '$lib/api/client';
  import { blockingOverlayActive } from '$lib/stores/overlays';
  import { auth } from '$lib/stores/auth';
  import { registerDashboardLauncher, type DashboardLaunchInput } from '$lib/dashboard/launcher';
  import ResourceModal from '$lib/components/library/ResourceModal.svelte';
  import { libraryHref } from '$lib/library';

  let { projectId, userEmail, kind, entityId, agents, onClose, onRefresh, onContinueConversation, onNavigate, canMutateSchedule = true } = $props<{
    projectId: string;
    canMutateSchedule?: boolean;
    userEmail: string;
    kind: DashboardLaunchInput['kind'] | null;
    entityId: string | null;
    agents: Agent[];
    onClose: () => void;
    onRefresh: () => Promise<void>;
    onContinueConversation?: (conversation: Conversation) => void;
    onNavigate: (kind: DashboardLaunchInput['kind'], id: string) => void | Promise<void>;
  }>();
  let ready = $state(false);
  let windowsEnabled = $state(false);
  let device = $state<'desktop' | 'tablet' | 'phone'>('phone');
  let urlWindowKey: string | null = null;
  let reconcilingHistory = false;
  let documentVisible = $state(true);
  let loadedChats = $state(new Set<string>());
  let observedChats = new Set<string>();
  let summaries = $state<Record<string, Conversation>>({});
  let alive = true;
  let requestVersion = 0;
  const requests = new Map<string, number>();
  async function refreshConversation(id: string): Promise<void> {
    const request = ++requestVersion;
    requests.set(id, request);
    try {
      const detail = await api.conversations.detail(id, { includeState: false });
      if (alive && requests.get(id) === request) summaries = { ...summaries, [id]: detail };
    } catch { /* Existing window renders its own load error. */ }
  }
  const readTracker = new DashboardConversationReadTracker({
    markRead: (id) => api.conversations.markRead(id),
    optimisticRead: (id, through) => {
      summaries = { ...summaries, [id]: { ...summaries[id], has_unread: false, ...(through ? { last_read_at: through } : {}) } };
    },
    reconcile: async () => { await onRefresh(); },
    isRetryable: (error) => {
      const status = asApiError(error).status ?? 0;
      return status === 0 || status === 408 || status === 429 || status >= 500;
    },
  });
  const manager = new WorkspaceManager('desktop', { x: 0, y: 0, width: 1440, height: 900 }, (closed) => {
    if (reconcilingHistory) return;
    if (closed.entityId === entityId && closed.kind === kind) onClose();
    else void onRefresh();
  });
  const useWindows = $derived(windowsEnabled && device !== 'phone');
  $effect(() => {
    if (!$auth.user || $auth.user.email !== userEmail) return;
    const owner = userEmail;
    const scope = projectId;
    return registerDashboardLauncher({
      projectId: scope,
      async open(input) {
        if (!alive || auth.getSnapshot().user?.email !== owner ||
          projectId !== scope || input.project_id !== scope) {
          throw new Error('Project workspace is no longer active.');
        }
        await onNavigate(input.kind, input.id);
      },
    });
  });
  $effect(() => {
    const visible = useWindows && documentVisible && !$blockingOverlayActive
      ? $manager.filter((item) => !item.minimized).flatMap((item) => item.kind === 'conversation'
        ? [item.entityId] : item.resolvedConversationId ? [item.resolvedConversationId] : [])
      : [];
    const next = new Set(visible.filter((id) => loadedChats.has(id)));
    for (const id of observedChats) if (!next.has(id)) readTracker.close(id);
    for (const id of next) {
      if (!observedChats.has(id)) readTracker.open(id);
      const conversation = summaries[id];
      if (conversation) {
        void readTracker.initialLoaded(conversation);
        void readTracker.observe([conversation]);
      }
    }
    observedChats = next;
  });
  beforeNavigate(({ type }) => {
    if (type !== 'popstate' || !urlWindowKey) return;
    reconcilingHistory = true;
    manager.close(urlWindowKey);
    urlWindowKey = null;
    reconcilingHistory = false;
  });

  onMount(() => {
    alive = true;
    let unsubscribe = () => {};
    const stopEvents = wsClient.subscribe((event) => {
      const patch = dashboardConversationPatch(event);
      if (patch && summaries[patch.conversation_id]) summaries = {
        ...summaries, [patch.conversation_id]: { ...summaries[patch.conversation_id], ...patch },
      };
      if (dashboardRealtimeLoads(event.type, 'reason' in event ? event.reason : undefined).includes('conversations')) {
        for (const id of loadedChats) void refreshConversation(id);
      }
    });
    const refresh = () => { for (const id of loadedChats) void refreshConversation(id); };
    window.addEventListener('focus', refresh);
    const measure = () => { device = workspaceDeviceClass(window.innerWidth, window.matchMedia('(pointer: coarse)').matches); };
    const visibility = () => { documentVisible = document.visibilityState === 'visible'; };
    visibility();
    document.addEventListener('visibilitychange', visibility);
    measure();
    window.addEventListener('resize', measure);
    // Entity links inside reused chat/output renderers stay in this project.
    // Explicit full-page buttons and modified/new-tab clicks retain their behavior.
    const navigateLink = (event: MouseEvent) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = (event.target as Element | null)?.closest<HTMLAnchorElement>('a[href]');
      if (!anchor || anchor.target === '_blank' || anchor.hasAttribute('download')) return;
      if (!anchor.closest('[data-testid="dashboard-entity-modal"], [data-workspace-window], [data-testid^="workspace-window-"]')) return;
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin) return;
      const match = /^\/(chat|tasks|schedules)\/([^/]+)$/.exec(url.pathname);
      if (!match || ['new', 'main'].includes(match[2])) return;
      event.preventDefault();
      event.stopPropagation();
      onNavigate(match[1] === 'chat' ? 'conversation' : match[1] === 'tasks' ? 'task' : 'schedule', decodeURIComponent(match[2]));
    };
    document.addEventListener('click', navigateLink, true);
    void loadUserPreferences(userEmail).then(() => {
      if (!alive) return;
      windowsEnabled = get(userPreferences).display.dashboard_workspace_windows;
      const key = `cognis-project-workspace:${encodeURIComponent(userEmail)}:${encodeURIComponent(projectId)}`;
      if (windowsEnabled) {
        try { manager.hydrate(localStorage.getItem(key)); } catch { /* Storage may be disabled. */ }
        unsubscribe = manager.subscribe((windows) => {
          try { localStorage.setItem(key, serializeWorkspaceWindows(windows)); } catch { /* Optional layout persistence. */ }
        });
      }
      ready = true;
    }).catch(() => { if (alive) ready = true; });
    return () => {
      alive = false; unsubscribe(); stopEvents(); readTracker.close();
      window.removeEventListener('focus', refresh);
      document.removeEventListener('visibilitychange', visibility);
      window.removeEventListener('resize', measure); document.removeEventListener('click', navigateLink, true);
    };
  });

  export function activate(nextKind: DashboardLaunchInput['kind'], nextId: string): void {
    if (!ready || !useWindows) return;
    const result = manager.open({
      kind: nextKind, entityId: nextId,
      title: nextKind === 'conversation' ? 'Conversation' : nextKind === 'task' ? 'Task' : nextKind === 'schedule' ? 'Schedule' : 'Resource',
      canonicalHref: nextKind === 'artifact' || nextKind === 'deliverable'
        ? libraryHref({ kind: nextKind, id: nextId })
        : `/${nextKind === 'conversation' ? 'chat' : nextKind === 'task' ? 'tasks' : 'schedules'}/${encodeURIComponent(nextId)}`
    });
    if (result.status === 'opened' || result.status === 'focused') urlWindowKey = result.key;
    else if (result.status === 'failed' || result.status === 'capped') addToast(workspaceOpenFailureMessage(result), 'info');
  }
  $effect(() => {
    if (!kind || !entityId) return;
    activate(kind, entityId);
  });
</script>

{#if ready && useWindows}
  <WorkspaceLayer {manager} {agents} {canMutateSchedule} conversations={Object.values(summaries)} deviceClass={device} {onContinueConversation} onTaskMutationSettled={onRefresh} onOpenTask={(id) => onNavigate('task', id)}
    onConversationInitialLoaded={(id) => { loadedChats = new Set([...loadedChats, id]); void refreshConversation(id); }} />
{:else if ready && kind && entityId}
  {#key `${kind}:${entityId}`}
    {#if kind === 'artifact' || kind === 'deliverable'}
      <ResourceModal {kind} ref={entityId} {onClose} />
    {:else}
      <DashboardEntityModal
      {kind} {agents} {onClose} {onContinueConversation}
      {canMutateSchedule}
      onOpenTask={(id) => onNavigate('task', id)}
      conversationId={kind === 'conversation' ? entityId : null}
      taskId={kind === 'task' ? entityId : null}
      scheduleId={kind === 'schedule' ? entityId : null}
      onTaskMutationSettled={onRefresh}
      />
    {/if}
  {/key}
{/if}
