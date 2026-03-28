import { describe, expect, it } from 'vitest';

import { createEmptyWorkflowForm, importWorkflowYaml, validateWorkflowForm } from '$lib/workflows';

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
});
