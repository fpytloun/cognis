import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import WorkflowPhases from './WorkflowPhases.svelte';

afterEach(cleanup);

describe('Task Cockpit workflow phases', () => {
  it('renders backend projection and invokes lazy detail actions only on demand', async () => {
    const onStepSelect = vi.fn();
    const onStepOutputOpen = vi.fn();
    const onStepLogsOpen = vi.fn();
    render(WorkflowPhases, {
      projection: {
        workflow_id: 'workflow:triage',
        workflow_version: 3,
        workflow_digest: 'sha256:projection',
        current_phase_id: 'investigate',
        current_step_name: 'inspect',
        phases: [{
          id: 'investigate',
          title: 'Investigate',
          description: 'Inspect bounded evidence.',
          status: 'active',
          steps: [{
            name: 'inspect',
            type: 'tool_call',
            status: 'active',
            attempt_count: 1,
            max_attempts: 1,
            started_at: '2026-07-30T12:00:00Z',
            completed_at: '2026-07-30T12:00:01.200Z',
            duration_seconds: 1.2,
            action_required: true,
            pause_type: 'gate',
            summary: 'Inspection complete',
            error: null,
            has_output: true,
            has_logs: true,
            has_deliverable: false,
            skip_reason: null,
            step_run_id: 'step-run-inspect',
            output_url: '/api/v1/step-runs/step-run-inspect',
            logs_url: '/api/v1/chat/v2/task-steps/step-run-inspect/snapshot',
            deliverables_url: null,
            metadata: {
              execution_kind: 'deterministic',
              tool_name: 'alertmanager.alerts',
              selected_branch: 'investigate',
              deterministic_substate: 'dispatched',
              recovery_state: 'safe'
            }
          }]
        }]
      },
      selectedStepName: '',
      onStepSelect,
      onStepOutputOpen,
      onStepLogsOpen
    });

    expect(screen.getByText('Investigate')).toBeTruthy();
    expect(screen.getByText(/Tool call · active · 1.2 s/)).toBeTruthy();
    expect(screen.getByTestId('task-cockpit-action-inspect')).toBeTruthy();
    expect(screen.getByTestId('task-cockpit-evidence-inspect')).toHaveTextContent(/Tool:\s*alertmanager\.alerts/);
    expect(screen.getByTestId('task-cockpit-evidence-inspect')).toHaveTextContent(/Branch:\s*investigate/);
    expect(screen.getByTestId('task-cockpit-evidence-inspect')).toHaveTextContent(/Recovery:\s*safe/);
    expect(onStepOutputOpen).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('button', { name: 'Output' }));
    expect(onStepOutputOpen).toHaveBeenCalledWith('inspect');
    await fireEvent.click(screen.getByRole('button', { name: 'Logs' }));
    expect(onStepLogsOpen).toHaveBeenCalledWith('inspect');
    await fireEvent.click(screen.getByRole('button', { name: /inspect/i }));
    expect(onStepSelect).toHaveBeenCalledWith('inspect');
  });

  it('renders an explicit workflow-less state', () => {
    render(WorkflowPhases, {
      projection: null,
      onStepSelect: vi.fn(),
      onStepOutputOpen: vi.fn(),
      onStepLogsOpen: vi.fn()
    });
    expect(screen.getByText('No workflow assigned')).toBeTruthy();
  });
});
