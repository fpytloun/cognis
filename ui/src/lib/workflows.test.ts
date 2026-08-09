import { describe, expect, it } from 'vitest';

import {
  buildStepProfileMap,
  createEmptyWorkflowForm,
  formStateToWorkflowPayload,
  exportWorkflowYaml,
  importWorkflowYaml,
  validateWorkflowForm,
  workflowIssueGroup,
  workflowStepSummary,
  workflowToFormState
} from '$lib/workflows';
import type { Workflow } from '$lib/types/api';

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

  it('validates deterministic fields and condition targets', () => {
    const form = createEmptyWorkflowForm();
    form.name = 'Deterministic';
    form.steps = [
      { ...form.steps[0], name: 'fetch', type: 'tool_call', toolName: '', toolArgsText: '[]' },
      {
        ...form.steps[0],
        name: 'route',
        type: 'condition',
        conditionExpression: '',
        conditionThen: 'missing'
      },
      { ...form.steps[0], name: 'finish', type: 'complete', completeSummary: '' }
    ];

    const issues = validateWorkflowForm(form);
    expect(issues).toContain('Tool call step fetch requires a tool.');
    expect(issues).toContain('Tool call step fetch arguments must be a JSON object.');
    expect(issues).toContain('Condition step route requires an expression.');
    expect(issues).toContain('Condition step route references unknown branch target: missing.');
    expect(issues).toContain('Complete step finish requires a summary.');
  });
});

