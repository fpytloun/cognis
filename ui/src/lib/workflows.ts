import YAML from 'yaml';

import { GENERIC_THINKING_EFFORTS } from '$lib/thinking';
import type { StepProfileDefinition, Workflow } from '$lib/types/api';
import { isRecord } from '$lib/utils';

type OutcomeAction = 'none' | 'fail' | 'gate' | 'continue' | 'cancel' | 'revise';

export const STEP_PROFILE_CAPABILITIES = ['read', 'write', 'privileged', 'destructive'] as const;
export const STEP_PROFILE_GROUPS = [
  'filesystem',
  'shell',
  'web',
  'browser',
  'development',
  'office',
  'personal',
  'communication',
  'memory',
  'system'
] as const;

export interface WorkflowStepProfileRowFormState {
  category: string;
  capabilities: string[];
}

export interface WorkflowStepFormState {
  name: string;
  type: 'run' | 'gate' | 'tool_call' | 'condition' | 'complete';
  phaseId: string;
  prompt: string;
  objective: string;
  responsibilitiesText: string;
  deferToText: string;
  agentOverride: string;
  agentProfileId: string;
  reasoningEffort: string;
  requireDeliverable: boolean;
  stepProfileId: string;
  stepProfileMode: 'soft' | 'hard';
  stepProfileBaseMode: 'soft' | 'hard';
  stepProfileAllowToolSearch: boolean;
  stepProfileBaseAllowToolSearch: boolean;
  stepProfileMatrix: WorkflowStepProfileRowFormState[];
  stepProfileBaseMatrix: WorkflowStepProfileRowFormState[];
  stepProfileIncludeText: string;
  stepProfileExcludeText: string;
  stepProfileBaseIncludeText: string;
  stepProfileBaseExcludeText: string;
  inputMode: 'auto' | 'null' | 'last' | 'full' | 'summary';
  inputText: string;
  reuseSessionFrom: string;
  allowQuestions: boolean;
  evaluate: boolean;
  maxAttempts: number;
  onExhausted: string;
  gateMessage: string;
  gateOptionsText: string;
  gateInputText: string;
  gateConditionsText: string;
  gateThresholdsText: string;
  gateTimeoutSeconds: number;
  gateTimeoutAction: 'fail' | 'continue' | 'cancel';
  evaluatorRejectTarget: string;
  evaluatorRejectMaxLoops: number;
  evaluatorRejectOnExhausted: string;
  outcomeSuccessAction: OutcomeAction;
  outcomeSuccessTarget: string;
  outcomeSuccessMaxLoops: number;
  outcomeSuccessOnExhausted: string;
  outcomeRejectedAction: OutcomeAction;
  outcomeRejectedTarget: string;
  outcomeRejectedMaxLoops: number;
  outcomeRejectedOnExhausted: string;
  outcomeFailedAction: OutcomeAction;
  outcomeFailedTarget: string;
  outcomeFailedMaxLoops: number;
  outcomeFailedOnExhausted: string;
  toolName: string;
  toolArgsText: string;
  toolSummary: string;
  toolOutputsText: string;
  toolFailOnError: boolean;
  toolTimeoutSeconds: number;
  toolAllowSideEffects: boolean;
  toolRedactArgsText: string;
  deterministicWhen: string;
  deterministicOnSkipText: string;
  deterministicOnError: '' | 'fail' | 'continue' | 'skip' | 'gate';
  deterministicNext: string;
  conditionExpression: string;
  conditionThen: string;
  conditionElse: string;
  conditionOutputText: string;
  conditionRevisionSource: string;
  conditionMaxLoopIterations: number | null;
  conditionOnExhausted: 'continue' | 'fail' | 'gate';
  completeStatus: 'completed' | 'failed';
  completeSummary: string;
  completeContent: string;
  completeOutputsText: string;
  completeNotificationText: string;
  completeDeliveryMode: string;
}

export interface WorkflowPhaseFormState {
  id: string;
  title: string;
  description: string;
}

export interface WorkflowFormState {
  workflowId: string;
  name: string;
  description: string;
  version: number;
  criteria: string;
  tagsText: string;
  lifecycle: 'persistent' | 'ephemeral';
  lineage: Record<string, unknown> | null;
  interactionMode: string;
  defaultEvaluate: boolean;
  defaultMaxAttempts: number;
  defaultOnExhausted: string;
  defaultCompletionModeFamily: 'default' | 'direct';
  defaultAllowSilentCompletion: boolean;
  allowPolicyText: string;
  denyPolicyText: string;
  steps: WorkflowStepFormState[];
  phases: WorkflowPhaseFormState[];
  presentationEdited: boolean;
}

export type WorkflowInspectorGroup =
  | 'basics'
  | 'agent-runtime'
  | 'context-session'
  | 'tools'
  | 'routing-review'
  | 'completion-evaluation'
  | 'advanced';

export function workflowStepSummary(step: WorkflowStepFormState): string {
  if (step.type === 'tool_call') return step.toolName || 'Choose a deterministic tool';
  if (step.type === 'condition') return step.conditionExpression || 'Configure branch expression';
  if (step.type === 'complete') return step.completeSummary || 'Configure terminal result';
  if (step.type === 'gate') return step.gateMessage || 'Configure approval message';
  return step.objective || step.prompt.replace(/\s+/g, ' ').trim() || 'Configure agent instructions';
}

