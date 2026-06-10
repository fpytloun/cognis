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

/**
 * Stage 36 (multi-executor agents): one entry in
 * ``execution.additional_executors``. Exactly one of ``executorId`` or
 * ``executorSelector`` (newline-separated ``key=value`` pairs) must be set.
 */
export interface AdditionalExecutorEntry {
  executorId: string;
  executorSelector: string;
  description: string;
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
  allowedCredentials: string[];
  allowedKnowledgebases: string[];
  canDelegate: boolean;
  maxDelegationDepth: number;
  toolPermissions: Record<string, string>;
  providerId: string;
  model: string;
  temperature: string;
  maxTokens: string;
  reasoningEffort: string;
  voice: string;
  availableWorkflowIds: string[];
  defaultWorkflowId: string;
  workflowSelectionMode: string;
  defaultChatMode: 'default' | 'plan' | 'build';
  stepAgentOverridesJson: string;
  mcpServers: MCPServerFormState[];
  intarisMcpServers: string[];
  originalSkills: Record<string, unknown> | null;
  originalTools: Record<string, unknown>;
  executorId: string;
  executorSelector: string;
  /**
   * Stage 36: additional executors assigned to the agent. Each entry must
   * have either an explicit executor_id or a non-empty selector (not both).
   * Additional executors are NOT auto-selected by the controller; they are
   * reachable only via target_executor on a tool call or switch_executor.
   */
  additionalExecutors: AdditionalExecutorEntry[];
  disabledCategories: string[];
  disabledTools: string[];
  disabledMcpServers: string[];
  optInBuiltinTools: string[];
  selectedSkillIds: string[];
  autoLoadSkillIds: string[];
}

const DEFAULT_SYSTEM_PROMPT = `You are {name}, a capable general-purpose AI assistant.

Your goal is to help the user achieve outcomes with clear, accurate, practical responses. Be conversational by default, and become structured when the task benefits from planning, comparison, troubleshooting, or execution.

Optimize for correctness, usefulness, and actionability over verbosity. Adapt depth to the user's apparent expertise:
- Beginner: provide brief context and avoid unnecessary jargon.
- Intermediate: explain tradeoffs, reasoning, and common pitfalls.
- Expert: be concise and focus on constraints, risks, edge cases, and implications.

For simple requests, answer directly. For complex requests, start with a brief verdict or recommendation, then provide structured rationale, assumptions, risks, and next actions.

When several approaches are viable, recommend one primary path and one reasonable alternative, including when to choose each.

If important information is missing, ask at most one targeted clarifying question. If a safe default exists, proceed with explicit assumptions.

Do not expose hidden chain-of-thought. Present concise reasoning, decision drivers, and verification steps when useful.

Mirror the user's language and formality. Keep formatting clean and readable.`;

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
    allowedCredentials: [],
    allowedKnowledgebases: [],
    canDelegate: true,
    maxDelegationDepth: 3,
    toolPermissions: {},
    providerId: '',
    model: '',
    temperature: '',
    maxTokens: '',
    reasoningEffort: '',
    voice: '',
    availableWorkflowIds: systemWorkflowIds,
    defaultWorkflowId: 'system:direct',
    workflowSelectionMode: 'automatic',
    defaultChatMode: 'default',
    stepAgentOverridesJson: '{}',
    mcpServers: [],
    intarisMcpServers: [],
    originalSkills: null,
    originalTools: {},
    executorId: '',
    executorSelector: '',
    additionalExecutors: [],
    disabledCategories: [],
    disabledTools: [],
    disabledMcpServers: [],
    optInBuiltinTools: [],
    selectedSkillIds: [],
    autoLoadSkillIds: []
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
    allowedCredentials: Array.isArray(permissions.allowed_credentials)
      ? (permissions.allowed_credentials as unknown[]).filter((v): v is string => typeof v === 'string')
      : [],
    allowedKnowledgebases: Array.isArray(permissions.allowed_knowledgebases)
      ? (permissions.allowed_knowledgebases as unknown[]).filter((v): v is string => typeof v === 'string')
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
    voice: typeof llmConfig.voice === 'string' ? llmConfig.voice : '',
    availableWorkflowIds: Array.isArray(execution.available_workflow_ids)
      ? execution.available_workflow_ids.filter((value): value is string => typeof value === 'string')
      : [],
    defaultWorkflowId:
      typeof execution.default_workflow_id === 'string' ? execution.default_workflow_id : '',
    workflowSelectionMode:
      typeof execution.workflow_selection_mode === 'string'
        ? execution.workflow_selection_mode
        : 'automatic',
    defaultChatMode:
      execution.default_chat_mode === 'plan' || execution.default_chat_mode === 'build'
        ? execution.default_chat_mode
        : 'default',
    executorId: typeof execution.executor_id === 'string' ? execution.executor_id : '',
    executorSelector:
      execution.executor_selector && typeof execution.executor_selector === 'object'
        ? Object.entries(execution.executor_selector as Record<string, unknown>)
            .map(([key, value]) => `${key}=${String(value)}`)
            .join('\n')
        : '',
    additionalExecutors: Array.isArray(execution.additional_executors)
      ? (execution.additional_executors as unknown[])
          .filter((entry): entry is Record<string, unknown> => Boolean(entry && typeof entry === 'object'))
          .map((entry) => ({
            executorId: typeof entry.executor_id === 'string' ? entry.executor_id : '',
            executorSelector:
              entry.executor_selector && typeof entry.executor_selector === 'object'
                ? Object.entries(entry.executor_selector as Record<string, unknown>)
                    .map(([key, value]) => `${key}=${String(value)}`)
                    .join('\n')
                : '',
            description: typeof entry.description === 'string' ? entry.description : ''
          }))
      : [],
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
    disabledMcpServers: Array.isArray(tools.disabled_mcp_servers)
      ? tools.disabled_mcp_servers.filter((value): value is string => typeof value === 'string')
      : [],
    optInBuiltinTools: Array.isArray(tools.opt_in_builtin_tools)
      ? tools.opt_in_builtin_tools.filter((value): value is string => typeof value === 'string')
      : [],
    selectedSkillIds: extractSkillIds(agent.skills),
    autoLoadSkillIds: extractAutoLoadSkillIds(agent.skills),
    originalSkills: agent.skills,
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

