import type { Agent, AgentRuntimeProfile, LLMProvider, SecretMetadata, Skill, ToolDefinitionSummary, Workflow } from '$lib/types/api';
import { DEFAULT_INTARIS_EVALUATION } from '$lib/session-policy';

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

export interface AgentRuntimeProfileFormState {
  profileId: string;
  description: string;
  providerId: string;
  model: string;
  reasoningEffort: string;
  fastMode: 'inherit' | 'enabled' | 'disabled';
  systemPromptExtra: string;
  memoryAvailability: 'inherit' | 'enabled' | 'disabled';
  memoryMode: string;
  memoryBackendOptions: Record<string, unknown>;
  enabled: boolean;
  agentSwitchable: boolean;
  quickSwitch: boolean;
  quickSwitchIndex: number;
  quickSwitchColor?: AgentRuntimeProfile['quick_switch_color'];
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
  delegationEnabled: boolean;
  primaryDelegationEnabled: boolean;
  systemDelegationEnabled: boolean;
  maxManagedDepth: number;
  restrictDelegationTargets: boolean;
  allowedAgentIds: string[];
  restrictControllers: boolean;
  allowedControllerAgentIds: string[];
  toolPermissions: Record<string, string>;
  providerId: string;
  model: string;
  temperature: string;
  maxTokens: string;
  reasoningEffort: string;
  fastMode: 'inherit' | 'enabled' | 'disabled';
  voice: string;
  voices?: Record<string, string>;
  ttsProviderId?: string;
  ttsModel?: string;
  ttsVoice?: string;
  memoryBackend: string;
  memoryMode: string;
  memoryBackendOptions: Record<string, unknown>;
  guardrailsBackend: 'intaris' | 'none';
  analyticsEnabled: boolean;
  intarisEvaluation: 'legacy' | 'strict' | 'balanced' | 'bounded_autonomy';
  minimumOutcome: '' | 'deny' | 'escalate' | 'approve';
  maximumOutcome: '' | 'deny' | 'escalate' | 'approve';
  approvalMode: 'ask' | 'unattended';
  escalationTimeoutSeconds: string;
  agentProfiles: AgentRuntimeProfileFormState[];
  defaultAgentProfileId: string;
  autoProfileSelection: boolean;
  autoProfileMinConfidence: number;
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

const LEGACY_HIDDEN_DISABLED_CATEGORIES = new Set(['mcp']);

function activeDisabledCategories(categories: unknown): string[] {
  if (!Array.isArray(categories)) return [];
  return categories.filter(
    (value): value is string =>
      typeof value === 'string' && !LEGACY_HIDDEN_DISABLED_CATEGORIES.has(value)
  );
}

function normalizeProfileId(value: string): string {
  return value.trim();
}

export interface AgentProfileOption {
  profileId: string;
  label: string;
  description: string;
  isDefault: boolean;
}

export function profileOptionsForAgent(agent: Agent | null | undefined): AgentProfileOption[] {
  const profiles = agent?.agent_profiles;
  if (!profiles || typeof profiles !== 'object') return [];
  const defaultProfileId = typeof agent?.default_agent_profile_id === 'string'
    ? agent.default_agent_profile_id
    : '';
  return Object.entries(profiles)
    .filter(([, profile]) => profile && typeof profile === 'object' && profile.enabled !== false)
    .map(([profileId, profile]) => {
      const normalizedProfileId =
        typeof profile.profile_id === 'string' && profile.profile_id.trim()
          ? profile.profile_id.trim()
          : profileId;
      const description = typeof profile.description === 'string' ? profile.description.trim() : '';
      return {
        profileId: normalizedProfileId,
        label: normalizedProfileId === defaultProfileId
          ? `${normalizedProfileId} (default)`
          : normalizedProfileId,
        description,
        isDefault: normalizedProfileId === defaultProfileId
      };
    })
    .sort((left, right) => left.profileId.localeCompare(right.profileId));
}

export function normalizeSelectedAgentProfileId(
  agent: Agent | null | undefined,
  selectedProfileId: string | null | undefined
): string {
  const profileId = typeof selectedProfileId === 'string' ? selectedProfileId.trim() : '';
  if (!profileId) return '';
  return profileOptionsForAgent(agent).some((option) => option.profileId === profileId)
    ? profileId
    : '';
}

function profileFormStateFromAgentProfiles(
  profiles: Record<string, AgentRuntimeProfile> | undefined
): AgentRuntimeProfileFormState[] {
  if (!profiles || typeof profiles !== 'object') {
    return [];
  }
  return Object.entries(profiles)
    .filter(([, profile]) => profile && typeof profile === 'object')
    .map(([profileId, profile]) => ({
      profileId: typeof profile.profile_id === 'string' && profile.profile_id.trim()
        ? profile.profile_id.trim()
        : profileId,
      description: typeof profile.description === 'string' ? profile.description : '',
      providerId: typeof profile.model === 'string' && profile.model.startsWith('cognis/')
        ? 'cognis' : typeof profile.provider_id === 'string' ? profile.provider_id : '',
      model: typeof profile.model === 'string' ? profile.model : '',
      reasoningEffort: typeof profile.reasoning_effort === 'string' ? profile.reasoning_effort : '',
      fastMode: (
        profile.fast_mode === true ? 'enabled' : profile.fast_mode === false ? 'disabled' : 'inherit'
      ) as AgentRuntimeProfileFormState['fastMode'],
      systemPromptExtra:
        typeof profile.system_prompt_extra === 'string' ? profile.system_prompt_extra : '',
      memoryAvailability: (
        profile.memory_enabled === true
          ? 'enabled'
          : profile.memory_enabled === false
            ? 'disabled'
            : 'inherit'
      ) as AgentRuntimeProfileFormState['memoryAvailability'],
      memoryMode:
        typeof profile.memory_backend_options?.mode === 'string'
          ? profile.memory_backend_options.mode
          : '',
      memoryBackendOptions: { ...(profile.memory_backend_options ?? {}) },
      enabled: profile.enabled !== false,
      agentSwitchable: profile.agent_switchable === true,
      quickSwitch: profile.quick_switch === true,
      quickSwitchIndex: profile.quick_switch_index ?? 0,
      quickSwitchColor: profile.quick_switch_color ?? 'neutral'
    }))
    .sort((left, right) => left.profileId.localeCompare(right.profileId));
}

function serializeAgentProfiles(
  profiles: AgentRuntimeProfileFormState[]
): Record<string, AgentRuntimeProfile> {
  const serialized: Record<string, AgentRuntimeProfile> = {};
  for (const profile of profiles) {
    const profileId = normalizeProfileId(profile.profileId);
    if (!profileId) continue;
    const memoryBackendOptions = { ...profile.memoryBackendOptions };
    delete memoryBackendOptions.mode;
    if (profile.memoryMode) {
      memoryBackendOptions.mode = profile.memoryMode;
    }
    serialized[profileId] = {
      profile_id: profileId,
      description: profile.description.trim(),
      provider_id: profile.providerId === 'cognis' ? null : profile.providerId.trim() || null,
      model: profile.model.trim() || null,
      reasoning_effort: profile.reasoningEffort.trim() || null,
      fast_mode:
        profile.fastMode === 'enabled' ? true : profile.fastMode === 'disabled' ? false : null,
      system_prompt_extra: profile.systemPromptExtra.trim() || null,
      memory_enabled:
        profile.memoryAvailability === 'enabled'
          ? true
          : profile.memoryAvailability === 'disabled'
            ? false
            : null,
      memory_backend_options: memoryBackendOptions,
      enabled: profile.enabled,
      agent_switchable: profile.agentSwitchable,
      quick_switch: profile.quickSwitch,
      quick_switch_index: profile.quickSwitchIndex,
      quick_switch_color: profile.quickSwitchColor ?? 'neutral'
    };
  }
  return serialized;
}

export function createEmptyAgentForm(): AgentFormState {
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
    delegationEnabled: true,
    primaryDelegationEnabled: true,
    systemDelegationEnabled: true,
    maxManagedDepth: 1,
    restrictDelegationTargets: false,
    allowedAgentIds: [],
    restrictControllers: false,
    allowedControllerAgentIds: [],
    toolPermissions: {},
    providerId: '',
    model: '',
    temperature: '',
    maxTokens: '',
    reasoningEffort: '',
    fastMode: 'inherit',
    voice: '',
    voices: {},
    ttsProviderId: '',
    ttsModel: '',
    ttsVoice: '',
    memoryBackend: 'mnemory',
    memoryMode: 'full_auto',
    memoryBackendOptions: {},
    guardrailsBackend: 'intaris',
    analyticsEnabled: true,
    intarisEvaluation: DEFAULT_INTARIS_EVALUATION,
    minimumOutcome: '',
    maximumOutcome: '',
    approvalMode: 'ask',
    escalationTimeoutSeconds: '',
    agentProfiles: [],
    defaultAgentProfileId: '',
    autoProfileSelection: true,
    autoProfileMinConfidence: 0.55,
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
  const delegation =
    permissions.delegation && typeof permissions.delegation === 'object'
      ? (permissions.delegation as Record<string, unknown>)
      : {};
  const primaryDelegation =
    delegation.primary && typeof delegation.primary === 'object'
      ? (delegation.primary as Record<string, unknown>)
      : {};
  const systemDelegation =
    delegation.system && typeof delegation.system === 'object'
      ? (delegation.system as Record<string, unknown>)
      : {};
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
    delegationEnabled:
      typeof delegation.enabled === 'boolean'
        ? delegation.enabled
        : permissions.can_delegate !== false,
    primaryDelegationEnabled: primaryDelegation.enabled !== false,
    systemDelegationEnabled: systemDelegation.enabled !== false,
    maxManagedDepth:
      typeof primaryDelegation.max_managed_depth === 'number'
        ? primaryDelegation.max_managed_depth
        : 1,
    restrictDelegationTargets: Array.isArray(delegation.allowed_agent_ids),
    allowedAgentIds: Array.isArray(delegation.allowed_agent_ids)
      ? delegation.allowed_agent_ids.filter((value): value is string => typeof value === 'string')
      : [],
    restrictControllers: Array.isArray(primaryDelegation.allowed_controller_agent_ids),
    allowedControllerAgentIds: Array.isArray(primaryDelegation.allowed_controller_agent_ids)
      ? primaryDelegation.allowed_controller_agent_ids.filter(
          (value): value is string => typeof value === 'string'
        )
      : [],
    toolPermissions:
      permissions.tool_permissions && typeof permissions.tool_permissions === 'object'
        ? (permissions.tool_permissions as Record<string, string>)
        : {},
    providerId: typeof llmConfig.model === 'string' && llmConfig.model.startsWith('cognis/')
      ? 'cognis' : typeof llmConfig.provider_id === 'string' ? llmConfig.provider_id : '',
    model: typeof llmConfig.model === 'string' ? llmConfig.model : '',
    temperature:
      typeof llmConfig.temperature === 'number' ? String(llmConfig.temperature) : '',
    maxTokens: typeof llmConfig.max_tokens === 'number' ? String(llmConfig.max_tokens) : '',
    reasoningEffort:
      typeof llmConfig.reasoning_effort === 'string' ? llmConfig.reasoning_effort : '',
    fastMode: (
      llmConfig.fast_mode === true ? 'enabled' : llmConfig.fast_mode === false ? 'disabled' : 'inherit'
    ) as AgentFormState['fastMode'],
    voice: typeof llmConfig.voice === 'string' ? llmConfig.voice : '',
    ttsProviderId: typeof llmConfig.tts_provider_id === 'string' ? llmConfig.tts_provider_id : '',
    ttsModel: typeof llmConfig.tts_model === 'string' ? llmConfig.tts_model : '',
    ttsVoice: typeof llmConfig.tts_voice === 'string' ? llmConfig.tts_voice : '',
    voices: llmConfig.voices && typeof llmConfig.voices === 'object'
      ? Object.fromEntries(Object.entries(llmConfig.voices).filter(([, value]) => typeof value === 'string')) as Record<string, string>
      : {},
    memoryBackend:
      typeof agent.capabilities?.memory_backend === 'string'
        ? agent.capabilities.memory_backend
        : 'mnemory',
    memoryMode:
      typeof agent.capabilities?.memory_backend_options?.mode === 'string'
        ? agent.capabilities.memory_backend_options.mode
        : agent.capabilities?.memory_backend === 'mnemory'
          ? 'full_auto'
          : '',
    memoryBackendOptions: { ...(agent.capabilities?.memory_backend_options ?? {}) },
    guardrailsBackend:
      agent.capabilities?.guardrails_backend === 'none' ? 'none' : 'intaris',
    analyticsEnabled: agent.capabilities?.analytics_enabled ?? true,
    minimumOutcome: agent.capabilities?.minimum_outcome ?? '',
    intarisEvaluation: agent.capabilities?.intaris_evaluation ?? DEFAULT_INTARIS_EVALUATION,
    maximumOutcome: agent.capabilities?.maximum_outcome ?? '',
    approvalMode: agent.capabilities?.approval_mode ?? 'ask',
    escalationTimeoutSeconds: agent.capabilities?.escalation_timeout_seconds?.toString() ?? '',
    agentProfiles: profileFormStateFromAgentProfiles(agent.agent_profiles),
    defaultAgentProfileId:
      typeof agent.default_agent_profile_id === 'string' ? agent.default_agent_profile_id : '',
    autoProfileSelection: autoProfileSelectionForAgent(agent).enabled,
    autoProfileMinConfidence: autoProfileSelectionForAgent(agent).min_confidence,
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
    disabledCategories: activeDisabledCategories(tools.disabled_categories),
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
  const agentProfiles = serializeAgentProfiles(form.agentProfiles);
  const agentProfileIds = Object.keys(agentProfiles);
  const defaultAgentProfileId = agentProfileIds.includes(form.defaultAgentProfileId)
    ? form.defaultAgentProfileId
    : agentProfileIds[0] ?? null;
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
  const disabledCategories = [...new Set(activeDisabledCategories(form.disabledCategories))];

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
        can_delegate: form.delegationEnabled,
        delegation: {
          enabled: form.delegationEnabled,
          allowed_agent_ids: form.restrictDelegationTargets
            ? [...new Set(form.allowedAgentIds)]
            : null,
          primary: {
            enabled: form.primaryDelegationEnabled,
            max_managed_depth: form.maxManagedDepth,
            allowed_controller_agent_ids: form.restrictControllers
              ? [...new Set(form.allowedControllerAgentIds)]
              : null
          },
          system: {
            enabled: form.systemDelegationEnabled
          }
        }
      },
    tools: {
      ...preservedTools,
      delegation_tools: form.delegationEnabled,
      ...(disabledCategories.length > 0
        ? { disabled_categories: disabledCategories }
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
      provider_id: form.providerId === 'cognis' ? undefined : form.providerId || undefined,
      model: form.model || undefined,
      temperature: form.temperature ? Number(form.temperature) : undefined,
      max_tokens: form.maxTokens ? Number(form.maxTokens) : undefined,
      reasoning_effort: form.reasoningEffort || undefined,
      fast_mode:
        form.fastMode === 'enabled' ? true : form.fastMode === 'disabled' ? false : undefined,
      voice: form.voice || undefined,
      voices: form.voices ?? {},
      tts_provider_id: form.ttsProviderId || null,
      tts_model: form.ttsModel || null,
      tts_voice: form.ttsVoice || null
    },
    capabilities: {
      memory_backend: form.memoryBackend,
      memory_backend_options: form.memoryBackend === 'none'
        ? {}
        : {
            ...form.memoryBackendOptions,
            ...(form.memoryMode ? { mode: form.memoryMode } : {})
          },
      guardrails_backend: form.guardrailsBackend,
      analytics_enabled: form.analyticsEnabled,
      intaris_evaluation: form.intarisEvaluation,
      approval_mode: form.guardrailsBackend === 'intaris' ? form.approvalMode : 'ask',
      ...(form.guardrailsBackend === 'intaris' && form.minimumOutcome
        ? { minimum_outcome: form.minimumOutcome } : {}),
      ...(form.guardrailsBackend === 'intaris' && form.maximumOutcome
        ? { maximum_outcome: form.maximumOutcome } : {}),
      ...(form.guardrailsBackend === 'intaris' && form.escalationTimeoutSeconds
        ? { escalation_timeout_seconds: Number(form.escalationTimeoutSeconds) }
        : {})
    },
    agent_profiles: agentProfiles,
    default_agent_profile_id: defaultAgentProfileId,
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
      default_chat_mode: form.defaultChatMode,
      auto_profile_selection: {
        enabled: form.autoProfileSelection,
        min_confidence: form.autoProfileMinConfidence,
      },
      step_agent_overrides: form.stepAgentOverridesJson ? JSON.parse(form.stepAgentOverridesJson) : {}
    }
  };

  return payload;
}

