import { describe, expect, it } from 'vitest';
import { COMPLETION_MODE_OPTIONS, completionModeLabel, resolveCompletionMode } from './task-delivery';
import { createEmptyWorkflowForm, workflowToFormState } from './workflows';
import type { Workflow } from './types/api';

describe('task outcome delivery policies', () => {
  it('labels the new hybrid Default and preserves the saved Agent wire value', () => {
    expect(COMPLETION_MODE_OPTIONS.map((option) => [option.value, option.name])).toEqual([
      ['auto', 'Default'], ['default', 'Agent'], ['direct', 'Direct'],
    ]);
    expect(completionModeLabel('default')).toBe('Agent');
    expect(completionModeLabel('auto')).toBe('Default');
    expect(resolveCompletionMode(undefined)).toBe('auto');
    expect(createEmptyWorkflowForm().defaultCompletionModeFamily).toBe('auto');
  });

  it.each(['auto', 'default', 'direct'] as const)('round-trips workflow policy %s', (mode) => {
    const workflow: Workflow = {
      workflow_id: 'workflow-delivery-policy', name: 'Policy', description: '', steps: [],
      version: 1, criteria: '', tags: [], interaction: {}, is_system: false,
      owner_email: 'owner@example.com', lifecycle: 'persistent', archived_at: null,
      lineage: null, editable_fields: [], has_overrides: false, disabled: false,
      disableable: true, override_warnings: [],
      defaults: { delivery: { completion_mode_family: mode, allow_silent_completion: false } },
    };
    expect(workflowToFormState(workflow).defaultCompletionModeFamily).toBe(mode);
  });
});