function extractAutoLoadSkillIds(skills: Record<string, unknown> | null): string[] {
  if (!skills || typeof skills !== 'object') return [];
  const items = (skills as Record<string, unknown>).items;
  if (!Array.isArray(items)) return [];
  return items
    .filter((item): item is Record<string, unknown> =>
      typeof item === 'object' && item !== null && typeof (item as Record<string, unknown>).skill_id === 'string' && !('tool_names' in (item as Record<string, unknown>))
    )
    .filter((item) => item.enabled !== false && item.auto_load_instructions === true)
    .map((item) => String(item.skill_id));
}

function skillItems(form: AgentFormState): Array<Record<string, unknown> & { skill_id: string; enabled: boolean }> {
  const existingItems =
    form.originalSkills && typeof form.originalSkills === 'object' && Array.isArray(form.originalSkills.items)
      ? form.originalSkills.items.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !('tool_names' in item)))
      : [];
  const autoLoadIds = new Set(form.autoLoadSkillIds);
  const selectedIds = new Set(form.selectedSkillIds);
  const seen = new Set<string>();
  const items: Array<Record<string, unknown> & { skill_id: string; enabled: boolean }> = [];

  for (const item of existingItems) {
    const skillId = typeof item.skill_id === 'string' ? item.skill_id : '';
    if (!skillId || seen.has(skillId)) continue;
    seen.add(skillId);
    if (selectedIds.has(skillId)) {
      const nextItem: Record<string, unknown> & { skill_id: string; enabled: boolean } = {
        ...item,
        skill_id: skillId,
        enabled: true
      };
      if (autoLoadIds.has(skillId)) {
        nextItem.auto_load_instructions = true;
      } else {
        delete nextItem.auto_load_instructions;
      }
      items.push(nextItem);
      continue;
    }
    if (item.enabled === false) {
      items.push({
        ...item,
        skill_id: skillId,
        enabled: false
      } as { skill_id: string; enabled: boolean; auto_load_instructions?: boolean });
    }
  }

  for (const skillId of form.selectedSkillIds) {
    if (seen.has(skillId)) continue;
    seen.add(skillId);
    items.push({
      skill_id: skillId,
      enabled: true,
      ...(autoLoadIds.has(skillId) ? { auto_load_instructions: true } : {})
    });
  }

  return items;
}