export function autoProfileSelectionForAgent(agent: Agent | null | undefined): {
  enabled: boolean;
  min_confidence: number;
} {
  const raw = agent?.execution?.auto_profile_selection;
  const settings = raw && typeof raw === 'object' && !Array.isArray(raw)
    ? raw as Record<string, unknown> : {};
  return {
    enabled: settings.enabled !== false,
    min_confidence: typeof settings.min_confidence === 'number' ? settings.min_confidence : 0.55,
  };
}

export function formStateToSystemOverridePayload(form: AgentFormState): Record<string, unknown> {
  const fullPayload = formStateToPayload(form);
  return {
    tools: fullPayload.tools,
    permissions: fullPayload.permissions,
    skills: {
      items: skillItems(form)
    },
    llm_config: {
      provider_id: form.providerId === 'cognis' ? undefined : form.providerId || undefined,
      model: form.model || undefined,
      temperature: form.temperature ? Number(form.temperature) : undefined,
      max_tokens: form.maxTokens ? Number(form.maxTokens) : undefined,
      reasoning_effort: form.reasoningEffort || undefined,
      fast_mode:
        form.fastMode === 'enabled' ? true : form.fastMode === 'disabled' ? false : undefined,
      tts_provider_id: form.ttsProviderId || null,
      tts_model: form.ttsModel || null,
      tts_voice: form.ttsVoice || null
    },
    agent_profiles: fullPayload.agent_profiles,
    default_agent_profile_id: fullPayload.default_agent_profile_id
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
