import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Schedule, Workflow } from '$lib/types/api';
import ScheduleDashboardModal from './ScheduleDashboardModal.svelte';

const { workflowDetail, scheduleDetail } = vi.hoisted(() => ({
  workflowDetail: vi.fn(),
  scheduleDetail: vi.fn(),
}));

vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$lib/api/client', () => ({
  api: {
    workflows: { detail: workflowDetail },
    schedules: { detail: scheduleDetail },
  },
  asApiError: (error: unknown) => error instanceof Error ? error : new Error(String(error))
}));

afterEach(cleanup);
beforeEach(() => {
  workflowDetail.mockReset();
  scheduleDetail.mockReset();
});

function schedule(workflowId: string | null): Schedule {
  return {
    schedule_id: 'schedule-1',
    name: 'Daily review',
    description: 'Review current work.',
    schedule_type: 'cron',
    cron_expr: '0 8 * * *',
    interval_seconds: null,
    one_shot_at: null,
    timezone: 'UTC',
    agent_id: 'riker',
    workflow_id: workflowId,
    project_id: 'project-1',
    skill_id: null,
    task_template: { title: 'Review task', description: 'Review the workspace.' },
    enabled: true,
    max_concurrent_runs: 1,
    delete_after_run: false,
    retry_failed_tasks: false,
    fail_paused_task_on_next_fire: false,
    completion_mode_family: 'default',
    allow_silent_completion: false,
    interaction_mode_override: null,
    session_policy: null,
    last_fired_at: null,
    next_fire_at: '2026-08-24T08:00:00Z',
    last_run_status: null,
    consecutive_errors: 0,
    disabled_reason: null,
    created_by: 'user@example.com',
    created_at: '2026-08-23T00:00:00Z',
    updated_at: '2026-08-23T00:00:00Z',
    human_schedule: 'Every day at 08:00 UTC',
    is_expired: false,
    expiration_grace_until: null
  };
}

describe('ScheduleDashboardModal', () => {
  it('shows only Description and Planned steps and loads the workflow once', async () => {
    scheduleDetail.mockResolvedValue(schedule('workflow-1'));
    workflowDetail.mockResolvedValue({
      workflow_id: 'workflow-1',
      steps: [
        { name: 'prepare', type: 'run', description: 'Prepare evidence.' },
        { name: 'review', type: 'gate', description: 'Review evidence.' }
      ]
    } as Workflow);
    render(ScheduleDashboardModal, {
      scheduleId: 'schedule-1',
      agents: [],
      onClose: vi.fn()
    });

    expect(await screen.findByTestId('dashboard-schedule-modal-tab-description')).toBeInTheDocument();
    expect(screen.queryByTestId('dashboard-schedule-modal-tab-chat')).not.toBeInTheDocument();
    expect(screen.queryByTestId('dashboard-schedule-modal-tab-activity')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByTestId('dashboard-schedule-modal-tab-steps'));
    await waitFor(() => expect(screen.getByTestId('dashboard-schedule-planned-steps')).toHaveTextContent('Prepare evidence.'));
    await fireEvent.click(screen.getByTestId('dashboard-schedule-modal-tab-description'));
    await fireEvent.click(screen.getByTestId('dashboard-schedule-modal-tab-steps'));
    expect(workflowDetail).toHaveBeenCalledTimes(1);
  });

  it('shows the ad-hoc state without a workflow request', async () => {
    scheduleDetail.mockResolvedValue(schedule(null));
    render(ScheduleDashboardModal, {
      scheduleId: 'schedule-1',
      agents: [],
      onClose: vi.fn()
    });

    await fireEvent.click(await screen.findByTestId('dashboard-schedule-modal-tab-steps'));
    expect(screen.getByTestId('dashboard-schedule-ad-hoc')).toBeInTheDocument();
    expect(workflowDetail).not.toHaveBeenCalled();
  });

  it('retries a transient workflow failure while the modal remains open', async () => {
    scheduleDetail.mockResolvedValue(schedule('workflow-1'));
    workflowDetail
      .mockRejectedValueOnce(new Error('Temporary workflow failure'))
      .mockResolvedValueOnce({
        workflow_id: 'workflow-1',
        steps: [{ name: 'prepare', type: 'run', description: 'Prepare evidence.' }]
      } as Workflow);
    render(ScheduleDashboardModal, {
      scheduleId: 'schedule-1',
      agents: [],
      onClose: vi.fn()
    });

    await fireEvent.click(await screen.findByTestId('dashboard-schedule-modal-tab-steps'));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Temporary workflow failure'));
    await fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByTestId('dashboard-schedule-planned-steps')).toHaveTextContent('Prepare evidence.'));
    expect(workflowDetail).toHaveBeenCalledTimes(2);
  });

  it('ignores stale detail responses after the schedule ID changes', async () => {
    let resolveFirst!: (value: Schedule) => void;
    scheduleDetail
      .mockReturnValueOnce(new Promise<Schedule>((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValueOnce({ ...schedule(null), schedule_id: 'schedule-2', name: 'Current schedule' });
    const metadata = vi.fn();
    const view = render(ScheduleDashboardModal, {
      scheduleId: 'schedule-1',
      agents: [],
      onClose: vi.fn(),
      onMetadataChange: metadata,
    });

    await view.rerender({
      scheduleId: 'schedule-2',
      agents: [],
      onClose: vi.fn(),
      onMetadataChange: metadata,
    });
    expect(await screen.findByText('Current schedule')).toBeInTheDocument();
    resolveFirst(schedule(null));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('Daily review')).not.toBeInTheDocument();
    expect(metadata).toHaveBeenLastCalledWith(expect.objectContaining({ title: 'Current schedule' }));
  });

  it('replaces a delayed workflow when schedule refresh changes the workflow ID', async () => {
    let resolveOldWorkflow!: (value: Workflow) => void;
    scheduleDetail
      .mockResolvedValueOnce(schedule('workflow-1'))
      .mockResolvedValueOnce({ ...schedule('workflow-2'), updated_at: '2026-08-24T00:00:00Z' });
    workflowDetail
      .mockReturnValueOnce(new Promise<Workflow>((resolve) => { resolveOldWorkflow = resolve; }))
      .mockResolvedValueOnce({
        workflow_id: 'workflow-2',
        steps: [{ name: 'current', type: 'run', description: 'Current workflow.' }],
      } as Workflow);
    const view = render(ScheduleDashboardModal, {
      scheduleId: 'schedule-1',
      modalRefreshToken: 0,
      agents: [],
      onClose: vi.fn(),
    });
    await fireEvent.click(await screen.findByTestId('dashboard-schedule-modal-tab-steps'));

    await view.rerender({
      scheduleId: 'schedule-1',
      modalRefreshToken: 1,
      agents: [],
      onClose: vi.fn(),
    });
    expect(await screen.findByText('Current workflow.')).toBeInTheDocument();
    resolveOldWorkflow({
      workflow_id: 'workflow-1',
      steps: [{ name: 'old', type: 'run', description: 'Stale workflow.' }],
    } as Workflow);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('Stale workflow.')).not.toBeInTheDocument();
  });
});
