<script lang="ts">
  import { findTarget } from '$lib/find';
  import { agentModelCapabilities, type AgentModelCapabilities } from '$lib/agent-model-capabilities';
  import type { Snippet } from 'svelte';
  import type { InferenceRoute } from '$lib/inference-routes';
  import Sparkles from 'lucide-svelte/icons/sparkles';
import Loader2 from 'lucide-svelte/icons/loader-2';
  import Eye from 'lucide-svelte/icons/eye';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import AvatarGenerateModal from '$lib/components/agents/AvatarGenerateModal.svelte';
  import AgentSpeechVoice from './AgentSpeechVoice.svelte';
  import SkillDetailSheet from '$lib/components/skills/SkillDetailSheet.svelte';
  import ImageLightbox from '$lib/components/ImageLightbox.svelte';
  import { api } from '$lib/api/client';
  import { INSTANCE_MANAGEMENT_TOOLS } from '$lib/instance-management';
  import { thinkingEffortLabel } from '$lib/thinking';
  import {
    buildSystemPromptPreview,
    defaultSystemPrompt,
    formStateToPayload,
    formStateToSystemOverridePayload,
    slugify,
    type AdditionalExecutorEntry,
    type AgentRuntimeProfileFormState,
    type AgentFormState
  } from '$lib/agents';
  import type { Agent, AgentSkillUsage, CredentialMetadata, EffectiveToolItem, ExecutorConfig, IntarisMCPServer, KnowledgebaseModel, LLMProvider, MemoryBackendDescriptor, MemoryModeDescriptor, ModelEntry, SecretMetadata, Skill, ToolDefinitionSummary, Workflow } from '$lib/types/api';

  type AgentToolOption = (ToolDefinitionSummary & { tool_id?: string; permission?: string }) | EffectiveToolItem;
  type McpServerToolGroup = {
    key: string;
    label: string;
    sourceType: string;
    tools: AgentToolOption[];
  };

  let {
    mode,
    form = $bindable(),
    tools,
    workflows,
    providers,
    memoryBackends = [],
    executors = [],
    secrets = [],
    credentials = [],
    knowledgebases = [],
    skills = [],
    skillUsage = [],
    intarisMcpServers = [],
    agents = [],
    secondaryAgents = [],
    secondaryBindings = [],
    saving = false,
    error = '',
    readonly = false,
    isSystemAsset = false,
    editableFields = [],
    onSave,
    onBindingsChange,
    activeSection = $bindable('identity'),
    identityActions,
    accessActions,
    extraSections = [],
    dirty = true,
    bindingsSaving = false,
    toolsLoading = false,
    toolsError = '',
    onRetryTools,
    toolsKnown = true,
  } = $props<{
    mode: 'create' | 'edit';
    form: AgentFormState;
    tools: AgentToolOption[];
    workflows: Workflow[];
    providers: LLMProvider[];
    memoryBackends?: MemoryBackendDescriptor[];
    executors?: ExecutorConfig[];
    secrets?: SecretMetadata[];
    credentials?: CredentialMetadata[];
    knowledgebases?: KnowledgebaseModel[];
    skills?: Skill[];
    skillUsage?: AgentSkillUsage[];
    intarisMcpServers?: IntarisMCPServer[];
    agents?: Agent[];
    secondaryAgents?: Agent[];
    secondaryBindings?: string[];
    saving?: boolean;
    error?: string;
    readonly?: boolean;
    isSystemAsset?: boolean;
    editableFields?: string[];
    onSave: (payload: Record<string, unknown>) => void | Promise<void>;
    onBindingsChange?: ((bindings: string[]) => void | Promise<void>) | null;
    activeSection?: string;
    identityActions?: Snippet;
    accessActions?: Snippet;
    extraSections?: { id: string; label: string; content: Snippet }[];
    dirty?: boolean;
    bindingsSaving?: boolean;
    toolsLoading?: boolean;
    toolsError?: string;
    onRetryTools?: () => void;
    toolsKnown?: boolean;
  }>();

  let localBindings = $state<string[]>([]);
  const editorId = $props.id();
  let visitedSections = $state(new Set(['identity']));
  let toolSearch = $state('');
  const sections = $derived.by(() => [
    { id: 'identity', label: 'Identity', suffix: errors.name ? '!' : '' },
    { id: 'providers', label: 'Providers & models', suffix: errors.reasoningEffort ? '!' : '' },
    { id: 'tools', label: 'Tools', count: tools.length || undefined },
    { id: 'access', label: 'Access & delegation' },
    ...(!isSystemAsset || editableFieldSet.has('agent_profiles')
      ? [{ id: 'profiles', label: 'Profiles', count: form.agentProfiles.length, suffix: errors.agentProfiles ? '!' : '' }]
      : []),
    ...(form.agentType === 'primary'
      ? [{ id: 'workflows', label: 'Workflows', suffix: errors.stepAgentOverridesJson ? '!' : '' }]
      : []),
    ...extraSections
  ]);
  $effect(() => {
    if (!sections.some((section) => section.id === activeSection)) activeSection = 'identity';
    if (!visitedSections.has(activeSection)) {
      visitedSections = new Set([...visitedSections, activeSection]);
    }
  });

  function matchesTool(tool: AgentToolOption): boolean {
    return [tool.name, tool.description, tool.category, tool.source?.raw_tool_name]
      .join(' ').toLowerCase().includes(toolSearch.trim().toLowerCase());
  }
  let showAvatarModal = $state(false);
  let showAvatarLightbox = $state(false);
  let skillDetailId = $state<string | null>(null);
  let uploadingAvatar = $state(false);
  let fileInput: HTMLInputElement | undefined = $state();
  const editableFieldSet = $derived(new Set(editableFields));
  const selectedSkillDetail = $derived(skills.find((skill: Skill) => skill.skill_id === skillDetailId) ?? null);
  const selectedMemoryBackend = $derived(
    memoryBackends.find((backend: MemoryBackendDescriptor) => backend.id === form.memoryBackend) ?? null
  );
  const selectedMemoryMode = $derived(
    selectedMemoryBackend?.modes.find((item: MemoryModeDescriptor) => item.id === form.memoryMode)
      ?? (!form.memoryMode ? selectedMemoryBackend?.modes[0] : null)
      ?? null
  );
  const memoryToolsAvailable = $derived(
    tools.some((tool: AgentToolOption) => tool.category === 'memory' && (!('enabled' in tool) || tool.enabled))
  );

  $effect(() => {
    localBindings = [...secondaryBindings];
  });

  function canEditField(field: string): boolean {
    if (!isSystemAsset) return !readonly;
    return editableFieldSet.has(field);
  }

  function canEditTools(): boolean {
    return canEditField('tools');
  }

  function canEditPermissions(): boolean {
    return canEditField('permissions');
  }

  function canEditProfiles(): boolean {
    return canEditField('agent_profiles');
  }

  function handleAvatarAccept(imageId: string, avatarUrl: string) {
    form.avatarImageId = imageId;
    form.avatarUrl = avatarUrl;
    showAvatarModal = false;
  }

  async function handleAvatarUpload(event: Event) {
    const target = event.target as HTMLInputElement;
    const file = target.files?.[0];
    if (!file) return;
    uploadingAvatar = true;
    try {
      const result = await api.images.upload(file);
      form.avatarImageId = result.image_id;
      form.avatarUrl = result.url;
    } catch (e) {
      // Show error via the form error field
      error = e instanceof Error ? e.message : 'Failed to upload avatar';
    } finally {
      uploadingAvatar = false;
      if (target) target.value = '';
    }
  }

  function removeAvatar() {
    form.avatarImageId = '';
    form.avatarUrl = '';
  }

  let generatingField = $state<string | null>(null);

  function fieldContext(): Record<string, string> {
    return {
      name: form.name,
      description: form.description,
      tone: form.tone,
      temperament: form.temperament,
      purpose: form.purpose,
      behavioral_rules: form.behavioralRules,
      system_prompt: form.systemPrompt
    };
  }

  async function generateField(field: string, getter: () => string, setter: (v: string) => void) {
    generatingField = field;
    try {
      const result = await api.agents.generateField(field, getter(), fieldContext());
      setter(result.value);
    } catch (e) {
      error = e instanceof Error ? e.message : 'Failed to generate field';
    } finally {
      generatingField = null;
    }
  }

  function toggleBinding(agentId: string): void {
    const next = localBindings.includes(agentId)
      ? localBindings.filter((id) => id !== agentId)
      : [...localBindings, agentId];
    onBindingsChange?.(next);
  }

  function toggleAllowedAgent(agentId: string): void {
    form.allowedAgentIds = form.allowedAgentIds.includes(agentId)
      ? form.allowedAgentIds.filter((id: string) => id !== agentId)
      : [...form.allowedAgentIds, agentId];
  }

  function toggleAllowedController(agentId: string): void {
    form.allowedControllerAgentIds = form.allowedControllerAgentIds.includes(agentId)
      ? form.allowedControllerAgentIds.filter((id: string) => id !== agentId)
      : [...form.allowedControllerAgentIds, agentId];
  }

  const primaryAgentOptions = $derived(
    agents.filter((agent: Agent) => agent.agent_type === 'primary' && !agent.is_system && agent.status === 'active')
  );
  const systemSpecialistOptions = $derived(
    agents.filter((agent: Agent) => agent.agent_type === 'secondary' && agent.is_system && agent.status === 'active')
  );
  const unavailableAllowedAgentIds = $derived(
    form.allowedAgentIds.filter(
      (agentId: string) =>
        !primaryAgentOptions.some((agent: Agent) => agent.agent_id === agentId)
        && !systemSpecialistOptions.some((agent: Agent) => agent.agent_id === agentId)
    )
  );
  const unavailableControllerAgentIds = $derived(
    form.allowedControllerAgentIds.filter((agentId: string) => !primaryAgentOptions.some((agent: Agent) => agent.agent_id === agentId))
  );

  const permissionOptions = ['', 'allow', 'evaluate', 'deny'];
  function setToolPermission(toolId: string, value: string): void {
    if (value) form.toolPermissions[toolId] = value;
    else delete form.toolPermissions[toolId];
  }
  let inferenceRoutes = $state<InferenceRoute[]>([]);
  let routeLoadState = $state<'idle' | 'loading' | 'loaded' | 'error'>('idle');
  const selectedRouteModels = $derived([
    form.model,
    ...form.agentProfiles.map((profile: AgentRuntimeProfileFormState) => profile.model || form.model)
  ].filter(model => model.trim().startsWith('cognis/')));
  const routeMetadataIssue = $derived(
    selectedRouteModels.length === 0 ? null
      : routeLoadState === 'error' ? 'Could not load route capabilities. Retry before saving.'
      : routeLoadState !== 'loaded' ? 'Loading route capabilities…'
      : selectedRouteModels.some(model =>
        !agentModelCapabilities(providers, inferenceRoutes, 'cognis', model))
        ? 'Selected route or target model metadata is unavailable. Check the route and providers, then retry.'
        : null
  );
  async function loadInferenceRoutes(): Promise<void> {
    if (routeLoadState === 'loading') return;
    routeLoadState = 'loading';
    try {
      inferenceRoutes = (await api.inferenceRoutes.list()).routes;
      routeLoadState = 'loaded';
    } catch {
      routeLoadState = 'error';
    }
  }
  $effect(() => {
    if (routeLoadState !== 'idle') return;
    if (selectedRouteModels.length || ['providers', 'profiles'].includes(activeSection)) {
      void loadInferenceRoutes();
    }
  });

  const toolCategories = $derived<string[]>(
    [...new Set(tools.filter((tool: AgentToolOption) => !isMcpTool(tool)).map((tool: AgentToolOption) => tool.category))].sort() as string[]
  );

  const mcpTools = $derived<AgentToolOption[]>(
    tools.filter((tool: AgentToolOption) => isMcpTool(tool))
  );

  const mcpServerGroups = $derived<McpServerToolGroup[]>(groupMcpToolsByServer(mcpTools));

  function toolsForCategory(category: string): AgentToolOption[] {
    return tools.filter((tool: AgentToolOption) => tool.category === category);
  }

  function toolKey(tool: AgentToolOption): string {
    return tool.tool_id ?? tool.name;
  }

  function isMcpTool(tool: AgentToolOption): boolean {
    return tool.source?.type === 'local_mcp' || tool.source?.type === 'intaris_mcp';
  }

  function mcpServerKey(tool: AgentToolOption): string {
    const source = tool.source;
    const sourceType = source?.type === 'intaris_mcp' ? 'intaris_mcp' : 'local_mcp';
    return `${sourceType}:${source?.server_id || source?.server_name || 'unknown'}`;
  }

  function mcpSourceLabel(sourceType: string): string {
    if (sourceType === 'intaris_mcp') return 'Intaris MCP';
    if (sourceType === 'local_mcp') return 'Executor MCP';
    return sourceType;
  }

  function groupMcpToolsByServer(items: AgentToolOption[]): McpServerToolGroup[] {
    const grouped = new Map<string, McpServerToolGroup>();
    for (const tool of items) {
      const key = mcpServerKey(tool);
      const sourceType = tool.source?.type || 'unknown';
      const label = tool.source?.server_name || tool.source?.server_id || 'unknown';
      const group = grouped.get(key) ?? { key, label, sourceType, tools: [] };
      group.tools.push(tool);
      grouped.set(key, group);
    }
    return Array.from(grouped.values())
      .map((group) => ({
        ...group,
        tools: group.tools.slice().sort((left, right) => toolKey(left).localeCompare(toolKey(right)))
      }))
      .sort((left, right) => {
        const sourceCompare = mcpSourceLabel(left.sourceType).localeCompare(mcpSourceLabel(right.sourceType));
        if (sourceCompare !== 0) return sourceCompare;
        return left.label.localeCompare(right.label);
      });
  }

  function categoryDisabled(category: string): boolean {
    return form.disabledCategories.includes(category);
  }

  function toggleCategory(category: string): void {
    if (categoryDisabled(category)) {
      form.disabledCategories = form.disabledCategories.filter((value: string) => value !== category);
      return;
    }
    form.disabledCategories = [...form.disabledCategories, category];
  }

  function toolDisabled(toolName: string): boolean {
    return form.disabledTools.includes(toolName);
  }

  function toggleTool(toolName: string): void {
    if (toolDisabled(toolName)) {
      form.disabledTools = form.disabledTools.filter((value: string) => value !== toolName);
      return;
    }
    form.disabledTools = [...form.disabledTools, toolName];
  }

  function mcpServerDisabled(serverKey: string): boolean {
    return form.disabledMcpServers.includes(serverKey);
  }

  function toggleMcpServer(group: McpServerToolGroup): void {
    if (mcpServerDisabled(group.key)) {
      form.disabledMcpServers = form.disabledMcpServers.filter((value: string) => value !== group.key);
      return;
    }
    const groupToolKeys = new Set(group.tools.map((tool) => toolKey(tool)));
    form.disabledMcpServers = [...form.disabledMcpServers, group.key];
    form.disabledTools = form.disabledTools.filter((value: string) => !groupToolKeys.has(value));
  }

  function validateJson(value: string, label: string): string | null {
    if (!value.trim()) {
      return null;
    }
    try {
      JSON.parse(value);
      return null;
    } catch {
      return `${label} must be valid JSON.`;
    }
  }

  function validationErrors(): Record<string, string> {
    const errors: Record<string, string> = {};
    if (!form.name.trim()) {
      errors.name = 'Name is required.';
    }
    if (canEditField('llm_config.reasoning_effort') &&
        !(form.model.trim().startsWith('cognis/') && routeMetadataIssue) &&
        invalidThinking(form.reasoningEffort, availableThinkingEfforts())) {
      errors.reasoningEffort = 'Thinking effort is unavailable for this model. Select an available value.';
    }
    if (!Number.isFinite(form.autoProfileMinConfidence) ||
        form.autoProfileMinConfidence < 0.5 || form.autoProfileMinConfidence > 1) {
      errors.agentProfiles = 'Auto selection minimum confidence must be between 0.5 and 1.';
    }
    const seenProfileIds = new Set<string>();
    for (const profile of form.agentProfiles) {
      const profileId = profile.profileId.trim();
      if (canEditProfiles() &&
          !((profile.model || form.model).trim().startsWith('cognis/') && routeMetadataIssue) &&
          invalidThinking(profile.reasoningEffort, availableThinkingEffortsForProfile(profile))) {
        errors.agentProfiles = `Thinking effort for profile "${profileId}" is unavailable. Select an available value.`;
        break;
      }
      if (!profileId) {
        errors.agentProfiles = 'Each runtime profile needs a profile ID.';
        break;
      }
      if (profileId.includes('/')) {
        errors.agentProfiles = 'Runtime profile IDs must not contain "/".';
        break;
      }
      if (seenProfileIds.has(profileId)) {
        errors.agentProfiles = `Runtime profile ID "${profileId}" is duplicated.`;
        break;
      }
      if (profile.agentSwitchable && !profile.description.trim()) {
        errors.agentProfiles =
          `Runtime profile "${profileId}" needs description routing guidance before agents can switch to it.`;
        break;
      }
      if (!Number.isSafeInteger(profile.quickSwitchIndex) || profile.quickSwitchIndex < 0) {
        errors.agentProfiles = `Runtime profile "${profileId}" needs a non-negative integer quick-switch position.`;
        break;
      }
      seenProfileIds.add(profileId);
    }
    const stepJsonError = validateJson(form.stepAgentOverridesJson, 'Step agent overrides');
    if (stepJsonError) {
      errors.stepAgentOverridesJson = stepJsonError;
    }
    return errors;
  }

  const errors = $derived(validationErrors());
  const canSubmit = $derived(!routeMetadataIssue && Object.keys(errors).length === 0 && Boolean(form.name.trim()));

  // Keep agentId in sync with name until user manually edits the ID field
  $effect(() => {
    if (!form.customId && mode === 'create') {
      form.agentId = slugify(form.name);
    }
  });

  function toggleSecret(secretName: string): void {
    if (form.allowedSecrets.includes(secretName)) {
      form.allowedSecrets = form.allowedSecrets.filter((v: string) => v !== secretName);
    } else {
      form.allowedSecrets = [...form.allowedSecrets, secretName];
    }
  }

  function toggleCredential(credentialId: string): void {
    if (form.allowedCredentials.includes(credentialId)) {
      form.allowedCredentials = form.allowedCredentials.filter((v: string) => v !== credentialId);
    } else {
      form.allowedCredentials = [...form.allowedCredentials, credentialId];
    }
  }

  function toggleKnowledgebase(knowledgebaseId: string): void {
    if (form.allowedKnowledgebases.includes(knowledgebaseId)) {
      form.allowedKnowledgebases = form.allowedKnowledgebases.filter((v: string) => v !== knowledgebaseId);
    } else {
      form.allowedKnowledgebases = [...form.allowedKnowledgebases, knowledgebaseId];
    }
  }

  function toggleSkill(skillId: string): void {
    if (form.selectedSkillIds.includes(skillId)) {
      form.selectedSkillIds = form.selectedSkillIds.filter((v: string) => v !== skillId);
      form.autoLoadSkillIds = form.autoLoadSkillIds.filter((v: string) => v !== skillId);
    } else {
      form.selectedSkillIds = [...form.selectedSkillIds, skillId];
    }
  }

  function toggleSkillAutoLoad(skillId: string): void {
    if (!form.selectedSkillIds.includes(skillId)) {
      return;
    }
    if (form.autoLoadSkillIds.includes(skillId)) {
      form.autoLoadSkillIds = form.autoLoadSkillIds.filter((v: string) => v !== skillId);
    } else {
      form.autoLoadSkillIds = [...form.autoLoadSkillIds, skillId];
    }
  }

  function attachedToAllAgents(skill: Skill): boolean {
    return Boolean(skill.attach_to_all_agents ?? skill.auto_load);
  }

  const globallyAttachedSkills = $derived(skills.filter((s: Skill) => attachedToAllAgents(s) && !form.selectedSkillIds.includes(s.skill_id)));
  const selectableSkills = $derived(skills.filter((s: Skill) => !attachedToAllAgents(s)));
  const skillUsageById = $derived(new Map<string, AgentSkillUsage>(skillUsage.map((item: AgentSkillUsage) => [item.skill_id, item])));

  function skillUsageHint(skillId: string, selected: boolean): string | null {
    const usage = skillUsageById.get(skillId);
    if (selected && usage?.suggestion === 'consider_removing' && !form.autoLoadSkillIds.includes(skillId)) {
      return 'Not loaded in the last 30 days. Consider removing it.';
    }
    if (!selected && usage?.suggestion === 'consider_attaching') {
      return `Found and loaded through search ${usage.search_load_count} times. Consider attaching it.`;
    }
    return null;
  }
  const optionalBuiltinTools = INSTANCE_MANAGEMENT_TOOLS;

  function toggleOptInBuiltinTool(toolId: string): void {
    if (form.optInBuiltinTools.includes(toolId)) {
      form.optInBuiltinTools = form.optInBuiltinTools.filter((value: string) => value !== toolId);
    } else {
      form.optInBuiltinTools = [...form.optInBuiltinTools, toolId];
    }
  }

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!canSubmit || saving || !dirty || (readonly && !isSystemAsset)) {
      return;
    }
    await onSave(isSystemAsset ? formStateToSystemOverridePayload(form) : formStateToPayload(form));
  }

  function resetSystemPrompt(): void {
    form.systemPrompt = defaultSystemPrompt(form.name);
  }

  function modelsForProvider(providerId: string): string[] {
    if (!providerId || providerId === 'cognis') {
      return inferenceRoutes.map(route => route.reference);
    }
    const provider = providers.find((p: LLMProvider) => p.provider_id === providerId);
    if (!provider) {
      return [];
    }
    const models: string[] = [];
    const defaultModel = provider.config?.default_model;
    if (typeof defaultModel === 'string' && defaultModel) {
      models.push(defaultModel);
    }
    for (const m of provider.models ?? []) {
      const id = typeof m.model_id === 'string' ? m.model_id : '';
      if (id && !models.includes(id)) {
        models.push(id);
      }
    }
    return models;
  }

  /** Get models configured on the currently selected provider */
  function selectedProviderModels(): string[] {
    return modelsForProvider(form.providerId);
  }

  function modelInfoForProvider(providerId: string, modelId: string): AgentModelCapabilities | null {
    return agentModelCapabilities(providers, inferenceRoutes, providerId, modelId);
  }

  function selectedProviderModelInfo(): AgentModelCapabilities | null {
    return modelInfoForProvider(form.providerId, form.model);
  }

  function supportsFastMode(): boolean {
    return selectedProviderModelInfo()?.supports_fast_mode === true;
  }

  function supportsFastModeForProfile(profile: AgentRuntimeProfileFormState): boolean {
    return modelInfoForProvider(
      profile.providerId || form.providerId,
      profile.model || form.model
    )?.supports_fast_mode === true;
  }

  function availableThinkingEfforts(): string[] {
    const modelInfo = selectedProviderModelInfo();
    if (modelInfo) {
      return modelInfo.reasoning_efforts.length > 0 ? modelInfo.reasoning_efforts : ['default'];
    }
    return ['default'];
  }

  function availableThinkingEffortsForProfile(profile: AgentRuntimeProfileFormState): string[] {
    const modelInfo = modelInfoForProvider(
      profile.providerId || form.providerId, profile.model || form.model
    );
    if (modelInfo) {
      return modelInfo.reasoning_efforts.length > 0 ? modelInfo.reasoning_efforts : ['default'];
    }
    return ['default'];
  }

  function invalidThinking(value: string, available: string[]): boolean {
    return value !== '' && !available.includes(value);
  }

  function addRuntimeProfile(): void {
    const existing = new Set(
      form.agentProfiles.map((profile: AgentRuntimeProfileFormState) => profile.profileId)
    );
    let base = 'fast';
    if (existing.has(base)) {
      base = 'profile';
    }
    let profileId = base;
    let suffix = 2;
    while (existing.has(profileId)) {
      profileId = `${base}-${suffix}`;
      suffix += 1;
    }
    const nextProfile: AgentRuntimeProfileFormState = {
      profileId,
      description: '',
      providerId: '',
      model: '',
      reasoningEffort: '',
      fastMode: 'inherit',
      systemPromptExtra: '',
      memoryAvailability: 'inherit',
      memoryMode: '',
      memoryBackendOptions: {},
      enabled: true,
      agentSwitchable: false,
      quickSwitch: false,
      quickSwitchIndex: form.agentProfiles.length,
      quickSwitchColor: 'neutral'
    };
    form.agentProfiles = [...form.agentProfiles, nextProfile];
    if (!form.defaultAgentProfileId) {
      form.defaultAgentProfileId = profileId;
    }
  }

  function selectMemoryBackend(event: Event): void {
    const backendId = (event.currentTarget as HTMLSelectElement).value;
    form.memoryBackend = backendId;
    const descriptor = memoryBackends.find(
      (backend: MemoryBackendDescriptor) => backend.id === backendId
    );
    const defaultMode = descriptor?.defaults.mode;
    form.memoryMode =
      typeof defaultMode === 'string' ? defaultMode : (descriptor?.modes[0]?.id ?? '');
    form.memoryBackendOptions = {};
    form.agentProfiles = form.agentProfiles.map((profile: AgentRuntimeProfileFormState) => ({
      ...profile,
      memoryMode: '',
      memoryBackendOptions: {}
    }));
  }

  function removeRuntimeProfile(profileId: string): void {
    form.agentProfiles = form.agentProfiles.filter(
      (profile: AgentRuntimeProfileFormState) => profile.profileId !== profileId
    );
    if (form.defaultAgentProfileId === profileId) {
      form.defaultAgentProfileId = form.agentProfiles[0]?.profileId ?? '';
    }
  }
