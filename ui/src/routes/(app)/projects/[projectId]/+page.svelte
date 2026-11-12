<script lang="ts">
  import { prepareProjectAvatar } from '$lib/projects/avatar-upload';
  import { openProjectWindow } from '$lib/projects/window';
  import ProjectAvatar from '$lib/components/projects/ProjectAvatar.svelte';
  import ProjectIconPicker from '$lib/components/projects/ProjectIconPicker.svelte';
  import { untrack } from 'svelte';
  import { wsClient } from '$lib/ws/client';
  import { subscribeDashboardRealtime } from '$lib/dashboard/realtime';
  import { RefreshCooldown, SidebarRefreshController } from '$lib/sidebar-refresh';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { api } from '$lib/api/client';
  import { useSectionConnectivity } from '$lib/connectivity-section.svelte';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import ImageLightbox from '$lib/components/ImageLightbox.svelte';
  import AvatarGenerateModal from '$lib/components/agents/AvatarGenerateModal.svelte';
  import { addToast } from '$lib/stores/toasts';
  import { confirmAction } from '$lib/stores/confirm';
  import Button from '$lib/components/ui/Button.svelte';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import ProjectEntityWorkspace from '$lib/components/projects/ProjectEntityWorkspace.svelte';
  import ConversationsSection from '$lib/components/dashboard/ConversationsSection.svelte';
  import { WaitingActivity } from '$lib/complete-waiting';
  import AttentionActionDialog from '$lib/components/attention/AttentionActionDialog.svelte';
  import AgentQuickChatModal from '$lib/components/dashboard/AgentQuickChatModal.svelte';
  import TasksSection from '$lib/components/dashboard/TasksSection.svelte';
  import { CursorFeed } from '$lib/projects/cursor-feed.svelte';
  import { projectTaskBoard } from '$lib/projects/task-board';
  import ProjectResources from '$lib/components/projects/ProjectResources.svelte';
  import ResourceBrowser from '$lib/components/library/ResourceBrowser.svelte';
  import SchedulesWorkspace from '$lib/components/schedules/SchedulesWorkspace.svelte';
  import NewChatModal from '$lib/components/NewChatModal.svelte';
  import { preferredNewChatAgentId } from '$lib/chat/new-chat-agent';
  import { formatRelativeTime } from '$lib/time';
  import { auth } from '$lib/stores/auth';
  import { rememberProject } from '$lib/projects/navigation';
  import type {
    Agent,
    AttentionActionSummary,
    Conversation,
    ExecutorScope,
    Project,
    ProjectActivity,
    ProjectAgent,
    ProjectGrant,
    ProjectGrantRole,
    ProjectSource,
    ProjectTaskSummary,
    Schedule,
    Workflow
  } from '$lib/types/api';

  import ProjectKnowledge from '$lib/components/projects/ProjectKnowledge.svelte';
  import UsageWorkspace from '$lib/usage/UsageWorkspace.svelte';
  import { usageAnalyticsEnabled } from '$lib/system';
  import { getUsageHistory } from '$lib/usage/history.svelte';
  const history = getUsageHistory();
  type ProjectTab = 'overview' | 'conversations' | 'tasks' | 'schedules' | 'agents' | 'resources' | 'knowledge' | 'usage' | 'settings' | 'sharing';

  const projectId = $derived(page.params.projectId);
  const isWindowMode = $derived(page.url.searchParams.get('window') === '1');

  function openProjectInSeparateWindow(): void {
    if (projectId) openProjectWindow(projectId, page.url);
  }
  let project = $state<Project | null>(null);
  $effect(() => {
    if (project?.project_id === projectId && projectId) {
      rememberProject($auth.user?.email, projectId, page.url.searchParams.get('tab'));
    }
  });
  let workflows = $state<Workflow[]>([]);
  let error = $state<string | null>(null);
  // Hidden while the global connection banner applies; reload on restore.
  const net = useSectionConnectivity({ hasError: () => Boolean(error), retry: () => load() });
  let savingProject = $state(false);
  let savingSource = $state(false);
  let savingGrant = $state(false);
  let savingWorkflow = $state(false);
  let uploadingAvatar = $state(false);
  let showAvatarModal = $state(false);
  let showAvatarLightbox = $state(false);
  let editingSourceId = $state<string | null>(null);

  let projectForm = $state({ name: '', description: '', instructions: '', default_workflow_id: '', color: '#38bdf8', automaticColor: true, icon: '' });
  let sourceForm = $state({ name: '', local_path: '', remote_url: '', default_branch: '', credential_ref: '', instructions: '' });
  let selectedWorkflowId = $state('');
  let grantForm = $state<{ grantee_user_email: string; note: string; permission: ProjectGrantRole }>({
    grantee_user_email: '',
    note: '',
    permission: 'member'
  });
  let activeTab = $state<ProjectTab>('overview');
  const conversationFeed = new CursorFeed<Conversation>((item) => item.conversation_id);
  const taskFeed = new CursorFeed<ProjectTaskSummary>((item) => item.task_id);
  const conversations = $derived(conversationFeed.items);
  const tasks = $derived(taskFeed.items);
  const taskMetadata = $derived(new Map(tasks.map((task) => [
    task.task_id,
    `${authorLabel(task.created_by)} · ${task.is_readonly_for_caller ? 'Read-only' : task.visibility === 'private' ? 'Private' : 'Project'}`,
  ])));
  let schedules = $state<Schedule[]>([]);
  let scheduleError = $state('');
  let showNewChat = $state(false);
  let chatAgentId = $state('');
  let chatProfileId = $state('');
  let chatProfileMode = $state<'auto' | 'default' | null>(null);
  let creatingChat = $state(false);
  let ownAgents = $state<Agent[]>([]);
  let entityWorkspace: ProjectEntityWorkspace | undefined = $state();
  let waitingConversations = $state<Conversation[]>([]);
  let waitingError = $state<string | null>(null);
  let selectedAttention = $state<AttentionActionSummary | null>(null);
  let refreshWaiting = () => {};
  const openKind = $derived(page.url.searchParams.get('view'));
  const openId = $derived(page.url.searchParams.get('item'));
  const chatAgents = $derived(ownAgents.filter((agent) =>
    agent.status === 'active' && agent.agent_type === 'primary'
    && (!(agent.shared_via_project_ids?.length) || agent.shared_via_project_ids.includes(projectId ?? ''))
  ));
  function selectTab(tab: ProjectTab): void {
    activeTab = tab;
    const url = new URL(page.url);
    url.searchParams.set('tab', tab);
    url.searchParams.delete('view');
    url.searchParams.delete('item');
    void goto(url, { replaceState: true, noScroll: true, keepFocus: true });
  }
  const stripAgents = $derived(chatAgents.filter((agent) =>
    projectAgents.some((assignment) => assignment.agent_id === agent.agent_id)
  ));
  const quickChatAgent = $derived(openKind === 'agent'
    ? stripAgents.find((agent) => agent.agent_id === openId) : undefined);

  function openItem(kind: 'conversation' | 'task' | 'schedule' | 'deliverable' | 'artifact' | 'agent', id: string): Promise<void> {
    if (kind !== 'agent') entityWorkspace?.activate(kind, id);
    const url = new URL(page.url);
    url.searchParams.set('tab', activeTab);
    url.searchParams.set('view', kind);
    url.searchParams.set('item', id);
    return goto(url, { noScroll: true, keepFocus: true });
  }

  function closeItem(): void {
    const url = new URL(page.url);
    url.searchParams.delete('view');
    url.searchParams.delete('item');
    void goto(url, { replaceState: true, noScroll: true, keepFocus: true });
    void loadActivity();
  }

  function startChat(agentId?: string): void {
    const currentAgentId = openKind === 'agent' ? openId
      : openKind === 'conversation' ? conversations.find((item) => item.conversation_id === openId)?.agent_id : null;
    chatAgentId = preferredNewChatAgentId(chatAgents, [agentId, currentAgentId, chatAgentId], projectAgents[0]?.agent_id);
    chatProfileId = '';
    chatProfileMode = null;
    showNewChat = true;
  }

  async function createChat(): Promise<void> {
    if (!project || creatingChat || !chatAgentId || role === 'viewer') return;
    creatingChat = true;
    const targetProject = project.project_id;
    try {
      const conversation = await api.conversations.create({
        agent_id: chatAgentId,
        agent_profile_id: chatProfileId || null,
        agent_profile_mode: chatProfileMode,
        project_id: targetProject,
        context: { type: 'web' }
      });
      if (projectId !== targetProject) return;
      showNewChat = false;
      await loadActivity();
      if (projectId !== targetProject) return;
      openItem('conversation', conversation.conversation_id);
    } catch (caught) {
      addToast(caught instanceof Error ? caught.message : 'Could not create conversation', 'error');
    } finally { creatingChat = false; }
  }

  async function continueChat(source: Conversation): Promise<void> {
    if (!project || creatingChat || role === 'viewer') return;
    creatingChat = true;
    const targetProject = project.project_id;
    try {
      const conversation = await api.conversations.create({
        agent_id: source.agent_id,
        project_id: targetProject,
        title: `Continue: ${source.title ?? 'Shared conversation'}`,
        context: { type: 'web', platform_data: { continued_from_conversation_id: source.conversation_id } }
      });
      if (projectId !== targetProject) return;
      await loadActivity();
      if (projectId !== targetProject) return;
      openItem('conversation', conversation.conversation_id);
    } catch (caught) {
      addToast(caught instanceof Error ? caught.message : 'Could not continue conversation', 'error');
    } finally { creatingChat = false; }
  }
  let activity = $state<ProjectActivity>({ deliverables: [], artifacts: [] });
  let conversationAgentFilter = $state('');
  let agentForm = $state<{ agent_id: string; executor_scope: ExecutorScope; is_default: boolean }>({
    agent_id: '',
    executor_scope: 'owner_executor',
    is_default: false
  });
  let savingAgent = $state(false);
  let currentUserEmail = $state<string | null>(null);

  const role = $derived(project?.caller_role ?? (project?.is_shared_with_me ? 'member' : 'owner'));
  const canManage = $derived(role === 'owner' || role === 'manager');
  const isOwner = $derived(role === 'owner');
  const activeGrants = $derived((project?.grants ?? []).filter((grant: ProjectGrant) => !grant.revoked_at));
  const projectAgents = $derived<ProjectAgent[]>(project?.agents ?? []);
  const assignableAgents = $derived(
    ownAgents.filter(
      (agent) =>
        agent.agent_type === 'primary'
        && agent.status === 'active'
        && !agent.is_shared_with_me
        && !projectAgents.some((item) => item.agent_id === agent.agent_id)
    )
  );
  const agentNameById = $derived(
    new Map<string, string>([
      ...ownAgents.map((agent) => [agent.agent_id, agent.display_name ?? agent.name] as [string, string]),
      ...projectAgents.map((agent) => [agent.agent_id, agent.agent_display_name ?? agent.agent_name] as [string, string])
    ])
  );
  $effect(() => {
    const id = projectId;
    const agentId = conversationAgentFilter;
    if (!id || !$auth.user) return;
    untrack(() => conversationFeed.configure((cursor, signal) => api.projects.conversationsPage(id, cursor, agentId, signal)));
    return () => conversationFeed.dispose();
  });
  $effect(() => {
    const id = projectId;
    if (!id || !$auth.user) return;
    untrack(() => taskFeed.configure((cursor, signal) => api.projects.tasksPage(id, cursor, signal)));
    return () => taskFeed.dispose();
  });
  $effect(() => {
    const id = projectId;
    const owner = $auth.user?.email;
    const agentId = conversationAgentFilter;
    waitingConversations = [];
    waitingError = null;
    selectedAttention = null;
    if (!id || !owner) return;
    const scope = JSON.stringify([owner, id, agentId]);
    const activity = new WaitingActivity();
    const controller = new SidebarRefreshController(async ({ signal }) => {
      try {
        const items = await api.conversations.waiting(
          { contextType: 'web', projectId: id, agentId: agentId || null }, { signal, scope, activity },
        );
        if (!signal.aborted) { waitingConversations = items; waitingError = null; }
      } catch (caught) {
        if (!signal.aborted) waitingError = caught instanceof Error ? caught.message : 'Could not load waiting conversations';
        throw caught;
      }
    }, new RefreshCooldown());
    controller.setLifecycle({ scope, eligible: true });
    const refresh = () => { void controller.request(scope); };
    refreshWaiting = refresh;
    refresh();
    const stop = subscribeDashboardRealtime(wsClient, refresh);
    const visible = () => { if (document.visibilityState === 'visible') refresh(); };
    window.addEventListener('focus', visible);
    document.addEventListener('visibilitychange', visible);
    return () => {
      controller.dispose();
      stop();
      window.removeEventListener('focus', visible);
      document.removeEventListener('visibilitychange', visible);
    };
  });
  $effect(() => {
    const id = projectId;
    const owner = $auth.user?.email;
    if (!id || !owner) return;
    const controller = new SidebarRefreshController(async ({ signal }) => {
      await loadActivity(signal);
    }, new RefreshCooldown());
    controller.setLifecycle({ scope: id, eligible: true });
    const refresh = () => { void controller.request(id); };
    const stopDashboard = subscribeDashboardRealtime(wsClient, refresh);
    const stopWork = wsClient.subscribe((event) => {
      if (event.type === 'work_invalidated') refresh();
    });
    const visible = () => { if (document.visibilityState === 'visible') refresh(); };
    window.addEventListener('focus', visible);
    document.addEventListener('visibilitychange', visible);
    return () => {
      controller.dispose();
      stopDashboard();
      stopWork();
      window.removeEventListener('focus', visible);
      document.removeEventListener('visibilitychange', visible);
    };
  });
  const tabs = $derived([
    { id: 'overview', label: 'Overview' },
    ...($usageAnalyticsEnabled ? [{ id: 'usage', label: 'Usage' }] : []),
    { id: 'conversations', label: 'Conversations', count: conversationFeed.hasMore ? undefined : conversations.length },
    { id: 'tasks', label: 'Tasks', count: taskFeed.hasMore ? undefined : tasks.length },
    { id: 'schedules', label: 'Schedules', count: schedules.length },
    { id: 'agents', label: 'Agents', count: projectAgents.length },
    { id: 'resources', label: 'Resources', count: activity.deliverables.length + activity.artifacts.length },
    { id: 'knowledge', label: 'Knowledge' },
    { id: 'settings', label: 'Settings' },
    { id: 'sharing', label: 'Sharing', count: activeGrants.length + 1 }
  ]);

  function roleLabel(value: string | null | undefined): string {
    if (value === 'manager') return 'Manager';
    if (value === 'viewer') return 'Viewer';
    if (value === 'owner') return 'Owner';
    return 'Member';
  }

  function executorScopeLabel(scope: ExecutorScope): string {
    return scope === 'grantee_executor' ? 'Each member uses their own executor' : "Runs on the owner's executor";
  }

  function authorLabel(email: string): string {
    return email === currentUserEmail ? 'You' : email;
  }

  function relative(value: string | null | undefined): string {
    return value ? formatRelativeTime(value) : '';
  }

  async function loadActivity(signal?: AbortSignal): Promise<void> {
    if (!projectId) return;
    const requestedProject = projectId;
    scheduleError = '';
    const [nextActivity, nextSchedules] = await Promise.all([
      api.projects.activity(projectId, { limit: 30 }).catch(() => ({ deliverables: [], artifacts: [] }) as ProjectActivity),
      api.schedules.list({ project_id: projectId }).catch(() => {
        if (requestedProject === projectId) scheduleError = 'Schedules could not be loaded. Other project work is still available.';
        return [] as Schedule[];
      })
    ]);
    if (signal?.aborted || requestedProject !== projectId) return;
    activity = nextActivity;
    schedules = nextSchedules;
    await Promise.all([conversationFeed.refresh(), taskFeed.refresh()]);
  }

  async function openArtifact(artifactId: string): Promise<void> {
    openItem('artifact', artifactId);
  }

  async function saveAgentAssignment(agentId: string, payload: { is_default?: boolean; executor_scope?: ExecutorScope }): Promise<void> {
    if (!project) return;
    savingAgent = true;
    try {
      await api.projects.upsertAgent(project.project_id, agentId, payload);
      await load();
      addToast('Project agent updated', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to update project agent', 'error');
    } finally {
      savingAgent = false;
    }
  }

  async function assignAgent(): Promise<void> {
    if (!agentForm.agent_id) return;
    const shared = activeGrants.length > 0;
    if (
      shared
      && !(await confirmAction({
        title: 'Share this agent with the project?',
        message: `${activeGrants.length + 1} people can use it inside this project. ${executorScopeLabel(agentForm.executor_scope)}.`,
        confirmLabel: 'Assign agent'
      }))
    ) return;
    await saveAgentAssignment(agentForm.agent_id, {
      executor_scope: agentForm.executor_scope,
      is_default: agentForm.is_default
    });
    agentForm = { agent_id: '', executor_scope: 'owner_executor', is_default: false };
  }

  async function removeAgent(agent: ProjectAgent): Promise<void> {
    if (!project) return;
    if (!(await confirmAction({ title: `Remove ${agent.agent_display_name ?? agent.agent_name}?`, message: 'Project members lose access to this agent inside the project.', confirmLabel: 'Remove' }))) return;
    try {
      await api.projects.removeAgent(project.project_id, agent.agent_id);
      await load();
      addToast('Agent removed from project', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to remove agent', 'error');
    }
  }

  async function changeGrantRole(grant: ProjectGrant, permission: ProjectGrantRole): Promise<void> {
    if (!project) return;
    try {
      await api.projects.updateGrant(project.project_id, grant.grant_id, permission);
      await load();
      addToast('Role updated', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to update role', 'error');
    }
  }

  async function leaveProject(): Promise<void> {
    if (!project) return;
    const makePrivate = await confirmAction({
      title: `Leave ${project.name}?`,
      message: 'Make your conversations and tasks in this project private before leaving? Choose Cancel to keep them visible to the project.',
      confirmLabel: 'Make private and leave'
    });
    if (!makePrivate && !(await confirmAction({ title: 'Leave and keep your work shared?', message: 'Your project-visible conversations stay readable by the remaining members.', confirmLabel: 'Leave' }))) return;
    try {
      await api.projects.leave(project.project_id, makePrivate);
      addToast('You left the project', 'success');
      window.location.href = '/projects';
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to leave project', 'error');
    }
  }

  const workflowNameById = $derived(new Map(workflows.map((workflow) => [workflow.workflow_id, workflow.name])));
  const attachableWorkflows = $derived(workflows.filter((workflow) => !workflow.is_system && !project?.workflow_ids.includes(workflow.workflow_id)));
  const readonly = $derived(Boolean(project?.is_readonly_for_caller));
  const routeTab = $derived(page.url.searchParams.get('tab'));
  $effect(() => {
    const tab = routeTab;
    if (tab && ['overview', 'conversations', 'tasks', 'schedules', 'agents', 'resources', 'knowledge', 'usage', 'settings', 'sharing'].includes(tab)) {
      activeTab = tab as ProjectTab;
    }
  });

  function resetProjectForm(next: Project): void {
    projectForm = {
      color: next.color ?? '#38bdf8',
      automaticColor: !next.metadata?.color,
      icon: typeof next.metadata?.icon === 'string' ? next.metadata.icon : '',
      name: next.name,
      description: next.description ?? '',
      instructions: next.instructions ?? '',
      default_workflow_id: next.default_workflow_id ?? ''
    };
  }

  function resetSourceForm(source?: ProjectSource): void {
    editingSourceId = source?.source_id ?? null;
    sourceForm = {
      name: source?.name ?? '',
      local_path: source?.local_path ?? '',
      remote_url: source?.remote_url ?? '',
      default_branch: source?.default_branch ?? '',
      credential_ref: source?.credential_ref ?? '',
      instructions: source?.instructions ?? ''
    };
  }

  async function load(): Promise<void> {
    if (!projectId) return;
    const requestedProject = projectId;
    error = null;
    try {
      const [nextProject, nextWorkflows, nextAgents] = await Promise.all([
        api.projects.detail(projectId),
        api.workflows.listAll({ project_id: projectId }).catch(() => [] as Workflow[]),
        api.agents.listAll().catch(() => [] as Agent[])
      ]);
      if (requestedProject !== projectId) return;
      project = nextProject;
      workflows = nextWorkflows;
      ownAgents = nextAgents;
      resetProjectForm(nextProject);
      void loadActivity();
    } catch (err) {
      if (requestedProject === projectId) error = err instanceof Error ? err.message : 'Failed to load project';
    }
  }

  async function saveProject(): Promise<void> {
    if (!project || readonly || !projectForm.name.trim()) return;
    savingProject = true;
    try {
      await api.projects.update(project.project_id, {
        name: projectForm.name.trim(),
        description: projectForm.description.trim() || null,
        instructions: projectForm.instructions.trim() || null,
        default_workflow_id: projectForm.default_workflow_id || null,
        metadata: { ...project.metadata, color: projectForm.automaticColor ? null : projectForm.color, icon: projectForm.icon || null }
      });
      await load();
      addToast('Project updated', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to update project', 'error');
    } finally {
      savingProject = false;
    }
  }

  async function updateAvatar(imageId: string | null, avatarUrl: string | null): Promise<void> {
    if (!project || readonly) return;
    try {
      await api.projects.update(project.project_id, {
        avatar_image_id: imageId,
        avatar_url: avatarUrl
      });
      await load();
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to update avatar', 'error');
      throw err;
    }
  }

  async function handleAvatarUpload(event: Event): Promise<void> {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    if (!file || !project || readonly) return;
    uploadingAvatar = true;
    try {
      const result = await api.images.upload(await prepareProjectAvatar(file));
      await updateAvatar(result.image_id, result.url);
      addToast('Project avatar uploaded', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to upload avatar', 'error');
    } finally {
      uploadingAvatar = false;
      input.value = '';
    }
  }

  async function handleGeneratedAvatar(imageId: string, avatarUrl: string): Promise<void> {
    await updateAvatar(imageId, avatarUrl);
    showAvatarModal = false;
    addToast('Project avatar updated', 'success');
  }

  async function removeAvatar(): Promise<void> {
    await updateAvatar(null, null);
    addToast('Project avatar removed', 'success');
  }

  async function saveSource(): Promise<void> {
    if (!project || readonly || !sourceForm.name.trim()) return;
    savingSource = true;
    const payload = {
      name: sourceForm.name.trim(),
      local_path: sourceForm.local_path.trim() || null,
      remote_url: sourceForm.remote_url.trim() || null,
      default_branch: sourceForm.default_branch.trim() || null,
      credential_ref: sourceForm.credential_ref.trim() || null,
      instructions: sourceForm.instructions.trim() || null
    };
    try {
      if (editingSourceId) {
        await api.projects.updateSource(project.project_id, editingSourceId, payload);
        addToast('Source updated', 'success');
      } else {
        await api.projects.addSource(project.project_id, payload);
        addToast('Source added', 'success');
      }
      resetSourceForm();
      await load();
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to save source', 'error');
    } finally {
      savingSource = false;
    }
  }

  async function deleteSource(source: ProjectSource): Promise<void> {
    if (!project || readonly) return;
    if (!(await confirmAction({ title: `Delete source "${source.name}"?`, message: 'This removes the project source hint only.', confirmLabel: 'Delete' }))) return;
    try {
      await api.projects.deleteSource(project.project_id, source.source_id);
      await load();
      addToast('Source deleted', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to delete source', 'error');
    }
  }

  async function attachWorkflow(): Promise<void> {
    if (!project || readonly || !selectedWorkflowId) return;
    savingWorkflow = true;
    try {
      project = await api.projects.attachWorkflow(project.project_id, selectedWorkflowId);
      selectedWorkflowId = '';
      await load();
      addToast('Workflow bound to project', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to bind workflow', 'error');
    } finally {
      savingWorkflow = false;
    }
  }

  async function detachWorkflow(workflowId: string): Promise<void> {
    if (!project || readonly) return;
    try {
      project = await api.projects.detachWorkflow(project.project_id, workflowId);
      await load();
      addToast('Workflow unbound', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to unbind workflow', 'error');
    }
  }

  async function createGrant(): Promise<void> {
    if (!project || readonly || !grantForm.grantee_user_email.trim()) return;
    savingGrant = true;
    try {
      await api.projects.createGrant(project.project_id, {
        grantee_type: 'user',
        grantee_user_email: grantForm.grantee_user_email.trim(),
        permission: grantForm.permission,
        note: grantForm.note.trim() || null
      });
      grantForm = { grantee_user_email: '', note: '', permission: 'member' };
      await load();
      addToast('Project shared', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to share project', 'error');
    } finally {
      savingGrant = false;
    }
  }

  async function revokeGrant(grant: ProjectGrant): Promise<void> {
    if (!project || readonly) return;
    try {
      await api.projects.revokeGrant(project.project_id, grant.grant_id);
      await load();
      addToast('Project grant revoked', 'success');
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to revoke grant', 'error');
    }
  }

  $effect(() => {
    const id = projectId;
    currentUserEmail = $auth.user?.email ?? null;
    if (!id || !currentUserEmail) return;
    untrack(() => {
      project = null;
      schedules = [];
      activity = { deliverables: [], artifacts: [] };
      showNewChat = false;
      void load();
    });
  });
</script>

{#snippet conversationListing()}
  <div class="min-w-0 md:h-[40rem]">
    <ConversationsSection
      projects={project ? [project] : []}
      {conversations} agents={ownAgents} {stripAgents} showAgents={role !== 'viewer'} onSeeAll={null}
      {waitingConversations} {waitingError} waitingScope={JSON.stringify([projectId, conversationAgentFilter])}
      onOpenAttention={(action) => { selectedAttention = action; }}
      onSelectAgent={(agent) => {
        if (agent.shared_via_project_ids?.length) startChat(agent.agent_id);
        else openItem('agent', agent.agent_id);
      }}
      metadataForItem={(item) => item.is_readonly_for_caller ? `${authorLabel(item.user_email)} · Read-only` : ''}
      loading={conversationFeed.loading} error={conversationFeed.items.length ? null : conversationFeed.error}
      onRetry={() => { void conversationFeed.refresh(); refreshWaiting(); }}
      onNewChat={role !== 'viewer' ? () => startChat() : undefined}
      onOpenConversation={(id) => openItem('conversation', id)}
      recentHasMore={conversationFeed.hasMore} recentLoading={conversationFeed.loading}
      recentError={conversationFeed.items.length ? conversationFeed.error : null}
      onLoadMoreRecent={() => void conversationFeed.more()}
    />
  </div>
{/snippet}

{#snippet taskListing()}
  <div class="min-w-0 md:h-[40rem]">
    <TasksSection
      projects={project ? [project] : []}
      board={projectTaskBoard(tasks, projectId ?? '')} agents={ownAgents} schedules={[]} onSeeAll={null}
      metadataForItem={(item) => taskMetadata.get(item.task_id) ?? ''}
      loading={taskFeed.loading} error={taskFeed.items.length ? null : taskFeed.error}
      onRetry={() => void taskFeed.refresh()}
      onOpenTask={(id) => openItem('task', id)} onOpenSchedule={(item) => openItem('schedule', item.schedule_id)}
      recentHasMore={taskFeed.hasMore} recentLoading={taskFeed.loading}
      recentError={taskFeed.items.length ? taskFeed.error : null}
      onLoadMoreRecent={() => void taskFeed.more()}
    />
  </div>
{/snippet}

<svelte:head><title>{project?.name ?? 'Project'} · Cognis</title></svelte:head>

<section class="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 sm:p-6">
  {#if error && !net.hideErrors}<p class="rounded-xl border border-danger-border bg-danger-subtle p-3 text-sm text-danger">{error}</p>{/if}

  {#if !project}
    <p class="text-sm text-muted">Loading project…</p>
  {:else}
    <div class="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
      <div class="flex items-start gap-4">
        <button type="button" class="shrink-0 cursor-pointer disabled:cursor-default" disabled={!project.avatar_url} onclick={() => { if (project?.avatar_url) showAvatarLightbox = true; }} aria-label="View project avatar">
          <ProjectAvatar name={project.name} avatarUrl={project.avatar_url} icon={project.metadata?.icon} color={project.color} class="h-16 w-16" />
        </button>
        <div>
          {#if !isWindowMode}
            <a class="text-sm text-accent hover:text-accent-hover" href="/projects?view=all">← All projects</a>
          {/if}
          <h1 class={`${isWindowMode ? '' : 'mt-2'} text-3xl font-semibold text-strong`}>{project.name}</h1>
          {#if project.description?.trim()}
            <p class="mt-2 max-w-3xl text-sm text-muted">{project.description}</p>
          {/if}
          <div class="mt-3 flex flex-wrap gap-2 text-xs text-muted">
            <span class="rounded-full border border-control-border px-2 py-1">{project.status}</span>
            <span class="rounded-full border border-violet-border bg-violet-subtle px-2 py-1 text-violet-tone" data-testid="project-role">{roleLabel(role)}</span>
            {#if activeGrants.length > 0 || project.is_shared_with_me}
              <span class="rounded-full border border-success-border bg-success-subtle px-2 py-1 text-success">Shared · {activeGrants.length + 1} people</span>
            {/if}
            {#if project.is_shared_with_me}<span class="rounded-full border border-warning-border px-2 py-1 text-warning">owned by {project.shared_by_email}</span>{/if}
          </div>
        </div>
      </div>
      <div class="flex flex-wrap gap-2">
        {#if !isWindowMode}
          <Button variant="secondary" size="icon" title="Open project in separate window" aria-label="Open project in separate window" onclick={openProjectInSeparateWindow}>
            <ExternalLink size={16} />
          </Button>
        {/if}
        {#if role !== 'viewer'}
          <Button onclick={() => startChat()}>New chat in project</Button>
        {/if}
        {#if project.is_shared_with_me}
          <Button variant="secondary" onclick={leaveProject}>Leave project</Button>
        {/if}
      </div>
    </div>

    <AccessibleTabs
      tabs={tabs}
      activeId={activeTab}
      idPrefix="project"
      ariaLabel="Project sections"
      testIdPrefix="project-tab"
      onChange={(id) => selectTab(id as ProjectTab)}
    />

    <div id={`project-panel-${activeTab}`} role="tabpanel" aria-labelledby={`project-tab-${activeTab}`}>
      {#if activeTab === 'overview'}
        <div class="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div class="min-w-0 lg:col-span-2">{@render conversationListing()}</div>
          <Card class="min-w-0 p-4">
            <h2 class="text-lg font-semibold text-strong">At a glance</h2>
            <dl class="mt-3 grid grid-cols-2 gap-3 text-sm">
              <div class="rounded-lg bg-canvas/60 p-3"><dt class="text-xs text-quiet">Agents</dt><dd class="mt-1 text-xl font-semibold text-strong">{projectAgents.length}</dd></div>
              <div class="rounded-lg bg-canvas/60 p-3"><dt class="text-xs text-quiet">Members</dt><dd class="mt-1 text-xl font-semibold text-strong">{activeGrants.length + 1}</dd></div>
              <div class="rounded-lg bg-canvas/60 p-3"><dt class="text-xs text-quiet">Tasks loaded</dt><dd class="mt-1 text-xl font-semibold text-strong">{tasks.length}{taskFeed.hasMore ? '+' : ''}</dd></div>
              <div class="rounded-lg bg-canvas/60 p-3"><dt class="text-xs text-quiet">Schedules</dt><dd><button type="button" class="mt-1 text-xl font-semibold text-strong" onclick={() => selectTab('schedules')}>{schedules.length}</button></dd></div>
            </dl>
            {#if projectAgents.length > 0}
              <div class="mt-4 flex flex-wrap gap-2">
                {#each projectAgents as agent (agent.agent_id)}
                  <button type="button" disabled={role === 'viewer'} onclick={() => startChat(agent.agent_id)} class="inline-flex items-center gap-1.5 rounded-full border border-control-border px-2 py-1 text-xs text-secondary">
                    <AgentAvatar name={agent.agent_display_name ?? agent.agent_name} avatarUrl={agent.agent_avatar_url} class="h-4 w-4" />
                    {agent.agent_display_name ?? agent.agent_name}{#if agent.is_default}<span class="text-violet-tone">· default</span>{/if}
                  </button>
                {/each}
              </div>
            {/if}
          </Card>
          <div class="min-w-0 lg:col-span-3">{@render taskListing()}</div>
          <div class="min-w-0 space-y-3 lg:col-span-3">
            <div class="flex items-center justify-between">
              <h2 class="text-lg font-semibold text-strong">Recent deliverables & artifacts</h2>
              <button type="button" class="text-xs text-accent hover:text-accent-hover" onclick={() => selectTab('resources')}>View all</button>
            </div>
            <ProjectResources {activity} onOpen={openItem} limit={4} />
          </div>
        </div>
      {:else if activeTab === 'usage'}
        {#key project.project_id}
          <UsageWorkspace url={page.url} historyState={history.state} project={{ id: project.project_id, label: project.name }} navigate={(url, state) => void goto(url, { noScroll: true, keepFocus: true, state: { ...page.state, ...state } })}/>
        {/key}
      {:else if activeTab === 'conversations'}
        <div class="min-w-0 space-y-3">
            <select bind:value={conversationAgentFilter} class="max-w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" aria-label="Filter by agent">
              <option value="">All agents</option>
              {#each [...new Set([...projectAgents.map((item) => item.agent_id), ...ownAgents.map((item) => item.agent_id)])] as agentId}
                <option value={agentId}>{agentNameById.get(agentId) ?? agentId}</option>
              {/each}
            </select>
          {@render conversationListing()}
        </div>
      {:else if activeTab === 'tasks'}
        {@render taskListing()}
      {:else if activeTab === 'schedules'}
        {#key project.project_id}
          <SchedulesWorkspace embedded projectId={project.project_id}
            canMutate={role !== 'viewer' && $auth.user?.role !== 'viewer'}
            onOpenSchedule={(id) => openItem('schedule', id)}
            onOpenTask={(id) => openItem('task', id)}
          />
        {/key}
      {:else if activeTab === 'agents'}
        <Card class="p-4">
          <h2 class="text-lg font-semibold text-strong">Project agents</h2>
          <p class="mt-1 text-sm text-muted">Assigned agents are available to every member inside this project. A default project classifies the agent's channel and direct chats as project work.</p>
          <div class="mt-4 space-y-2">
            {#each projectAgents as agent (agent.agent_id)}
              <div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-separator bg-canvas/50 p-3" data-testid="project-agent-row">
                <div class="flex min-w-0 items-center gap-3">
                  <AgentAvatar name={agent.agent_display_name ?? agent.agent_name} avatarUrl={agent.agent_avatar_url} class="h-9 w-9" />
                  <div class="min-w-0">
                    <p class="font-medium text-strong">{agent.agent_display_name ?? agent.agent_name}
                      {#if agent.is_default}<span class="ml-2 rounded-full border border-violet-border bg-violet-subtle px-2 py-0.5 text-[11px] text-violet-tone">default project</span>{/if}
                    </p>
                    <p class="text-xs text-quiet">Owner {authorLabel(agent.agent_owner_email)} · {executorScopeLabel(agent.executor_scope)}</p>
                  </div>
                </div>
                {#if role !== 'viewer'}
                  <Button size="sm" variant="secondary" onclick={() => startChat(agent.agent_id)}>New project chat</Button>
                {/if}
                {#if agent.is_owned_by_caller && canManage}
                  <div class="flex flex-wrap items-center gap-2">
                    <select class="rounded-lg border border-control-border bg-canvas/80 px-2 py-1.5 text-xs text-foreground" value={agent.executor_scope} disabled={savingAgent} onchange={(event) => void saveAgentAssignment(agent.agent_id, { executor_scope: (event.currentTarget as HTMLSelectElement).value as ExecutorScope })} aria-label="Executor">
                      <option value="owner_executor">Owner's executor</option>
                      <option value="grantee_executor">Member's own executor</option>
                    </select>
                    <Button size="sm" variant="secondary" disabled={savingAgent} onclick={() => void saveAgentAssignment(agent.agent_id, { is_default: !agent.is_default })}>{agent.is_default ? 'Unset default' : 'Make default'}</Button>
                    <Button size="sm" variant="danger" onclick={() => void removeAgent(agent)}>Remove</Button>
                  </div>
                {:else if isOwner}
                  <Button size="sm" variant="danger" onclick={() => void removeAgent(agent)}>Remove</Button>
                {/if}
              </div>
            {/each}
            {#if projectAgents.length === 0}<p class="text-sm text-muted">No agents assigned yet.</p>{/if}
          </div>
          {#if canManage}
            <form class="mt-5 grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto_auto] md:items-end" onsubmit={(event) => { event.preventDefault(); void assignAgent(); }} data-testid="project-assign-agent">
              <label class="block space-y-1"><span class="block text-xs uppercase tracking-widest text-quiet">Your agent</span>
                <select bind:value={agentForm.agent_id} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                  <option value="">Select an agent</option>
                  {#each assignableAgents as agent (agent.agent_id)}<option value={agent.agent_id}>{agent.display_name ?? agent.name}</option>{/each}
                </select>
              </label>
              <label class="block space-y-1"><span class="block text-xs uppercase tracking-widest text-quiet">Executor for members</span>
                <select bind:value={agentForm.executor_scope} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                  <option value="owner_executor">My executor (shared)</option>
                  <option value="grantee_executor">Bring your own</option>
                </select>
              </label>
              <label class="flex items-center gap-2 pb-2 text-sm text-secondary"><input type="checkbox" bind:checked={agentForm.is_default} /> Default project</label>
              <Button type="submit" disabled={savingAgent || !agentForm.agent_id}>Assign</Button>
            </form>
            <p class="mt-2 text-xs text-quiet">Only agents you own can be assigned. Members can use them only inside this project.</p>
          {/if}
        </Card>
      {:else if activeTab === 'knowledge'}
        {#key project.project_id}
          <ProjectKnowledge projectId={project.project_id} {canManage} />
        {/key}
      {:else if activeTab === 'resources'}
        <div class="min-w-0 space-y-4">
          <ResourceBrowser projectId={project.project_id} />
          <Card class="p-4">
            <h2 class="text-lg font-semibold text-strong">Sources</h2>
            <div class="mt-4 grid gap-3">
              {#each project.sources as source}
                <div class="rounded-xl border border-separator bg-canvas/50 p-3">
                  <div class="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <p class="font-medium text-strong">{source.name}</p>
                      <p class="mt-1 break-all text-sm text-muted">{source.local_path || source.remote_url || 'No path or remote configured'}</p>
                      {#if source.default_branch}<p class="mt-1 text-xs text-quiet">Branch: {source.default_branch}</p>{/if}
                      {#if source.credential_ref}<p class="mt-1 text-xs text-quiet">Credential clue: {source.credential_ref}</p>{/if}
                      {#if source.instructions}<p class="mt-2 whitespace-pre-wrap text-sm text-secondary">{source.instructions}</p>{/if}
                    </div>
                    {#if !readonly}<div class="flex gap-2"><Button size="sm" variant="secondary" onclick={() => resetSourceForm(source)}>Edit</Button><Button size="sm" variant="danger" onclick={() => deleteSource(source)}>Delete</Button></div>{/if}
                  </div>
                </div>
              {/each}
              {#if project.sources.length === 0}<p class="text-sm text-muted">No source hints configured.</p>{/if}
            </div>
            {#if !readonly}
              <form class="mt-5 grid gap-3 md:grid-cols-2" onsubmit={(event) => { event.preventDefault(); void saveSource(); }}>
                <Input bind:value={sourceForm.name} placeholder="Source name" />
                <Input bind:value={sourceForm.local_path} placeholder="Local path hint" />
                <Input bind:value={sourceForm.remote_url} placeholder="Remote URL" />
                <Input bind:value={sourceForm.default_branch} placeholder="Default branch" />
                <Input bind:value={sourceForm.credential_ref} placeholder="Credential reference clue" />
                <textarea bind:value={sourceForm.instructions} class="min-h-20 rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground md:col-span-2" placeholder="Source-specific instructions"></textarea>
                <div class="flex gap-2 md:col-span-2"><Button type="submit" disabled={savingSource || !sourceForm.name.trim()}>{savingSource ? 'Saving…' : editingSourceId ? 'Update source' : 'Add source'}</Button>{#if editingSourceId}<Button type="button" variant="secondary" onclick={() => resetSourceForm()}>Cancel edit</Button>{/if}</div>
              </form>
            {/if}
          </Card>
          <Card class="p-4">
            <h2 class="text-lg font-semibold text-strong">Bound Workflows</h2>
            <div class="mt-3 space-y-2">
              {#each project.workflow_ids as workflowId}
                <div class="flex items-center justify-between gap-2 rounded-lg bg-canvas/60 px-3 py-2 text-sm text-secondary">
                  <span>{workflowNameById.get(workflowId) ?? workflowId}</span>
                  {#if !readonly}<Button size="sm" variant="secondary" onclick={() => detachWorkflow(workflowId)}>Unbind</Button>{/if}
                </div>
              {/each}
              {#if project.workflow_ids.length === 0}<p class="text-sm text-muted">No project-bound workflows yet. System workflows remain available.</p>{/if}
            </div>
            {#if !readonly}
              <div class="mt-4 flex gap-2">
                <select bind:value={selectedWorkflowId} class="min-w-0 flex-1 rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground">
                  <option value="">Select workflow</option>
                  {#each attachableWorkflows as workflow}<option value={workflow.workflow_id}>{workflow.name}</option>{/each}
                </select>
                <Button onclick={attachWorkflow} disabled={savingWorkflow || !selectedWorkflowId}>{savingWorkflow ? 'Binding…' : 'Bind'}</Button>
              </div>
            {/if}
          </Card>
        </div>
      {:else if activeTab === 'settings'}
        <Card class="p-4">
          <h2 class="text-lg font-semibold text-strong">Project Settings</h2>
          {#if readonly}<p class="mt-1 text-sm text-muted">Managers and the owner can edit settings. Your role: {roleLabel(role)}.</p>{/if}
          <div class="mt-4 grid gap-3 md:grid-cols-2">
            <Input bind:value={projectForm.name} disabled={readonly} placeholder="Project name" />
            <div class="flex items-center gap-3">
              <label class="flex items-center gap-2 text-sm">Project color
                <input type="color" bind:value={projectForm.color} disabled={readonly || projectForm.automaticColor} class="h-8 w-10 rounded" />
              </label>
              <label class="flex items-center gap-2 text-xs text-muted"><input type="checkbox" bind:checked={projectForm.automaticColor} disabled={readonly} />Automatic</label>
            </div>
            <select bind:value={projectForm.default_workflow_id} disabled={readonly} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground disabled:opacity-60">
              <option value="">No default workflow</option>
              {#each workflows as workflow}<option value={workflow.workflow_id}>{workflow.name}</option>{/each}
            </select>
            <Input bind:value={projectForm.description} disabled={readonly} placeholder="Description" />
            <textarea bind:value={projectForm.instructions} disabled={readonly} class="min-h-40 rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground disabled:opacity-60 md:col-span-2" placeholder="Project instructions for agents"></textarea>
          </div>
          {#if activeGrants.length > 0 || project.is_shared_with_me}<p class="mt-2 text-xs text-warning/80">Instructions apply to every member's chats in this project.</p>{/if}
          {#if !readonly}
            <div class="mt-4 flex flex-wrap gap-2">
              <Button onclick={saveProject} disabled={savingProject || !projectForm.name.trim()}>{savingProject ? 'Saving…' : 'Save project'}</Button>
              <label class="inline-flex min-h-[40px] cursor-pointer items-center justify-center rounded-xl border border-control-border bg-surface px-4 py-2 text-sm font-medium text-foreground transition hover:border-border-strong hover:bg-raised md:min-h-[36px] md:py-1.5">
                <input class="hidden" type="file" accept="image/*" onchange={handleAvatarUpload} disabled={uploadingAvatar} />
                {uploadingAvatar ? 'Uploading…' : 'Upload avatar'}
              </label>
              <Button variant="secondary" onclick={() => { showAvatarModal = true; }}>Generate avatar</Button>
              {#if project.avatar_image_id || project.avatar_url}<Button variant="secondary" onclick={removeAvatar}>Remove avatar</Button>{/if}
            </div>
          {/if}
          <ProjectIconPicker bind:value={projectForm.icon} color={projectForm.color} disabled={readonly} />
        </Card>
      {:else if activeTab === 'sharing'}
        <Card class="p-4">
          <h2 class="text-lg font-semibold text-strong">Members</h2>
          <p class="mt-1 text-sm text-muted">Viewers read project work. Members also chat with project agents. Managers edit settings, sources, workflows, and assign their own agents. Only the owner shares the project.</p>
          <div class="mt-4 space-y-2" data-testid="project-members">
            <div class="flex items-center justify-between gap-2 rounded-lg bg-canvas/60 px-3 py-2 text-sm text-secondary">
              <span>{authorLabel(project.owner_email)}</span><span class="text-xs text-quiet">Owner</span>
            </div>
            {#each activeGrants as grant (grant.grant_id)}
              <div class="flex items-center justify-between gap-2 rounded-lg bg-canvas/60 px-3 py-2 text-sm text-secondary">
                <span>{authorLabel(grant.grantee_user_email ?? grant.grantee_group_id ?? '')}</span>
                {#if isOwner}
                  <div class="flex items-center gap-2">
                    <select class="rounded-lg border border-control-border bg-canvas/80 px-2 py-1 text-xs text-foreground" value={grant.permission} onchange={(event) => void changeGrantRole(grant, (event.currentTarget as HTMLSelectElement).value as ProjectGrantRole)} aria-label="Role">
                      <option value="viewer">Viewer</option>
                      <option value="member">Member</option>
                      <option value="manager">Manager</option>
                    </select>
                    <Button size="sm" variant="secondary" onclick={() => revokeGrant(grant)}>Remove</Button>
                  </div>
                {:else}
                  <span class="text-xs text-quiet">{roleLabel(grant.permission)}</span>
                {/if}
              </div>
            {/each}
          </div>
          {#if isOwner}
            <form class="mt-5 grid gap-2 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_minmax(0,2fr)_auto]" onsubmit={(event) => { event.preventDefault(); void createGrant(); }} data-testid="project-invite">
              <Input bind:value={grantForm.grantee_user_email} placeholder="user@example.com" />
              <select bind:value={grantForm.permission} class="rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" aria-label="Role">
                <option value="viewer">Viewer</option>
                <option value="member">Member</option>
                <option value="manager">Manager</option>
              </select>
              <Input bind:value={grantForm.note} placeholder="Note (optional)" />
              <Button type="submit" disabled={savingGrant || !grantForm.grantee_user_email.trim()}>{savingGrant ? 'Inviting…' : 'Invite'}</Button>
            </form>
            <p class="mt-2 text-xs text-quiet">New chats in a shared project are visible to members by default; each chat can be made private from its header.</p>
          {/if}
        </Card>
      {/if}
    </div>
  {/if}
</section>

{#if project}
  {#if quickChatAgent}
    {#key quickChatAgent.agent_id}
      <AgentQuickChatModal agent={quickChatAgent} onClose={closeItem} />
    {/key}
  {/if}
  {#if showNewChat}
  <NewChatModal
    agents={chatAgents}
    bind:selectedAgentId={chatAgentId}
    bind:selectedAgentProfileId={chatProfileId}
    bind:selectedAgentProfileMode={chatProfileMode}
    title={`New chat · ${project.name}`}
    busy={creatingChat}
    oncancel={() => { showNewChat = false; }}
    onconfirm={() => void createChat()}
  />
  {/if}
  {#key `${project.project_id}:${$auth.user?.email}`}
      <ProjectEntityWorkspace
        bind:this={entityWorkspace}
        canMutateSchedule={role !== 'viewer' && $auth.user?.role !== 'viewer'}
        projectId={project.project_id}
        userEmail={$auth.user?.email ?? ''}
        kind={openKind === 'conversation' || openKind === 'task' || openKind === 'schedule' || openKind === 'deliverable' || openKind === 'artifact' ? openKind : null}
        entityId={openId}
        agents={ownAgents}
        onClose={closeItem}
        onRefresh={loadActivity}
        onNavigate={openItem}
        onContinueConversation={role !== 'viewer' ? (conversation) => {
          if (chatAgents.some((agent) => agent.agent_id === conversation.agent_id)) void continueChat(conversation);
          else addToast('This agent is not available to you in this project.', 'error');
        } : undefined}
      />
  {/key}
{/if}

{#if selectedAttention}
  <AttentionActionDialog actionId={selectedAttention.action_id} title={selectedAttention.title}
    onClose={() => { selectedAttention = null; }}
    onSettled={() => { selectedAttention = null; refreshWaiting(); void loadActivity(); }}
    onUnavailable={() => { refreshWaiting(); void loadActivity(); }} />
{/if}

{#if showAvatarModal && project}
  <AvatarGenerateModal
    name={project.name}
    description={project.description ?? ''}
    personality={{ instructions: project.instructions ?? '' }}
    onAccept={handleGeneratedAvatar}
    onClose={() => { showAvatarModal = false; }}
  />
{/if}

{#if showAvatarLightbox && project?.avatar_url}
  <ImageLightbox src={project.avatar_url} alt={`${project.name} avatar`} onClose={() => { showAvatarLightbox = false; }} />
{/if}
