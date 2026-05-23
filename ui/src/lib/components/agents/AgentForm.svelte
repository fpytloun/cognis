<script lang="ts">
  import Sparkles from 'lucide-svelte/icons/sparkles';
import Loader2 from 'lucide-svelte/icons/loader-2';
  import Eye from 'lucide-svelte/icons/eye';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import AvatarGenerateModal from '$lib/components/agents/AvatarGenerateModal.svelte';
  import SkillDetailSheet from '$lib/components/skills/SkillDetailSheet.svelte';
  import ImageLightbox from '$lib/components/ImageLightbox.svelte';
  import { api } from '$lib/api/client';
  import { GENERIC_THINKING_EFFORTS, thinkingEffortLabel } from '$lib/thinking';
  import {
    buildSystemPromptPreview,
    defaultSystemPrompt,
    formStateToPayload,
    formStateToSystemOverridePayload,
    slugify,
    type AdditionalExecutorEntry,
    type AgentFormState
  } from '$lib/agents';
  import type { Agent, CredentialMetadata, EffectiveToolItem, ExecutorConfig, IntarisMCPServer, KnowledgebaseModel, LLMProvider, ModelEntry, SecretMetadata, Skill, ToolDefinitionSummary, Workflow } from '$lib/types/api';

  type AgentToolOption = (ToolDefinitionSummary & { tool_id?: string; permission?: string }) | EffectiveToolItem;

  let {
    mode,
    form,
    tools,
    workflows,
    providers,
    executors = [],
    secrets = [],
    credentials = [],
    knowledgebases = [],
    skills = [],
    intarisMcpServers = [],
    secondaryAgents = [],
    secondaryBindings = [],
    saving = false,
    error = '',
    readonly = false,
    isSystemAsset = false,
    editableFields = [],
    onSave,
    onBindingsChange,
  } = $props<{
    mode: 'create' | 'edit';
    form: AgentFormState;
    tools: AgentToolOption[];
    workflows: Workflow[];
    providers: LLMProvider[];
    executors?: ExecutorConfig[];
    secrets?: SecretMetadata[];
    credentials?: CredentialMetadata[];
    knowledgebases?: KnowledgebaseModel[];
    skills?: Skill[];
    intarisMcpServers?: IntarisMCPServer[];
    secondaryAgents?: Agent[];
    secondaryBindings?: string[];
    saving?: boolean;
    error?: string;
    readonly?: boolean;
    isSystemAsset?: boolean;
    editableFields?: string[];
    onSave: (payload: Record<string, unknown>) => void | Promise<void>;
    onBindingsChange?: ((bindings: string[]) => void | Promise<void>) | null;
  }>();

  let localBindings = $state<string[]>([]);
  let showAvatarModal = $state(false);
  let showAvatarLightbox = $state(false);
  let skillDetailId = $state<string | null>(null);
  let uploadingAvatar = $state(false);
  let fileInput: HTMLInputElement | undefined = $state();
  const editableFieldSet = $derived(new Set(editableFields));
  const selectedSkillDetail = $derived(skills.find((skill: Skill) => skill.skill_id === skillDetailId) ?? null);

  $effect(() => {
    localBindings = [...secondaryBindings];
  });

  function canEditField(field: string): boolean {
    if (!isSystemAsset) return !readonly;
    return editableFieldSet.has(field);
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
    if (localBindings.includes(agentId)) {
      localBindings = localBindings.filter((id) => id !== agentId);
    } else {
      localBindings = [...localBindings, agentId];
    }
    onBindingsChange?.(localBindings);
  }

  const permissionOptions = ['', 'allow', 'evaluate', 'deny'];

  const toolCategories = $derived<string[]>(
    [...new Set(tools.map((tool: AgentToolOption) => tool.category))].sort() as string[]
  );

  function toolsForCategory(category: string): AgentToolOption[] {
    return tools.filter((tool: AgentToolOption) => tool.category === category);
  }

  function toolKey(tool: AgentToolOption): string {
    return tool.tool_id ?? tool.name;
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
    const stepJsonError = validateJson(form.stepAgentOverridesJson, 'Step agent overrides');
    if (stepJsonError) {
      errors.stepAgentOverridesJson = stepJsonError;
    }
    return errors;
  }

  const errors = $derived(validationErrors());
  const canSubmit = $derived(Object.keys(errors).length === 0 && Boolean(form.name.trim()));

  // Keep agentId in sync with name until user manually edits the ID field
  $effect(() => {
    if (!form.customId && mode === 'create') {
      form.agentId = slugify(form.name);
    }
  });

  function toggleWorkflow(workflowId: string): void {
    if (form.availableWorkflowIds.includes(workflowId)) {
      form.availableWorkflowIds = form.availableWorkflowIds.filter((value: string) => value !== workflowId);
      if (form.defaultWorkflowId === workflowId) {
        form.defaultWorkflowId = '';
      }
      return;
    }
    form.availableWorkflowIds = [...form.availableWorkflowIds, workflowId];
  }

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
    } else {
      form.selectedSkillIds = [...form.selectedSkillIds, skillId];
    }
  }

  function attachedToAllAgents(skill: Skill): boolean {
    return Boolean(skill.attach_to_all_agents ?? skill.auto_load);
  }

  const globallyAttachedSkills = $derived(skills.filter((s: Skill) => attachedToAllAgents(s) && !form.selectedSkillIds.includes(s.skill_id)));
  const selectableSkills = $derived(skills.filter((s: Skill) => !attachedToAllAgents(s)));
  const optionalBuiltinTools = [{ id: 'manage_agents', label: 'Manage agents', description: 'Create, edit, archive, bind, and share your other agents from chat.' }];

  function toggleOptInBuiltinTool(toolId: string): void {
    if (form.optInBuiltinTools.includes(toolId)) {
      form.optInBuiltinTools = form.optInBuiltinTools.filter((value: string) => value !== toolId);
    } else {
      form.optInBuiltinTools = [...form.optInBuiltinTools, toolId];
    }
  }

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    await onSave(isSystemAsset ? formStateToSystemOverridePayload(form) : formStateToPayload(form));
  }

  function resetSystemPrompt(): void {
    form.systemPrompt = defaultSystemPrompt(form.name);
  }

  /** Get models configured on the currently selected provider */
  function selectedProviderModels(): string[] {
    if (!form.providerId) {
      return [];
    }
    const provider = providers.find((p: LLMProvider) => p.provider_id === form.providerId);
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

  function selectedProviderModelInfo(): ModelEntry | null {
    if (!form.providerId) {
      return null;
    }
    const provider = providers.find((p: LLMProvider) => p.provider_id === form.providerId);
    if (!provider) {
      return null;
    }
    const explicitModel = form.model.trim();
    const defaultModel = typeof provider.config?.default_model === 'string' ? provider.config.default_model : '';
    const resolvedModel = explicitModel || defaultModel;
    if (!resolvedModel) {
      return null;
    }
    return provider.models.find((model: ModelEntry) => model.model_id === resolvedModel) ?? null;
  }

  function availableThinkingEfforts(): string[] {
    const modelInfo = selectedProviderModelInfo();
    if (modelInfo) {
      return modelInfo.reasoning_efforts.length > 0 ? modelInfo.reasoning_efforts : ['default'];
    }
    return [...GENERIC_THINKING_EFFORTS];
  }
</script>

<form class="space-y-5" onsubmit={handleSubmit}>
  <div class="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
    <div class="space-y-5">
      <!-- Identity -->
      <Card class="p-5">
        <p class="mb-3 text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Identity</p>
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Name <span class="text-rose-300">*</span></span>
            <Input aria-invalid={errors.name ? 'true' : 'false'} bind:value={form.name} placeholder="Research Assistant" disabled={readonly} />
            {#if errors.name}
              <span class="text-xs text-rose-300">{errors.name}</span>
            {/if}
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>ID <span class="text-slate-500">(optional)</span></span>
            <Input
              bind:value={form.agentId}
              disabled={mode === 'edit' || readonly}
              placeholder="auto-generated from name"
              oninput={() => { if (mode === 'create') form.customId = true; }}
            />
            {#if mode === 'create' && !form.customId}
              <span class="text-xs text-slate-400">Auto-generated from name. Type here to override.</span>
            {:else if mode === 'create' && form.customId}
              <button type="button" class="text-xs text-sky-400 hover:text-sky-300" onclick={() => { form.customId = false; form.agentId = slugify(form.name); }}>Reset to auto</button>
            {/if}
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Type</span>
            <select bind:value={form.agentType} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={mode === 'edit' || readonly}>
              <option value="primary">Primary</option>
              <option value="secondary">Secondary</option>
            </select>
            <span class="block text-xs text-slate-400">
              {form.agentType === 'primary' ? 'Interactive agent with personality and memory.' : 'Lightweight task executor for focused sub-tasks.'}
            </span>
          </label>
          <div class="space-y-2 text-sm font-medium text-slate-200">
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
                      class="rounded-lg border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-700 hover:text-slate-100 disabled:opacity-50"
                      onclick={() => fileInput?.click()}
                      disabled={uploadingAvatar}
                    >
                      {uploadingAvatar ? 'Uploading...' : 'Upload'}
                    </button>
                    <button
                      type="button"
                      class="rounded-lg border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs text-sky-400 hover:bg-slate-700 hover:text-sky-300"
                      onclick={() => { showAvatarModal = true; }}
                    >
                      Generate
                    </button>
                    {#if form.avatarImageId || form.avatarUrl}
                      <button
                        type="button"
                        class="rounded-lg border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-xs text-rose-400 hover:bg-slate-700 hover:text-rose-300"
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
        <div class="mt-4 space-y-2 text-sm font-medium text-slate-200">
          <div class="flex items-center justify-between">
            <span>Description</span>
            {#if !readonly}
              <button
                type="button"
                class="flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300 disabled:opacity-50"
                disabled={generatingField === 'description'}
                onclick={() => generateField('description', () => form.description, (v) => { form.description = v; })}
                title="Generate with AI"
              >
                {#if generatingField === 'description'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
              </button>
            {/if}
          </div>
          <textarea bind:value={form.description} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" disabled={readonly}></textarea>
        </div>
      </Card>

      <!-- Personality (primary only) -->
      {#if form.agentType === 'primary'}
        <Card class="p-5">
          <p class="mb-3 text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Personality</p>
          <div class="grid gap-4 md:grid-cols-3">
            <div class="space-y-2 text-sm font-medium text-slate-200">
              <div class="flex items-center justify-between">
                <span>Tone</span>
                {#if !readonly && !isSystemAsset}
                  <button type="button" class="text-sky-400 hover:text-sky-300 disabled:opacity-50" disabled={generatingField === 'tone'} onclick={() => generateField('tone', () => form.tone, (v) => { form.tone = v; })} title="Generate with AI">
                    {#if generatingField === 'tone'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                  </button>
                {/if}
              </div>
              <Input bind:value={form.tone} placeholder="e.g. formal, friendly, witty" disabled={readonly} />
              <span class="block text-xs text-slate-500">How the agent communicates — voice and style.</span>
            </div>
            <div class="space-y-2 text-sm font-medium text-slate-200">
              <div class="flex items-center justify-between">
                <span>Temperament</span>
                {#if !readonly && !isSystemAsset}
                  <button type="button" class="text-sky-400 hover:text-sky-300 disabled:opacity-50" disabled={generatingField === 'temperament'} onclick={() => generateField('temperament', () => form.temperament, (v) => { form.temperament = v; })} title="Generate with AI">
                    {#if generatingField === 'temperament'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                  </button>
                {/if}
              </div>
              <Input bind:value={form.temperament} placeholder="e.g. patient, cautious, bold" disabled={readonly} />
              <span class="block text-xs text-slate-500">How the agent behaves — disposition and reactions.</span>
            </div>
            <div class="space-y-2 text-sm font-medium text-slate-200">
              <div class="flex items-center justify-between">
                <span>Purpose</span>
                {#if !readonly && !isSystemAsset}
                  <button type="button" class="text-sky-400 hover:text-sky-300 disabled:opacity-50" disabled={generatingField === 'purpose'} onclick={() => generateField('purpose', () => form.purpose, (v) => { form.purpose = v; })} title="Generate with AI">
                    {#if generatingField === 'purpose'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                  </button>
                {/if}
              </div>
              <Input bind:value={form.purpose} placeholder="research specialist" disabled={readonly} />
            </div>
          </div>
          <div class="mt-4 space-y-2 text-sm font-medium text-slate-200">
            <div class="flex items-center justify-between">
              <span>Behavioral rules (one per line)</span>
              {#if !readonly && !isSystemAsset}
                <button type="button" class="flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300 disabled:opacity-50" disabled={generatingField === 'behavioral_rules'} onclick={() => generateField('behavioral_rules', () => form.behavioralRules, (v) => { form.behavioralRules = v; })} title="Generate with AI">
                  {#if generatingField === 'behavioral_rules'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                </button>
              {/if}
            </div>
            <textarea bind:value={form.behavioralRules} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="Always cite sources&#10;Prefer concise answers" disabled={readonly}></textarea>
          </div>
        </Card>
      {/if}

      <!-- Editable identity instructions -->
      <Card class="p-5">
        <div class="mb-3 flex items-center justify-between gap-3">
          <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Agent instructions</p>
          {#if !readonly && !isSystemAsset}
            <Button size="sm" variant="secondary" type="button" onclick={resetSystemPrompt}>Reset to default</Button>
          {:else if isSystemAsset}
            <span class="text-xs text-slate-500">Managed by the shipped system agent definition.</span>
          {:else}
            <span class="text-xs text-slate-500">Read-only because this agent is shared with you.</span>
          {/if}
        </div>
        <textarea bind:value={form.systemPrompt} class="min-h-[180px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="You are {'{'}name{'}'}, a capable general-purpose AI assistant.&#10;&#10;Optimize for correctness, usefulness, and actionability over verbosity." disabled={readonly}></textarea>
        <p class="mt-2 text-xs text-slate-400">Defines this agent's identity, communication style, and domain-specific behavior. Cognis runtime rules, tool policy, memory context, workflow instructions, and safety constraints are injected separately and cannot be overridden here.</p>
      </Card>

      <!-- Tools & Permissions -->
      <Card class="p-5">
        <div class="mb-4 grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Executor</span>
            <select bind:value={form.executorId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={readonly}>
              <option value="">Use default executor</option>
              {#each executors as executor}
                <option value={executor.executor_id}>{executor.name} ({executor.executor_type})</option>
              {/each}
            </select>
            <span class="block text-xs text-slate-400">Choose a specific executor or leave empty to use executor label matching / default resolution.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Executor selector (optional, key=value)</span>
            <textarea bind:value={form.executorSelector} class="min-h-[72px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100" placeholder="tier=standard&#10;location=local" disabled={readonly || !!form.executorId}></textarea>
            <span class="block text-xs text-slate-400">Used when no explicit executor is selected. Matches executor labels like Kubernetes selectors.</span>
          </label>
        </div>

        <!-- Stage 36: Additional executors (multi-executor agents) -->
        <div class="mb-4 rounded-2xl border border-slate-700 bg-slate-950/40 p-4">
          <div class="mb-2 flex items-center justify-between">
            <div>
              <p class="text-sm font-medium text-slate-200">Additional executors (optional)</p>
              <p class="mt-1 text-xs text-slate-400">
                Extra executors the agent can target via <code class="text-slate-300">target_executor</code>
                or <code class="text-slate-300">switch_executor</code>. Never auto-selected by the controller.
              </p>
            </div>
            <button
              type="button"
              class="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1 text-xs text-slate-200 hover:bg-slate-800"
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
            <p class="text-xs text-slate-500">No additional executors configured.</p>
          {/if}
          {#each form.additionalExecutors as additional, idx (idx)}
            <div class="mt-3 grid gap-3 rounded-xl border border-slate-800 bg-slate-950/60 p-3 md:grid-cols-2">
              <label class="space-y-1 text-xs font-medium text-slate-200">
                <span>Executor</span>
                <select
                  bind:value={additional.executorId}
                  class="w-full rounded-lg border border-slate-700 bg-slate-950/80 px-2 py-1 text-sm text-slate-100"
                  disabled={readonly || !!additional.executorSelector.trim()}
                >
                  <option value="">— or use selector below —</option>
                  {#each executors as executor}
                    <option value={executor.executor_id}>{executor.name} ({executor.executor_type})</option>
                  {/each}
                </select>
              </label>
              <label class="space-y-1 text-xs font-medium text-slate-200">
                <span>Selector (key=value, one per line)</span>
                <textarea
                  bind:value={additional.executorSelector}
                  class="min-h-[60px] w-full rounded-lg border border-slate-700 bg-slate-950/80 px-2 py-1 font-mono text-xs text-slate-100"
                  placeholder="role=browser&#10;loc=local"
                  disabled={readonly || !!additional.executorId.trim()}
                ></textarea>
              </label>
              <label class="space-y-1 text-xs font-medium text-slate-200 md:col-span-2">
                <span>Description (optional)</span>
                <input
                  bind:value={additional.description}
                  class="w-full rounded-lg border border-slate-700 bg-slate-950/80 px-2 py-1 text-sm text-slate-100"
                  placeholder="e.g. personal Mac for coding"
                  disabled={readonly}
                />
              </label>
              <div class="md:col-span-2">
                <button
                  type="button"
                  class="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-1 text-xs text-rose-200 hover:bg-rose-500/20"
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

        <div class="grid gap-4 md:grid-cols-2">
          <label class="flex items-center gap-3 text-sm font-medium text-slate-200">
            <input bind:checked={form.canDelegate} class="h-4 w-4 rounded border-slate-600 bg-slate-950" type="checkbox" disabled={readonly} />
            Can delegate
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Max delegation depth</span>
            <Input bind:value={form.maxDelegationDepth} type="number" disabled={readonly} />
          </label>
        </div>

        <div class="mt-4 rounded-2xl border border-amber-500/20 bg-amber-500/10 p-4">
          <p class="text-sm font-medium text-amber-100">Optional built-in tools</p>
          <p class="mt-1 text-xs text-amber-100/70">These privileged controller tools are off by default and are not enabled by wildcard tool access.</p>
          <div class="mt-3 space-y-2">
            {#each optionalBuiltinTools as tool}
              <label class="flex items-start gap-3 text-sm text-slate-200">
                <input
                  checked={form.optInBuiltinTools.includes(tool.id)}
                  class="mt-0.5 h-4 w-4 rounded border-slate-600 bg-slate-950"
                  type="checkbox"
                  onchange={() => toggleOptInBuiltinTool(tool.id)}
                  disabled={readonly || form.agentType === 'secondary'}
                />
                <span>
                  <span class="font-medium">{tool.label}</span>
                  <span class="block text-xs text-slate-400">{tool.description}</span>
                </span>
              </label>
            {/each}
          </div>
        </div>

        {#if tools.length > 0}
          <div class="mt-4 space-y-3 rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
            <div>
              <p class="text-sm font-medium text-slate-200">Tool categories</p>
              <p class="mt-1 text-xs text-slate-400">Agents inherit all tools from their executor by default. Disable categories or individual tools here, then use permissions to require evaluation or deny access.</p>
            </div>

            <div class="flex flex-wrap gap-2">
              {#each toolCategories as category}
                {@const disabled = categoryDisabled(category)}
                <button
                  type="button"
                  class="px-3 py-1.5 rounded-lg text-sm border transition-colors {disabled ? 'bg-slate-900 border-slate-700 text-slate-400' : 'bg-emerald-500/15 border-emerald-500/40 text-emerald-200'}"
                  onclick={() => toggleCategory(category)}
                  disabled={readonly}
                >
                  {category}
                  <span class="ml-1 text-xs opacity-60">({toolsForCategory(category).length})</span>
                </button>
              {/each}
            </div>

            <!-- Scroll-within-scroll is painful on touch. On mobile let the
                 whole page scroll through this list; on desktop cap it. -->
            <div class="space-y-2 md:max-h-80 md:overflow-y-auto">
              {#each toolCategories as category}
                {@const categoryTools = toolsForCategory(category)}
                <details class="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
                  <summary class="cursor-pointer text-sm font-medium text-slate-200">
                    {category}
                    <span class="ml-2 text-xs text-slate-500">{categoryDisabled(category) ? 'disabled' : 'enabled'}</span>
                  </summary>
                  <div class="mt-3 space-y-2">
                    {#each categoryTools as tool}
                      <div class="grid gap-2 md:grid-cols-[1fr_auto_auto] items-center text-sm">
                        <label class="flex items-center gap-3 text-slate-200">
                          <input type="checkbox" checked={!toolDisabled(toolKey(tool))} onchange={() => toggleTool(toolKey(tool))} disabled={readonly || categoryDisabled(category)} class="h-4 w-4 rounded border-slate-600 bg-slate-950" />
                          <span class="font-mono">{tool.source?.type === 'skill' && tool.source?.raw_tool_name ? tool.source.raw_tool_name : tool.name}</span>
                        </label>
                        <span class="text-xs text-slate-500">
                          {tool.description}
                          {#if (tool as EffectiveToolItem).available_on && ((tool as EffectiveToolItem).available_on?.length ?? 0) > 1}
                            <span class="ml-2 rounded-full bg-slate-800 px-2 py-0.5 font-mono text-[10px] text-slate-300" title="Stage 36: this tool is available on more than one assigned executor. Use target_executor on the tool call to pick.">
                              on: {((tool as EffectiveToolItem).available_on ?? []).join(', ')}
                            </span>
                          {/if}
                        </span>
                        <select bind:value={form.toolPermissions[toolKey(tool)]} class="w-32 rounded-lg border border-slate-700 bg-slate-950/80 px-2 py-1 text-xs text-slate-100" disabled={readonly || toolDisabled(toolKey(tool)) || categoryDisabled(category)}>
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
          </div>
        {/if}

        {#if credentials.length > 0}
          <div class="mt-4">
            <p class="mb-2 text-sm font-medium text-slate-200">Allowed credentials</p>
            <div class="grid gap-2 md:grid-cols-2">
              {#each credentials as credential}
                <label class="flex items-center gap-2 text-sm text-slate-200">
                  <input
                    checked={form.allowedCredentials.includes(credential.credential_id)}
                    class="h-4 w-4 rounded border-slate-600 bg-slate-950"
                    type="checkbox"
                    onchange={() => toggleCredential(credential.credential_id)}
                    disabled={readonly}
                  />
                  {credential.label}
                  <span class="text-xs text-slate-400">({credential.kind})</span>
                </label>
              {/each}
            </div>
          </div>
        {/if}

        {#if knowledgebases.length > 0}
          <div class="mt-4">
            <p class="mb-2 text-sm font-medium text-slate-200">Allowed knowledgebases</p>
            <div class="grid gap-2 md:grid-cols-2">
              {#each knowledgebases as kb}
                <label class="flex items-start gap-2 text-sm text-slate-200">
                  <input
                    checked={form.allowedKnowledgebases.includes(kb.knowledgebase_id)}
                    class="mt-0.5 h-4 w-4 rounded border-slate-600 bg-slate-950"
                    type="checkbox"
                    onchange={() => toggleKnowledgebase(kb.knowledgebase_id)}
                    disabled={readonly}
                  />
                  <span>
                    <span>{kb.name}</span>
                    <span class="block font-mono text-xs text-slate-500">{kb.knowledgebase_id}</span>
                  </span>
                </label>
              {/each}
            </div>
          </div>
        {/if}

        <!-- Allowed secrets -->
        {#if secrets.length > 0}
          <div class="mt-4">
            <p class="mb-2 text-sm font-medium text-slate-200">Allowed secrets</p>
            <div class="grid gap-2 md:grid-cols-2">
              {#each secrets as secret}
                <label class="flex items-center gap-2 text-sm text-slate-200">
                  <input
                    checked={form.allowedSecrets.includes(secret.name)}
                    class="h-4 w-4 rounded border-slate-600 bg-slate-950"
                    type="checkbox"
                    onchange={() => toggleSecret(secret.name)}
                    disabled={readonly}
                  />
                  {secret.name}
                  <span class="text-xs text-slate-400">({secret.scope})</span>
                </label>
              {/each}
            </div>
          </div>
        {/if}

        <!-- Skills -->
        {#if skills.length > 0}
          <div class="mt-4 space-y-3">
            <p class="text-sm font-medium text-slate-200">Skills</p>
            <p class="text-xs text-slate-400">Select skills to attach to this agent. Skills attached to all agents are always available.</p>
            {#if selectableSkills.length > 0}
              <div class="grid gap-2 md:grid-cols-2">
                {#each selectableSkills as skill}
                  <div class="flex items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-950/70 px-3 py-2 text-sm text-slate-200">
                    <label class="flex min-w-0 items-center gap-2">
                      <input
                        checked={form.selectedSkillIds.includes(skill.skill_id)}
                        class="h-4 w-4 rounded border-slate-600 bg-slate-950"
                        type="checkbox"
                        onchange={() => toggleSkill(skill.skill_id)}
                        disabled={!canEditField('skills')}
                      />
                      <span class="truncate">{skill.name}</span>
                      {#if skill.current_version?.tools && skill.current_version.tools.length > 0}
                        <span class="text-xs text-slate-500">({skill.current_version.tools.length} tools)</span>
                      {/if}
                      {#if skill.current_version?.asset_manifest && skill.current_version.asset_manifest.length > 0}
                        <span class="text-xs text-slate-500">({skill.current_version.asset_manifest.length} assets)</span>
                      {/if}
                    </label>
                    <button type="button" class="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-slate-200" onclick={() => { skillDetailId = skill.skill_id; }}>
                      <Eye class="h-3.5 w-3.5" /> View
                    </button>
                  </div>
                {/each}
              </div>
            {/if}
            {#if globallyAttachedSkills.length > 0}
              <div class="mt-2">
                <p class="mb-1 text-xs text-slate-500">Attached to all agents:</p>
                <div class="flex flex-wrap gap-1">
                  {#each globallyAttachedSkills as skill}
                    <span class="rounded bg-slate-700 px-2 py-0.5 text-xs text-slate-300">{skill.name}</span>
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
            <p class="text-sm font-medium text-slate-200">Intaris MCP servers</p>
            <p class="text-xs text-slate-400">Remote MCP servers managed by Intaris. Select which servers this agent can use.</p>
            <div class="flex flex-wrap gap-2">
              {#each intarisMcpServers as server}
                {@const selected = (form.intarisMcpServers || []).includes(server.name)}
                <button
                  type="button"
                  class="px-3 py-1.5 rounded-lg text-sm border transition-colors {selected ? 'bg-sky-500/20 border-sky-500/50 text-sky-300' : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-600'}"
                  disabled={readonly}
                  onclick={() => {
                    if (readonly) return;
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
          <div class="mt-4 rounded-2xl border border-sky-500/30 bg-sky-500/10 px-4 py-3 text-sm text-sky-100">
            <p class="font-medium">This agent still has legacy inline MCP server definitions.</p>
            <p class="mt-1 text-sky-50/90">Create these servers in Settings → Tools and assign them to an executor. Existing inline MCP config is preserved for backward compatibility.</p>
          </div>
        {/if}
      </Card>

      <!-- Provider & Model -->
      <Card class="p-5">
        <p class="mb-3 text-xs font-medium uppercase tracking-[0.25em] text-slate-400">LLM Configuration</p>
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Provider</span>
            <select bind:value={form.providerId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!canEditField('llm_config.provider_id')}>
              <option value="">Use default provider</option>
              {#each providers as provider}
                <option value={provider.provider_id}>{provider.display_name}{provider.is_default ? ' ⭐' : ''}</option>
              {/each}
            </select>
            <span class="block text-xs text-slate-400">Leave empty to use the system default.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Model</span>
            {#if selectedProviderModels().length > 0}
               <select bind:value={form.model} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!canEditField('llm_config.model')}>
                <option value="">Use provider default</option>
                {#each selectedProviderModels() as modelId}
                  <option value={modelId}>{modelId}</option>
                {/each}
              </select>
            {:else}
               <Input bind:value={form.model} placeholder="Use provider default" disabled={!canEditField('llm_config.model')} />
            {/if}
            <span class="block text-xs text-slate-400">Leave empty to use the provider's default model.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Temperature</span>
            <Input bind:value={form.temperature} type="number" placeholder="default" disabled={!canEditField('llm_config.temperature')} />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Max tokens</span>
            <Input bind:value={form.maxTokens} type="number" placeholder="default" disabled={!canEditField('llm_config.max_tokens')} />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Thinking effort</span>
            <select bind:value={form.reasoningEffort} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={!canEditField('llm_config.reasoning_effort')}>
              <option value="">Default</option>
              {#each availableThinkingEfforts().filter((value: string) => value !== 'default') as value}
                <option value={value}>{thinkingEffortLabel(value)}</option>
              {/each}
            </select>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Voice</span>
            <Input bind:value={form.voice} placeholder="Use system default" disabled={!canEditField('llm_config.voice')} />
            <span class="block text-xs text-slate-400">TTS voice for the speaker button and conversation mode (e.g. <code>alloy</code>, <code>nova</code>, an ElevenLabs voice ID). Leave empty to use the system default.</span>
          </label>
        </div>
      </Card>

      <!-- Secondary Agent Bindings (primary only) -->
      {#if form.agentType === 'primary' && !readonly && secondaryAgents.length > 0}
        <Card class="p-5">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Secondary agent bindings</p>
            <p class="mt-1 text-sm text-slate-400">Select which user-created secondary agents this primary agent can delegate to. System secondary agents are always available.</p>
          </div>
          <div class="mt-3 grid gap-2 md:grid-cols-2">
            {#each secondaryAgents.filter((a: Agent) => !a.is_system) as agent}
              <label class="flex items-center gap-3 rounded-xl border border-slate-800 bg-slate-950/60 px-3 py-2 text-sm text-slate-200">
                <input
                  checked={localBindings.includes(agent.agent_id)}
                  class="h-4 w-4 rounded border-slate-600 bg-slate-950"
                  type="checkbox"
                  onchange={() => toggleBinding(agent.agent_id)}
                />
                <span>{agent.display_name ?? agent.name}</span>
                <span class="text-xs text-slate-400">({agent.agent_id})</span>
              </label>
            {:else}
              <p class="text-sm text-slate-500">No user-created secondary agents. System agents are always available.</p>
            {/each}
          </div>
        </Card>
      {/if}

      <!-- Workflow Settings (primary only) -->
      {#if form.agentType === 'primary'}
      <Card class="p-5">
        <div class="space-y-4">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Workflow settings</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Available workflows</h2>
          </div>

          <div class="grid gap-3 md:grid-cols-2">
            {#each workflows as workflow}
              <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-950/60 px-4 py-3 text-sm text-slate-200">
                <input checked={form.availableWorkflowIds.includes(workflow.workflow_id)} class="mt-1 h-4 w-4 rounded border-slate-600 bg-slate-950" type="checkbox" onchange={() => toggleWorkflow(workflow.workflow_id)} disabled={readonly} />
                <span>
                  <span class="block font-medium text-white">{workflow.name}</span>
                  <span class="mt-1 block text-xs text-slate-400">{workflow.workflow_id}</span>
                </span>
              </label>
            {/each}
          </div>

          <div class="grid gap-4 md:grid-cols-2">
            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Default workflow</span>
              <select bind:value={form.defaultWorkflowId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={readonly}>
                <option value="">Automatic (decision engine)</option>
                {#each workflows as workflow}
                  {#if form.availableWorkflowIds.includes(workflow.workflow_id)}
                    <option value={workflow.workflow_id}>{workflow.name}</option>
                  {/if}
                {/each}
              </select>
            </label>

            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Selection mode</span>
              <select bind:value={form.workflowSelectionMode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={readonly}>
                <option value="automatic">automatic</option>
                <option value="always_ask">always_ask</option>
                <option value="use_default">use_default</option>
              </select>
            </label>

            <label class="space-y-2 text-sm font-medium text-slate-200">
              <span>Default chat mode</span>
              <select bind:value={form.defaultChatMode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100" disabled={readonly}>
                <option value="default">default</option>
                <option value="plan">plan</option>
                <option value="build">build</option>
              </select>
              <span class="block text-xs font-normal text-slate-500">Controls chat behavior before any conversation or one-shot slash override.</span>
            </label>
          </div>

          <label class="block space-y-2 text-sm font-medium text-slate-200">
            <span>Step agent overrides (JSON)</span>
            <textarea aria-invalid={errors.stepAgentOverridesJson ? 'true' : 'false'} bind:value={form.stepAgentOverridesJson} class="min-h-[150px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100 placeholder:text-slate-500" disabled={readonly}></textarea>
            {#if errors.stepAgentOverridesJson}
              <span class="text-xs text-rose-300">{errors.stepAgentOverridesJson}</span>
            {/if}
          </label>
        </div>
      </Card>
      {/if}

      {#if error}
        <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">
          {error}
        </p>
      {/if}

      {#if !readonly || isSystemAsset}
        <div class="flex justify-end gap-3">
          <Button type="submit" disabled={saving || !canSubmit}>{saving ? 'Saving…' : mode === 'create' ? 'Create agent' : isSystemAsset ? 'Save overrides' : 'Save changes'}</Button>
        </div>
      {/if}
    </div>

    <div class="space-y-5">
      {#if form.agentType === 'primary'}
        <Card class="p-5">
          <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Editable identity preview</p>
          <p class="mt-2 text-sm text-slate-300">This preview shows only the editable identity block. Runtime instructions, memory, tools, skills, and workflow context are added separately.</p>
          <pre class="mt-4 whitespace-pre-wrap rounded-2xl border border-slate-800 bg-slate-950/70 p-4 text-sm leading-6 text-slate-200">{buildSystemPromptPreview(form)}</pre>
        </Card>
      {:else}
        <Card class="p-5">
          <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Secondary agent</p>
          <p class="mt-2 text-sm text-slate-300">Secondary agents are lightweight task executors. They have no personality or memory integration — just a focused system prompt and scoped tools.</p>
          <p class="mt-2 text-sm text-slate-300">They are invoked by primary agents via delegation or by the workflow engine via step agent overrides.</p>
        </Card>
      {/if}
    </div>
  </div>
</form>

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
