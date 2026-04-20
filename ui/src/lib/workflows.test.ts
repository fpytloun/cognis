import { describe, expect, it } from 'vitest';

import {
  createEmptyWorkflowForm,
  formStateToWorkflowPayload,
  importWorkflowYaml,
  validateWorkflowForm,
  workflowToFormState
} from '$lib/workflows';

describe('validateWorkflowForm', () => {
  it('rejects duplicate step names and later input references', () => {
    const form = createEmptyWorkflowForm();
    form.name = 'Review loop';
    form.steps = [
      {
        ...form.steps[0],
        name: 'plan',
        prompt: 'Plan the work'
      },
      {
        ...form.steps[0],
        name: 'plan',
        inputMode: 'last',
        inputText: 'review',
        prompt: 'Duplicate and invalid input'
      }
    ];

    const issues = validateWorkflowForm(form);
    expect(issues.some((issue) => issue.includes('Duplicate step name'))).toBe(true);
    expect(issues.some((issue) => issue.includes('missing or later input'))).toBe(true);
  });
});

describe('importWorkflowYaml', () => {
  it('rejects oversized imports', () => {
    expect(() => importWorkflowYaml('a'.repeat(100_001))).toThrow('Workflow import is limited to 100KB.');
  });

  it('rejects malformed workflow step shapes', () => {
    expect(() => importWorkflowYaml('name: bad\nsteps:\n  - prompt: missing-fields')).toThrow(
      'Each step must provide string name and type fields.'
    );
  });

  it('normalizes imported workflows back to persistent lifecycle', () => {
    const form = importWorkflowYaml(`workflow_id: wf_imported\nname: imported\nversion: 1\ncriteria: ''\ntags: []\nlifecycle: ephemeral\ninteraction:\n  mode: explicit_gates\ndefaults:\n  evaluate: true\n  max_attempts: 3\n  on_exhausted: gate\n  delivery:\n    completion_mode_family: default\n    allow_silent_completion: false\nsteps:\n  - name: gather\n    type: run\n`);

    expect(form.lifecycle).toBe('persistent');
  });
});

describe('workflowToFormState', () => {
  it('maps evaluator and outcome routing separately', () => {
    const form = workflowToFormState({
      workflow_id: 'wf:test',
      name: 'Test',
      description: '',
      version: 1,
      criteria: '',
      tags: [],
      interaction: { mode: 'explicit_gates' },
      defaults: { evaluate: true, max_attempts: 3, on_exhausted: 'gate' },
      steps: [
        { name: 'plan', type: 'run', completion: { evaluate: true } },
        {
          name: 'review',
          type: 'run',
          completion: { evaluate: true },
          on_reject: { target: 'plan', max_loop_iterations: 2, on_exhausted: 'gate' },
          outcome_routes: [
            { status: 'rejected', action: 'revise(plan)', max_loop_iterations: 3, on_exhausted: 'gate' },
            { status: 'failed', action: 'gate' }
          ]
        }
      ],
      is_system: false,
      owner_email: null,
      lifecycle: 'persistent',
      archived_at: null,
      lineage: null,
      editable_fields: [],
      has_overrides: false,
      disabled: false,
      disableable: false,
      override_warnings: []
    });

    expect(form.steps[1].evaluatorRejectTarget).toBe('plan');
    expect(form.steps[1].outcomeRejectedAction).toBe('revise');
    expect(form.steps[1].outcomeRejectedTarget).toBe('plan');
    expect(form.steps[1].outcomeRejectedMaxLoops).toBe(3);
    expect(form.steps[1].outcomeFailedAction).toBe('gate');
  });

  it('preserves non-revise rejected and failed routes', () => {
    const form = workflowToFormState({
      workflow_id: 'wf:test',
      name: 'Test',
      description: '',
      version: 1,
      criteria: '',
      tags: [],
      interaction: { mode: 'explicit_gates' },
      defaults: { evaluate: true, max_attempts: 3, on_exhausted: 'gate' },
      steps: [
        { name: 'plan', type: 'run', completion: { evaluate: true } },
        {
          name: 'commit',
          type: 'run',
          completion: { evaluate: false },
          outcome_routes: [
            { status: 'rejected', action: 'gate' },
            { status: 'failed', action: 'revise(plan)', max_loop_iterations: 5, on_exhausted: 'fail' }
          ]
        }
      ],
      is_system: false,
      owner_email: null,
      lifecycle: 'persistent',
      archived_at: null,
      lineage: null,
      editable_fields: [],
      has_overrides: false,
      disabled: false,
      disableable: false,
      override_warnings: []
    });

    expect(form.steps[1].outcomeRejectedAction).toBe('gate');
    expect(form.steps[1].outcomeFailedAction).toBe('revise');
    expect(form.steps[1].outcomeFailedTarget).toBe('plan');
    expect(form.steps[1].outcomeFailedMaxLoops).toBe(5);
    expect(form.steps[1].outcomeFailedOnExhausted).toBe('fail');
  });

  it('preserves success routes', () => {
    const form = workflowToFormState({
      workflow_id: 'wf:test',
      name: 'Test',
      description: '',
      version: 1,
      criteria: '',
      tags: [],
      interaction: { mode: 'explicit_gates' },
      defaults: { evaluate: true, max_attempts: 3, on_exhausted: 'gate' },
      steps: [
        { name: 'plan', type: 'run', completion: { evaluate: true } },
        {
          name: 'publish',
          type: 'run',
          completion: { evaluate: false },
          outcome_routes: [
            { status: 'success', action: 'revise(plan)', max_loop_iterations: 2, on_exhausted: 'gate' }
          ]
        }
      ],
      is_system: false,
      owner_email: null,
      lifecycle: 'persistent',
      archived_at: null,
      lineage: null,
      editable_fields: [],
      has_overrides: false,
      disabled: false,
      disableable: false,
      override_warnings: []
    });

    expect(form.steps[1].outcomeSuccessAction).toBe('revise');
    expect(form.steps[1].outcomeSuccessTarget).toBe('plan');
    expect(form.steps[1].outcomeSuccessMaxLoops).toBe(2);
  });
});

