<script lang="ts">
  import { Sparkles, Loader2 } from 'lucide-svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import AvatarGenerateModal from '$lib/components/agents/AvatarGenerateModal.svelte';
  import ImageLightbox from '$lib/components/ImageLightbox.svelte';
  import { api } from '$lib/api/client';
  import {
    buildBootstrapPreview,
    defaultSystemPrompt,
    formStateToPayload,
    slugify,
    type AgentFormState,
    type MCPEnvVar,
    type MCPServerFormState
  } from '$lib/agents';
  import type { Agent, IntarisMCPServer, LLMProvider, MCPServerTestResponse, SecretMetadata, ToolDefinitionSummary, Workflow } from '$lib/types/api';

  let {
    mode,
    form,
    tools,
    workflows,
    providers,
    secrets = [],
    intarisMcpServers = [],
    secondaryAgents = [],
    secondaryBindings = [],
    saving = false,
    error = '',
    readonly = false,
    onSave,
    onTestMcp,
    onBindingsChange,
    mcpTesting = false,
    mcpTestResult = null
  } = $props<{
    mode: 'create' | 'edit';
    form: AgentFormState;
    tools: ToolDefinitionSummary[];
    workflows: Workflow[];
    providers: LLMProvider[];
    secrets?: SecretMetadata[];
    intarisMcpServers?: IntarisMCPServer[];
    secondaryAgents?: Agent[];
    secondaryBindings?: string[];
    saving?: boolean;
    error?: string;
    readonly?: boolean;
    onSave: (payload: Record<string, unknown>) => void | Promise<void>;
    onTestMcp?: (() => void | Promise<void>) | null;
    onBindingsChange?: ((bindings: string[]) => void | Promise<void>) | null;
    mcpTesting?: boolean;
    mcpTestResult?: MCPServerTestResponse | null;
  }>();

  let localBindings = $state<string[]>([...secondaryBindings]);
  let showAvatarModal = $state(false);
  let showAvatarLightbox = $state(false);
  let uploadingAvatar = $state(false);
  let fileInput: HTMLInputElement | undefined = $state();

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

  function mcpServerError(server: MCPServerFormState): string | null {
    const hasAnyField = server.name.trim() || server.command.trim() || server.argsText.trim() || server.envVars.length > 0;
    if (!hasAnyField) {
      return null;
    }
    if (!server.name.trim()) {
      return 'Server name is required.';
    }
    if (!server.command.trim()) {
      return 'Command is required.';
    }
    for (const envVar of server.envVars) {
      if (!envVar.key.trim()) {
        return 'Environment variable name is required.';
      }
      if (envVar.type === 'secret' && !envVar.value) {
        return `Secret reference for ${envVar.key} is required.`;
      }
    }
    return null;
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
    const mcpErrors = form.mcpServers
      .map((server: MCPServerFormState, index: number) => [index, mcpServerError(server)] as const)
      .filter((entry: readonly [number, string | null]): entry is readonly [number, string] => Boolean(entry[1]));
    for (const [index, value] of mcpErrors) {
      errors[`mcpServers.${index}`] = value;
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

  async function handleSubmit(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    await onSave(formStateToPayload(form));
  }

  function addMcpServer(): void {
    const next: MCPServerFormState = {
      name: '',
      command: '',
      argsText: '',
      envVars: [],
      timeoutSeconds: 30
    };
    form.mcpServers = [...form.mcpServers, next];
  }

  function addEnvVar(server: MCPServerFormState): void {
    server.envVars = [...server.envVars, { key: '', value: '', type: 'literal' }];
  }

  function removeEnvVar(server: MCPServerFormState, index: number): void {
    server.envVars = server.envVars.filter((_: MCPEnvVar, i: number) => i !== index);
  }

  function removeMcpServer(index: number): void {
    form.mcpServers = form.mcpServers.filter((_: MCPServerFormState, itemIndex: number) => itemIndex !== index);
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
</script>

<form class="space-y-5" onsubmit={handleSubmit}>
  <div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
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
                {#if !readonly}
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
                {#if !readonly}
                  <button type="button" class="text-sky-400 hover:text-sky-300 disabled:opacity-50" disabled={generatingField === 'tone'} onclick={() => generateField('tone', () => form.tone, (v) => { form.tone = v; })} title="Generate with AI">
                    {#if generatingField === 'tone'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                  </button>
                {/if}
              </div>
              <Input bind:value={form.tone} placeholder="calm, direct, curious" disabled={readonly} />
            </div>
            <div class="space-y-2 text-sm font-medium text-slate-200">
              <div class="flex items-center justify-between">
                <span>Temperament</span>
                {#if !readonly}
                  <button type="button" class="text-sky-400 hover:text-sky-300 disabled:opacity-50" disabled={generatingField === 'temperament'} onclick={() => generateField('temperament', () => form.temperament, (v) => { form.temperament = v; })} title="Generate with AI">
                    {#if generatingField === 'temperament'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                  </button>
                {/if}
              </div>
              <Input bind:value={form.temperament} placeholder="patient" disabled={readonly} />
            </div>
            <div class="space-y-2 text-sm font-medium text-slate-200">
              <div class="flex items-center justify-between">
                <span>Purpose</span>
                {#if !readonly}
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
              {#if !readonly}
                <button type="button" class="flex items-center gap-1 text-xs text-sky-400 hover:text-sky-300 disabled:opacity-50" disabled={generatingField === 'behavioral_rules'} onclick={() => generateField('behavioral_rules', () => form.behavioralRules, (v) => { form.behavioralRules = v; })} title="Generate with AI">
                  {#if generatingField === 'behavioral_rules'}<Loader2 class="h-3 w-3 animate-spin" />{:else}<Sparkles class="h-3 w-3" />{/if}
                </button>
              {/if}
            </div>
            <textarea bind:value={form.behavioralRules} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="Always cite sources&#10;Prefer concise answers" disabled={readonly}></textarea>
          </div>
        </Card>
      {/if}

      <!-- System prompt -->
      <Card class="p-5">
        <div class="mb-3 flex items-center justify-between gap-3">
          <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">System prompt</p>
          <Button size="sm" variant="secondary" type="button" onclick={resetSystemPrompt}>Reset to default</Button>
        </div>
        <textarea bind:value={form.systemPrompt} class="min-h-[180px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="You are {'{'}name{'}'}.&#10;&#10;Be helpful, direct, and concise." disabled={readonly}></textarea>
        <p class="mt-2 text-xs text-slate-400">The agent's base instructions. Memory context and tool descriptions are injected separately at runtime.</p>
      </Card>

      <!-- Tools & Permissions -->
      <Card class="p-5">
        <div class="grid gap-4 md:grid-cols-2">
          <label class="flex items-center gap-3 text-sm font-medium text-slate-200">
            <input bind:checked={form.canDelegate} class="h-4 w-4 rounded border-slate-600 bg-slate-950" type="checkbox" />
            Can delegate
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Max delegation depth</span>
            <Input bind:value={form.maxDelegationDepth} type="number" />
          </label>
        </div>

        {#if tools.length > 0}
          <div class="mt-4 max-h-64 overflow-y-auto rounded-xl border border-slate-800 bg-slate-950/60 p-3">
            <div class="space-y-2">
              {#each tools as tool}
                <div class="flex items-center justify-between gap-3 text-sm">
                  <span class="text-slate-200">{tool.name}</span>
                  <select bind:value={form.toolPermissions[tool.name]} class="w-32 rounded-lg border border-slate-700 bg-slate-950/80 px-2 py-1 text-xs text-slate-100">
                    {#each permissionOptions as option}
                      <option value={option}>{option || 'inherit'}</option>
                    {/each}
                  </select>
                </div>
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
                  />
                  {secret.name}
                  <span class="text-xs text-slate-400">({secret.scope})</span>
                </label>
              {/each}
            </div>
          </div>
        {/if}

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
                  class="px-3 py-1.5 rounded-lg text-sm border transition-colors {selected ? 'bg-blue-500/20 border-blue-500/50 text-blue-300' : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-600'}"
                  onclick={() => {
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

        <!-- Local MCP Servers -->
        <div class="mt-4 space-y-3">
          <div class="flex items-center justify-between gap-3">
            <p class="text-sm font-medium text-slate-200">Local MCP servers</p>
            <Button size="sm" variant="secondary" type="button" onclick={addMcpServer}>Add server</Button>
          </div>

          {#each form.mcpServers as server, index}
            <div class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4 space-y-3">
              <div class="grid gap-3 md:grid-cols-2">
                <label class="space-y-1 text-sm font-medium text-slate-200">
                  <span>Name</span>
                  <Input bind:value={server.name} placeholder="filesystem" />
                </label>
                <label class="space-y-1 text-sm font-medium text-slate-200">
                  <span>Command</span>
                  <Input bind:value={server.command} placeholder="npx" />
                </label>
              </div>
              <label class="block space-y-1 text-sm font-medium text-slate-200">
                <span>Arguments (one per line)</span>
                <textarea bind:value={server.argsText} class="min-h-[60px] w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 font-mono text-sm text-slate-100" placeholder="-y&#10;@modelcontextprotocol/server-filesystem&#10;/path/to/project"></textarea>
              </label>
              <div class="space-y-2">
                <div class="flex items-center justify-between gap-2">
                  <span class="text-sm font-medium text-slate-200">Environment variables</span>
                  <Button size="sm" variant="secondary" type="button" onclick={() => addEnvVar(server)}>Add variable</Button>
                </div>
                {#each server.envVars as envVar, envIndex}
                  <div class="flex items-center gap-2">
                    <Input bind:value={envVar.key} placeholder="KEY" class="w-36" />
                    <select bind:value={envVar.type} class="w-24 rounded-lg border border-slate-700 bg-slate-950/80 px-2 py-2 text-xs text-slate-100">
                      <option value="literal">Value</option>
                      <option value="secret">Secret</option>
                    </select>
                    {#if envVar.type === 'secret'}
                      <select bind:value={envVar.value} class="flex-1 rounded-lg border border-slate-700 bg-slate-950/80 px-2 py-2 text-xs text-slate-100">
                        <option value="">Select secret...</option>
                        {#each secrets as secret}
                          <option value={secret.name}>{secret.name}</option>
                        {/each}
                      </select>
                    {:else}
                      <Input bind:value={envVar.value} placeholder="value" class="flex-1" />
                    {/if}
                    <button type="button" class="text-xs text-rose-400 hover:text-rose-300" onclick={() => removeEnvVar(server, envIndex)}>Remove</button>
                  </div>
                {/each}
              </div>
              <div class="flex items-center gap-3">
                <label class="space-y-1 text-sm font-medium text-slate-200">
                  <span>Timeout (s)</span>
                  <Input bind:value={server.timeoutSeconds} type="number" />
                </label>
                <Button size="sm" variant="danger" type="button" onclick={() => removeMcpServer(index)}>Remove</Button>
              </div>
              {#if errors[`mcpServers.${index}`]}
                <p class="text-xs text-rose-300">{errors[`mcpServers.${index}`]}</p>
              {/if}
            </div>
          {/each}

          {#if mode === 'edit' && onTestMcp}
            <Button variant="secondary" type="button" disabled={mcpTesting} onclick={onTestMcp}>
              {mcpTesting ? 'Testing…' : 'Test MCP servers'}
            </Button>
            {#if mcpTestResult}
              <div class="space-y-2">
                {#each mcpTestResult.servers as server}
                  <div class={`rounded-xl border px-3 py-2 text-sm ${server.ok ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100' : 'border-rose-500/30 bg-rose-500/10 text-rose-100'}`}>
                    <span class="font-medium">{server.name}</span>
                    {#if server.ok}
                      — {server.tool_count} tools discovered
                    {:else}
                      — {server.error}
                    {/if}
                  </div>
                {/each}
              </div>
            {/if}
          {/if}
        </div>
      </Card>

      <!-- Provider & Model -->
      <Card class="p-5">
        <p class="mb-3 text-xs font-medium uppercase tracking-[0.25em] text-slate-400">LLM Configuration</p>
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Provider</span>
            <select bind:value={form.providerId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
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
              <select bind:value={form.model} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="">Use provider default</option>
                {#each selectedProviderModels() as modelId}
                  <option value={modelId}>{modelId}</option>
                {/each}
              </select>
            {:else}
              <Input bind:value={form.model} placeholder="Use provider default" />
            {/if}
            <span class="block text-xs text-slate-400">Leave empty to use the provider's default model.</span>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Temperature</span>
            <Input bind:value={form.temperature} type="number" placeholder="default" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Max tokens</span>
            <Input bind:value={form.maxTokens} type="number" placeholder="default" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Reasoning effort</span>
            <select bind:value={form.reasoningEffort} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              <option value="">Default</option>
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
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
                <input checked={form.availableWorkflowIds.includes(workflow.workflow_id)} class="mt-1 h-4 w-4 rounded border-slate-600 bg-slate-950" type="checkbox" onchange={() => toggleWorkflow(workflow.workflow_id)} />
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
              <select bind:value={form.defaultWorkflowId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
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
              <select bind:value={form.workflowSelectionMode} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                <option value="automatic">automatic</option>
                <option value="always_ask">always_ask</option>
                <option value="use_default">use_default</option>
              </select>
            </label>
          </div>

          <label class="block space-y-2 text-sm font-medium text-slate-200">
            <span>Step agent overrides (JSON)</span>
            <textarea aria-invalid={errors.stepAgentOverridesJson ? 'true' : 'false'} bind:value={form.stepAgentOverridesJson} class="min-h-[150px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100 placeholder:text-slate-500"></textarea>
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

      {#if !readonly}
        <div class="flex justify-end gap-3">
          <Button type="submit" disabled={saving || !canSubmit}>{saving ? 'Saving…' : mode === 'create' ? 'Create agent' : 'Save changes'}</Button>
        </div>
      {/if}
    </div>

    <div class="space-y-5">
      {#if form.agentType === 'primary'}
        <Card class="p-5">
          <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Mnemory bootstrap preview</p>
          <pre class="mt-4 whitespace-pre-wrap rounded-2xl border border-slate-800 bg-slate-950/70 p-4 text-sm leading-6 text-slate-200">{buildBootstrapPreview(form)}</pre>
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