function nonEmptyLines(value: string): string[] {
  return value
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export function formStateToPayload(form: AgentFormState): Record<string, unknown> {
  const serializedSkillItems = skillItems(form);
  const toolPermissions = Object.fromEntries(
    Object.entries(form.toolPermissions).filter(([, value]) => value.length > 0)
  );
  const {
    delegation_tools: _legacyDelegationTools,
    disabled_categories: _legacyDisabledCategories,
    disabled_tools: _legacyDisabledTools,
    disabled_mcp_servers: _legacyDisabledMcpServers,
    opt_in_builtin_tools: _legacyOptInBuiltinTools,
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
        allowed_credentials: form.allowedCredentials,
        allowed_knowledgebases: form.allowedKnowledgebases,
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
      ...(form.disabledMcpServers.length > 0
        ? { disabled_mcp_servers: [...new Set(form.disabledMcpServers)] }
        : {}),
      ...(form.agentType === 'primary' && form.optInBuiltinTools.length > 0
        ? { opt_in_builtin_tools: [...new Set(form.optInBuiltinTools)] }
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
    skills: serializedSkillItems.length > 0
      ? {
          items: serializedSkillItems
        }
      : null,
    llm_config: {
      provider_id: form.providerId || undefined,
      model: form.model || undefined,
      temperature: form.temperature ? Number(form.temperature) : undefined,
      max_tokens: form.maxTokens ? Number(form.maxTokens) : undefined,
      reasoning_effort: form.reasoningEffort || undefined,
      voice: form.voice || undefined
    },
    execution: {
      executor_id: form.executorId || undefined,
      executor_selector:
        !form.executorId && Object.keys(executorSelector).length > 0 ? executorSelector : undefined,
      additional_executors:
        form.additionalExecutors.length > 0
          ? form.additionalExecutors
              .map((entry) => {
                const id = entry.executorId.trim();
                const selectorEntries = Object.fromEntries(
                  nonEmptyLines(entry.executorSelector)
                    .map((line) => {
                      const [key, ...rest] = line.split('=');
                      return [key?.trim(), rest.join('=').trim()] as const;
                    })
                    .filter(([key, value]) => Boolean(key) && Boolean(value))
                );
                if (id) {
                  return {
                    executor_id: id,
                    ...(entry.description.trim()
                      ? { description: entry.description.trim() }
                      : {})
                  };
                }
                if (Object.keys(selectorEntries).length > 0) {
                  return {
                    executor_selector: selectorEntries,
                    ...(entry.description.trim()
                      ? { description: entry.description.trim() }
                      : {})
                  };
                }
                return null;
              })
              .filter((entry): entry is NonNullable<typeof entry> => entry !== null)
          : undefined,
      available_workflow_ids: form.availableWorkflowIds,
      default_workflow_id: form.defaultWorkflowId || undefined,
      workflow_selection_mode: form.workflowSelectionMode,
      default_chat_mode: form.defaultChatMode,
      step_agent_overrides: form.stepAgentOverridesJson ? JSON.parse(form.stepAgentOverridesJson) : {}
    }
  };

  return payload;
}

export function formStateToSystemOverridePayload(form: AgentFormState): Record<string, unknown> {
  return {
    skills: {
      items: skillItems(form)
    },
    llm_config: {
      provider_id: form.providerId || undefined,
      model: form.model || undefined,
      temperature: form.temperature ? Number(form.temperature) : undefined,
      max_tokens: form.maxTokens ? Number(form.maxTokens) : undefined,
      reasoning_effort: form.reasoningEffort || undefined,
      voice: form.voice || undefined
    }
  };
}

export function formStateToEffectiveToolsPreviewPayload(form: AgentFormState): Record<string, unknown> {
  const payload = formStateToPayload(form);
  return {
    agent_id: payload.agent_id ?? null,
    agent_type: form.agentType,
    tools: payload.tools,
    permissions: payload.permissions,
    execution: payload.execution,
    skills: payload.skills ?? {}
  };
}

/**
 * Build a preview of the editable identity instructions.
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
