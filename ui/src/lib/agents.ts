import type { Agent, LLMProvider, SecretMetadata, ToolDefinitionSummary, Workflow } from '$lib/types/api';

export interface MCPEnvVar {
  key: string;
  value: string;
  type: 'literal' | 'secret';
}

export interface MCPServerFormState {
  name: string;
  command: string;
  argsText: string;
  envVars: MCPEnvVar[];
  timeoutSeconds: number;
}

export interface AgentFormState {
  agentId: string;
  customId: boolean;
  name: string;
  description: string;
  avatarUrl: string;
  systemPrompt: string;
  tone: string;
  temperament: string;
  purpose: string;
  behavioralRules: string;
  allowedSecrets: string[];
  canDelegate: boolean;
  maxDelegationDepth: number;
  toolPermissions: Record<string, string>;
  providerId: string;
  model: string;
  temperature: string;
  maxTokens: string;
  reasoningEffort: string;
  availableWorkflowIds: string[];
  defaultWorkflowId: string;
  workflowSelectionMode: string;
  stepAgentOverridesJson: string;
  mcpServers: MCPServerFormState[];
  originalTools: Record<string, unknown>;
}

const DEFAULT_SYSTEM_PROMPT = `You are {name}.

Be helpful, direct, and concise.`;

export function defaultSystemPrompt(name: string): string {
  return DEFAULT_SYSTEM_PROMPT.replace('{name}', name || 'an AI assistant');
}

export function slugify(text: string): string {
  return text
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, '')
    .replace(/[\s_]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 64) || 'unnamed';
}

export function createEmptyAgentForm(workflows: Workflow[] = []): AgentFormState {
  // Pre-select all system workflows for new agents
  const systemWorkflowIds = workflows
    .filter((w) => w.is_system)
    .map((w) => w.workflow_id);

  return {
    agentId: '',
    customId: false,
    name: '',
    description: '',
    avatarUrl: '',
    systemPrompt: '',
    tone: '',
    temperament: '',
    purpose: '',
    behavioralRules: '',
    allowedSecrets: [],
    canDelegate: true,
    maxDelegationDepth: 3,
    toolPermissions: {},
    providerId: '',
    model: '',
    temperature: '',
    maxTokens: '',
    reasoningEffort: '',
    availableWorkflowIds: systemWorkflowIds,
    defaultWorkflowId: 'system:direct',
    workflowSelectionMode: 'automatic',
    stepAgentOverridesJson: '{}',
    mcpServers: [],
    originalTools: {}
  };
}

export function agentToFormState(agent: Agent): AgentFormState {
  const form = createEmptyAgentForm();
  const personality = agent.personality ?? {};
  const permissions = agent.permissions ?? {};
  const llmConfig = agent.llm_config ?? {};
  const execution = agent.execution ?? {};
  const tools = agent.tools ?? {};

  return {
    ...form,
    agentId: agent.agent_id,
    customId: true, // existing agent always has a custom ID
    name: agent.display_name || agent.name,
    description: agent.description ?? '',
    avatarUrl: agent.avatar_url ?? '',
    systemPrompt: agent.system_prompt ?? '',
    tone: typeof personality.tone === 'string' ? personality.tone : '',
    temperament: typeof personality.temperament === 'string' ? personality.temperament : '',
    purpose: typeof personality.purpose === 'string' ? personality.purpose : '',
    behavioralRules: Array.isArray(personality.behavioral_rules)
      ? personality.behavioral_rules.join('\n')
      : '',
    allowedSecrets: Array.isArray(permissions.allowed_secrets)
      ? (permissions.allowed_secrets as unknown[]).filter((v): v is string => typeof v === 'string')
      : [],
    canDelegate: permissions.can_delegate !== false,
    maxDelegationDepth:
      typeof permissions.max_delegation_depth === 'number' ? permissions.max_delegation_depth : 3,
    toolPermissions:
      permissions.tool_permissions && typeof permissions.tool_permissions === 'object'
        ? (permissions.tool_permissions as Record<string, string>)
        : {},
    providerId: typeof llmConfig.provider_id === 'string' ? llmConfig.provider_id : '',
    model: typeof llmConfig.model === 'string' ? llmConfig.model : '',
    temperature:
      typeof llmConfig.temperature === 'number' ? String(llmConfig.temperature) : '',
    maxTokens: typeof llmConfig.max_tokens === 'number' ? String(llmConfig.max_tokens) : '',
    reasoningEffort:
      typeof llmConfig.reasoning_effort === 'string' ? llmConfig.reasoning_effort : '',
    availableWorkflowIds: Array.isArray(execution.available_workflow_ids)
      ? execution.available_workflow_ids.filter((value): value is string => typeof value === 'string')
      : [],
    defaultWorkflowId:
      typeof execution.default_workflow_id === 'string' ? execution.default_workflow_id : '',
    workflowSelectionMode:
      typeof execution.workflow_selection_mode === 'string'
        ? execution.workflow_selection_mode
        : 'automatic',
    stepAgentOverridesJson: JSON.stringify(execution.step_agent_overrides ?? {}, null, 2),
    mcpServers: Array.isArray(tools.mcp_servers)
      ? tools.mcp_servers
          .filter((value): value is Record<string, unknown> => Boolean(value && typeof value === 'object'))
          .map((server) => ({
            name: typeof server.name === 'string' ? server.name : '',
            command: typeof server.command === 'string' ? server.command : '',
            argsText: Array.isArray(server.args)
              ? server.args.filter((value): value is string => typeof value === 'string').join('\n')
              : '',
            envVars:
              server.env && typeof server.env === 'object'
                ? Object.entries(server.env as Record<string, unknown>).map(([key, val]) => ({
                    key,
                    value: String(val),
                    type: (String(val).startsWith('$secret:') ? 'secret' : 'literal') as 'literal' | 'secret'
                  }))
                : [],
            timeoutSeconds:
              typeof server.timeout_seconds === 'number' ? server.timeout_seconds : 30
          }))
      : [],
    originalTools: tools
  };
}

