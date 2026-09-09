import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), 'utf8');
}

describe('Control Center route contract', () => {
  it('uses the app root for the Control Center and logo navigation', () => {
    const page = source('../../routes/(app)/+page.svelte');
    const layout = source('../../routes/(app)/+layout.svelte');

    expect(page).toContain('ControlCenter');
    expect(layout).toContain('href="/"');
    expect(layout).toContain('data-testid="sidebar-logo-link"');
    expect(layout).toContain("{ href: '/', label: 'Dashboard'");
    expect(layout).toContain("href === '/' ? pathname === '/'");
    expect(source('../components/BottomTabBar.svelte')).toContain("{ href: '/', label: 'Dashboard'");
    expect(layout).not.toContain("href: '/work'");
    expect(source('../components/BottomTabBar.svelte')).not.toContain("href: '/work'");
  });

  it('does not use owner-wide work activity requests', () => {
    const components = [
      source('../components/dashboard/ControlCenter.svelte'),
      source('../components/dashboard/DashboardEntityModal.svelte'),
      source('../components/dashboard/TasksSection.svelte'),
      source('../components/dashboard/ConversationsSection.svelte')
    ].join('\n');

    expect(components).not.toContain('/api/v1/work/activities');
    expect(components).not.toContain('api.work.activities');
    expect(source('../components/dashboard/ConversationModalWorkspace.svelte')).toContain('SharedInspectorTabs');
    expect(components).toContain('TaskWorkPanel');
    expect(components).not.toContain("conversationTimelineScope(taskChat");
  });

  it('keeps task tabs and uses the canonical conversation inspector workspace', () => {
    const modal = source('../components/dashboard/DashboardEntityModal.svelte');
    const workspace = source('../components/dashboard/ConversationModalWorkspace.svelte');

    expect(modal).toContain('ConversationModalWorkspace');
    expect(workspace).toContain('SharedInspectorTabs');
    expect(workspace).toContain('SessionDetailsButton');
    expect(workspace).toContain('initialTab="overview"');
    expect(workspace).toContain('DEFAULT_INSPECTOR_WIDTH = 352');
    expect(workspace).toContain('LEGACY_DEFAULT_INSPECTOR_WIDTH = 400');
    expect(workspace).toContain('aria-label="Resize conversation inspector"');
    expect(workspace).toContain('data-layout={narrowLayout');
    expect(modal).toContain('TaskBrief');
    expect(modal).toContain('WorkflowPhases');
    expect(modal).toContain('TaskProgressPanel');
    expect(modal).toContain("{ id: 'control', label: 'Control' }");
    expect(modal).toContain("{ id: 'control-chat', label: 'Control chat' }");
    expect(modal.indexOf("{ id: 'control', label: 'Control' }")).toBeLessThan(
      modal.indexOf("{ id: 'control-chat', label: 'Control chat' }")
    );
    expect(modal).toContain('TaskDashboardControl');
    expect(modal).toContain('TaskWorkPanel');
    expect(modal).toContain('view="activity"');
    expect(modal).toContain('view="deliverable"');
    expect(modal).toContain('deliverableCollapsedByDefault={false}');
    expect(modal).toContain('StepOutputModal');
    expect(modal).toContain('SessionLogsDrawer');
    expect(modal).not.toContain('onStepLogsOpen={() => { if (taskId) void goto');
    expect(modal).not.toContain('onStepOutputOpen={() => { if (taskId) void goto');
    const stepOutput = source('../components/tasks/StepOutputModal.svelte');
    const sessionLogs = source('../components/tasks/SessionLogsDrawer.svelte');
    for (const viewer of [stepOutput, sessionLogs]) {
      expect(viewer).toContain("import { portal } from '$lib/actions/portal'");
      expect(viewer).toContain('use:portal');
      expect(viewer).toContain('data-overlay-id={overlayId}');
    }
    expect(stepOutput).toContain('data-testid="step-output-panel"');
    expect(sessionLogs).toContain('data-testid="session-logs-panel"');
    expect(modal).toContain("['completed', 'failed', 'cancelled'].includes(detail.status)");
  });

  it('uses the approved wide grid and keeps the agent strip inside Conversations', () => {
    const dashboard = source('../components/dashboard/ControlCenter.svelte');
    const tasks = source('../components/dashboard/TasksSection.svelte');
    const conversations = source('../components/dashboard/ConversationsSection.svelte');

    expect(dashboard).toContain('max-w-[100rem]');
    expect(dashboard).toContain('lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]');
    expect(dashboard).not.toContain('control-center-agents-footer');
    expect(dashboard).not.toContain('data-testid="control-center-agents"');
    expect(conversations).toContain('dashboard-conversations-agents');
    expect(tasks).toContain('WorkstreamTodoProgress');
    expect(tasks).toContain('WorkstreamExecutionStatus');
    expect(tasks).toContain('DiffStat');
    expect(dashboard).toContain('api.agents.listAll({ include_system: false })');
    expect(dashboard).not.toContain('attention_only');
  });

  it('uses server filters and independent infinite-scroll sentinels', () => {
    const dashboard = source('../components/dashboard/ControlCenter.svelte');
    const tasks = source('../components/dashboard/TasksSection.svelte');
    const conversations = source('../components/dashboard/ConversationsSection.svelte');

    expect(dashboard).toContain('Search tasks and conversations');
    expect(dashboard).toContain('q: searchQuery || null');
    expect(dashboard).toContain('project_id: selectedProjectId || null');
    expect(dashboard).toContain('query: searchQuery || null');
    expect(dashboard).toContain('projectId: selectedProjectId || null');
    expect(tasks).toContain('dashboard-tasks-recent-sentinel');
    expect(conversations).toContain('dashboard-conversations-recent-sentinel');
    expect(tasks).toContain("rootMargin: '160px 0px'");
    expect(conversations).toContain("rootMargin: '160px 0px'");
    expect(dashboard).toContain('if (boardLoading || !done?.has_more');
    expect(dashboard).toContain('recentTaskAppendGate.invalidate()');
    expect(dashboard).toContain('cursor: null');
    expect(dashboard).toContain('recentTasksError = null');
    expect(dashboard).toContain('const requestScopeKey = `${searchQuery}\\u0000${selectedProjectId}`');
    expect(dashboard).toContain('const retainedCursor = preserveLoadedTail ? conversationsCursor : null');
    expect(dashboard).toContain('conversationAppendController = new AbortController()');
    expect(dashboard).toContain('includeAttentionActions: true');
    expect(dashboard).toContain('reconcileConversationProjection(');
  });

  it('resolves agent quick chat with agent_direct scope', () => {
    const modal = source('../components/dashboard/AgentQuickChatModal.svelte');
    const compact = source('../components/chat-v2/CompactConversationChat.svelte');

    expect(modal).toContain("scope: 'agent_direct'");
    expect(modal).toContain('api.conversations.resolve');
    expect(modal).toContain('DashboardModalShell');
    expect(modal).toContain('ConversationModalWorkspace');
    expect(modal).not.toContain('lg:rounded-2xl lg:border');
    expect(compact).toContain('bind:userScrolledUp');
    expect(compact).toContain("data-auto-tail={userScrolledUp ? 'paused' : 'following'}");
  });

  it('routes normal and embedded composers through the shared dispatcher', () => {
    const compact = source('../components/chat-v2/CompactConversationChat.svelte');
    const normalChat = source('../../routes/(app)/chat/[conversationId]/+page.svelte');
    expect(compact).toContain('dispatchChatComposerMessage');
    expect(normalChat).toContain('dispatchChatComposerMessage');
    expect(compact).toContain('normalizeChatComposerInput');
    expect(normalChat).toContain('normalizeChatComposerInput');
    expect(normalChat).not.toContain('parseChatModeDirectiveInput');
    expect(compact).not.toContain('Slash commands are not yet available through Chat v2 send.');
  });

  it('keeps modal detail refreshes identity-preserving and metadata-scoped', () => {
    const dashboard = source('../components/dashboard/ControlCenter.svelte');
    const modal = source('../components/dashboard/DashboardEntityModal.svelte');
    const realtime = source('./realtime.ts');
    expect(dashboard).toContain('dashboardBatchRefreshesModal(batch, selectedEntity)');
    expect(dashboard).not.toContain('if (selectedEntity) modalRefreshToken += 1');
    expect(modal).toContain('const blocking =');
    expect(modal).toContain('refreshError');
    expect(modal).toContain('request !== loadRequest');
    expect(realtime).toContain('CONVERSATION_METADATA_FIELDS');
    expect(realtime).not.toContain("'message_complete', 'modal'");
  });

  it('keeps workspace windows separate from blocking overlays and reuses entity surfaces', () => {
    const dashboard = source('../components/dashboard/ControlCenter.svelte');
    const layer = source('../components/dashboard/WorkspaceLayer.svelte');
    const window = source('../components/dashboard/WorkspaceWindow.svelte');
    const shell = source('../components/dashboard/DashboardModalShell.svelte');
    expect(dashboard).toContain('WorkspaceManager');
    expect(dashboard).toContain('workspaceGateEnabled');
    expect(layer).toContain('use:portal');
    expect(layer).toContain('<DashboardEntityModal');
    expect(layer).toContain('<AgentQuickChatModal');
    expect(layer).not.toContain('BlockingDialog');
    expect(layer).not.toContain('registerOverlay');
    expect(window).toContain('aria-modal="false"');
    expect(window).toContain('style="touch-action:none"');
    expect(shell).toContain('{#if embedded}');
    const entity = source('../components/dashboard/DashboardEntityModal.svelte');
    const dialog = source('../components/ui/BlockingDialog.svelte');
    expect(entity).not.toContain('<svelte:window');
    expect(entity).toContain('export function handleEscape(): boolean');
    expect(entity).toContain('onEscape={handleEscape}');
    expect(entity).toContain('!stepViewerLoading');
    expect((layer.match(/<svelte:window/g) ?? [])).toHaveLength(1);
    expect(layer).toContain('bind:this={entityRefs[workspaceWindow.key]}');
    expect(layer).toContain('dispatchWorkspaceEscape');
    expect(dialog).toContain('onEscape?: (() => boolean)');
    expect(dialog).toContain('if (onEscape?.())');
    expect(dialog).toContain('if (event.defaultPrevented) return');
    expect(shell).toContain('{onEscape}');
    expect(source('../components/tasks/StepOutputModal.svelte'))
      .toContain('event.stopImmediatePropagation()');
    expect(source('../components/tasks/SessionLogsDrawer.svelte'))
      .toContain('event.stopImmediatePropagation()');
  });

  it('uses one event-driven dashboard subscription without polling', () => {
    const dashboard = source('../components/dashboard/ControlCenter.svelte');
    const realtime = source('./realtime.ts');
    expect(dashboard).toContain('subscribeDashboardRealtime(wsClient');
    expect(dashboard).not.toContain('setInterval');
    expect(realtime).toContain('sidebar_conversation_upsert');
    expect(realtime).toContain('task_paused');
  });

  it('uses one PWA-safe shell for entity, schedule, and agent modals', () => {
    const shell = source('../components/dashboard/DashboardModalShell.svelte');
    const entity = source('../components/dashboard/DashboardEntityModal.svelte');
    const schedule = source('../components/dashboard/ScheduleDashboardModal.svelte');
    const dialog = source('../components/ui/BlockingDialog.svelte');

    expect(shell).toContain('env(safe-area-inset-top)');
    expect(shell).not.toContain('env(safe-area-inset-bottom)');
    expect(shell).toContain('h-full max-w-none rounded-none');
    expect(shell).toContain('lg:rounded-3xl');
    expect(dialog).toContain('app-viewport-frame fixed inset-x-0');
    expect(dialog).not.toContain('height: var(--app-viewport-height, 100dvh)');
    expect(dialog).toContain("import { portal } from '$lib/actions/portal'");
    expect(dialog).toContain('use:portal');
    expect(dialog).toContain('data-blocking-dialog-scroll');
    expect(entity).not.toContain('!pt-0');
    expect(entity).not.toContain('!pb-0');
    expect(schedule).toContain('DashboardModalShell');
  });

  it('keeps schedule modal limited to description and planned steps with one workflow lookup', () => {
    const schedule = source('../components/dashboard/ScheduleDashboardModal.svelte');

    expect(schedule).toContain("{ id: 'description', label: 'Description' }");
    expect(schedule).toContain("{ id: 'steps', label: 'Planned steps' }");
    expect(schedule).toContain('api.workflows.detail(workflowId)');
    expect(schedule).toContain("workflowId === loadedWorkflowId");
    expect(schedule).not.toContain("label: 'Chat'");
    expect(schedule).not.toContain("label: 'Activity'");
  });
});
