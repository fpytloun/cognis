<script lang="ts">
  import Button from '$lib/components/ui/Button.svelte';
  import Card from '$lib/components/ui/Card.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import {
    buildBootstrapPreview,
    formStateToPayload,
    type AgentFormState,
    type MCPServerFormState
  } from '$lib/agents';
  import type { LLMProvider, MCPServerTestResponse, ToolDefinitionSummary, Workflow } from '$lib/types/api';

  let {
    mode,
    form,
    tools,
    workflows,
    providers,
    saving = false,
    error = '',
    onSave,
    onTestMcp,
    mcpTesting = false,
    mcpTestResult = null
  } = $props<{
    mode: 'create' | 'edit';
    form: AgentFormState;
    tools: ToolDefinitionSummary[];
    workflows: Workflow[];
    providers: LLMProvider[];
    saving?: boolean;
    error?: string;
    onSave: (payload: Record<string, unknown>) => void | Promise<void>;
    onTestMcp?: (() => void | Promise<void>) | null;
    mcpTesting?: boolean;
    mcpTestResult?: MCPServerTestResponse | null;
  }>();

  const permissionOptions = ['', 'allow', 'evaluate', 'deny'];

  function validateJson(value: string, label: string): string | null {
    if (!value.trim()) {
      return null;
    }
    try {
      JSON.parse(value);
      return null;
    } catch (error) {
      return `${label} must be valid JSON.`;
    }
  }

  function mcpServerError(server: MCPServerFormState): string | null {
    if (!server.name.trim() && !server.command.trim() && !server.argsText.trim() && !server.envText.trim()) {
      return null;
    }
    if (!server.name.trim()) {
      return 'Server name is required.';
    }
    if (!server.command.trim()) {
      return 'Server command is required.';
    }
    const invalidEnvLine = server.envText
      .split(/\n+/)
      .map((line) => line.trim())
      .filter(Boolean)
      .find((line) => !line.includes('='));
    if (invalidEnvLine) {
      return 'Environment variables must use KEY=value format.';
    }
    return null;
  }

  function validationErrors(): Record<string, string> {
    const errors: Record<string, string> = {};
    if (!form.agentId.trim()) {
      errors.agentId = 'Agent ID is required.';
    }
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
  const canSubmit = $derived(Object.keys(errors).length === 0 && Boolean(form.agentId.trim() && form.name.trim()));

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
      envText: '',
      timeoutSeconds: 30
    };
    form.mcpServers = [...form.mcpServers, next];
  }

  function removeMcpServer(index: number): void {
    form.mcpServers = form.mcpServers.filter((_: MCPServerFormState, itemIndex: number) => itemIndex !== index);
  }
</script>