export function workflowIssueGroup(issue: string): WorkflowInspectorGroup {
  const normalized = issue.toLowerCase();
  if (normalized.includes('tool call') || normalized.includes('json object')) return 'tools';
  if (normalized.includes('input') || normalized.includes('reuse') || normalized.includes('session')) return 'context-session';
  if (normalized.includes('target') || normalized.includes('route') || normalized.includes('branch')) return 'routing-review';
  if (normalized.includes('complete step') || normalized.includes('gate message') || normalized.includes('evaluator')) {
    return 'completion-evaluation';
  }
  if (normalized.includes('agent') || normalized.includes('profile')) return 'agent-runtime';
  if (normalized.includes('name') || normalized.includes('phase')) return 'basics';
  return 'advanced';
}

export function createEmptyStep(): WorkflowStepFormState {
  return {
    name: '',
    type: 'run',
    phaseId: 'main',
    prompt: '',
    objective: '',
    responsibilitiesText: '',
    deferToText: '',
    agentOverride: '',
    agentProfileId: '',
    reasoningEffort: '',
    requireDeliverable: true,
    stepProfileId: '',
    stepProfileMode: 'soft',
    stepProfileBaseMode: 'soft',
    stepProfileAllowToolSearch: true,
    stepProfileBaseAllowToolSearch: true,
    stepProfileMatrix: [],
    stepProfileBaseMatrix: [],
    stepProfileIncludeText: '',
    stepProfileExcludeText: '',
    stepProfileBaseIncludeText: '',
    stepProfileBaseExcludeText: '',
    inputMode: 'null',
    inputText: '',
    reuseSessionFrom: '',
    allowQuestions: false,
    evaluate: true,
    maxAttempts: 3,
    onExhausted: 'gate',
    gateMessage: '',
    gateOptionsText: '',
    gateInputText: '',
    gateConditionsText: '[]',
    gateThresholdsText: '{}',
    gateTimeoutSeconds: 3600,
    gateTimeoutAction: 'fail',
    evaluatorRejectTarget: '',
    evaluatorRejectMaxLoops: 2,
    evaluatorRejectOnExhausted: 'gate',
    outcomeSuccessAction: 'none',
    outcomeSuccessTarget: '',
    outcomeSuccessMaxLoops: 2,
    outcomeSuccessOnExhausted: 'gate',
    outcomeRejectedAction: 'none',
    outcomeRejectedTarget: '',
    outcomeRejectedMaxLoops: 2,
    outcomeRejectedOnExhausted: 'gate',
    outcomeFailedAction: 'none',
    outcomeFailedTarget: '',
    outcomeFailedMaxLoops: 2,
    outcomeFailedOnExhausted: 'gate',
    toolName: '',
    toolArgsText: '{}',
    toolSummary: '',
    toolOutputsText: '{}',
    toolFailOnError: true,
    toolTimeoutSeconds: 0,
    toolAllowSideEffects: false,
    toolRedactArgsText: '',
    deterministicWhen: '',
    deterministicOnSkipText: '',
    deterministicOnError: '',
    deterministicNext: '',
    conditionExpression: '',
    conditionThen: '',
    conditionElse: '',
    conditionOutputText: '',
    conditionRevisionSource: '',
    conditionMaxLoopIterations: null,
    conditionOnExhausted: 'gate',
    completeStatus: 'completed',
    completeSummary: '',
    completeContent: '',
    completeOutputsText: '{}',
    completeNotificationText: '',
    completeDeliveryMode: ''
  };
}

export function buildStepProfileMap(
  profiles: readonly StepProfileDefinition[]
): Record<string, StepProfileDefinition> {
  return Object.fromEntries(profiles.map((profile) => [profile.profile_id, profile]));
}

export function createEmptyWorkflowForm(): WorkflowFormState {
  return {
    workflowId: '',
    name: '',
    description: '',
    version: 1,
    criteria: '',
    tagsText: '',
    lifecycle: 'persistent',
    lineage: null,
    interactionMode: 'explicit_gates',
    defaultEvaluate: true,
    defaultMaxAttempts: 3,
    defaultOnExhausted: 'gate',
    defaultCompletionModeFamily: 'default',
    defaultAllowSilentCompletion: false,
    allowPolicyText: '',
    denyPolicyText: '',
    steps: [createEmptyStep()],
    phases: [{ id: 'main', title: 'Workflow', description: '' }],
    presentationEdited: true
  };
}

function stringifyObject(value: unknown, fallback = '{}'): string {
  return isRecord(value) ? JSON.stringify(value, null, 2) : fallback;
}

function stringifyArray(value: unknown, fallback = '[]'): string {
  return Array.isArray(value) ? JSON.stringify(value, null, 2) : fallback;
}

function parseObject(text: string, label: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (!trimmed) return {};
  const parsed = JSON.parse(trimmed) as unknown;
  if (!isRecord(parsed)) throw new Error(`${label} must be a JSON object.`);
  return parsed;
}

function parseRecordArray(text: string, label: string): Array<Record<string, unknown>> {
  const trimmed = text.trim();
  if (!trimmed) return [];
  const parsed = JSON.parse(trimmed) as unknown;
  if (!Array.isArray(parsed) || !parsed.every(isRecord)) {
    throw new Error(`${label} must be a JSON array of objects.`);
  }
  return parsed;
}

export function workflowThinkingEfforts(): string[] {
  return [...GENERIC_THINKING_EFFORTS].filter((value) => value !== 'default');
}

function joinOptions(options: Array<Record<string, unknown>>): string {
  return options
    .map((option) => `${String(option.label ?? '')}|${String(option.action ?? '')}|${option.prompt === true ? 'true' : 'false'}`)
    .join('\n');
}

function workflowInputSourceNames(input: Workflow['steps'][number]['input']): string[] {
  if (!input) return [];
  if (typeof input === 'string') return [input];
  if (Array.isArray(input)) return input.filter((item): item is string => typeof item === 'string');
  if (typeof input.source === 'string') return [input.source];
  if (Array.isArray(input.source)) {
    return input.source.filter((item): item is string => typeof item === 'string');
  }
  return [];
}