describe('workflow builder information architecture', () => {
  it('provides compact semantic summaries for every step type', () => {
    const base = createEmptyWorkflowForm().steps[0];
    expect(workflowStepSummary({ ...base, name: 'run', objective: 'Implement the change.' })).toBe('Implement the change.');
    expect(workflowStepSummary({ ...base, type: 'gate', gateMessage: 'Approve release?' })).toBe('Approve release?');
    expect(workflowStepSummary({ ...base, type: 'tool_call', toolName: 'builtin:read' })).toBe('builtin:read');
    expect(workflowStepSummary({ ...base, type: 'condition', conditionExpression: '{{ ok }}' })).toBe('{{ ok }}');
    expect(workflowStepSummary({ ...base, type: 'complete', completeSummary: 'Done' })).toBe('Done');
  });

  it('maps validation failures to the inspector group that owns the field', () => {
    expect(workflowIssueGroup('Step 2 is missing a name.')).toBe('basics');
    expect(workflowIssueGroup('Tool call step fetch arguments must be a JSON object.')).toBe('tools');
    expect(workflowIssueGroup('Step review reuses a missing or later step: plan.')).toBe('context-session');
    expect(workflowIssueGroup('Condition step route references unknown branch target: missing.')).toBe('routing-review');
    expect(workflowIssueGroup('Complete step finish requires a summary.')).toBe('completion-evaluation');
  });

  it('round-trips a long 20 phase by 8 step workflow without changing phase order', () => {
    const form = createEmptyWorkflowForm();
    form.name = 'Long workflow';
    form.phases = Array.from({ length: 20 }, (_, phaseIndex) => ({
      id: `phase-${phaseIndex + 1}`,
      title: `Phase ${phaseIndex + 1}`,
      description: ''
    }));
    form.steps = form.phases.flatMap((phase, phaseIndex) =>
      Array.from({ length: 8 }, (_, stepIndex) => ({
        ...form.steps[0],
        name: `step-${phaseIndex + 1}-${stepIndex + 1}`,
        phaseId: phase.id,
        prompt: `Run ${phaseIndex + 1}.${stepIndex + 1}`
      }))
    );

    const payload = formStateToWorkflowPayload(form) as unknown as Workflow;
    const hydrated = workflowToFormState(payload);
    expect(hydrated.steps).toHaveLength(160);
    expect(hydrated.phases.map((phase) => phase.id)).toEqual(form.phases.map((phase) => phase.id));
    expect(hydrated.steps.map((step) => step.phaseId)).toEqual(form.steps.map((step) => step.phaseId));
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

  it('preserves composable run-step contracts through YAML and payload round trips', () => {
    const form = importWorkflowYaml(`name: composable
version: 1
criteria: ""
tags: []
interaction:
  mode: explicit_gates
defaults:
  evaluate: false
  max_attempts: 3
  on_exhausted: gate
steps:
  - name: implement
    type: run
    objective: Implement the approved change.
    responsibilities:
      - Edit code
      - Run focused tests
    defer_to:
      - review
      - commit
`);

    expect(form.steps[0].objective).toBe('Implement the approved change.');
    expect(form.steps[0].responsibilitiesText).toBe('Edit code\nRun focused tests');
    expect(form.steps[0].deferToText).toBe('review\ncommit');
    const step = (formStateToWorkflowPayload(form).steps as Array<Record<string, unknown>>)[0];
    expect(step.objective).toBe('Implement the approved change.');
    expect(step.responsibilities).toEqual(['Edit code', 'Run focused tests']);
    expect(step.defer_to).toEqual(['review', 'commit']);
  });

  it('preserves every GateConfig field through YAML, form, API payload, and YAML export', () => {
    const form = importWorkflowYaml(`name: gated
version: 1
criteria: ""
tags: []
interaction:
  mode: explicit_gates
defaults: {}
steps:
  - name: plan
    type: run
  - name: review
    type: gate
    gate:
      message: Review the release.
      input:
        - plan
      options:
        - label: Approve
          action: continue
          prompt: false
        - label: Request changes
          action: revise(plan)
          prompt: true
      conditions:
        - expression: metadata.risk > 0
      thresholds:
        approvals: 2
        score: 0.9
      timeout_seconds: 900
      timeout_action: cancel
`);

    expect(form.steps[1]).toMatchObject({
      gateMessage: 'Review the release.',
      gateInputText: 'plan',
      gateOptionsText: 'Approve|continue|false\nRequest changes|revise(plan)|true',
      gateTimeoutSeconds: 900,
      gateTimeoutAction: 'cancel'
    });

    const payload = formStateToWorkflowPayload(form);
    const gate = ((payload.steps as Array<Record<string, unknown>>)[1].gate);
    expect(gate).toEqual({
      message: 'Review the release.',
      input: ['plan'],
      options: [
        { label: 'Approve', action: 'continue', prompt: false },
        { label: 'Request changes', action: 'revise(plan)', prompt: true }
      ],
      conditions: [{ expression: 'metadata.risk > 0' }],
      thresholds: { approvals: 2, score: 0.9 },
      timeout_seconds: 900,
      timeout_action: 'cancel'
    });

    const reimported = importWorkflowYaml(exportWorkflowYaml(form));
    expect((formStateToWorkflowPayload(reimported).steps as Array<Record<string, unknown>>)[1].gate).toEqual(gate);
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
    expect(form.phases).toEqual([{ id: 'main', title: 'Test', description: '' }]);
    expect(form.presentationEdited).toBe(false);
    expect(formStateToWorkflowPayload(form)).not.toHaveProperty('presentation');
  });

  it('hydrates and serializes composable step contracts', () => {
    const workflow = {
      workflow_id: 'wf:composable',
      name: 'Composable',
      description: '',
      version: 1,
      criteria: '',
      tags: [],
      interaction: { mode: 'explicit_gates' },
      defaults: { evaluate: false, max_attempts: 3, on_exhausted: 'gate' },
      steps: [
        {
          name: 'implement',
          type: 'run',
          objective: 'Implement the approved change.',
          responsibilities: ['Edit code', 'Run focused tests'],
          defer_to: ['review', 'commit']
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
    } satisfies Workflow;

    const form = workflowToFormState(workflow);
    const step = (formStateToWorkflowPayload(form).steps as Array<Record<string, unknown>>)[0];

    expect(form.steps[0].objective).toBe('Implement the approved change.');
    expect(step.objective).toBe('Implement the approved change.');
    expect(step.responsibilities).toEqual(['Edit code', 'Run focused tests']);
    expect(step.defer_to).toEqual(['review', 'commit']);
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

  it('hydrates effective step profile matrix from selected preset', () => {
    const profileMap = buildStepProfileMap([
      {
        profile_id: 'system:coding',
        name: 'Coding',
        mode: 'soft',
        config: {
          matrix: {
            filesystem: ['read', 'write'],
            shell: ['write', 'privileged']
          },
          allow_tool_search: true
        }
      }
    ]);

    const form = workflowToFormState(
      {
        workflow_id: 'wf:test',
        name: 'Test',
        description: '',
        version: 1,
        criteria: '',
        tags: [],
        interaction: { mode: 'explicit_gates' },
        defaults: { evaluate: true, max_attempts: 3, on_exhausted: 'gate' },
        steps: [{ name: 'implement', type: 'run', step_profile_id: 'system:coding' }],
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
      },
      profileMap
    );

    expect(form.steps[0].stepProfileMatrix).toEqual([
      { category: 'filesystem', capabilities: ['read', 'write'] },
      { category: 'shell', capabilities: ['privileged', 'write'] }
    ]);
    expect(form.steps[0].stepProfileBaseMatrix).toEqual(form.steps[0].stepProfileMatrix);
  });
});

describe('formStateToWorkflowPayload', () => {
  it('serializes phases and deterministic step configs', () => {
    const form = createEmptyWorkflowForm();
    form.name = 'Phased workflow';
    form.phases = [
      { id: 'collect', title: 'Collect', description: 'Fetch data' },
      { id: 'finish', title: 'Finish', description: '' }
    ];
    form.steps = [
      {
        ...form.steps[0],
        name: 'fetch',
        phaseId: 'collect',
        type: 'tool_call',
        toolName: 'builtin:read',
        toolArgsText: '{"path":"{{ vars.path }}"}',
        toolSummary: 'Fetched input'
      },
      {
        ...form.steps[0],
        name: 'done',
        phaseId: 'finish',
        type: 'complete',
        completeSummary: 'Finished',
        completeDeliveryMode: 'silent'
      }
    ];

    const payload = formStateToWorkflowPayload(form);
    expect(payload.presentation).toEqual({
      phases: [
        { id: 'collect', title: 'Collect', description: 'Fetch data', step_names: ['fetch'] },
        { id: 'finish', title: 'Finish', description: '', step_names: ['done'] }
      ]
    });
    const steps = payload.steps as Array<Record<string, unknown>>;
    expect(steps[0].tool_call).toMatchObject({ tool: 'builtin:read', args: { path: '{{ vars.path }}' } });
    expect(steps[1].complete).toMatchObject({ status: 'completed', summary: 'Finished', delivery_mode_override: 'silent' });
  });

  it('round-trips every deterministic control field and complete notification', () => {
    const workflow: Workflow = {
      workflow_id: 'wf:round-trip', name: 'Round trip', description: '', version: 1,
      criteria: '', tags: [], interaction: { mode: 'explicit_gates' }, defaults: {},
      steps: [
        {
          name: 'fetch', type: 'tool_call', when: '{{ vars.enabled }}',
          on_skip: { summary: 'Disabled', outputs: { skipped: true }, metadata: { reason: 'flag' } },
          on_error: 'gate', next: 'done',
          tool_call: { tool: 'builtin:read', args: { path: '{{ vars.path }}' } }
        },
        {
          name: 'done', type: 'complete',
          complete: {
            status: 'completed', summary: 'Done', outputs: { count: 0 },
            notification: { mode: 'silent', reason: 'No work found' },
            delivery_mode_override: 'silent'
          }
        }
      ],
      is_system: false, owner_email: null, lifecycle: 'persistent', archived_at: null,
      lineage: null, editable_fields: [], has_overrides: false, disabled: false,
      disableable: false, override_warnings: []
    };

    const payload = formStateToWorkflowPayload(workflowToFormState(workflow));
    const [fetch, done] = JSON.parse(JSON.stringify(payload.steps)) as Array<Record<string, unknown>>;
    expect(fetch).toMatchObject(workflow.steps[0]);
    expect(done).toEqual(workflow.steps[1]);
  });

  it('round-trips input-scoped session reuse', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      { ...form.steps[0], name: 'plan' },
      {
        ...form.steps[0],
        name: 'implement',
        inputMode: 'last',
        inputText: 'plan',
        reuseSessionFrom: 'plan'
      }
    ];

    const payload = formStateToWorkflowPayload(form);
    const implement = (payload.steps as Array<Record<string, unknown>>)[1];
    expect(implement.input).toEqual({
      type: 'last',
      source: 'plan',
      reuse_session_from: 'plan'
    });

    const restored = workflowToFormState(payload as unknown as Workflow);
    expect(restored.steps[1].reuseSessionFrom).toBe('plan');
    expect(validateWorkflowForm(restored)).toEqual([]);
  });

  it.each([
    ['tool_call', { toolName: 'builtin:read' }],
    ['condition', { conditionExpression: 'true', deterministicNext: 'other' }],
    ['complete', { completeSummary: 'Done', deterministicNext: 'other' }]
  ] as const)('serializes a fresh %s step without agent-only fields', (type, config) => {
    const form = createEmptyWorkflowForm();
    form.steps = [{ ...form.steps[0], name: 'deterministic', type, ...config }];

    const [step] = JSON.parse(
      JSON.stringify(formStateToWorkflowPayload(form).steps)
    ) as Array<Record<string, unknown>>;
    for (const field of [
      'input', 'prompt', 'agent_override', 'agent_profile_id', 'completion',
      'allow_questions', 'require_deliverable', 'step_profile_mode'
    ]) {
      expect(step).not.toHaveProperty(field);
    }
    if (type !== 'tool_call') {
      expect(step).not.toHaveProperty('next');
    }
  });

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

  it("accepts 'all' as a standalone input source", () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      { ...form.steps[0], name: 'setup' },
      { ...form.steps[0], name: 'collect' },
      { ...form.steps[0], name: 'synthesize', inputMode: 'last', inputText: 'all' }
    ];

    expect(validateWorkflowForm(form)).toEqual([]);
  });

  it("rejects 'all' mixed with named input sources", () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      { ...form.steps[0], name: 'setup' },
      { ...form.steps[0], name: 'synthesize', inputMode: 'last', inputText: 'all, setup' }
    ];

    expect(validateWorkflowForm(form).some((issue) => issue.includes("'all' as the only source"))).toBe(true);
  });

  it('omits inline step profile payload when step matches preset defaults', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      {
        ...form.steps[0],
        name: 'implement',
        stepProfileId: 'system:coding',
        stepProfileMode: 'soft',
        stepProfileBaseMode: 'soft',
        stepProfileAllowToolSearch: true,
        stepProfileBaseAllowToolSearch: true,
        stepProfileMatrix: [
          { category: 'filesystem', capabilities: ['read', 'write'] },
          { category: 'shell', capabilities: ['write', 'privileged'] }
        ],
        stepProfileBaseMatrix: [
          { category: 'filesystem', capabilities: ['read', 'write'] },
          { category: 'shell', capabilities: ['write', 'privileged'] }
        ]
      }
    ];

    const payload = formStateToWorkflowPayload(form);
    const step = (payload.steps as Array<Record<string, unknown>>)[0];

    expect(step.step_profile_id).toBe('system:coding');
    expect(step.step_profile).toBeUndefined();
  });

  it('serializes only step profile deltas against the preset', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      {
        ...form.steps[0],
        name: 'implement',
        stepProfileId: 'system:coding',
        stepProfileMode: 'soft',
        stepProfileBaseMode: 'soft',
        stepProfileAllowToolSearch: false,
        stepProfileBaseAllowToolSearch: true,
        stepProfileMatrix: [
          { category: 'filesystem', capabilities: ['read', 'write'] },
          { category: 'shell', capabilities: ['privileged', 'write'] },
          { category: 'web', capabilities: ['read'] }
        ],
        stepProfileBaseMatrix: [
          { category: 'filesystem', capabilities: ['read', 'write'] },
          { category: 'shell', capabilities: ['privileged', 'write'] }
        ]
      }
    ];

    const payload = formStateToWorkflowPayload(form);
    const step = (payload.steps as Array<Record<string, unknown>>)[0];

    expect(step.step_profile).toEqual({
      matrix: {
        web: ['read']
      },
      tool_overrides: {
        include: [],
        exclude: []
      },
      allow_tool_search: false
    });
  });

  it('serializes removed preset categories as empty capability rows', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      {
        ...form.steps[0],
        name: 'review',
        stepProfileId: 'system:review',
        stepProfileMode: 'soft',
        stepProfileBaseMode: 'soft',
        stepProfileAllowToolSearch: true,
        stepProfileBaseAllowToolSearch: true,
        stepProfileMatrix: [{ category: 'filesystem', capabilities: ['read'] }],
        stepProfileBaseMatrix: [
          { category: 'filesystem', capabilities: ['read'] },
          { category: 'web', capabilities: ['read'] }
        ]
      }
    ];

    const payload = formStateToWorkflowPayload(form);
    const step = (payload.steps as Array<Record<string, unknown>>)[0];

    expect(step.step_profile).toEqual({
      matrix: {
        web: []
      },
      tool_overrides: {
        include: [],
        exclude: []
      },
      allow_tool_search: true
    });
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

  it('preserves deterministic condition loop budgets', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      { ...form.steps[0], name: 'implement' },
      {
        ...form.steps[0],
        name: 'review_route',
        type: 'condition',
        conditionExpression: "steps.review.metadata.decision == 'revise'",
        conditionThen: 'implement',
        conditionMaxLoopIterations: 5,
        conditionOnExhausted: 'gate'
      }
    ];

    const payload = formStateToWorkflowPayload(form);
    const route = (payload.steps as Array<Record<string, unknown>>)[1];

    expect(route.condition).toEqual({
      if: "steps.review.metadata.decision == 'revise'",
      then: 'implement',
      else: undefined,
      output: undefined,
      revision_source: undefined,
      max_loop_iterations: 5,
      on_exhausted: 'gate'
    });
  });

  it('does not add a loop budget to legacy conditions', () => {
    const form = createEmptyWorkflowForm();
    form.steps = [
      { ...form.steps[0], name: 'implement' },
      {
        ...form.steps[0],
        name: 'route',
        type: 'condition',
        conditionExpression: 'true',
        conditionThen: 'implement'
      }
    ];

    const route = (
      formStateToWorkflowPayload(form).steps as Array<Record<string, unknown>>
    )[1].condition as Record<string, unknown>;

    expect(route.max_loop_iterations).toBeUndefined();
    expect(route.on_exhausted).toBeUndefined();
  });
});