<form class="space-y-5" onsubmit={handleSubmit}>
  <div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
    <div class="space-y-5">
      <Card class="p-5">
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Agent ID <span class="text-rose-300">*</span></span>
            <Input aria-invalid={errors.agentId ? 'true' : 'false'} bind:value={form.agentId} disabled={mode === 'edit'} placeholder="research-assistant" />
            {#if errors.agentId}
              <span class="text-xs text-rose-300">{errors.agentId}</span>
            {/if}
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Name <span class="text-rose-300">*</span></span>
            <Input aria-invalid={errors.name ? 'true' : 'false'} bind:value={form.name} placeholder="Research Assistant" />
            {#if errors.name}
              <span class="text-xs text-rose-300">{errors.name}</span>
            {/if}
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Display name</span>
            <Input bind:value={form.displayName} placeholder="Aria" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Avatar URL</span>
            <Input bind:value={form.avatarUrl} placeholder="https://…" />
          </label>
        </div>

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Description</span>
          <textarea bind:value={form.description} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"></textarea>
        </label>
      </Card>

      <Card class="p-5">
        <div class="grid gap-4 md:grid-cols-3">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Tone</span>
            <Input bind:value={form.tone} placeholder="calm, direct, curious" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Temperament</span>
            <Input bind:value={form.temperament} placeholder="patient" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Purpose</span>
            <Input bind:value={form.purpose} placeholder="research specialist" />
          </label>
        </div>

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>Behavioral rules</span>
          <textarea bind:value={form.behavioralRules} class="min-h-[110px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500" placeholder="One rule per line"></textarea>
        </label>

        <label class="mt-4 block space-y-2 text-sm font-medium text-slate-200">
          <span>System prompt</span>
          <textarea bind:value={form.systemPrompt} class="min-h-[180px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500"></textarea>
        </label>
      </Card>

      <Card class="p-5">
        <div class="flex items-center justify-between gap-3">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Tools & permissions</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Execution controls</h2>
          </div>
        </div>

        <div class="mt-4 grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Allowed secrets</span>
            <Input bind:value={form.allowedSecrets} placeholder="openai_api_key, github_token" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Max delegation depth</span>
            <Input bind:value={form.maxDelegationDepth} type="number" />
          </label>
        </div>

        <label class="mt-4 flex items-center gap-3 text-sm text-slate-200">
          <input bind:checked={form.canDelegate} class="h-4 w-4 rounded border-slate-600 bg-slate-950" type="checkbox" />
          <span>Allow delegation tools</span>
        </label>

        <div class="mt-5 overflow-hidden rounded-2xl border border-slate-800">
          <table class="min-w-full divide-y divide-slate-800 text-sm">
            <thead class="bg-slate-900/80 text-left text-slate-300">
              <tr>
                <th class="px-4 py-3 font-medium">Tool</th>
                <th class="px-4 py-3 font-medium">Permission</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-800 bg-slate-950/60">
              {#each tools as tool}
                <tr>
                  <td class="px-4 py-3 text-slate-100">{tool.name}</td>
                  <td class="px-4 py-3">
                    <select bind:value={form.toolPermissions[tool.name]} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
                      {#each permissionOptions as option}
                        <option value={option}>{option || 'inherit'}</option>
                      {/each}
                    </select>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </Card>

      <Card class="p-5">
        <div class="flex items-center justify-between gap-3">
          <div>
            <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">MCP servers</p>
            <h2 class="mt-1 text-lg font-semibold text-white">Local MCP integrations</h2>
            <p class="mt-2 text-sm leading-6 text-slate-400">
              Configure local MCP server commands that will be launched by the executor when this agent needs them.
            </p>
          </div>
          <Button type="button" variant="secondary" onclick={addMcpServer}>Add server</Button>
        </div>

        <div class="mt-4 space-y-4">
          {#if form.mcpServers.length === 0}
            <p class="rounded-2xl border border-dashed border-slate-800 bg-slate-950/40 px-4 py-4 text-sm text-slate-400">
              No MCP servers configured yet.
            </p>
          {/if}

          {#each form.mcpServers as server, index}
            <div class="rounded-2xl border border-slate-800 bg-slate-950/60 p-4">
              <div class="grid gap-4 md:grid-cols-2">
                <label class="space-y-2 text-sm font-medium text-slate-200">
                  <span>Name</span>
                  <Input bind:value={server.name} placeholder="filesystem" />
                </label>
                <label class="space-y-2 text-sm font-medium text-slate-200">
                  <span>Command</span>
                  <Input bind:value={server.command} placeholder="npx" />
                </label>
                <label class="space-y-2 text-sm font-medium text-slate-200 md:col-span-2">
                  <span>Args (one per line)</span>
                  <textarea bind:value={server.argsText} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100" placeholder="@modelcontextprotocol/server-filesystem&#10;/path/to/project"></textarea>
                </label>
                <label class="space-y-2 text-sm font-medium text-slate-200 md:col-span-2">
                  <span>Environment variables (KEY=value per line)</span>
                  <textarea bind:value={server.envText} class="min-h-[90px] w-full rounded-2xl border border-slate-700 bg-slate-950/80 px-4 py-3 font-mono text-sm text-slate-100" placeholder="GITHUB_TOKEN=secret_name"></textarea>
                </label>
                <label class="space-y-2 text-sm font-medium text-slate-200">
                  <span>Timeout seconds</span>
                  <Input bind:value={server.timeoutSeconds} type="number" min="1" />
                </label>
              </div>

              <div class="mt-4 flex justify-end">
                <Button type="button" variant="danger" size="sm" onclick={() => removeMcpServer(index)}>Remove</Button>
              </div>
              {#if errors[`mcpServers.${index}`]}
                <p class="mt-3 text-xs text-rose-300">{errors[`mcpServers.${index}`]}</p>
              {/if}
            </div>
          {/each}
        </div>

        {#if mode === 'edit' && onTestMcp}
          <div class="mt-5 flex items-center gap-3">
            <Button type="button" variant="secondary" onclick={() => onTestMcp?.()} disabled={mcpTesting || form.mcpServers.length === 0}>
              {mcpTesting ? 'Testing MCP…' : 'Test MCP servers'}
            </Button>
          </div>
        {/if}

        {#if mcpTestResult}
          <div class="mt-4 space-y-3">
            {#each mcpTestResult.items as item}
              <div class={`rounded-2xl border px-4 py-3 text-sm ${item.ok ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100' : 'border-rose-500/30 bg-rose-500/10 text-rose-100'}`}>
                <p class="font-medium">{item.name}</p>
                {#if item.ok}
                  <p class="mt-1">Discovered {item.tools.length} tool{item.tools.length === 1 ? '' : 's'}.</p>
                {:else}
                  <p class="mt-1">{item.error_detail ?? 'Unable to discover MCP tools.'}</p>
                {/if}
              </div>
            {/each}
          </div>
        {/if}
      </Card>

      <Card class="p-5">
        <div class="grid gap-4 md:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Provider</span>
            <select bind:value={form.providerId} class="w-full rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2 text-sm text-slate-100">
              <option value="">Manual / default</option>
              {#each providers as provider}
                <option value={provider.provider_id}>{provider.display_name}</option>
              {/each}
            </select>
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Model</span>
            <Input bind:value={form.model} placeholder="gpt-5.4-mini" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Temperature</span>
            <Input bind:value={form.temperature} type="number" placeholder="0.2" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Max tokens</span>
            <Input bind:value={form.maxTokens} type="number" placeholder="4096" />
          </label>
        </div>
      </Card>

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
                <option value="">None</option>
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

      {#if error}
        <p class="rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100">
          {error}
        </p>
      {/if}

      <div class="flex justify-end gap-3">
        <Button type="submit" disabled={saving || !canSubmit}>{saving ? 'Saving…' : mode === 'create' ? 'Create agent' : 'Save changes'}</Button>
      </div>
    </div>

    <div class="space-y-5">
      <Card class="p-5">
        <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Mnemory bootstrap preview</p>
        <pre class="mt-4 whitespace-pre-wrap rounded-2xl border border-slate-800 bg-slate-950/70 p-4 text-sm leading-6 text-slate-200">{buildBootstrapPreview(form)}</pre>
      </Card>
    </div>
  </div>
</form>