function workflowInputMode(input: Workflow['steps'][number]['input']): WorkflowStepFormState['inputMode'] {
  if (input == null) return 'auto';
  if (typeof input === 'string' || Array.isArray(input)) return 'last';
  if (input.type === 'full' || input.type === 'summary' || input.type === 'last' || input.type === 'null') {
    return input.type;
  }
  return 'auto';
}

function formInputToPayload(
  inputMode: WorkflowStepFormState['inputMode'],
  inputText: string,
  reuseSessionFrom: string
): { type: string; source?: string | string[]; reuse_session_from?: string } | undefined {
  const refs = parseList(inputText);
  const reuse = reuseSessionFrom.trim() || undefined;
  if (inputMode === 'auto') return undefined;
  if (inputMode === 'null' || refs.length === 0) {
    return inputMode === 'null' ? { type: 'null' } : { type: inputMode, reuse_session_from: reuse };
  }
  if (inputMode === 'full') return { type: 'full', source: refs[0], reuse_session_from: reuse };
  if (refs.length === 1) return { type: inputMode, source: refs[0], reuse_session_from: reuse };
  return { type: inputMode, source: refs, reuse_session_from: reuse };
}

function parseList(value: string): string[] {
  return value.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);
}

function policyText(defaults: Record<string, unknown>, key: 'allow_policies' | 'deny_policies'): string {
  const policy = defaults.session_policy;
  if (!isRecord(policy)) return '';
  const values = policy[key];
  if (!Array.isArray(values)) return '';
  return values
    .filter((item) => typeof item === 'string' || isRecord(item))
    .map((item) => (typeof item === 'string' ? item : JSON.stringify(item)))
    .join('\n');
}

function policyFromText(allowText: string, denyText: string): Record<string, unknown> {
  return {
    allow_policies: parsePolicyLines(allowText),
    deny_policies: parsePolicyLines(denyText)
  };
}

function parsePolicyLines(text: string): Array<string | Record<string, unknown>> {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      if (!line.startsWith('{')) return line;
      try {
        const parsed = JSON.parse(line) as unknown;
        return isRecord(parsed) ? parsed : line;
      } catch {
        return line;
      }
    });
}

function parseProfileMatrix(value: Workflow['steps'][number]['step_profile']): WorkflowStepProfileRowFormState[] {
  const matrix = isRecord(value?.matrix) ? value.matrix : {};
  return Object.entries(matrix).map(([category, capabilities]) => ({
    category,
    capabilities: Array.isArray(capabilities)
      ? capabilities.filter((item): item is string => typeof item === 'string')
      : []
  }));
}

function cloneProfileRows(rows: WorkflowStepProfileRowFormState[]): WorkflowStepProfileRowFormState[] {
  return rows.map((row) => ({ category: row.category, capabilities: [...row.capabilities] }));
}

function mergeProfileRows(
  baseRows: WorkflowStepProfileRowFormState[],
  overrideRows: WorkflowStepProfileRowFormState[]
): WorkflowStepProfileRowFormState[] {
  const merged = new Map(baseRows.map((row) => [row.category, [...row.capabilities]]));
  overrideRows.forEach((row) => {
    merged.set(row.category, [...row.capabilities]);
  });
  return [...merged.entries()]
    .map(([category, capabilities]) => ({ category, capabilities }))
    .sort((a, b) => a.category.localeCompare(b.category));
}

function normalizeProfileRows(rows: WorkflowStepProfileRowFormState[]): WorkflowStepProfileRowFormState[] {
  return cloneProfileRows(rows)
    .map((row) => ({
      category: row.category.trim(),
      capabilities: [...new Set(row.capabilities.map((item) => item.trim()).filter(Boolean))].sort()
    }))
    .filter((row) => row.category && row.capabilities.length > 0)
    .sort((a, b) => a.category.localeCompare(b.category));
}

function rowsEqual(a: WorkflowStepProfileRowFormState[], b: WorkflowStepProfileRowFormState[]): boolean {
  const left = JSON.stringify(normalizeProfileRows(a));
  const right = JSON.stringify(normalizeProfileRows(b));
  return left === right;
}

function profileOverrideList(
  value: Workflow['steps'][number]['step_profile'],
  key: 'include' | 'exclude'
): string {
  const overrides = isRecord(value?.tool_overrides) ? value.tool_overrides : null;
  const items = Array.isArray(overrides?.[key])
    ? overrides[key].filter((item): item is string => typeof item === 'string')
    : [];
  return items.join(', ');
}

function resolveStepProfilePreset(
  step: Workflow['steps'][number],
  profileMap: Record<string, StepProfileDefinition>
): {
  baseRows: WorkflowStepProfileRowFormState[];
  effectiveRows: WorkflowStepProfileRowFormState[];
  baseAllowToolSearch: boolean;
  effectiveAllowToolSearch: boolean;
  baseMode: 'soft' | 'hard';
  effectiveMode: 'soft' | 'hard';
} {
  const preset = typeof step.step_profile_id === 'string' ? profileMap[step.step_profile_id] : undefined;
  const presetRows = parseProfileMatrix(preset?.config);
  const overrideRows = parseProfileMatrix(step.step_profile);
  const baseAllowToolSearch = preset?.config?.allow_tool_search !== false;
  const effectiveAllowToolSearch = step.step_profile?.allow_tool_search ?? baseAllowToolSearch;
  const baseMode = preset?.mode === 'hard' ? 'hard' : 'soft';
  const effectiveMode = step.step_profile_mode === 'hard' ? 'hard' : baseMode;
  return {
    baseRows: normalizeProfileRows(presetRows),
    effectiveRows: normalizeProfileRows(mergeProfileRows(presetRows, overrideRows)),
    baseAllowToolSearch,
    effectiveAllowToolSearch,
    baseMode,
    effectiveMode
  };
}

