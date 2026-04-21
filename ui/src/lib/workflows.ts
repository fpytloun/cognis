import YAML from 'yaml';

import { GENERIC_THINKING_EFFORTS } from '$lib/thinking';
import type { StepProfileDefinition, Workflow } from '$lib/types/api';
import { isRecord } from '$lib/utils';

type OutcomeAction = 'none' | 'fail' | 'gate' | 'continue' | 'cancel' | 'revise';

export const STEP_PROFILE_CAPABILITIES = ['read', 'write', 'privileged', 'destructive'] as const;

export interface WorkflowStepProfileRowFormState {
  category: string;
  capabilities: string[];
}

export interface WorkflowStepFormState {
  name: string;
  type: 'run' | 'gate';
  prompt: string;
  agentOverride: string;
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
  allowQuestions: boolean;
  evaluate: boolean;
  maxAttempts: number;
  onExhausted: string;
  gateMessage: string;
  gateOptionsText: string;
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
  steps: WorkflowStepFormState[];
}

export function createEmptyStep(): WorkflowStepFormState {
  return {
    name: '',
    type: 'run',
    prompt: '',
    agentOverride: '',
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
    allowQuestions: false,
    evaluate: true,
    maxAttempts: 3,
    onExhausted: 'gate',
    gateMessage: '',
    gateOptionsText: '',
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
    outcomeFailedOnExhausted: 'gate'
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
    steps: [createEmptyStep()]
  };
}

export function workflowThinkingEfforts(): string[] {
  return [...GENERIC_THINKING_EFFORTS].filter((value) => value !== 'default');
}

function joinOptions(options: Array<Record<string, unknown>>): string {
  return options
    .map((option) => `${String(option.label ?? '')}|${String(option.action ?? '')}`)
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
  inputText: string
): { type: string; source?: string | string[] } | undefined {
  const refs = parseList(inputText);
  if (inputMode === 'auto') return undefined;
  if (inputMode === 'null' || refs.length === 0) {
    return inputMode === 'null' ? { type: 'null' } : { type: inputMode };
  }
  if (inputMode === 'full') return { type: 'full', source: refs[0] };
  if (refs.length === 1) return { type: inputMode, source: refs[0] };
  return { type: inputMode, source: refs };
}

function parseList(value: string): string[] {
  return value.split(',').map((item) => item.trim()).filter(Boolean);
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

function parseGateOptions(value: string): Array<{ label: string; action: string }> {
  return value
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [label, action] = line.split('|').map((item) => item.trim());
      return { label: label || action, action: action || label };
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
  const deliveryDefaults = isRecord(workflow.defaults.delivery) ? workflow.defaults.delivery : null;
  return {
    workflowId: workflow.workflow_id,
    name: workflow.name,
    description: workflow.description,
    version: workflow.version,
    criteria: workflow.criteria,
    tagsText: workflow.tags.join(', '),
    lifecycle: workflow.lifecycle === 'ephemeral' ? 'ephemeral' : 'persistent',
    lineage: workflow.lineage ?? null,
    interactionMode: typeof workflow.interaction.mode === 'string' ? workflow.interaction.mode : 'explicit_gates',
    defaultEvaluate: workflow.defaults.evaluate !== false,
    defaultMaxAttempts: typeof workflow.defaults.max_attempts === 'number' ? workflow.defaults.max_attempts : 3,
    defaultOnExhausted: typeof workflow.defaults.on_exhausted === 'string' ? workflow.defaults.on_exhausted : 'gate',
    defaultCompletionModeFamily:
      deliveryDefaults?.completion_mode_family === 'direct' ? 'direct' : 'default',
    defaultAllowSilentCompletion: deliveryDefaults?.allow_silent_completion === true,
    steps: workflow.steps.map((step) => {
      const successRoute = outcomeRouteForStatus(step, 'success');
      const rejectedRoute = outcomeRouteForStatus(step, 'rejected');
      const failedRoute = outcomeRouteForStatus(step, 'failed');
      const stepProfile = resolveStepProfilePreset(step, profileMap);
      return {
        name: step.name,
        type: (step.type as 'run' | 'gate') ?? 'run',
        prompt: step.prompt ?? '',
        agentOverride: step.agent_override ?? '',
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
        allowQuestions: step.allow_questions ?? false,
        evaluate: step.completion?.evaluate !== false,
        maxAttempts: typeof step.completion?.max_attempts === 'number' ? step.completion.max_attempts : 3,
        onExhausted: typeof step.completion?.on_exhausted === 'string' ? step.completion.on_exhausted : 'gate',
        gateMessage: typeof step.gate?.message === 'string' ? step.gate.message : '',
        gateOptionsText: Array.isArray(step.gate?.options) ? joinOptions(step.gate.options as Array<Record<string, unknown>>) : '',
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
        outcomeFailedOnExhausted: routeOnExhausted(failedRoute)
      };
    })
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
      }
    },
    steps: form.steps.map((step) => {
      const inputPayload = formInputToPayload(step.inputMode, step.inputText);
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
        prompt: step.prompt,
        agent_override: step.agentOverride || null,
        reasoning_effort: step.reasoningEffort || undefined,
        require_deliverable: step.requireDeliverable,
        step_profile_id: step.stepProfileId.trim() || undefined,
        step_profile_mode: step.stepProfileMode,
        step_profile: formProfileToPayload(step),
        ...(inputPayload ? { input: inputPayload } : {}),
        allow_questions: step.allowQuestions,
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
                options: parseGateOptions(step.gateOptionsText)
              }
            : undefined,
        on_reject: step.evaluatorRejectTarget
          ? {
              target: step.evaluatorRejectTarget,
              max_loop_iterations: Number(step.evaluatorRejectMaxLoops),
              on_exhausted: step.evaluatorRejectOnExhausted
            }
          : undefined,
        outcome_routes: outcomeRoutes.length > 0 ? outcomeRoutes : undefined
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

  form.steps.forEach((step, index) => {
    const previousNames = form.steps.slice(0, index).map((item) => item.name);
    if (!step.name.trim()) {
      issues.push(`Step ${index + 1} is missing a name.`);
    }
    if (names.has(step.name)) {
      issues.push(`Duplicate step name: ${step.name}.`);
    }
    names.add(step.name);

    if (step.type === 'gate' && !step.gateMessage.trim()) {
      issues.push(`Gate step ${step.name || index + 1} requires a gate message.`);
    }

    const referencedInputs = step.inputMode === 'null' || step.inputMode === 'auto' ? [] : parseList(step.inputText);
    if (step.inputMode === 'full' && referencedInputs.length !== 1) {
      issues.push(`Step ${step.name || index + 1} uses full input and requires exactly one source step.`);
    }
    referencedInputs.forEach((inputName) => {
      if (!previousNames.includes(inputName)) {
        issues.push(`Step ${step.name || index + 1} references missing or later input: ${inputName}.`);
      }
    });

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
