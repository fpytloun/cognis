import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Workflow } from '$lib/types/api';
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
    expect(screen.getByText(/Tool call · In progress · 1.2 s/)).toBeTruthy();
    expect(screen.getByTestId('task-cockpit-action-inspect')).toBeTruthy();
    expect(screen.getByTestId('task-cockpit-evidence-inspect')).toHaveTextContent(/Tool:\s*alertmanager\.alerts/);
    expect(screen.getByTestId('task-cockpit-evidence-inspect')).toHaveTextContent(/Branch:\s*investigate/);
    expect(screen.getByTestId('task-cockpit-evidence-inspect')).toHaveTextContent(/Recovery:\s*safe/);
    expect(onStepOutputOpen).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('button', { name: 'Result' }));
    expect(onStepOutputOpen).toHaveBeenCalledWith('inspect');
    await fireEvent.click(screen.getByRole('button', { name: 'Transcript' }));
    expect(onStepLogsOpen).toHaveBeenCalledWith('inspect');
    await fireEvent.click(screen.getByRole('button', { name: /inspect/i }));
    expect(onStepSelect).toHaveBeenCalledWith('inspect');
  });

  it('shows the open step of a paused task as paused without a running clock', () => {
    render(WorkflowPhases, {
      projection: {
        workflow_id: 'workflow:general',
        workflow_version: 1,
        workflow_digest: 'sha256:paused',
        current_phase_id: 'execute',
        current_step_name: 'execute',
        phases: [{
          id: 'execute',
          title: 'Execute',
          description: '',
          status: 'waiting',
          steps: [
            { name: 'execute', type: 'run', status: 'waiting', attempt_count: 1, max_attempts: 1, started_at: '2026-11-08T18:00:00Z', completed_at: null, duration_seconds: 2826, action_required: false, pause_type: null, summary: null, error: null, has_output: false, has_logs: true, has_deliverable: false, skip_reason: null, step_run_id: 'run-1', output_url: null, logs_url: null, deliverables_url: null, metadata: {} },
            { name: 'report', type: 'run', status: 'pending', attempt_count: 0, max_attempts: 1, started_at: null, completed_at: null, duration_seconds: null, action_required: false, pause_type: null, summary: null, error: null, has_output: false, has_logs: false, has_deliverable: false, skip_reason: null, step_run_id: null, output_url: null, logs_url: null, deliverables_url: null, metadata: {} }
          ]
        }]
      },
      taskStatus: 'paused',
      onStepSelect: vi.fn()
    });

    expect(screen.getByText('Agent · Paused')).toBeTruthy();
    expect(screen.getByText('Agent · Not started')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Execute · Paused' })).toBeTruthy();
    expect(screen.queryByText(/47m/)).toBeNull();
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

  it('shows the planned steps of a draft instead of claiming no workflow', () => {
    render(WorkflowPhases, {
      projection: null,
      plannedWorkflow: {
        workflow_id: 'system:general-task', name: 'General Task',
        steps: [{ name: 'execute', type: 'run' }, { name: 'review', type: 'gate' }]
      } as unknown as Workflow,
      taskStatus: 'draft',
      onStepSelect: vi.fn()
    });
    expect(screen.queryByText('No workflow assigned')).toBeNull();
    expect(screen.getByText('Will run: execute → review')).toBeTruthy();
    expect(screen.getByText('Submit the task to start the first step.')).toBeTruthy();
  });
});