function formProfileToPayload(step: WorkflowStepFormState): Workflow['steps'][number]['step_profile'] | undefined {
  const normalizedCurrent = normalizeProfileRows(step.stepProfileMatrix);
  const normalizedBase = normalizeProfileRows(step.stepProfileBaseMatrix);
  const baseByCategory = new Map(normalizedBase.map((row) => [row.category, row.capabilities]));
  const deltaEntries: Array<[string, string[]]> = [];
  normalizedCurrent.forEach((row) => {
    const baseCapabilities = baseByCategory.get(row.category);
    if (JSON.stringify(baseCapabilities ?? []) !== JSON.stringify(row.capabilities)) {
      deltaEntries.push([row.category, row.capabilities]);
    }
  });
  normalizedBase.forEach((row) => {
    if (!normalizedCurrent.some((candidate) => candidate.category === row.category)) {
      deltaEntries.push([row.category, []]);
    }
  });
  const matrix = Object.fromEntries(deltaEntries);
  const include = parseList(step.stepProfileIncludeText);
  const exclude = parseList(step.stepProfileExcludeText);
  const baseInclude = parseList(step.stepProfileBaseIncludeText);
  const baseExclude = parseList(step.stepProfileBaseExcludeText);
  const sameMatrix = rowsEqual(normalizedCurrent, normalizedBase);
  const sameInclude = JSON.stringify(include) === JSON.stringify(baseInclude);
  const sameExclude = JSON.stringify(exclude) === JSON.stringify(baseExclude);
  const sameSearch = step.stepProfileAllowToolSearch === step.stepProfileBaseAllowToolSearch;
  if (
    sameMatrix &&
    sameInclude &&
    sameExclude &&
    sameSearch
  ) {
    return undefined;
  }
  return {
    ...(deltaEntries.length > 0 ? { matrix } : {}),
    tool_overrides: {
      include,
      exclude
    },
    allow_tool_search: step.stepProfileAllowToolSearch
  };
}

function parseGateOptions(value: string): Array<{ label: string; action: string; prompt: boolean }> {
  return value
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [label, action, prompt] = line.split('|').map((item) => item.trim());
      return {
        label: label || action,
        action: action || label,
        prompt: (prompt ?? '').toLowerCase() === 'true'
      };
    });
}

function outcomeRouteForStatus(
  step: Workflow['steps'][number],
  status: 'success' | 'rejected' | 'failed'
): Record<string, unknown> | null {
  if (!Array.isArray(step.outcome_routes)) return null;
  const route = step.outcome_routes.find((candidate) => isRecord(candidate) && candidate.status === status);
  return isRecord(route) ? route : null;
}

function reviseTargetFromAction(action: unknown): string {
  if (typeof action !== 'string') return '';
  const match = /^revise\((.+)\)$/.exec(action.trim());
  return match?.[1] ?? '';
}

function routeAction(action: unknown): OutcomeAction {
  const reviseTarget = reviseTargetFromAction(action);
  if (reviseTarget !== '') return 'revise';
  if (action === 'gate' || action === 'continue' || action === 'cancel' || action === 'fail') {
    return action;
  }
  return 'none';
}

function routeTarget(action: unknown): string {
  return reviseTargetFromAction(action);
}

function routeLoopCount(route: Record<string, unknown> | null): number {
  return typeof route?.max_loop_iterations === 'number' ? route.max_loop_iterations : 2;
}

function routeOnExhausted(route: Record<string, unknown> | null): string {
  return typeof route?.on_exhausted === 'string' ? route.on_exhausted : 'gate';
}

function pushOutcomeRoute(
  routes: Array<Record<string, unknown>>,
  config: {
    status: 'success' | 'rejected' | 'failed';
    action: OutcomeAction;
    target: string;
    maxLoops: number;
    onExhausted: string;
  }
): void {
  const { status, action, target, maxLoops, onExhausted } = config;
  if (action === 'none') return;
  if (action === 'revise') {
    if (!target.trim()) return;
    routes.push({
      status,
      action: `revise(${target.trim()})`,
      max_loop_iterations: Number(maxLoops),
      on_exhausted: onExhausted
    });
    return;
  }
  routes.push({ status, action });
}