</script>

<div class="space-y-5">
  <AccessibleTabs tabs={sections} activeId={activeSection} idPrefix={editorId}
    ariaLabel="Agent configuration" sticky onChange={(id) => { activeSection = id; }} />
<form id={`${editorId}-form`} class="space-y-5" onsubmit={handleSubmit}>
  <div class="min-w-0 space-y-5">
    <div class="min-w-0 space-y-5">
       <div hidden={activeSection !== 'identity'} id={`${editorId}-panel-identity`} role="tabpanel" aria-labelledby={`${editorId}-tab-identity`} tabindex="0">
       {#if visitedSections.has('identity')}
       <div class="space-y-5">
       {@render identityActions?.()}
      <!-- Identity -->
      <Card class="p-5">
        <p class="mb-3 text-xs font-medium uppercase tracking-[0.25em] text-muted">Identity</p>
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Name <span class="text-danger">*</span></span>
            <Input aria-invalid={errors.name ? 'true' : 'false'} bind:value={form.name} placeholder="Research Assistant" disabled={readonly} />
            {#if errors.name}
              <span class="text-xs text-danger">{errors.name}</span>
            {/if}
          </label>
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>ID <span class="text-quiet">(optional)</span></span>
            <Input
              bind:value={form.agentId}
              disabled={mode === 'edit' || readonly}
              placeholder="auto-generated from name"
              oninput={() => { if (mode === 'create') form.customId = true; }}
            />
            {#if mode === 'create' && !form.customId}
              <span class="text-xs text-muted">Auto-generated from name. Type here to override.</span>
            {:else if mode === 'create' && form.customId}
              <button type="button" class="text-xs text-accent hover:text-accent-hover" onclick={() => { form.customId = false; form.agentId = slugify(form.name); }}>Reset to auto</button>
            {/if}
          </label>
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Type</span>
            <select bind:value={form.agentType} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={mode === 'edit' || readonly}>
              <option value="primary">Primary</option>
              <option value="secondary">Secondary</option>
            </select>
            <span class="block text-xs text-muted">
              {form.agentType === 'primary' ? 'Interactive agent with personality and memory.' : 'Lightweight task executor for focused sub-tasks.'}
            </span>
          </label>
          <div class="space-y-2 text-sm font-medium text-foreground">
            <span>Avatar</span>
            <div class="flex items-center gap-3">
              {#if form.avatarUrl}
                <button type="button" class="cursor-pointer" onclick={() => { showAvatarLightbox = true; }} title="View full size">
                  <AgentAvatar name={form.name || 'A'} avatarUrl={form.avatarUrl} class="h-14 w-14" />
                </button>
              {:else}
                <AgentAvatar name={form.name || 'A'} avatarUrl={null} class="h-14 w-14" />
              {/if}
              <div class="flex flex-col gap-1.5">
                {#if !readonly && !isSystemAsset}
                  <div class="flex gap-2">
                    <input
                      bind:this={fileInput}
                      type="file"
                      accept="image/png,image/jpeg,image/webp"
                      class="hidden"
                      onchange={handleAvatarUpload}
                    />
                    <button
                      type="button"
                      class="rounded-lg border border-control-border bg-raised/60 px-2.5 py-1 text-xs text-secondary hover:bg-inset hover:text-foreground disabled:opacity-50"
                      onclick={() => fileInput?.click()}
                      disabled={uploadingAvatar}
                    >
                      {uploadingAvatar ? 'Uploading...' : 'Upload'}
                    </button>
                    <button
                      type="button"
                      class="rounded-lg border border-control-border bg-raised/60 px-2.5 py-1 text-xs text-accent hover:bg-inset hover:text-accent-hover"
                      onclick={() => { showAvatarModal = true; }}
                    >
                      Generate
                    </button>
                    {#if form.avatarImageId || form.avatarUrl}
                      <button
                        type="button"
                        class="rounded-lg border border-control-border bg-raised/60 px-2.5 py-1 text-xs text-danger hover:bg-inset hover:text-danger"
                        onclick={removeAvatar}
                      >
                        Remove
                      </button>
                    {/if}
                  </div>
                {/if}
              </div>
            </div>
          </div>
        </div>
        <div class="mt-4 space-y-2 text-sm font-medium text-foreground">
          <div class="flex items-center justify-between">
            <span>Description</span>
            {#if !readonly}
              <button
                type="button"
                class="flex items-center gap-1 text-xs text-accent hover:text-accent-hover disabled:opacity-50"
                disabled={generatingField === 'description'}
                onclick={() => generateField('description', () => form.description, (v) => { form.description = v; })}
                title="Generate with AI"
              >
                {#if generatingField === 'description'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
              </button>
            {/if}
          </div>
          <textarea bind:value={form.description} class="min-h-[90px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet" disabled={readonly}></textarea>
        </div>
      </Card>

      <!-- Personality (primary only) -->
      {#if form.agentType === 'primary'}
        <Card class="p-5">
          <p class="mb-3 text-xs font-medium uppercase tracking-[0.25em] text-muted">Personality</p>
          <div class="grid gap-4 md:grid-cols-3">
            <div class="space-y-2 text-sm font-medium text-foreground">
              <div class="flex items-center justify-between">
                <span>Tone</span>
                {#if !readonly && !isSystemAsset}
                  <button type="button" class="text-accent hover:text-accent-hover disabled:opacity-50" disabled={generatingField === 'tone'} onclick={() => generateField('tone', () => form.tone, (v) => { form.tone = v; })} title="Generate with AI">
                    {#if generatingField === 'tone'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                  </button>
                {/if}
              </div>
              <Input bind:value={form.tone} placeholder="e.g. formal, friendly, witty" disabled={readonly} />
              <span class="block text-xs text-quiet">How the agent communicates — voice and style.</span>
            </div>
            <div class="space-y-2 text-sm font-medium text-foreground">
              <div class="flex items-center justify-between">
                <span>Temperament</span>
                {#if !readonly && !isSystemAsset}
                  <button type="button" class="text-accent hover:text-accent-hover disabled:opacity-50" disabled={generatingField === 'temperament'} onclick={() => generateField('temperament', () => form.temperament, (v) => { form.temperament = v; })} title="Generate with AI">
                    {#if generatingField === 'temperament'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                  </button>
                {/if}
              </div>
              <Input bind:value={form.temperament} placeholder="e.g. patient, cautious, bold" disabled={readonly} />
              <span class="block text-xs text-quiet">How the agent behaves — disposition and reactions.</span>
            </div>
            <div class="space-y-2 text-sm font-medium text-foreground">
              <div class="flex items-center justify-between">
                <span>Purpose</span>
                {#if !readonly && !isSystemAsset}
                  <button type="button" class="text-accent hover:text-accent-hover disabled:opacity-50" disabled={generatingField === 'purpose'} onclick={() => generateField('purpose', () => form.purpose, (v) => { form.purpose = v; })} title="Generate with AI">
                    {#if generatingField === 'purpose'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                  </button>
                {/if}
              </div>
              <Input bind:value={form.purpose} placeholder="research specialist" disabled={readonly} />
            </div>
          </div>
          <div class="mt-4 space-y-2 text-sm font-medium text-foreground">
            <div class="flex items-center justify-between">
              <span>Behavioral rules (one per line)</span>
              {#if !readonly && !isSystemAsset}
                <button type="button" class="flex items-center gap-1 text-xs text-accent hover:text-accent-hover disabled:opacity-50" disabled={generatingField === 'behavioral_rules'} onclick={() => generateField('behavioral_rules', () => form.behavioralRules, (v) => { form.behavioralRules = v; })} title="Generate with AI">
                  {#if generatingField === 'behavioral_rules'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                </button>
              {/if}
            </div>
            <textarea bind:value={form.behavioralRules} class="min-h-[110px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet" placeholder="Always cite sources&#10;Prefer concise answers" disabled={readonly}></textarea>
          </div>
        </Card>
      {/if}

      <!-- Editable identity instructions -->
      <Card class="p-5">
        <div class="mb-3 flex items-center justify-between gap-3">
          <p class="text-xs font-medium uppercase tracking-[0.25em] text-muted">Agent instructions</p>
          {#if !readonly && !isSystemAsset}
            <Button size="sm" variant="secondary" type="button" onclick={resetSystemPrompt}>Reset to default</Button>
          {:else if isSystemAsset}
            <span class="text-xs text-quiet">Managed by the shipped system agent definition.</span>
          {:else}
            <span class="text-xs text-quiet">Read-only because this agent is shared with you.</span>
          {/if}
        </div>
        <textarea bind:value={form.systemPrompt} class="min-h-[180px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 text-sm text-foreground placeholder:text-quiet" placeholder="You are {'{'}name{'}'}, a capable general-purpose AI assistant.&#10;&#10;Optimize for correctness, usefulness, and actionability over verbosity." disabled={readonly}></textarea>
        <p class="mt-2 text-xs text-muted">Defines this agent's identity, communication style, and domain-specific behavior. Cognis runtime rules, tool policy, memory context, workflow instructions, and safety constraints are injected separately and cannot be overridden here.</p>
      </Card>

      <details class="rounded-2xl border border-separator p-5">
        <summary class="cursor-pointer text-sm font-medium text-foreground">System prompt preview</summary>
        <p class="mt-2 text-sm text-muted">Editable identity only. Runtime instructions, memory, tools, skills, and workflow context are added separately.</p>
        <pre class="mt-4 whitespace-pre-wrap break-words rounded-xl bg-canvas/70 p-4 text-sm leading-6 text-foreground">{buildSystemPromptPreview(form)}</pre>
      </details>
       </div>
       {/if}
      </div>
      {#snippet executorSettings()}
      <Card class="p-5">
        <h2 class="mb-1 text-lg font-semibold text-strong">Execution access</h2>
        <p class="mb-5 text-sm text-muted">Choose where this agent works and which executors it can access.</p>
        <div class="mb-4 grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Executor</span>
            <select bind:value={form.executorId} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={readonly}>
              <option value="">Use default executor</option>
              {#each executors as executor}
                <option value={executor.executor_id}>{executor.name} ({executor.executor_type})</option>
              {/each}
            </select>
            <span class="block text-xs text-muted">Choose a specific executor or leave empty to use executor label matching / default resolution.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Executor selector (optional, key=value)</span>
            <textarea bind:value={form.executorSelector} class="min-h-[72px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 font-mono text-sm text-foreground" placeholder="tier=standard&#10;location=local" disabled={readonly || !!form.executorId}></textarea>
            <span class="block text-xs text-muted">Used when no explicit executor is selected. Matches executor labels like Kubernetes selectors.</span>
          </label>
        </div>

      </Card>
      {/snippet}
        {#snippet backendSettings()}
        <div class="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Card class="min-w-0 p-5">
          <h2 class="mb-4 text-sm font-semibold text-foreground">Memory</h2>
          <div class="grid gap-4">
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Memory backend</span>
              <select
                bind:value={form.memoryBackend}
                onchange={selectMemoryBackend}
                class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                disabled={readonly || isSystemAsset}
              >
                {#if !selectedMemoryBackend}
                  <option value={form.memoryBackend}>{form.memoryBackend}</option>
                {/if}
                {#each memoryBackends as backend}
                  <option value={backend.id}>{backend.display_name}</option>
                {/each}
              </select>
              <span class="block text-xs text-muted">
                {selectedMemoryBackend?.description ?? 'Provider-owned memory backend.'}
              </span>
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span title="Mode behavior and copy come from the selected backend.">Memory mode</span>
              <select
                bind:value={form.memoryMode}
                class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                disabled={readonly || isSystemAsset || form.memoryBackend === 'none' || !selectedMemoryBackend?.modes.length}
              >
                {#if form.memoryMode && !selectedMemoryMode}
                  <option value={form.memoryMode}>{form.memoryMode}</option>
                {/if}
                {#each selectedMemoryBackend?.modes ?? [] as memoryMode}
                  <option value={memoryMode.id}>{memoryMode.label}</option>
                {/each}
              </select>
              <span class="block text-xs text-muted" title={selectedMemoryMode?.tooltip}>
                {selectedMemoryMode?.description ?? 'No memory mode applies.'}
              </span>
            </label>
          </div>
          {#if selectedMemoryMode}
            <details class="mt-4 text-xs text-muted">
              <summary class="cursor-pointer text-secondary">Memory mode details</summary>
              <p class="mt-3">{selectedMemoryMode.recommended_for}</p>
              <dl class="mt-3 grid grid-cols-2 gap-2">
                <dt>Core bootstrap</dt><dd>{selectedMemoryMode.behavior.core_bootstrap ? 'On' : 'Off'}</dd>
                <dt>Auto recall</dt><dd>{selectedMemoryMode.behavior.auto_recall ? 'On' : 'Off'}</dd>
                <dt>Auto remember</dt><dd>{selectedMemoryMode.behavior.auto_remember ? 'On' : 'Off'}</dd>
                <dt>Memory tools</dt><dd>{selectedMemoryMode.behavior.tools ? 'On' : 'Off'}</dd>
              </dl>
            </details>
          {/if}
          {#if toolsKnown && form.memoryBackend !== 'none' && selectedMemoryMode?.behavior.tools && !memoryToolsAvailable}
            <p class="mt-4 rounded-xl border border-warning-border bg-warning-subtle px-4 py-3 text-xs text-warning">
              This mode expects memory tools, but the current tool allowlist excludes them.
            </p>
          {/if}
        </Card>
        <Card class="min-w-0 p-5">
          <h2 class="mb-4 text-sm font-semibold text-foreground">Guardrails</h2>
          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-foreground md:col-span-2">
              <span>Guardrails backend</span>
              <select
                bind:value={form.guardrailsBackend}
                class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                disabled={readonly || isSystemAsset}
              >
                <option value="intaris">Intaris</option>
                <option value="none">None</option>
              </select>
              <span class="block text-xs text-muted">
                {form.guardrailsBackend === 'none' ? 'Ordinary tool-call evaluation and proactive guardrails are disabled. Mandatory checks and the event store remain active.' : 'Evaluates tool calls before execution.'}
              </span>
            </label>
            {#if form.guardrailsBackend === 'intaris'}
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Intaris evaluation</span>
              <select bind:value={form.intarisEvaluation} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={readonly || isSystemAsset}>
                <option value="legacy">Legacy</option>
                <option value="bounded_autonomy">Bounded autonomy</option>
                <option value="balanced">Balanced</option>
                <option value="strict">Strict</option>
              </select>
              <span class="block text-xs text-muted">Tasks inherit this evaluation unless overridden.</span>
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Approvals</span>
              <select bind:value={form.approvalMode} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={readonly || isSystemAsset}>
                <option value="ask">Ask me</option>
                <option value="unattended">Unattended (escalations are denied)</option>
              </select>
              <span class="block text-xs text-muted">{form.approvalMode === 'unattended' ? 'The Judge approves or denies; escalations are denied without waiting for you.' : 'Escalated calls wait for your approval.'}</span>
            </label>
            {/if}
          </div>
          <label class="mt-4 flex items-start gap-3 text-sm text-foreground">
            <input
              type="checkbox"
              bind:checked={form.analyticsEnabled}
              disabled={readonly || isSystemAsset}
              class="mt-1"
            />
            <span>
              <span class="block font-medium">Session analytics</span>
              <span class="block text-xs text-muted">Intaris summaries and behavioral analytics, independent of the guardrails backend. Session events are always retained.</span>
            </span>
          </label>
          {#if form.guardrailsBackend === 'intaris'}
          <details class="mt-4 border-t border-separator pt-4" open={!!(form.minimumOutcome || form.maximumOutcome || form.escalationTimeoutSeconds)}>
            <summary class="cursor-pointer text-sm font-medium text-secondary">Advanced evaluation controls</summary>
            <div class="mt-4 grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Minimum evaluation outcome</span>
              <select bind:value={form.minimumOutcome} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={readonly || isSystemAsset}>
                <option value="">None (standard evaluation)</option>
                <option value="deny">Deny all evaluated calls</option>
                <option value="escalate">Require human approval</option>
                <option value="approve">No additional restriction</option>
              </select>
              <span class="block text-xs text-muted">Selects the stricter outcome after evaluation. Explicit Cognis access denials still apply.</span>
            </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Maximum evaluation outcome</span>
              <select bind:value={form.maximumOutcome} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={readonly || isSystemAsset}>
                <option value="">None (standard evaluation)</option>
                <option value="deny">Do not upgrade denials</option>
                <option value="escalate">Escalate evaluated denials</option>
                <option value="approve">Allow evaluated denials (yolo)</option>
              </select>
              <span class="block text-xs text-muted">Selects the more permissive outcome; a minimum restriction takes precedence.</span>
            </label>
          <label class="block space-y-2 text-sm font-medium text-foreground md:col-span-2">
            <span>Escalation timeout (seconds)</span>
            <input
              type="number"
              min="1"
              max="86400"
              step="1"
              bind:value={form.escalationTimeoutSeconds}
              disabled={readonly || isSystemAsset}
              placeholder="Inherit global default (300 seconds)"
              class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
            />
            <span class="block text-xs text-muted">Pending tool approvals are denied after this wait. Tasks can override it; empty inherits the global setting.</span>
          </label>
            </div>
          </details>
          {/if}
          {#if form.guardrailsBackend === 'intaris' && form.maximumOutcome === 'approve'}
            <div role="alert" class="mt-4 rounded-xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">
              <strong>DANGEROUS — Yolo mode:</strong> Intaris still evaluates and audits every evaluated call, but critical and denied calls may execute. This does not override explicit tool permissions or session lifecycle blocks.
            </div>
          {/if}
          {#if mode === 'edit' && form.guardrailsBackend === 'none'}
            <div class="mt-4 rounded-xl border border-warning-border bg-warning-subtle px-4 py-3 text-sm text-warning">
              <p class="font-medium">Guardrails are disabled for this agent.</p>
              <p class="mt-1 text-xs text-warning/80">
                Ordinary tool calls are auto-approved. Mandatory runtime checks still apply. Use this only for trusted agents and controlled environments.
              </p>
            </div>
          {/if}
        </Card>
        </div>

        {/snippet}

       <div hidden={activeSection !== 'access'} id={`${editorId}-panel-access`} role="tabpanel" aria-labelledby={`${editorId}-tab-access`} tabindex="0" class="space-y-5">
       {#if visitedSections.has('access')}
       {@render accessActions?.()}
      {@render executorSettings()}
      <Card class="p-5">
        <!-- Stage 36: Additional executors (multi-executor agents) -->
        <div class="mb-4 rounded-2xl border border-control-border bg-canvas/40 p-4">
          <div class="mb-2 flex items-center justify-between">
            <div>
              <p class="text-sm font-medium text-foreground">Additional executors (optional)</p>
              <p class="mt-1 text-xs text-muted">
                Extra executors the agent can target via <code class="text-secondary">target_executor</code>
                or <code class="text-secondary">switch_executor</code>. Never auto-selected by the controller.
              </p>
            </div>
            <button
              type="button"
              class="rounded-lg border border-control-border bg-surface px-3 py-1 text-xs text-foreground hover:bg-raised"
              disabled={readonly}
              onclick={() => {
                form.additionalExecutors = [
                  ...form.additionalExecutors,
                  { executorId: '', executorSelector: '', description: '' }
                ];
              }}
            >
              Add
            </button>
          </div>
          {#if form.additionalExecutors.length === 0}
            <p class="text-xs text-quiet">No additional executors configured.</p>
          {/if}
          {#each form.additionalExecutors as additional, idx (idx)}
            <div class="mt-3 grid gap-3 rounded-xl border border-separator bg-canvas/60 p-3 md:grid-cols-2">
              <label class="space-y-1 text-xs font-medium text-foreground">
                <span>Executor</span>
                <select
                  bind:value={additional.executorId}
                  class="w-full rounded-lg border border-control-border bg-canvas/80 px-2 py-1 text-sm text-foreground"
                  disabled={readonly || !!additional.executorSelector.trim()}
                >
                  <option value="">— or use selector below —</option>
                  {#each executors as executor}
                    <option value={executor.executor_id}>{executor.name} ({executor.executor_type})</option>
                  {/each}
                </select>
              </label>
              <label class="space-y-1 text-xs font-medium text-foreground">
                <span>Selector (key=value, one per line)</span>
                <textarea
                  bind:value={additional.executorSelector}
                  class="min-h-[60px] w-full rounded-lg border border-control-border bg-canvas/80 px-2 py-1 font-mono text-xs text-foreground"
                  placeholder="role=browser&#10;loc=local"
                  disabled={readonly || !!additional.executorId.trim()}
                ></textarea>
              </label>
              <label class="space-y-1 text-xs font-medium text-foreground md:col-span-2">
                <span>Description (optional)</span>
                <input
                  bind:value={additional.description}
                  class="w-full rounded-lg border border-control-border bg-canvas/80 px-2 py-1 text-sm text-foreground"
                  placeholder="e.g. personal Mac for coding"
                  disabled={readonly}
                />
              </label>
              <div class="md:col-span-2">
                <button
                  type="button"
                  class="rounded-lg border border-danger-border bg-danger-subtle px-3 py-1 text-xs text-danger hover:bg-danger-subtle"
                  disabled={readonly}
                  onclick={() => {
                    form.additionalExecutors = form.additionalExecutors.filter(
                      (_entry: AdditionalExecutorEntry, i: number) => i !== idx
                    );
                  }}
                >
                  Remove
                </button>
              </div>
            </div>
          {/each}
        </div>

        <div class="space-y-4 rounded-2xl border border-separator bg-canvas/60 p-4">
          <div>
            <p class="text-sm font-medium text-foreground">Delegation</p>
            <p class="mt-1 text-xs text-muted">
              Primary agents receive managed conversations or tasks. System specialists receive
              bounded synchronous delegate calls and cannot delegate further.
            </p>
          </div>
          <label class="flex items-center gap-3 text-sm font-medium text-foreground">
            <input bind:checked={form.delegationEnabled} class="h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" disabled={readonly} />
            Allow this agent to assign work to other agents
          </label>
          <div class="grid gap-4 md:grid-cols-2">
            <label class="flex items-center gap-3 text-sm font-medium text-foreground">
              <input bind:checked={form.primaryDelegationEnabled} class="h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" disabled={readonly || !form.delegationEnabled} />
              Primary-agent managed work and tasks
            </label>
            <label class="flex items-center gap-3 text-sm font-medium text-foreground">
              <input bind:checked={form.systemDelegationEnabled} class="h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" disabled={readonly || !form.delegationEnabled} />
              Bounded system-specialist delegation
            </label>
          </div>
          <label class="block space-y-2 text-sm font-medium text-foreground">
            <span>Maximum managed depth</span>
            <select
              bind:value={form.maxManagedDepth}
              class="w-full rounded-lg border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
              disabled={readonly || !form.delegationEnabled || !form.primaryDelegationEnabled}
            >
              <option value={0}>0 — disabled</option>
              <option value={1}>1 — one worker level</option>
              <option value={2}>2 — coordinator and worker</option>
            </select>
          </label>
          <label class="flex items-center gap-3 text-sm font-medium text-foreground">
            <input bind:checked={form.restrictDelegationTargets} class="h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" disabled={readonly || !form.delegationEnabled} />
            Restrict allowed target agents
          </label>
          {#if form.restrictDelegationTargets}
            <div class="space-y-3">
              <p class="text-sm font-medium text-foreground">Allowed target agents</p>
              <p class="text-xs text-muted">This agent can always assign work to itself. An empty selection disables every other target.</p>
              {#if form.agentId}
                <div class="rounded-xl border border-success-border bg-success-subtle px-3 py-2 text-sm text-success">
                  <span class="font-medium">Self</span>
                  <span class="ml-2 font-mono text-xs text-success/80">{form.agentId}</span>
                  <span class="ml-2 text-xs text-success/70">always allowed</span>
                </div>
              {/if}
              <div class="grid gap-2 md:grid-cols-2">
                {#each primaryAgentOptions.filter((agent: Agent) => agent.agent_id !== form.agentId) as agent}
                  <label class="flex items-start gap-3 rounded-xl border border-separator bg-canvas/60 px-3 py-2 text-sm text-foreground">
                    <input checked={form.allowedAgentIds.includes(agent.agent_id)} class="mt-0.5 h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" onchange={() => toggleAllowedAgent(agent.agent_id)} disabled={readonly} />
                    <span><span class="block">{agent.display_name ?? agent.name}</span><span class="block font-mono text-xs text-quiet">{agent.agent_id} · primary</span></span>
                  </label>
                {/each}
                {#each systemSpecialistOptions as agent}
                  <label class="flex items-start gap-3 rounded-xl border border-separator bg-canvas/60 px-3 py-2 text-sm text-foreground">
                    <input checked={form.allowedAgentIds.includes(agent.agent_id)} class="mt-0.5 h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" onchange={() => toggleAllowedAgent(agent.agent_id)} disabled={readonly} />
                    <span><span class="block">{agent.display_name ?? agent.name}</span><span class="block font-mono text-xs text-quiet">{agent.agent_id} · system specialist</span></span>
                  </label>
                {/each}
              </div>
              {#if unavailableAllowedAgentIds.length > 0}
                <div class="rounded-xl border border-warning-border bg-warning-subtle p-3">
                  <p class="text-xs font-medium text-warning">Unavailable configured targets</p>
                  <div class="mt-2 flex flex-wrap gap-2">
                    {#each unavailableAllowedAgentIds as agentId}
                      <label class="flex items-center gap-2 rounded-lg border border-warning-border px-2 py-1 font-mono text-xs text-warning">
                        <input checked class="h-3.5 w-3.5 rounded border-warning-border" type="checkbox" onchange={() => toggleAllowedAgent(agentId)} disabled={readonly} />
                        {agentId}
                      </label>
                    {/each}
                  </div>
                </div>
              {/if}
            </div>
          {/if}
          <label class="flex items-center gap-3 text-sm font-medium text-foreground">
            <input bind:checked={form.restrictControllers} class="h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" disabled={readonly} />
            Restrict agents allowed to target this primary agent
          </label>
          {#if form.restrictControllers}
            <div class="space-y-3">
              <p class="text-sm font-medium text-foreground">Allowed controller agents</p>
              <p class="text-xs text-muted">Self is always allowed. An empty selection prevents every other agent from targeting this agent.</p>
              <div class="grid gap-2 md:grid-cols-2">
                {#each primaryAgentOptions.filter((agent: Agent) => agent.agent_id !== form.agentId) as agent}
                  <label class="flex items-start gap-3 rounded-xl border border-separator bg-canvas/60 px-3 py-2 text-sm text-foreground">
                    <input checked={form.allowedControllerAgentIds.includes(agent.agent_id)} class="mt-0.5 h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" onchange={() => toggleAllowedController(agent.agent_id)} disabled={readonly} />
                    <span><span class="block">{agent.display_name ?? agent.name}</span><span class="block font-mono text-xs text-quiet">{agent.agent_id}</span></span>
                  </label>
                {/each}
              </div>
              {#if unavailableControllerAgentIds.length > 0}
                <div class="rounded-xl border border-warning-border bg-warning-subtle p-3">
                  <p class="text-xs font-medium text-warning">Unavailable configured controllers</p>
                  <div class="mt-2 flex flex-wrap gap-2">
                    {#each unavailableControllerAgentIds as agentId}
                      <label class="flex items-center gap-2 rounded-lg border border-warning-border px-2 py-1 font-mono text-xs text-warning">
                        <input checked class="h-3.5 w-3.5 rounded border-warning-border" type="checkbox" onchange={() => toggleAllowedController(agentId)} disabled={readonly} />
                        {agentId}
                      </label>
                    {/each}
                  </div>
                </div>
              {/if}
            </div>
          {/if}
        </div>

        {#if credentials.length > 0}
          <div class="mt-4">
            <p class="mb-2 text-sm font-medium text-foreground">Allowed credentials</p>
            <div class="grid gap-2 md:grid-cols-2">
              {#each credentials as credential}
                <label class="flex items-center gap-2 text-sm text-foreground">
                  <input checked={form.allowedCredentials.includes(credential.credential_id)} class="h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" onchange={() => toggleCredential(credential.credential_id)} disabled={readonly} />
                  {credential.label}<span class="text-xs text-muted">({credential.kind})</span>
                </label>
              {/each}
            </div>
          </div>
        {/if}

        {#if knowledgebases.length > 0}
          <div class="mt-4">
            <p class="mb-2 text-sm font-medium text-foreground">Allowed knowledgebases</p>
            <div class="grid gap-2 md:grid-cols-2">
              {#each knowledgebases as kb}
                <label class="flex items-start gap-2 text-sm text-foreground">
                  <input checked={form.allowedKnowledgebases.includes(kb.knowledgebase_id)} class="mt-0.5 h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" onchange={() => toggleKnowledgebase(kb.knowledgebase_id)} disabled={readonly} />
                  <span><span>{kb.name}</span><span class="block font-mono text-xs text-quiet">{kb.knowledgebase_id}</span></span>
                </label>
              {/each}
            </div>
          </div>
        {/if}

        {#if secrets.length > 0}
          <div class="mt-4">
            <p class="mb-2 text-sm font-medium text-foreground">Allowed secrets</p>
            <div class="grid gap-2 md:grid-cols-2">
              {#each secrets as secret}
                <label class="flex items-center gap-2 text-sm text-foreground">
                  <input checked={form.allowedSecrets.includes(secret.name)} class="h-4 w-4 rounded border-control-border bg-canvas" type="checkbox" onchange={() => toggleSecret(secret.name)} disabled={readonly} />
                  {secret.name}<span class="text-xs text-muted">({secret.scope})</span>
                </label>
              {/each}
            </div>
          </div>
        {/if}
      </Card>
      {/if}
      </div>

      <div hidden={activeSection !== 'tools'} id={`${editorId}-panel-tools`} role="tabpanel" aria-labelledby={`${editorId}-tab-tools`} tabindex="0" class="space-y-5">
       {#if visitedSections.has('tools')}
       {#if toolsLoading}
         <p role="status" class="p-5 text-sm text-muted">Loading tool catalog…</p>
       {:else if toolsError}
         <p role="alert" class="p-5 text-sm text-danger">{toolsError} <button type="button" class="underline" onclick={onRetryTools}>Retry loading tools</button></p>
       {:else}
      <Card class="p-5">
        <div class="mt-4 rounded-2xl border border-warning-border bg-warning-subtle p-4">
          <p class="text-sm font-medium text-warning">Optional built-in tools</p>
          <p class="mt-1 text-xs text-warning/70">These privileged controller tools are off by default and are not enabled by wildcard tool access.</p>
          <div class="mt-3 space-y-2">
            {#each optionalBuiltinTools as tool}
              <label class="flex items-start gap-3 text-sm text-foreground">
                <input
                  checked={form.optInBuiltinTools.includes(tool.id)}
                  class="mt-0.5 h-4 w-4 rounded border-control-border bg-canvas"
                  type="checkbox"
                  onchange={() => toggleOptInBuiltinTool(tool.id)}
                  disabled={readonly || form.agentType === 'secondary'}
                />
                <span>
                  <span class="font-medium">{tool.label}</span>
                  <span class="block text-xs text-muted">{tool.description}</span>
                </span>
              </label>
            {/each}
          </div>
        </div>

        {#if tools.length > 0}
          <div class="mt-4 space-y-3 rounded-2xl border border-separator bg-canvas/60 p-4">
            <label class="block space-y-2 text-sm text-foreground">
              <span>Find a tool</span>
              <input use:findTarget type="search" bind:value={toolSearch} placeholder="Search names, descriptions, or categories"
                onkeydown={(event) => { if (event.key === 'Enter') event.preventDefault(); }}
                class="w-full rounded-xl border border-control-border bg-canvas px-3 py-2 text-sm text-foreground" />
            </label>
            {#if toolSearch.trim() && !tools.some(matchesTool)}
              <p role="status" class="text-sm text-muted">No tools match “{toolSearch}”.</p>
            {/if}
            <div>
              <p class="text-sm font-medium text-foreground">Tool categories</p>
              <p class="mt-1 text-xs text-muted">Agents inherit all tools from their executor by default. Disable categories or individual tools here, then use permissions to require evaluation or deny access.</p>
            </div>

            <div class="flex flex-wrap gap-2">
              {#each toolCategories as category}
                {@const disabled = categoryDisabled(category)}
                <button
                  type="button"
                  class="px-3 py-1.5 rounded-lg text-sm border transition-colors {disabled ? 'bg-surface border-control-border text-muted' : 'bg-success-subtle border-success-border text-success'}"
                  onclick={() => toggleCategory(category)}
                  disabled={!canEditTools()}
                >
                  {category}
                  <span class="ml-1 text-xs opacity-60">({toolsForCategory(category).length})</span>
                </button>
              {/each}
            </div>

            <!-- Use the page scroll instead of a nested tool-list scroller. -->
            <div class="space-y-2">
              {#each toolCategories.filter((category) => toolsForCategory(category).some(matchesTool)) as category}
                {@const categoryTools = toolsForCategory(category).filter(matchesTool)}
                <details open={!!toolSearch.trim()} class="rounded-xl border border-separator bg-canvas/50 p-3">
                  <summary class="cursor-pointer text-sm font-medium text-foreground">
                    {category}
                    <span class="ml-2 text-xs text-quiet">{categoryDisabled(category) ? 'disabled' : 'enabled'}</span>
                  </summary>
                  <div class="mt-3 space-y-2">
                    {#each categoryTools as tool}
                      <div class="grid gap-2 md:grid-cols-[1fr_auto_auto] items-center text-sm">
                        <label class="flex items-center gap-3 text-foreground">
                          <input type="checkbox" checked={!toolDisabled(toolKey(tool))} onchange={() => toggleTool(toolKey(tool))} disabled={!canEditTools() || categoryDisabled(category)} class="h-4 w-4 rounded border-control-border bg-canvas" />
                          <span class="font-mono">{tool.source?.type === 'skill' && tool.source?.raw_tool_name ? tool.source.raw_tool_name : tool.name}</span>
                        </label>
                        <span class="text-xs text-quiet">
                          {tool.description}
                          {#if (tool as EffectiveToolItem).available_on && ((tool as EffectiveToolItem).available_on?.length ?? 0) > 1}
                            <span class="ml-2 rounded-full bg-raised px-2 py-0.5 font-mono text-[10px] text-secondary" title="Stage 36: this tool is available on more than one assigned executor. Use target_executor on the tool call to pick.">
                              on: {((tool as EffectiveToolItem).available_on ?? []).join(', ')}
                            </span>
                          {/if}
                        </span>
                        <select aria-label={`Permission for ${tool.name}`} value={form.toolPermissions[toolKey(tool)] ?? ''} onchange={(event) => setToolPermission(toolKey(tool), event.currentTarget.value)} class="w-32 rounded-lg border border-control-border bg-canvas/80 px-2 py-1 text-xs text-foreground" disabled={!canEditPermissions() || toolDisabled(toolKey(tool)) || categoryDisabled(category)}>
                          {#each permissionOptions as option}
                            <option value={option}>{option || 'inherit'}</option>
                          {/each}
                        </select>
                      </div>
                    {/each}
                  </div>
                </details>
              {/each}
            </div>

            {#if mcpServerGroups.some((group) => group.tools.some(matchesTool))}
              <div class="mt-4 space-y-2 rounded-xl border border-accent/20 bg-accent-subtle p-3">
                <div>
                  <p class="text-sm font-medium text-accent">MCP servers</p>
                  <p class="mt-1 text-xs text-muted">Disable a whole MCP server group, or keep the server enabled and disable individual tools below.</p>
                </div>
                <div class="space-y-2">
                  {#each mcpServerGroups.filter((group) => group.tools.some(matchesTool)) as group}
                    {@const serverDisabled = mcpServerDisabled(group.key)}
                    <details open={!!toolSearch.trim()} class="rounded-lg border border-separator bg-canvas/60 p-3">
                      <summary class="cursor-pointer text-sm font-medium text-foreground">
                        <span>{group.label}</span>
                        <span class="ml-2 rounded-full bg-raised px-2 py-0.5 text-[10px] text-muted">{mcpSourceLabel(group.sourceType)}</span>
                        <span class="ml-2 text-xs text-quiet">{serverDisabled ? 'disabled' : 'enabled'} · {group.tools.length} tools</span>
                      </summary>
                      <div class="mt-3 space-y-3">
                        <label class="flex items-start gap-3 text-sm text-foreground">
                          <input
                            type="checkbox"
                            checked={!serverDisabled}
                            onchange={() => toggleMcpServer(group)}
                            disabled={!canEditTools()}
                            class="mt-0.5 h-4 w-4 rounded border-control-border bg-canvas"
                          />
                          <span>
                            <span class="font-medium">Enable server group</span>
                            <span class="block font-mono text-xs text-quiet">{group.key}</span>
                          </span>
                        </label>
                        <div class="space-y-2 border-t border-separator pt-3">
                          {#each group.tools.filter(matchesTool) as tool}
                            <div class="grid gap-2 md:grid-cols-[1fr_auto_auto] items-center text-sm {serverDisabled ? 'opacity-50' : ''}">
                              <label class="flex items-center gap-3 text-foreground">
                                <input
                                  type="checkbox"
                                  checked={!toolDisabled(toolKey(tool))}
                                  onchange={() => toggleTool(toolKey(tool))}
                                  disabled={!canEditTools() || serverDisabled}
                                  class="h-4 w-4 rounded border-control-border bg-canvas"
                                />
                                <span class="font-mono">{tool.source?.raw_tool_name || tool.name}</span>
                              </label>
                              <span class="text-xs text-quiet">
                                {tool.description}
                                {#if (tool as EffectiveToolItem).available_on && ((tool as EffectiveToolItem).available_on?.length ?? 0) > 1}
                                  <span class="ml-2 rounded-full bg-raised px-2 py-0.5 font-mono text-[10px] text-secondary" title="Stage 36: this tool is available on more than one assigned executor. Use target_executor on the tool call to pick.">
                                    on: {((tool as EffectiveToolItem).available_on ?? []).join(', ')}
                                  </span>
                                {/if}
                              </span>
                              <select aria-label={`Permission for ${tool.name}`} value={form.toolPermissions[toolKey(tool)] ?? ''} onchange={(event) => setToolPermission(toolKey(tool), event.currentTarget.value)} class="w-32 rounded-lg border border-control-border bg-canvas/80 px-2 py-1 text-xs text-foreground" disabled={!canEditPermissions() || serverDisabled || toolDisabled(toolKey(tool))}>
                                {#each permissionOptions as option}
                                  <option value={option}>{option || 'inherit'}</option>
                                {/each}
                              </select>
                            </div>
                          {/each}
                        </div>
                      </div>
                    </details>
                  {/each}
                </div>
              </div>
            {/if}
          </div>
        {/if}

        <!-- Skills -->
        {#if skills.length > 0}
          <div class="mt-4 space-y-3">
            <p class="text-sm font-medium text-foreground">Skills</p>
            <p class="text-xs text-muted">Select skills to attach to this agent. Auto-load instructions only for core skills that should be injected at startup.</p>
            {#if selectableSkills.length > 0}
              <div class="grid gap-2 md:grid-cols-2">
                {#each selectableSkills as skill}
                  {@const selected = form.selectedSkillIds.includes(skill.skill_id)}
                  <div class="space-y-2 rounded-lg border border-separator bg-canvas/70 px-3 py-2 text-sm text-foreground">
                    <div class="flex items-center justify-between gap-3">
                      <label class="flex min-w-0 items-center gap-2">
                        <input
                          checked={selected}
                          class="h-4 w-4 rounded border-control-border bg-canvas"
                          type="checkbox"
                          onchange={() => toggleSkill(skill.skill_id)}
                          disabled={!canEditField('skills')}
                        />
                        <span class="truncate">{skill.name}</span>
                        {#if skill.current_version?.tools && skill.current_version.tools.length > 0}
                          <span class="text-xs text-quiet">({skill.current_version.tools.length} tools)</span>
                        {/if}
                        {#if skill.current_version?.asset_manifest && skill.current_version.asset_manifest.length > 0}
                          <span class="text-xs text-quiet">({skill.current_version.asset_manifest.length} assets)</span>
                        {/if}
                      </label>
                      <button type="button" class="inline-flex items-center gap-1 text-xs text-muted hover:text-foreground" onclick={() => { skillDetailId = skill.skill_id; }}>
                        <Eye class="h-3.5 w-3.5" /> View
                      </button>
                    </div>
                    {#if skillUsageHint(skill.skill_id, selected)}
                      <p class="ml-6 text-xs text-muted">{skillUsageHint(skill.skill_id, selected)}</p>
                    {/if}
                    {#if selected}
                      <label class="ml-6 flex items-start gap-2 text-xs text-muted">
                        <input
                          checked={form.autoLoadSkillIds.includes(skill.skill_id)}
                          class="mt-0.5 h-3.5 w-3.5 rounded border-control-border bg-canvas"
                          type="checkbox"
                          onchange={() => toggleSkillAutoLoad(skill.skill_id)}
                          disabled={!canEditField('skills')}
                        />
                        <span>
                          Auto-load instructions
                          <span class="block text-quiet">Inject full skill instructions at startup; use sparingly because it increases prompt size.</span>
                        </span>
                      </label>
                    {/if}
                  </div>
                {/each}
              </div>
            {/if}
            {#if globallyAttachedSkills.length > 0}
              <div class="mt-2">
                <p class="mb-1 text-xs text-quiet">Globally attached to every agent:</p>
                <div class="flex flex-wrap gap-1">
                  {#each globallyAttachedSkills as skill}
                    <span class="rounded bg-inset px-2 py-0.5 text-xs text-secondary">{skill.name}</span>
                  {/each}
                </div>
              </div>
            {/if}
          </div>
        {/if}

        <SkillDetailSheet
          open={skillDetailId !== null}
          skill={selectedSkillDetail}
          mode="view"
          onClose={() => { skillDetailId = null; }}
          allowManage={false}
        />

        <!-- Intaris MCP Servers -->
        {#if intarisMcpServers.length > 0}
          <div class="mt-4 space-y-3">
            <p class="text-sm font-medium text-foreground">Intaris MCP servers</p>
            <p class="text-xs text-muted">Remote MCP servers managed by Intaris. Select which servers this agent can use.</p>
            <div class="flex flex-wrap gap-2">
              {#each intarisMcpServers as server}
                {@const selected = (form.intarisMcpServers || []).includes(server.name)}
                <button
                  type="button"
                  class="px-3 py-1.5 rounded-lg text-sm border transition-colors {selected ? 'bg-accent-subtle border-accent/50 text-accent' : 'bg-surface border-control-border text-muted hover:border-control-border'}"
                  disabled={!canEditTools()}
                  onclick={() => {
                    if (!canEditTools()) return;
                    const current = form.intarisMcpServers || [];
                    if (selected) {
                      form.intarisMcpServers = current.filter((n: string) => n !== server.name);
                    } else {
                      form.intarisMcpServers = [...current, server.name];
                    }
                  }}
                >
                  {server.name}
                  {#if server.tools_count > 0}
                    <span class="ml-1 text-xs opacity-60">({server.tools_count} tools)</span>
                  {/if}
                </button>
              {/each}
            </div>
          </div>
        {/if}

        {#if form.mcpServers.length > 0}
          <div class="mt-4 rounded-2xl border border-accent/30 bg-accent-subtle px-4 py-3 text-sm text-accent">
            <p class="font-medium">This agent still has legacy inline MCP server definitions.</p>
            <p class="mt-1 text-accent/90">Create these servers in Settings → Tools and assign them to an executor. Existing inline MCP config is preserved for backward compatibility.</p>
          </div>
        {/if}
      </Card>

      <!-- Provider & Model -->
      {/if}
      {/if}
      </div>
      <div hidden={activeSection !== 'providers'} id={`${editorId}-panel-providers`} role="tabpanel" aria-labelledby={`${editorId}-tab-providers`} tabindex="0" class="space-y-5">
      {#if visitedSections.has('providers')}
      {@render backendSettings()}
      <Card class="p-5">
        <p class="mb-3 text-xs font-medium uppercase tracking-[0.25em] text-muted">LLM Configuration</p>
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Provider</span>
            <select bind:value={form.providerId} onchange={() => {
              if (form.providerId === 'cognis' && !form.model.startsWith('cognis/')) form.model = '';
              else if (form.providerId !== 'cognis' && form.model.startsWith('cognis/')) form.model = '';
            }} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={!canEditField('llm_config.provider_id')}>
              <option value="">Use default provider</option>
              <option value="cognis">Cognis (custom routes)</option>
              {#each providers as provider}
                <option value={provider.provider_id}>{provider.display_name}{provider.is_default ? ' ⭐' : ''}</option>
              {/each}
            </select>
            <span class="block text-xs text-muted">Leave empty to use the system default.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Model</span>
            {#if selectedProviderModels().length > 0}
               <select bind:value={form.model} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={!canEditField('llm_config.model')}>
                <option value="">Use provider default</option>
                {#each selectedProviderModels() as modelId}
                  <option value={modelId}>{modelId}</option>
                {/each}
              </select>
            {:else}
               <Input bind:value={form.model} placeholder="Use provider default" disabled={!canEditField('llm_config.model')} />
            {/if}
            <span class="block text-xs text-muted">Leave empty to use the provider's default model.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Temperature</span>
            <Input bind:value={form.temperature} type="number" placeholder="default" disabled={!canEditField('llm_config.temperature')} />
          </label>
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Max tokens</span>
            <Input bind:value={form.maxTokens} type="number" placeholder="default" disabled={!canEditField('llm_config.max_tokens')} />
          </label>
          <label class="space-y-2 text-sm font-medium text-foreground">
            <span>Thinking effort</span>
            <select bind:value={form.reasoningEffort} aria-describedby={`${editorId}-thinking-help`} aria-invalid={invalidThinking(form.reasoningEffort, availableThinkingEfforts())} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={!canEditField('llm_config.reasoning_effort')}>
              <option value="">Inherit</option>
              {#if invalidThinking(form.reasoningEffort, availableThinkingEfforts())}
                <option value={form.reasoningEffort}>{thinkingEffortLabel(form.reasoningEffort)} (unavailable)</option>
              {/if}
              {#each availableThinkingEfforts() as value}
                <option value={value}>{thinkingEffortLabel(value)}</option>
              {/each}
            </select>
            <span id={`${editorId}-thinking-help`} class="block text-xs text-muted">
              Inherit uses routing/provider configuration. Provider default does not inherit an effort hint.
              {#if !selectedProviderModelInfo()}Capabilities unavailable; only inheritance and provider default can be selected.{/if}
              {#if invalidThinking(form.reasoningEffort, availableThinkingEfforts())}Select an available value before saving.{/if}
            </span>
          </label>
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Fast mode</span>
              <select bind:value={form.fastMode} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={!canEditField('llm_config.fast_mode')}>
                <option value="inherit">Inherit</option>
                <option value="enabled" disabled={!supportsFastMode()}>Enabled{supportsFastMode() ? '' : ' (unavailable)'}</option>
                <option value="disabled">Disabled</option>
              </select>
              <span class="block text-xs text-muted">
                {#if supportsFastMode()}Requests accelerated execution; account eligibility is not confirmed and premium pricing can apply.
                {:else if selectedProviderModelInfo()}Fast mode is not supported for this provider/model.
                {:else}Fast-mode capabilities unavailable.{/if}
                Inherit uses lower-layer configuration. Disabled suppresses inherited acceleration.
              </span>
            </label>
          <AgentSpeechVoice voices={form.voices ?? {}} legacyVoice={form.voice} {providers}
            providerId={form.ttsProviderId ?? ''} model={form.ttsModel ?? ''} voice={form.ttsVoice ?? ''}
            disabled={!canEditField('llm_config.tts_model')}
            onbindingchange={(providerId, model) => {
              form.ttsProviderId = providerId; form.ttsModel = model; form.ttsVoice = '';
              form.voice = ''; form.voices = {};
            }}
            onvoicechange={(voice) => { form.ttsVoice = voice; form.voice = ''; form.voices = {}; }}
            onchange={(providerId, voice) => { form.voices = { ...form.voices, [providerId]: voice }; }} />
        </div>
      </Card>

      {/if}
      </div>
      {#if !isSystemAsset || editableFieldSet.has('agent_profiles')}
      <div hidden={activeSection !== 'profiles'} id={`${editorId}-panel-profiles`} role="tabpanel" aria-labelledby={`${editorId}-tab-profiles`} tabindex="0">
      {#if visitedSections.has('profiles')}
      <Card class="p-5">
        <div class="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-muted">Runtime profiles</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">Per-agent execution profiles</h2>
            <p class="mt-1 text-sm text-muted">
               Profiles tune provider, model, thinking effort, memory availability/mode, and an extra prompt snippet.
               The memory backend remains agent-wide. Profile memory changes apply on the next logical turn and cannot loosen the frozen policy mid-turn.
            </p>
          </div>
          <Button type="button" variant="secondary" disabled={!canEditProfiles()} onclick={addRuntimeProfile}>Add profile</Button>
        </div>

        <div class="mt-4 grid gap-4 rounded-xl border border-control-border bg-surface p-4 sm:grid-cols-2">
          <label class="flex items-start gap-3 text-sm text-foreground">
            <input type="checkbox" bind:checked={form.autoProfileSelection} disabled={!canEditField('execution')} />
            <span>
              <span class="block font-medium">Automatically select a startup profile</span>
              <span class="mt-1 block text-xs text-muted">For new chats, match the first request to enabled, agent-selectable profiles. The agent receives the startup estimate and can switch profiles if needed. Existing conversations are unchanged.</span>
            </span>
          </label>
          <label class="block space-y-2 text-sm font-medium text-foreground">
            <span>Auto selection minimum confidence</span>
            <input
              type="number" min="0.5" max="1" step="0.01"
              bind:value={form.autoProfileMinConfidence}
              disabled={!canEditField('execution')}
              class="w-full rounded-xl border border-control-border bg-surface px-3 py-2 text-sm text-foreground"
            />
            <span class="block text-xs font-normal text-muted">Default: 0.55 (55%). Higher values prefer the configured default more often.</span>
          </label>
        </div>
        {#if form.agentProfiles.length > 0}
          <label class="mt-4 block space-y-2 text-sm font-medium text-foreground">
            <span>Default runtime profile</span>
            <select
              bind:value={form.defaultAgentProfileId}
              class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
              disabled={!canEditField('default_agent_profile_id')}
            >
              {#each form.agentProfiles as profile}
                {#if profile.profileId.trim()}
                  <option value={profile.profileId}>{profile.profileId}</option>
                {/if}
              {/each}
            </select>
            <span class="block text-xs text-muted">Used by Default, when Auto is disabled, or when Auto cannot confidently choose another profile.</span>
          </label>

          <div class="mt-5 space-y-4">
            {#each form.agentProfiles as profile, index (index)}
              <div class="rounded-2xl border border-separator bg-canvas/50 p-4">
                <div class="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                  <div>
                    <p class="text-sm font-semibold text-foreground">{profile.profileId || 'New profile'}</p>
                    <p class="mt-1 text-xs text-quiet">Canonical audit keeps <code>agent_id</code> and <code>agent_profile_id</code> as separate fields.</p>
                  </div>
                  <div class="flex items-center gap-3">
                    <label class="flex items-center gap-2 text-xs text-secondary">
                      <input
                        type="checkbox"
                        bind:checked={profile.enabled}
                        class="h-4 w-4 rounded border-control-border bg-canvas"
                        disabled={!canEditProfiles()}
                      />
                      Enabled
                    </label>
                    <label class="flex items-center gap-2 text-xs text-secondary">
                      <input
                        type="checkbox"
                        bind:checked={profile.agentSwitchable}
                        class="h-4 w-4 rounded border-control-border bg-canvas"
                        disabled={!canEditProfiles()}
                      />
                      Agent can switch to this profile
                    </label>
                    <Button type="button" variant="ghost" disabled={!canEditProfiles()} onclick={() => removeRuntimeProfile(profile.profileId)}>Remove</Button>
                  </div>
                </div>

                <div class="mt-4 grid gap-4 md:grid-cols-2">
                  <label class="flex min-h-11 items-center gap-2 text-sm text-foreground">
                    <input
                      type="checkbox"
                      bind:checked={profile.quickSwitch}
                      class="h-4 w-4 rounded border-control-border bg-canvas"
                      disabled={!canEditProfiles()}
                    />
                    Show in chat quick switcher
                  </label>
                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Quick-switch position</span>
                    <input
                      type="number"
                      min="0"
                      step="1"
                      bind:value={profile.quickSwitchIndex}
                      class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                      disabled={!canEditProfiles() || !profile.quickSwitch}
                    />
                    <span class="block text-xs text-muted">Lower positions appear first. The agent default is always available.</span>
                  </label>
                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Quick-switch color</span>
                    <select
                      bind:value={profile.quickSwitchColor}
                      disabled={!canEditProfiles()}
                      class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                    >
                      {#each ['neutral', 'blue', 'teal', 'amber', 'violet', 'rose'] as color}
                        <option value={color}>{color}</option>
                      {/each}
                    </select>
                  </label>
                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Profile ID</span>
                    <Input bind:value={profile.profileId} placeholder="fast" disabled={!canEditProfiles()} />
                    <span class="block text-xs text-muted">Agent-local ID used as <code>agent_profile_id</code>; do not include <code>/</code>.</span>
                  </label>

                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Memory availability</span>
                    <select
                      bind:value={profile.memoryAvailability}
                      class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                      disabled={!canEditProfiles() || form.memoryBackend === 'none'}
                    >
                      <option value="inherit">Inherit</option>
                      <option value="enabled">Enabled</option>
                      <option value="disabled">Disabled</option>
                    </select>
                    <span class="block text-xs text-muted">Effective: {form.memoryBackend === 'none' || profile.memoryAvailability === 'disabled' ? 'Disabled' : 'Enabled'}</span>
                  </label>

                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Memory mode</span>
                    <select
                      bind:value={profile.memoryMode}
                      class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                      disabled={!canEditProfiles() || form.memoryBackend === 'none' || profile.memoryAvailability === 'disabled'}
                    >
                      <option value="">Inherit agent setting ({selectedMemoryMode?.label ?? form.memoryMode})</option>
                      {#if profile.memoryMode && !selectedMemoryBackend?.modes.some((mode: MemoryModeDescriptor) => mode.id === profile.memoryMode)}
                        <option value={profile.memoryMode}>{profile.memoryMode}</option>
                      {/if}
                      {#each selectedMemoryBackend?.modes ?? [] as memoryMode}
                        <option value={memoryMode.id}>{memoryMode.label}</option>
                      {/each}
                    </select>
                    <span class="block text-xs text-muted">Backend is agent-wide; this override takes effect next logical turn.</span>
                  </label>

                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Description / routing guidance</span>
                    <Input bind:value={profile.description} placeholder="Low-latency responses for simple tasks" disabled={!canEditProfiles()} />
                    <span class="block text-xs text-muted">Used later by automatic routing to choose this profile.</span>
                  </label>

                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Provider</span>
                    <select
                      bind:value={profile.providerId}
                      onchange={() => {
                        if (profile.providerId === 'cognis' && !profile.model.startsWith('cognis/')) profile.model = '';
                        else if (profile.providerId !== 'cognis' && profile.model.startsWith('cognis/')) profile.model = '';
                      }}
                      class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                      disabled={!canEditProfiles()}
                    >
                      <option value="">Use agent/default provider</option>
                      <option value="cognis">Cognis (custom routes)</option>
                      {#each providers as provider}
                        <option value={provider.provider_id}>{provider.display_name}{provider.is_default ? ' ⭐' : ''}</option>
                      {/each}
                    </select>
                  </label>

                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Model</span>
                    {#if modelsForProvider(profile.providerId).length > 0}
                      <select
                        bind:value={profile.model}
                        class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                        disabled={!canEditProfiles()}
                      >
                        <option value="">Use provider/agent default</option>
                        {#each modelsForProvider(profile.providerId) as modelId}
                          <option value={modelId}>{modelId}</option>
                        {/each}
                      </select>
                    {:else}
                      <Input bind:value={profile.model} placeholder="Use provider/agent default" disabled={!canEditProfiles()} />
                    {/if}
                  </label>

                  <label class="space-y-2 text-sm font-medium text-foreground">
                    <span>Thinking effort</span>
                    <select
                      bind:value={profile.reasoningEffort}
                      aria-describedby={`${editorId}-profile-${profile.profileId}-thinking-help`}
                      aria-invalid={invalidThinking(profile.reasoningEffort, availableThinkingEffortsForProfile(profile))}
                      class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                      disabled={!canEditProfiles()}
                    >
                      <option value="">Inherit</option>
                      {#if invalidThinking(profile.reasoningEffort, availableThinkingEffortsForProfile(profile))}
                        <option value={profile.reasoningEffort}>{thinkingEffortLabel(profile.reasoningEffort)} (unavailable)</option>
                      {/if}
                      {#each availableThinkingEffortsForProfile(profile) as value}
                        <option value={value}>{thinkingEffortLabel(value)}</option>
                      {/each}
                    </select>
                    <span id={`${editorId}-profile-${profile.profileId}-thinking-help`} class="block text-xs text-muted">
                      Inherit uses the agent's thinking configuration. Provider default does not inherit an effort hint.
                      {#if invalidThinking(profile.reasoningEffort, availableThinkingEffortsForProfile(profile))}Select an available value before saving.{/if}
                    </span>
                  </label>
                    <label class="space-y-2 text-sm font-medium text-foreground">
                      <span>Fast mode</span>
                      <select
                        bind:value={profile.fastMode}
                        class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                        disabled={!canEditProfiles()}
                      >
                        <option value="inherit">Inherit agent setting</option>
                        <option value="enabled" disabled={!supportsFastModeForProfile(profile)}>Enabled{supportsFastModeForProfile(profile) ? '' : ' (unavailable)'}</option>
                        <option value="disabled">Disabled</option>
                      </select>
                      <span class="block text-xs text-muted">
                        {#if supportsFastModeForProfile(profile)}Account eligibility is not confirmed; premium pricing can apply.
                        {:else}Fast mode is unsupported or its capability metadata is unavailable.{/if}
                        Inherit uses the agent configuration. Disabled suppresses inherited acceleration.
                      </span>
                    </label>

                  <label class="space-y-2 text-sm font-medium text-foreground md:col-span-2">
                    <span>System prompt extra</span>
                    <textarea
                      bind:value={profile.systemPromptExtra}
                      class="min-h-28 w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground"
                      placeholder="Extra behavior tuning injected only when this profile is active."
                      disabled={!canEditProfiles()}
                    ></textarea>
                    <span class="block text-xs text-muted">This snippet tunes runtime behavior only; it must not redefine identity, memory scope, ownership, permissions, or tool rights.</span>
                  </label>
                </div>
              </div>
            {/each}
          </div>
        {:else}
          <div class="mt-4 rounded-2xl border border-separator bg-canvas/50 px-4 py-3 text-sm text-muted">
            No explicit runtime profiles. The agent will use the safe default derived from its base LLM configuration.
          </div>
        {/if}

        {#if errors.agentProfiles}
          <p class="mt-3 text-sm text-danger">{errors.agentProfiles}</p>
        {/if}
      </Card>
      {/if}
      </div>
      {/if}

      <!-- Secondary Agent Bindings (primary only) -->
      {#if form.agentType === 'primary'}
      <div hidden={activeSection !== 'workflows'} id={`${editorId}-panel-workflows`} role="tabpanel" aria-labelledby={`${editorId}-tab-workflows`} tabindex="0" class="space-y-5">
      {#if visitedSections.has('workflows')}
      {#if mode === 'edit' && !readonly && secondaryAgents.length > 0}
        <Card class="p-5">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-muted">Secondary agent bindings</p>
            <p class="mt-1 text-sm text-muted">Select which user-created secondary agents this primary agent can delegate to. System secondary agents are always available.</p>
          </div>
          <div class="mt-3 grid gap-2 md:grid-cols-2">
            {#each secondaryAgents.filter((a: Agent) => !a.is_system) as agent}
              <label class="flex items-center gap-3 rounded-xl border border-separator bg-canvas/60 px-3 py-2 text-sm text-foreground">
                <input
                  checked={localBindings.includes(agent.agent_id)}
                  class="h-4 w-4 rounded border-control-border bg-canvas"
                  type="checkbox"
                  onchange={(event) => {
                    event.currentTarget.checked = localBindings.includes(agent.agent_id);
                    toggleBinding(agent.agent_id);
                  }}
                  disabled={bindingsSaving || !onBindingsChange}
                />
                <span>{agent.display_name ?? agent.name}</span>
                <span class="text-xs text-muted">({agent.agent_id})</span>
              </label>
            {:else}
              <p class="text-sm text-quiet">No user-created secondary agents. System agents are always available.</p>
            {/each}
          </div>
        </Card>
      {/if}

      <!-- Workflow Settings (primary only) -->
      {#if form.agentType === 'primary'}
      <Card class="p-5">
        <div class="space-y-4">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-muted">Workflow settings</p>
            <h2 class="mt-1 text-lg font-semibold text-strong">Execution settings</h2>
          </div>

          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-foreground">
              <span>Default chat mode</span>
              <select bind:value={form.defaultChatMode} class="w-full rounded-xl border border-control-border bg-canvas/80 px-3 py-2 text-sm text-foreground" disabled={readonly}>
                <option value="default">default</option>
                <option value="plan">plan</option>
                <option value="build">build</option>
              </select>
              <span class="block text-xs font-normal text-quiet">Controls chat behavior before any conversation or one-shot slash override.</span>
            </label>
          </div>

          <details class="space-y-2">
          <summary class="cursor-pointer text-sm font-medium text-foreground">Advanced workflow overrides</summary>
          <label class="block space-y-2 text-sm font-medium text-foreground">
            <span>Step agent overrides (JSON)</span>
            <textarea aria-invalid={errors.stepAgentOverridesJson ? 'true' : 'false'} bind:value={form.stepAgentOverridesJson} class="min-h-[150px] w-full rounded-2xl border border-control-border bg-canvas/80 px-4 py-3 font-mono text-sm text-foreground placeholder:text-quiet" disabled={readonly}></textarea>
            {#if errors.stepAgentOverridesJson}
              <span class="text-xs text-danger">{errors.stepAgentOverridesJson}</span>
            {/if}
          </label>
          </details>
        </div>
      </Card>
      {/if}

      {/if}
      </div>
      {/if}
    </div>
  </div>
</form>
  {#each extraSections as section (section.id)}
    <div hidden={activeSection !== section.id} id={`${editorId}-panel-${section.id}`} role="tabpanel" aria-labelledby={`${editorId}-tab-${section.id}`} tabindex="0" class="space-y-5">
      {#if visitedSections.has(section.id)}
        {@render section.content()}
      {/if}
    </div>
  {/each}
      {#if error}
        <p role="alert" class="rounded-2xl border border-danger-border bg-danger-subtle px-4 py-3 text-sm text-danger">
          {error}
        </p>
      {/if}

      {#if !readonly || isSystemAsset}
        <div class="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-separator bg-canvas/95 p-4 backdrop-blur">
          <div class="text-xs text-muted">
            <p>{dirty ? 'Unsaved configuration changes' : 'Configuration saved'}</p>
            {#if routeMetadataIssue}
              <p role="status" class="mt-1 text-warning">{routeMetadataIssue}</p>
            {/if}
            {#if routeLoadState === 'error' || (routeLoadState === 'loaded' && routeMetadataIssue)}
              <button type="button" class="mt-1 text-accent underline" onclick={loadInferenceRoutes}>
                Retry route discovery
              </button>
            {/if}
            {#each Object.entries(errors) as [field, message]}
              <button type="button" class="mt-1 block text-left text-danger underline"
                 onclick={() => { activeSection = field === 'name' ? 'identity' : field === 'reasoningEffort' ? 'providers' : field === 'agentProfiles' ? 'profiles' : 'workflows'; }}>
                {message}
              </button>
            {/each}
          </div>
          <Button form={`${editorId}-form`} type="submit" disabled={saving || !canSubmit || !dirty}>{saving ? 'Saving…' : mode === 'create' ? 'Create agent' : isSystemAsset ? 'Save overrides' : 'Save changes'}</Button>
        </div>
      {/if}
</div>

{#if showAvatarModal}
  <AvatarGenerateModal
    name={form.name}
    description={form.description}
    personality={{ tone: form.tone, temperament: form.temperament, purpose: form.purpose }}
    onAccept={handleAvatarAccept}
    onClose={() => { showAvatarModal = false; }}
  />
{/if}

{#if showAvatarLightbox && form.avatarUrl}
  <ImageLightbox src={form.avatarUrl} alt="{form.name} avatar" onClose={() => { showAvatarLightbox = false; }} />
{/if}