function nonEmptyLines(value: string): string[] {
  return value
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export function formStateToPayload(form: AgentFormState): Record<string, unknown> {
  const toolPermissions = Object.fromEntries(
    Object.entries(form.toolPermissions).filter(([, value]) => value.length > 0)
  );

  const payload: Record<string, unknown> = {
    agent_id: form.agentId || undefined, // let backend auto-generate if empty
    name: form.name,
    description: form.description || null,
    avatar_url: form.avatarUrl || null,
    system_prompt: form.systemPrompt || null,
    personality: {
      tone: form.tone || undefined,
      temperament: form.temperament || undefined,
      purpose: form.purpose || undefined,
      behavioral_rules: nonEmptyLines(form.behavioralRules)
    },
    permissions: {
      tool_permissions: toolPermissions,
      allowed_secrets: form.allowedSecrets,
      can_delegate: form.canDelegate,
      max_delegation_depth: form.maxDelegationDepth
    },
    tools: {
      ...(form.originalTools ?? {}),
      delegation_tools: form.canDelegate,
      mcp_servers: form.mcpServers
        .filter((server) => server.name.trim() && server.command.trim())
        .map((server) => ({
          name: server.name.trim(),
          command: server.command.trim(),
          args: nonEmptyLines(server.argsText),
          env: Object.fromEntries(
            server.envVars
              .filter((e) => e.key.trim())
              .map((e) => [
                e.key.trim(),
                e.type === 'secret' ? `$secret:${e.value}` : e.value
              ])
          ),
          timeout_seconds: server.timeoutSeconds || 30
        }))
    },
    llm_config: {
      provider_id: form.providerId || undefined,
      model: form.model || undefined,
      temperature: form.temperature ? Number(form.temperature) : undefined,
      max_tokens: form.maxTokens ? Number(form.maxTokens) : undefined,
      reasoning_effort: form.reasoningEffort || undefined
    },
    execution: {
      available_workflow_ids: form.availableWorkflowIds,
      default_workflow_id: form.defaultWorkflowId || undefined,
      workflow_selection_mode: form.workflowSelectionMode,
      step_agent_overrides: form.stepAgentOverridesJson ? JSON.parse(form.stepAgentOverridesJson) : {}
    }
  };

  return payload;
}

export function buildBootstrapPreview(form: AgentFormState): string {
  const rules = nonEmptyLines(form.behavioralRules);
  return [
    `Name: ${form.name || 'Unnamed Agent'}`,
    `Purpose: ${form.purpose || 'No explicit purpose configured.'}`,
    `Tone: ${form.tone || 'adaptive'}`,
    `Temperament: ${form.temperament || 'balanced'}`,
    form.description ? `Description: ${form.description}` : null,
    rules.length > 0 ? `Behavioral rules: ${rules.join('; ')}` : null,
    form.systemPrompt ? `System prompt preview:\n${form.systemPrompt}` : null
  ]
    .filter((value): value is string => Boolean(value))
    .join('\n\n');
}

export function providerOptions(providers: LLMProvider[]): Array<{ value: string; label: string }> {
  return providers.map((provider) => ({ value: provider.provider_id, label: provider.display_name }));
}

export function workflowOptions(workflows: Workflow[]): Array<{ value: string; label: string; isSystem: boolean }> {
  return workflows.map((workflow) => ({
    value: workflow.workflow_id,
    label: workflow.name,
    isSystem: workflow.is_system
  }));
}

export function toolOptions(tools: ToolDefinitionSummary[]): Array<{ value: string; label: string }> {
  return tools.map((tool) => ({ value: tool.name, label: tool.name }));
}

export function secretOptions(secrets: SecretMetadata[]): Array<{ name: string; label: string }> {
  return secrets.map((s) => ({
    name: s.name,
    label: `${s.name}${s.description ? ` — ${s.description}` : ''} (${s.scope})`
  }));
}