export function workflowToFormState(
  workflow: Workflow,
  profileMap: Record<string, StepProfileDefinition> = {}
): WorkflowFormState {
  const workflowDefaults = isRecord(workflow.defaults) ? workflow.defaults : {};
  const interaction = isRecord(workflow.interaction) ? workflow.interaction : {};
  const deliveryDefaults = isRecord(workflowDefaults.delivery) ? workflowDefaults.delivery : null;
  const explicitPhases = workflow.presentation?.phases ?? [];
  const phases = explicitPhases.length > 0
    ? explicitPhases.map(({ id, title, description }) => ({ id, title, description: description ?? '' }))
    : [{ id: 'main', title: workflow.name || 'Workflow', description: '' }];
  const phaseByStep = new Map(
    explicitPhases.flatMap((phase) => phase.step_names.map((stepName) => [stepName, phase.id] as const))
  );
  return {
    workflowId: workflow.workflow_id,
    name: workflow.name,
    description: workflow.description,
    version: workflow.version,
    criteria: workflow.criteria,
    tagsText: workflow.tags.join(', '),
    lifecycle: workflow.lifecycle === 'ephemeral' ? 'ephemeral' : 'persistent',
    lineage: workflow.lineage ?? null,
    interactionMode: typeof interaction.mode === 'string' ? interaction.mode : 'explicit_gates',
    defaultEvaluate: workflowDefaults.evaluate !== false,
    defaultMaxAttempts: typeof workflowDefaults.max_attempts === 'number' ? workflowDefaults.max_attempts : 3,
    defaultOnExhausted: typeof workflowDefaults.on_exhausted === 'string' ? workflowDefaults.on_exhausted : 'gate',
    defaultCompletionModeFamily:
      deliveryDefaults?.completion_mode_family === 'direct' ? 'direct' : 'default',
    defaultAllowSilentCompletion: deliveryDefaults?.allow_silent_completion === true,
    allowPolicyText: policyText(workflowDefaults, 'allow_policies'),
    denyPolicyText: policyText(workflowDefaults, 'deny_policies'),
    steps: workflow.steps.map((step) => {
      const successRoute = outcomeRouteForStatus(step, 'success');
      const rejectedRoute = outcomeRouteForStatus(step, 'rejected');
      const failedRoute = outcomeRouteForStatus(step, 'failed');
      const stepProfile = resolveStepProfilePreset(step, profileMap);
      return {
        name: step.name,
        type: (['run', 'gate', 'tool_call', 'condition', 'complete'].includes(step.type)
          ? step.type
          : 'run') as WorkflowStepFormState['type'],
        phaseId: phaseByStep.get(step.name) ?? phases[0].id,
        prompt: step.prompt ?? '',
        objective: step.objective ?? '',
        responsibilitiesText: (step.responsibilities ?? []).join('\n'),
        deferToText: (step.defer_to ?? []).join('\n'),
        agentOverride: step.agent_override ?? '',
        agentProfileId: typeof step.agent_profile_id === 'string' ? step.agent_profile_id : '',
        reasoningEffort: typeof step.reasoning_effort === 'string' ? step.reasoning_effort : '',
        requireDeliverable: step.require_deliverable !== false,
        stepProfileId: typeof step.step_profile_id === 'string' ? step.step_profile_id : '',
        stepProfileMode: stepProfile.effectiveMode,
        stepProfileBaseMode: stepProfile.baseMode,
        stepProfileAllowToolSearch: stepProfile.effectiveAllowToolSearch,
        stepProfileBaseAllowToolSearch: stepProfile.baseAllowToolSearch,
        stepProfileMatrix: stepProfile.effectiveRows,
        stepProfileBaseMatrix: stepProfile.baseRows,
        stepProfileIncludeText: profileOverrideList(step.step_profile, 'include'),
        stepProfileExcludeText: profileOverrideList(step.step_profile, 'exclude'),
        stepProfileBaseIncludeText: profileOverrideList(profileMap[step.step_profile_id ?? '']?.config, 'include'),
        stepProfileBaseExcludeText: profileOverrideList(profileMap[step.step_profile_id ?? '']?.config, 'exclude'),
        inputMode: workflowInputMode(step.input),
        inputText: workflowInputSourceNames(step.input).join(', '),
        reuseSessionFrom:
          step.input && typeof step.input === 'object' && !Array.isArray(step.input)
            ? step.input.reuse_session_from ?? ''
            : '',
        allowQuestions: step.allow_questions ?? false,
        evaluate: step.completion?.evaluate !== false,
        maxAttempts: typeof step.completion?.max_attempts === 'number' ? step.completion.max_attempts : 3,
        onExhausted: typeof step.completion?.on_exhausted === 'string' ? step.completion.on_exhausted : 'gate',
        gateMessage: typeof step.gate?.message === 'string' ? step.gate.message : '',
        gateOptionsText: Array.isArray(step.gate?.options) ? joinOptions(step.gate.options as Array<Record<string, unknown>>) : '',
        gateInputText: Array.isArray(step.gate?.input)
          ? step.gate.input.filter((item): item is string => typeof item === 'string').join(', ')
          : '',
        gateConditionsText: stringifyArray(step.gate?.conditions),
        gateThresholdsText: stringifyObject(step.gate?.thresholds),
        gateTimeoutSeconds: typeof step.gate?.timeout_seconds === 'number' ? step.gate.timeout_seconds : 3600,
        gateTimeoutAction:
          step.gate?.timeout_action === 'continue' || step.gate?.timeout_action === 'cancel'
            ? step.gate.timeout_action
            : 'fail',
        evaluatorRejectTarget: typeof step.on_reject?.target === 'string' ? step.on_reject.target : '',
        evaluatorRejectMaxLoops: typeof step.on_reject?.max_loop_iterations === 'number' ? step.on_reject.max_loop_iterations : 2,
        evaluatorRejectOnExhausted: typeof step.on_reject?.on_exhausted === 'string' ? step.on_reject.on_exhausted : 'gate',
        outcomeSuccessAction: routeAction(successRoute?.action),
        outcomeSuccessTarget: routeTarget(successRoute?.action),
        outcomeSuccessMaxLoops: routeLoopCount(successRoute),
        outcomeSuccessOnExhausted: routeOnExhausted(successRoute),
        outcomeRejectedAction: routeAction(rejectedRoute?.action),
        outcomeRejectedTarget: routeTarget(rejectedRoute?.action),
        outcomeRejectedMaxLoops: routeLoopCount(rejectedRoute),
        outcomeRejectedOnExhausted: routeOnExhausted(rejectedRoute),
        outcomeFailedAction: routeAction(failedRoute?.action),
        outcomeFailedTarget: routeTarget(failedRoute?.action),
        outcomeFailedMaxLoops: routeLoopCount(failedRoute),
        outcomeFailedOnExhausted: routeOnExhausted(failedRoute),
        toolName: step.tool_call?.tool ?? '',
        toolArgsText: stringifyObject(step.tool_call?.args),
        toolSummary: step.tool_call?.summary ?? '',
        toolOutputsText: stringifyObject(step.tool_call?.outputs),
        toolFailOnError: step.tool_call?.fail_on_error !== false,
        toolTimeoutSeconds: step.tool_call?.timeout_seconds ?? 0,
        toolAllowSideEffects: step.tool_call?.allow_side_effects === true,
        toolRedactArgsText: step.tool_call?.redact_args?.join(', ') ?? '',
        deterministicWhen: step.when ?? '',
        deterministicOnSkipText: step.on_skip ? stringifyObject(step.on_skip) : '',
        deterministicOnError:
          step.on_error === 'fail' ||
          step.on_error === 'continue' ||
          step.on_error === 'skip' ||
          step.on_error === 'gate'
            ? step.on_error
            : '',
        deterministicNext: step.next ?? '',
        conditionExpression: step.condition?.if ?? '',
        conditionThen: step.condition?.then ?? '',
        conditionElse: step.condition?.else ?? '',
        conditionOutputText: step.condition?.output ? stringifyObject(step.condition.output) : '',
        conditionRevisionSource: step.condition?.revision_source ?? '',
        conditionMaxLoopIterations:
          typeof step.condition?.max_loop_iterations === 'number'
            ? step.condition.max_loop_iterations
            : null,
        conditionOnExhausted:
          step.condition?.on_exhausted === 'continue' ||
          step.condition?.on_exhausted === 'fail'
            ? step.condition.on_exhausted
            : 'gate',
        completeStatus: step.complete?.status === 'failed' ? 'failed' : 'completed',
        completeSummary: step.complete?.summary ?? '',
        completeContent: step.complete?.content ?? '',
        completeOutputsText: stringifyObject(step.complete?.outputs),
        completeNotificationText: step.complete?.notification
          ? stringifyObject(step.complete.notification)
          : '',
        completeDeliveryMode: step.complete?.delivery_mode_override ?? ''
      };
    }),
    phases,
    presentationEdited: explicitPhases.length > 0
  };
}

