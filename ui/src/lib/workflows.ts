import YAML from 'yaml';

import type { Workflow } from '$lib/types/api';
import { isRecord } from '$lib/utils';

export interface WorkflowStepFormState {
  name: string;
  type: 'run' | 'gate';
  prompt: string;
  inputMode: 'auto' | 'null' | 'last' | 'full' | 'summary';
  inputText: string;
  allowQuestions: boolean;
  evaluate: boolean;
  maxAttempts: number;
  onExhausted: string;
  gateMessage: string;
  gateOptionsText: string;
  rejectTarget: string;
  rejectMaxLoops: number;
  rejectOnExhausted: string;
}

export interface WorkflowFormState {
  workflowId: string;
  name: string;
  description: string;
  version: number;
  criteria: string;
  tagsText: string;
  interactionMode: string;
  defaultEvaluate: boolean;
  defaultMaxAttempts: number;
  defaultOnExhausted: string;
  steps: WorkflowStepFormState[];
}

export function createEmptyStep(): WorkflowStepFormState {
  return {
    name: '',
    type: 'run',
    prompt: '',
    inputMode: 'null',
    inputText: '',
    allowQuestions: false,
    evaluate: true,
    maxAttempts: 3,
    onExhausted: 'gate',
    gateMessage: '',
    gateOptionsText: '',
    rejectTarget: '',
    rejectMaxLoops: 2,
    rejectOnExhausted: 'gate'
  };
}

export function createEmptyWorkflowForm(): WorkflowFormState {
  return {
    workflowId: '',
    name: '',
    description: '',
    version: 1,
    criteria: '',
    tagsText: '',
    interactionMode: 'explicit_gates',
    defaultEvaluate: true,
    defaultMaxAttempts: 3,
    defaultOnExhausted: 'gate',
    steps: [createEmptyStep()]
  };
}

function joinOptions(options: Array<Record<string, unknown>>): string {
  return options
    .map((option) => `${String(option.label ?? '')}|${String(option.action ?? '')}`)
    .join('\n');
}

function workflowInputSourceNames(input: Workflow['steps'][number]['input']): string[] {
  if (!input) {
    return [];
  }

  if (typeof input === 'string') {
    return [input];
  }

  if (Array.isArray(input)) {
    return input.filter((item): item is string => typeof item === 'string');
  }

  if (typeof input.source === 'string') {
    return [input.source];
  }

  if (Array.isArray(input.source)) {
    return input.source.filter((item): item is string => typeof item === 'string');
  }

  return [];
}

function workflowInputMode(input: Workflow['steps'][number]['input']): WorkflowStepFormState['inputMode'] {
  if (input == null) {
    return 'auto';
  }
  if (typeof input === 'string' || Array.isArray(input)) {
    return 'last';
  }
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
  if (inputMode === 'auto') {
    return undefined;
  }
  if (inputMode === 'null' || refs.length === 0) {
    return inputMode === 'null' ? { type: 'null' } : { type: inputMode };
  }
  if (inputMode === 'full') {
    return { type: 'full', source: refs[0] };
  }
  if (refs.length === 1) {
    return { type: inputMode, source: refs[0] };
  }
  return { type: inputMode, source: refs };
}

export function workflowToFormState(workflow: Workflow): WorkflowFormState {
  return {
    workflowId: workflow.workflow_id,
    name: workflow.name,
    description: workflow.description,
    version: workflow.version,
    criteria: workflow.criteria,
    tagsText: workflow.tags.join(', '),
    interactionMode: typeof workflow.interaction.mode === 'string' ? workflow.interaction.mode : 'explicit_gates',
    defaultEvaluate: workflow.defaults.evaluate !== false,
    defaultMaxAttempts:
      typeof workflow.defaults.max_attempts === 'number' ? workflow.defaults.max_attempts : 3,
    defaultOnExhausted:
      typeof workflow.defaults.on_exhausted === 'string' ? workflow.defaults.on_exhausted : 'gate',
    steps: workflow.steps.map((step) => ({
      name: step.name,
      type: (step.type as 'run' | 'gate') ?? 'run',
      prompt: step.prompt ?? '',
      inputMode: workflowInputMode(step.input),
      inputText: workflowInputSourceNames(step.input).join(', '),
      allowQuestions: step.allow_questions ?? false,
      evaluate: step.completion?.evaluate !== false,
      maxAttempts:
        typeof step.completion?.max_attempts === 'number' ? step.completion.max_attempts : 3,
      onExhausted:
        typeof step.completion?.on_exhausted === 'string' ? step.completion.on_exhausted : 'gate',
      gateMessage: typeof step.gate?.message === 'string' ? step.gate.message : '',
      gateOptionsText: Array.isArray(step.gate?.options)
        ? joinOptions(step.gate.options as Array<Record<string, unknown>>)
        : '',
      rejectTarget: typeof step.on_reject?.target === 'string' ? step.on_reject.target : '',
      rejectMaxLoops:
        typeof step.on_reject?.max_loop_iterations === 'number'
          ? step.on_reject.max_loop_iterations
          : 2,
      rejectOnExhausted:
        typeof step.on_reject?.on_exhausted === 'string' ? step.on_reject.on_exhausted : 'gate'
    }))
  };
}

function parseList(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
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

export function formStateToWorkflowPayload(form: WorkflowFormState): Record<string, unknown> {
  return {
    workflow_id: form.workflowId || undefined,
    name: form.name,
    description: form.description,
    version: Number(form.version),
    criteria: form.criteria,
    tags: parseList(form.tagsText),
    interaction: { mode: form.interactionMode },
    defaults: {
      evaluate: form.defaultEvaluate,
      max_attempts: Number(form.defaultMaxAttempts),
      on_exhausted: form.defaultOnExhausted
    },
    steps: form.steps.map((step) => ({
      name: step.name,
      type: step.type,
      prompt: step.prompt,
      ...(formInputToPayload(step.inputMode, step.inputText) ? { input: formInputToPayload(step.inputMode, step.inputText) } : {}),
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
      on_reject: step.rejectTarget
        ? {
            target: step.rejectTarget,
            max_loop_iterations: Number(step.rejectMaxLoops),
            on_exhausted: step.rejectOnExhausted
          }
        : undefined
    }))
  };
}

export function validateWorkflowForm(form: WorkflowFormState): string[] {
  const issues: string[] = [];
  const names = new Set<string>();

  form.steps.forEach((step, index) => {
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
      const previousNames = form.steps.slice(0, index).map((item) => item.name);
      if (!previousNames.includes(inputName)) {
        issues.push(`Step ${step.name || index + 1} references missing or later input: ${inputName}.`);
      }
    });
  });

  return issues;
}

export function exportWorkflowYaml(form: WorkflowFormState): string {
  return YAML.stringify(formStateToWorkflowPayload(form));
}

export function importWorkflowYaml(raw: string): WorkflowFormState {
  if (raw.length > 100_000) {
    throw new Error('Workflow import is limited to 100KB.');
  }

  const parsed = YAML.parse(raw, { maxAliasCount: 0 });
  if (!isRecord(parsed) || typeof parsed.name !== 'string' || !Array.isArray(parsed.steps)) {
    throw new Error('Invalid workflow YAML format. Expected workflow metadata with a steps array.');
  }
  if (
    parsed.steps.some(
      (step) =>
        !isRecord(step) || typeof step.name !== 'string' || typeof step.type !== 'string'
    )
  ) {
    throw new Error('Invalid workflow YAML format. Each step must provide string name and type fields.');
  }
  return workflowToFormState(parsed as unknown as Workflow);
}
