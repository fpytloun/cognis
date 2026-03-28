import type { Agent, LLMProvider, ToolDefinitionSummary, Workflow } from '$lib/types/api';

export interface AgentFormState {
  agentId: string;
  name: string;
  displayName: string;
  description: string;
  avatarUrl: string;
  systemPrompt: string;
  tone: string;
  temperament: string;
  purpose: string;
  behavioralRules: string;
  allowedSecrets: string;
  canDelegate: boolean;
  maxDelegationDepth: number;
  toolPermissions: Record<string, string>;
  providerId: string;
  model: string;
  temperature: string;
  maxTokens: string;
  availableWorkflowIds: string[];
  defaultWorkflowId: string;
  workflowSelectionMode: string;
  stepAgentOverridesJson: string;
}

export function createEmptyAgentForm(): AgentFormState {
  return {
    agentId: '',
    name: '',
    displayName: '',
    description: '',
    avatarUrl: '',
    systemPrompt: '',
    tone: '',
    temperament: '',
    purpose: '',
    behavioralRules: '',
    allowedSecrets: '',
    canDelegate: true,
    maxDelegationDepth: 3,
    toolPermissions: {},
    providerId: '',
    model: '',
    temperature: '',
    maxTokens: '',
    availableWorkflowIds: [],
    defaultWorkflowId: '',
    workflowSelectionMode: 'automatic',
    stepAgentOverridesJson: '{}'
  };
}

export function agentToFormState(agent: Agent): AgentFormState {
  const form = createEmptyAgentForm();
  const personality = agent.personality ?? {};
  const permissions = agent.permissions ?? {};
  const llmConfig = agent.llm_config ?? {};
  const execution = agent.execution ?? {};

  return {
    ...form,
    agentId: agent.agent_id,
    name: agent.name,
    displayName: agent.display_name ?? '',
    description: agent.description ?? '',
    avatarUrl: agent.avatar_url ?? '',
    systemPrompt: agent.system_prompt ?? '',
    tone: typeof personality.tone === 'string' ? personality.tone : '',
    temperament: typeof personality.temperament === 'string' ? personality.temperament : '',
    purpose: typeof personality.purpose === 'string' ? personality.purpose : '',
    behavioralRules: Array.isArray(personality.behavioral_rules)
      ? personality.behavioral_rules.join('\n')
      : '',
    allowedSecrets: Array.isArray(permissions.allowed_secrets) ? permissions.allowed_secrets.join(', ') : '',
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
    availableWorkflowIds: Array.isArray(execution.available_workflow_ids)
      ? execution.available_workflow_ids.filter((value): value is string => typeof value === 'string')
      : [],
    defaultWorkflowId:
      typeof execution.default_workflow_id === 'string' ? execution.default_workflow_id : '',
    workflowSelectionMode:
      typeof execution.workflow_selection_mode === 'string'
        ? execution.workflow_selection_mode
        : 'automatic',
    stepAgentOverridesJson: JSON.stringify(execution.step_agent_overrides ?? {}, null, 2)
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
    agent_id: form.agentId,
    name: form.name,
    display_name: form.displayName || null,
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
      allowed_secrets: form.allowedSecrets
        .split(',')
        .map((value) => value.trim())
        .filter(Boolean),
      can_delegate: form.canDelegate,
      max_delegation_depth: form.maxDelegationDepth
    },
    llm_config: {
      provider_id: form.providerId || undefined,
      model: form.model || undefined,
      temperature: form.temperature ? Number(form.temperature) : undefined,
      max_tokens: form.maxTokens ? Number(form.maxTokens) : undefined
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
    `Name: ${form.displayName || form.name || 'Unnamed Agent'}`,
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
