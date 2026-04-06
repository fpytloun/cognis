import type { Agent, LLMProvider, SecretMetadata, Skill, ToolDefinitionSummary, Workflow } from '$lib/types/api';

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
  agentType: 'primary' | 'secondary';
  name: string;
  description: string;
  avatarImageId: string;
  avatarUrl: string;  // read-only display URL (computed from avatarImageId)
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
  intarisMcpServers: string[];
  originalTools: Record<string, unknown>;
  executorId: string;
  executorSelector: string;
  disabledCategories: string[];
  disabledTools: string[];
  selectedSkillIds: string[];
}

const DEFAULT_SYSTEM_PROMPT = `You are {name}, an AI assistant.

Be helpful, direct, and concise. Focus on accuracy over agreement.`;

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
    agentType: 'primary',
    name: '',
    description: '',
    avatarImageId: '',
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
    intarisMcpServers: [],
    originalTools: {},
    executorId: '',
    executorSelector: '',
    disabledCategories: [],
    disabledTools: [],
    selectedSkillIds: []
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
    agentType: (agent.agent_type === 'secondary' ? 'secondary' : 'primary') as 'primary' | 'secondary',
    name: agent.display_name || agent.name,
    description: agent.description ?? '',
    avatarImageId: agent.avatar_image_id ?? '',
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
    executorId: typeof execution.executor_id === 'string' ? execution.executor_id : '',
    executorSelector:
      execution.executor_selector && typeof execution.executor_selector === 'object'
        ? Object.entries(execution.executor_selector as Record<string, unknown>)
            .map(([key, value]) => `${key}=${String(value)}`)
            .join('\n')
        : '',
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
    intarisMcpServers: Array.isArray(tools.intaris_mcp_servers)
      ? tools.intaris_mcp_servers.filter((v): v is string => typeof v === 'string')
      : [],
    disabledCategories: Array.isArray(tools.disabled_categories)
      ? tools.disabled_categories.filter((value): value is string => typeof value === 'string')
      : [],
    disabledTools: Array.isArray(tools.disabled_tools)
      ? tools.disabled_tools.filter((value): value is string => typeof value === 'string')
      : [],
    selectedSkillIds: extractSkillIds(agent.skills),
    originalTools: tools
  };
}

function extractSkillIds(skills: Record<string, unknown> | null): string[] {
  if (!skills || typeof skills !== 'object') return [];
  const items = (skills as Record<string, unknown>).items;
  if (!Array.isArray(items)) return [];
  return items
    .filter((item): item is Record<string, unknown> =>
      typeof item === 'object' && item !== null && typeof (item as Record<string, unknown>).skill_id === 'string' && !('tool_names' in (item as Record<string, unknown>))
    )
    .filter((item) => item.enabled !== false)
    .map((item) => String(item.skill_id));
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
  const {
    delegation_tools: _legacyDelegationTools,
    disabled_categories: _legacyDisabledCategories,
    disabled_tools: _legacyDisabledTools,
    intaris_mcp_servers: _legacyIntarisMcpServers,
    mcp_servers: _legacyMcpServers,
    ...preservedTools
  } = (form.originalTools ?? {}) as Record<string, unknown>;

  const executorSelector = Object.fromEntries(
    nonEmptyLines(form.executorSelector)
      .map((entry) => {
        const [key, ...rest] = entry.split('=');
        return [key?.trim(), rest.join('=').trim()] as const;
      })
      .filter(([key, value]) => Boolean(key) && Boolean(value))
  );

  const payload: Record<string, unknown> = {
    agent_id: form.agentId || undefined, // let backend auto-generate if empty
    agent_type: form.agentType,
    name: form.name,
    description: form.description || null,
    avatar_image_id: form.avatarImageId || null,
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
      ...preservedTools,
      delegation_tools: form.canDelegate,
      ...(form.disabledCategories.length > 0
        ? { disabled_categories: [...new Set(form.disabledCategories)] }
        : {}),
      ...(form.disabledTools.length > 0
        ? { disabled_tools: [...new Set(form.disabledTools)] }
        : {}),
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
        })),
      ...(form.intarisMcpServers.length > 0
        ? { intaris_mcp_servers: form.intarisMcpServers }
        : {})
    },
    skills: form.selectedSkillIds.length > 0
      ? {
          items: form.selectedSkillIds.map((id) => ({ skill_id: id, enabled: true }))
        }
      : null,
    llm_config: {
      provider_id: form.providerId || undefined,
      model: form.model || undefined,
      temperature: form.temperature ? Number(form.temperature) : undefined,
      max_tokens: form.maxTokens ? Number(form.maxTokens) : undefined,
      reasoning_effort: form.reasoningEffort || undefined
    },
    execution: {
      executor_id: form.executorId || undefined,
      executor_selector:
        !form.executorId && Object.keys(executorSelector).length > 0 ? executorSelector : undefined,
      available_workflow_ids: form.availableWorkflowIds,
      default_workflow_id: form.defaultWorkflowId || undefined,
      workflow_selection_mode: form.workflowSelectionMode,
      step_agent_overrides: form.stepAgentOverridesJson ? JSON.parse(form.stepAgentOverridesJson) : {}
    }
  };

  return payload;
}

export function formStateToEffectiveToolsPreviewPayload(form: AgentFormState): Record<string, unknown> {
  const payload = formStateToPayload(form);
  return {
    agent_id: payload.agent_id ?? null,
    tools: payload.tools,
    permissions: payload.permissions,
    execution: payload.execution,
    skills: payload.skills ?? {}
  };
}

/**
 * Build a preview of the composed system prompt that the LLM will receive.
 * Mirrors the backend `AgentDefinition.compose_personality()` + system_prompt
 * composition in `context.py`.
 */
export function buildSystemPromptPreview(form: AgentFormState): string {
  const parts: string[] = [];

  // Personality fields (matches Python compose_personality order)
  const identityLines: string[] = [];
  const purpose = form.purpose.trim();
  const tone = form.tone.trim();
  const temperament = form.temperament.trim();
  if (purpose) identityLines.push(`Purpose: ${purpose}`);
  if (tone) identityLines.push(`Tone: ${tone}`);
  if (temperament) identityLines.push(`Temperament: ${temperament}`);
  const rules = nonEmptyLines(form.behavioralRules);
  if (rules.length > 0) identityLines.push(`Behavioral rules:\n${rules.map((r) => `- ${r}`).join('\n')}`);
  if (identityLines.length > 0) parts.push(identityLines.join('\n'));

  // System prompt (user-written instructions)
  if (form.systemPrompt.trim()) parts.push(form.systemPrompt.trim());

  return parts.join('\n\n');
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