export function formStateToWorkflowPayload(form: WorkflowFormState): Record<string, unknown> {
  return {
    workflow_id: form.workflowId || undefined,
    name: form.name,
    description: form.description,
    version: Number(form.version),
    criteria: form.criteria,
    tags: parseList(form.tagsText),
    lifecycle: form.lifecycle,
    lineage: form.lineage,
    interaction: { mode: form.interactionMode },
    defaults: {
      evaluate: form.defaultEvaluate,
      max_attempts: Number(form.defaultMaxAttempts),
      on_exhausted: form.defaultOnExhausted,
      delivery: {
        completion_mode_family: form.defaultCompletionModeFamily,
        allow_silent_completion: form.defaultAllowSilentCompletion
      },
      session_policy: policyFromText(form.allowPolicyText, form.denyPolicyText)
    },
    ...(form.presentationEdited
      ? {
          presentation: {
            phases: form.phases.map((phase) => ({
              id: phase.id,
              title: phase.title,
              description: phase.description,
              step_names: form.steps.filter((step) => step.phaseId === phase.id).map((step) => step.name)
            }))
          }
        }
      : {}),
    steps: form.steps.map((step) => {
      const deterministic = ['tool_call', 'condition', 'complete'].includes(step.type);
      const inputPayload = deterministic
        ? undefined
        : formInputToPayload(step.inputMode, step.inputText, step.reuseSessionFrom);
      const outcomeRoutes: Array<Record<string, unknown>> = [];
      pushOutcomeRoute(outcomeRoutes, {
        status: 'success',
        action: step.outcomeSuccessAction,
        target: step.outcomeSuccessTarget,
        maxLoops: step.outcomeSuccessMaxLoops,
        onExhausted: step.outcomeSuccessOnExhausted
      });
      pushOutcomeRoute(outcomeRoutes, {
        status: 'rejected',
        action: step.outcomeRejectedAction,
        target: step.outcomeRejectedTarget,
        maxLoops: step.outcomeRejectedMaxLoops,
        onExhausted: step.outcomeRejectedOnExhausted
      });
      pushOutcomeRoute(outcomeRoutes, {
        status: 'failed',
        action: step.outcomeFailedAction,
        target: step.outcomeFailedTarget,
        maxLoops: step.outcomeFailedMaxLoops,
        onExhausted: step.outcomeFailedOnExhausted
      });

      return {
        name: step.name,
        type: step.type,
        ...(!deterministic
          ? {
              prompt: step.prompt,
              objective: step.objective.trim() || undefined,
              responsibilities: parseList(step.responsibilitiesText),
              defer_to: parseList(step.deferToText),
              agent_override: step.agentOverride || null,
              agent_profile_id: step.agentProfileId.trim() || null,
              reasoning_effort: step.reasoningEffort || undefined,
              require_deliverable: step.requireDeliverable,
              step_profile_id: step.stepProfileId.trim() || undefined,
              step_profile_mode: step.stepProfileMode,
              step_profile: formProfileToPayload(step)
            }
          : {}),
        ...(inputPayload ? { input: inputPayload } : {}),
        ...(!deterministic ? { allow_questions: step.allowQuestions } : {}),
        ...(deterministic
          ? {
              when: step.deterministicWhen.trim() || undefined,
              on_skip: step.deterministicOnSkipText.trim()
                ? parseObject(step.deterministicOnSkipText, `Skip output for ${step.name}`)
                : undefined,
              on_error: step.deterministicOnError || undefined,
              next:
                step.type === 'tool_call'
                  ? step.deterministicNext.trim() || undefined
                  : undefined
            }
          : {}),
        completion:
          step.type === 'run'
            ? {
                evaluate: step.evaluate,
                max_attempts: Number(step.maxAttempts),
                on_exhausted: step.onExhausted
              }
            : undefined,
        gate:
          step.type === 'gate'
            ? {
                message: step.gateMessage,
                input: parseList(step.gateInputText),
                options: parseGateOptions(step.gateOptionsText),
                conditions: parseRecordArray(step.gateConditionsText, `Gate conditions for ${step.name}`),
                thresholds: parseObject(step.gateThresholdsText, `Gate thresholds for ${step.name}`),
                timeout_seconds: Number(step.gateTimeoutSeconds),
                timeout_action: step.gateTimeoutAction
              }
            : undefined,
        tool_call:
          step.type === 'tool_call'
            ? {
                tool: step.toolName.trim(),
                args: parseObject(step.toolArgsText, `Tool arguments for ${step.name}`),
                summary: step.toolSummary.trim() || undefined,
                outputs: parseObject(step.toolOutputsText, `Tool outputs for ${step.name}`),
                fail_on_error: step.toolFailOnError,
                timeout_seconds: step.toolTimeoutSeconds || undefined,
                allow_side_effects: step.toolAllowSideEffects,
                redact_args: parseList(step.toolRedactArgsText)
              }
            : undefined,
        condition:
          step.type === 'condition'
            ? {
                if: step.conditionExpression.trim(),
                then: step.conditionThen.trim() || undefined,
                else: step.conditionElse.trim() || undefined,
                output: step.conditionOutputText.trim()
                  ? parseObject(step.conditionOutputText, `Condition output for ${step.name}`)
                  : undefined,
                revision_source: step.conditionRevisionSource.trim() || undefined,
                max_loop_iterations:
                  step.conditionMaxLoopIterations !== null
                    ? Number(step.conditionMaxLoopIterations)
                    : undefined,
                on_exhausted:
                  step.conditionMaxLoopIterations !== null
                    ? step.conditionOnExhausted
                    : undefined
              }
            : undefined,
        complete:
          step.type === 'complete'
            ? {
                status: step.completeStatus,
                summary: step.completeSummary,
                content: step.completeContent.trim() || undefined,
                outputs: parseObject(step.completeOutputsText, `Completion outputs for ${step.name}`),
                notification: step.completeNotificationText.trim()
                  ? parseObject(
                      step.completeNotificationText,
                      `Completion notification for ${step.name}`
                    )
                  : undefined,
                delivery_mode_override: step.completeDeliveryMode || undefined
              }
            : undefined,
        on_reject: !deterministic && step.evaluatorRejectTarget
          ? {
              target: step.evaluatorRejectTarget,
              max_loop_iterations: Number(step.evaluatorRejectMaxLoops),
              on_exhausted: step.evaluatorRejectOnExhausted
            }
          : undefined,
        outcome_routes: !deterministic && outcomeRoutes.length > 0 ? outcomeRoutes : undefined
      };
    })
  };
}