describe('formStateToWorkflowPayload', () => {
  it('serializes outcome routes for rejected and failed outcomes', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      { ...form.steps[0], name: 'plan' },
      {
        ...form.steps[0],
        name: 'review',
        evaluatorRejectTarget: 'plan',
        outcomeRejectedAction: 'revise',
        outcomeRejectedTarget: 'plan',
        outcomeRejectedMaxLoops: 4,
        outcomeRejectedOnExhausted: 'gate',
        outcomeFailedAction: 'gate'
      }
    ];

    const payload = formStateToWorkflowPayload(form);
    const review = (payload.steps as Array<Record<string, unknown>>)[1];

    expect(review.on_reject).toEqual({
      target: 'plan',
      max_loop_iterations: 2,
      on_exhausted: 'gate'
    });
    expect(review.outcome_routes).toEqual([
      {
        status: 'rejected',
        action: 'revise(plan)',
        max_loop_iterations: 4,
        on_exhausted: 'gate'
      },
      {
        status: 'failed',
        action: 'gate'
      }
    ]);
  });

  it('serializes non-revise rejected routes and failed revise metadata', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      { ...form.steps[0], name: 'plan' },
      {
        ...form.steps[0],
        name: 'commit',
        outcomeRejectedAction: 'gate',
        outcomeFailedAction: 'revise',
        outcomeFailedTarget: 'plan',
        outcomeFailedMaxLoops: 6,
        outcomeFailedOnExhausted: 'continue'
      }
    ];

    const payload = formStateToWorkflowPayload(form);
    const review = (payload.steps as Array<Record<string, unknown>>)[1];

    expect(review.outcome_routes).toEqual([
      {
        status: 'rejected',
        action: 'gate'
      },
      {
        status: 'failed',
        action: 'revise(plan)',
        max_loop_iterations: 6,
        on_exhausted: 'continue'
      }
    ]);
  });

  it('leaves new steps valid by default', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [{ ...form.steps[0], name: 'new_step' }];

    expect(validateWorkflowForm(form)).toEqual([]);
  });

  it('serializes success routes when configured', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      { ...form.steps[0], name: 'plan' },
      {
        ...form.steps[0],
        name: 'publish',
        outcomeSuccessAction: 'revise',
        outcomeSuccessTarget: 'plan',
        outcomeSuccessMaxLoops: 2,
        outcomeSuccessOnExhausted: 'gate'
      }
    ];

    const payload = formStateToWorkflowPayload(form);
    const publish = (payload.steps as Array<Record<string, unknown>>)[1];

    expect(publish.outcome_routes).toEqual([
      {
        status: 'success',
        action: 'revise(plan)',
        max_loop_iterations: 2,
        on_exhausted: 'gate'
      }
    ]);
  });
});