export function formStateToSystemWorkflowOverridePayload(form: WorkflowFormState): Record<string, unknown> {
  return {
    steps: form.steps.map((step) => ({
      name: step.name,
      reasoning_effort: step.reasoningEffort || undefined,
      step_profile_id: step.stepProfileId.trim() || undefined,
      step_profile_mode: step.stepProfileMode,
      step_profile: formProfileToPayload(step),
      completion: {
        max_attempts: Number(step.maxAttempts)
      }
    }))
  };
}

function validateRouteTarget(
  issues: string[],
  config: {
    step: WorkflowStepFormState;
    stepIndex: number;
    previousNames: string[];
    label: string;
    action: OutcomeAction;
    target: string;
  }
): void {
  const { step, stepIndex, previousNames, label, action, target } = config;
  if (action !== 'revise') return;
  if (!target.trim()) {
    issues.push(`Step ${step.name || stepIndex + 1} uses ${label} revise and requires a target.`);
    return;
  }
  if (!previousNames.includes(target)) {
    issues.push(`Step ${step.name || stepIndex + 1} ${label} target must reference an earlier step: ${target}.`);
  }
}

export function validateWorkflowForm(form: WorkflowFormState): string[] {
  const issues: string[] = [];
  const names = new Set<string>();
  const phaseIds = new Set<string>();

  form.phases.forEach((phase, index) => {
    if (!phase.id.trim()) issues.push(`Phase ${index + 1} is missing an ID.`);
    if (!phase.title.trim()) issues.push(`Phase ${phase.id || index + 1} is missing a title.`);
    if (phaseIds.has(phase.id)) issues.push(`Duplicate phase ID: ${phase.id}.`);
    phaseIds.add(phase.id);
    if (!form.steps.some((step) => step.phaseId === phase.id)) {
      issues.push(`Phase ${phase.title || phase.id || index + 1} must contain at least one step.`);
    }
  });

  form.steps.forEach((step, index) => {
    const previousNames = form.steps.slice(0, index).map((item) => item.name);
    if (!step.name.trim()) {
      issues.push(`Step ${index + 1} is missing a name.`);
    }
    if (names.has(step.name)) {
      issues.push(`Duplicate step name: ${step.name}.`);
    }
    names.add(step.name);
    if (!phaseIds.has(step.phaseId)) {
      issues.push(`Step ${step.name || index + 1} references a missing phase.`);
    }

    if (step.type === 'gate') {
      const label = `Gate step ${step.name || index + 1}`;
      if (!step.gateMessage.trim()) {
        issues.push(`${label} requires a gate message.`);
      }
      if (!Number.isInteger(Number(step.gateTimeoutSeconds)) || Number(step.gateTimeoutSeconds) < 1) {
        issues.push(`${label} timeout must be a positive integer.`);
      }
      try {
        parseRecordArray(step.gateConditionsText, `${label} conditions`);
      } catch {
        issues.push(`${label} conditions must be a JSON array of objects.`);
      }
      try {
        parseObject(step.gateThresholdsText, `${label} thresholds`);
      } catch {
        issues.push(`${label} thresholds must be a JSON object.`);
      }
      parseList(step.gateInputText).forEach((inputName) => {
        if (!previousNames.includes(inputName)) {
          issues.push(`${label} references missing or later gate input: ${inputName}.`);
        }
      });
    }
    if (step.type === 'tool_call') {
      if (!step.toolName.trim()) issues.push(`Tool call step ${step.name || index + 1} requires a tool.`);
      for (const [text, label] of [[step.toolArgsText, 'arguments'], [step.toolOutputsText, 'outputs']] as const) {
        try { parseObject(text, label); } catch { issues.push(`Tool call step ${step.name || index + 1} ${label} must be a JSON object.`); }
      }
    }
    if (step.type === 'condition') {
      if (!step.conditionExpression.trim()) issues.push(`Condition step ${step.name || index + 1} requires an expression.`);
      for (const target of [step.conditionThen, step.conditionElse].filter(Boolean)) {
        if (!form.steps.some((candidate) => candidate.name === target)) {
          issues.push(`Condition step ${step.name || index + 1} references unknown branch target: ${target}.`);
        }
      }
    }
    if (step.type === 'complete' && !step.completeSummary.trim()) {
      issues.push(`Complete step ${step.name || index + 1} requires a summary.`);
    }

    const referencedInputs = step.inputMode === 'null' || step.inputMode === 'auto' ? [] : parseList(step.inputText);
    if (referencedInputs.includes('all') && referencedInputs.length > 1) {
      issues.push(`Step ${step.name || index + 1} must use 'all' as the only source step.`);
    }
    if (step.inputMode === 'full' && referencedInputs.length !== 1) {
      issues.push(`Step ${step.name || index + 1} uses full input and requires exactly one source step.`);
    }
    if (step.inputMode === 'full' && referencedInputs[0] === 'all') {
      issues.push(`Step ${step.name || index + 1} cannot use 'all' with full input.`);
    }
    referencedInputs.forEach((inputName) => {
      if (inputName === 'all') {
        return;
      }
      if (!previousNames.includes(inputName)) {
        issues.push(`Step ${step.name || index + 1} references missing or later input: ${inputName}.`);
      }
    });
    if (step.reuseSessionFrom.trim()) {
      const reuseSource = step.reuseSessionFrom.trim();
      if (step.type !== 'run') {
        issues.push(`Step ${step.name || index + 1} can reuse a session only for a run step.`);
      }
      if (!previousNames.includes(reuseSource)) {
        issues.push(`Step ${step.name || index + 1} reuses a missing or later step: ${reuseSource}.`);
      }
      if (!referencedInputs.includes(reuseSource) && !referencedInputs.includes('all')) {
        issues.push(`Step ${step.name || index + 1} must include the reused step in its input sources.`);
      }
      const sourceStep = form.steps.find((candidate) => candidate.name === reuseSource);
      if (sourceStep && sourceStep.type !== 'run') {
        issues.push(`Step ${step.name || index + 1} cannot reuse a non-run step.`);
      }
      if (
        sourceStep &&
        sourceStep.agentOverride &&
        step.agentOverride &&
        sourceStep.agentOverride !== step.agentOverride
      ) {
        issues.push(`Step ${step.name || index + 1} cannot reuse a session across different agents.`);
      }
      if (
        sourceStep &&
        sourceStep.agentProfileId &&
        step.agentProfileId &&
        sourceStep.agentProfileId !== step.agentProfileId
      ) {
        issues.push(`Step ${step.name || index + 1} cannot reuse a session across different runtime profiles.`);
      }
    }

    if (step.evaluatorRejectTarget && !previousNames.includes(step.evaluatorRejectTarget)) {
      issues.push(`Step ${step.name || index + 1} evaluator reject target must reference an earlier step: ${step.evaluatorRejectTarget}.`);
    }
    validateRouteTarget(issues, {
      step,
      stepIndex: index,
      previousNames,
      label: 'success outcome',
      action: step.outcomeSuccessAction,
      target: step.outcomeSuccessTarget
    });
    validateRouteTarget(issues, {
      step,
      stepIndex: index,
      previousNames,
      label: 'rejected outcome',
      action: step.outcomeRejectedAction,
      target: step.outcomeRejectedTarget
    });
    validateRouteTarget(issues, {
      step,
      stepIndex: index,
      previousNames,
      label: 'failed outcome',
      action: step.outcomeFailedAction,
      target: step.outcomeFailedTarget
    });
  });

  return issues;
}

export function exportWorkflowYaml(form: WorkflowFormState): string {
  return YAML.stringify(formStateToWorkflowPayload(form));
}

export function importWorkflowYaml(raw: string): WorkflowFormState {
  return importWorkflowYamlWithProfiles(raw, {});
}

export function importWorkflowYamlWithProfiles(
  raw: string,
  profileMap: Record<string, StepProfileDefinition>
): WorkflowFormState {
  if (raw.length > 100_000) {
    throw new Error('Workflow import is limited to 100KB.');
  }

  const parsed = YAML.parse(raw, { maxAliasCount: 0 });
  if (!isRecord(parsed) || typeof parsed.name !== 'string' || !Array.isArray(parsed.steps)) {
    throw new Error('Invalid workflow YAML format. Expected workflow metadata with a steps array.');
  }
  if (parsed.steps.some((step) => !isRecord(step) || typeof step.name !== 'string' || typeof step.type !== 'string')) {
    throw new Error('Invalid workflow YAML format. Each step must provide string name and type fields.');
  }
  const form = workflowToFormState(parsed as unknown as Workflow, profileMap);
  form.lifecycle = 'persistent';
  return form;
}
