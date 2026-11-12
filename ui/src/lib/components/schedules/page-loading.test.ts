import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import { api } from '$lib/api/client';
import type { Agent, Schedule } from '$lib/types/api';
import SchedulesWorkspace from './SchedulesWorkspace.svelte';
import ScheduleDetailWorkspace from './ScheduleDetailWorkspace.svelte';

vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$app/stores', () => ({ page: writable({ url: new URL('http://localhost/schedules') }) }));
vi.mock('$lib/stores/auth', () => ({
  auth: writable({ user: { role: 'user', email: 'owner@cognis.local' } })
}));
vi.mock('$lib/stores/toasts', () => ({ addToast: vi.fn() }));

const schedule = {
  schedule_id: 'daily-review', name: 'Daily review', schedule_type: 'cron',
  cron_expr: '0 9 * * *', timezone: 'UTC', agent_id: 'reviewer',
  agent_profile_id: 'careful', enabled: true, task_template: {},
  human_schedule: 'Every day at 09:00', consecutive_errors: 0,
  completion_mode_family: 'default', allow_silent_completion: false,
  max_concurrent_runs: 1, retry_failed_tasks: false, fail_paused_task_on_next_fire: true
} as Schedule;

function pending<T>(): Promise<T> { return new Promise(() => {}); }

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(api.schedules, 'list').mockResolvedValue([schedule]);
  vi.spyOn(api.schedules, 'detail').mockResolvedValue(schedule);
  vi.spyOn(api.schedules, 'runs').mockResolvedValue([]);
  vi.spyOn(api.agents, 'listAll').mockResolvedValue([]);
  vi.spyOn(api.projects, 'list').mockResolvedValue([]);
  vi.spyOn(api.skills, 'list').mockResolvedValue([]);
  vi.spyOn(api.workflows, 'listAll').mockResolvedValue([]);
  vi.spyOn(api.conversations, 'listAll').mockResolvedValue([]);
});
afterEach(cleanup);

describe('schedule loading boundaries', () => {
  it('allows a short prompt with more than six lines to be expanded', async () => {
    const prompt = 'First\nSecond\nThird\nFourth\nFifth\nSixth\nSeventh';
    vi.mocked(api.schedules.detail).mockResolvedValue({
      ...schedule, task_template: { description: prompt },
    });
    render(ScheduleDetailWorkspace, { id: schedule.schedule_id });
    const expand = await screen.findByRole('button', { name: 'Show full prompt' });
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    await fireEvent.click(expand);
    expect(screen.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('schedule-prompt')).toHaveTextContent('Seventh');
  });

  it('loads and saves follow-up instructions in the schedule edit form', async () => {
    const detail = { ...schedule, on_finish_instructions: 'Report the useful changes.' };
    vi.mocked(api.schedules.detail).mockResolvedValue(detail);
    const update = vi.spyOn(api.schedules, 'update').mockResolvedValue(detail);
    render(ScheduleDetailWorkspace, { id: schedule.schedule_id });
    await fireEvent.click(await screen.findByRole('button', { name: 'Edit schedule' }));
    const instructions = await screen.findByRole('textbox', { name: /Finish instructions/ });
    expect(instructions).toHaveValue('Report the useful changes.');
    await fireEvent.input(instructions, { target: { value: '  Check the result before reporting.  ' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(schedule.schedule_id, expect.objectContaining({
      completion_mode_family: 'default',
      on_finish_instructions: 'Check the result before reporting.',
    })));
  });

  it('keeps follow-up instructions discoverable when an existing schedule uses direct delivery', async () => {
    vi.mocked(api.schedules.detail).mockResolvedValue({ ...schedule, completion_mode_family: 'direct' });
    render(ScheduleDetailWorkspace, { id: schedule.schedule_id });
    await fireEvent.click(await screen.findByRole('button', { name: 'Edit schedule' }));
    const instructions = await screen.findByRole('textbox', { name: /Finish instructions/ });
    expect(instructions).toBeDisabled();
    await fireEvent.change(screen.getByLabelText('Completion notification behavior'), { target: { value: 'default' } });
    expect(instructions).toBeEnabled();
  });

  it('opens as a read view and returns to it when editing is cancelled', async () => {
    render(ScheduleDetailWorkspace, { id: schedule.schedule_id });
    expect(await screen.findByTestId('schedule-read-view')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Edit schedule' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    expect(await screen.findByTestId('schedule-read-view')).toBeVisible();
  });

  it('renders schedules while labels are pending, without loading create-only registries', async () => {
    vi.mocked(api.agents.listAll).mockReturnValue(pending<Agent[]>());
    vi.mocked(api.projects.list).mockReturnValue(pending());
    render(SchedulesWorkspace);
    expect(await screen.findByText('Daily review')).toBeVisible();
    expect(api.schedules.list).toHaveBeenCalledTimes(1);
    expect(api.skills.list).not.toHaveBeenCalled();
    expect(api.workflows.listAll).not.toHaveBeenCalled();
    expect(api.conversations.listAll).not.toHaveBeenCalled();
  });

  it('loads create choices on demand and conversation history only for targeted delivery', async () => {
    render(SchedulesWorkspace);
    await fireEvent.click(await screen.findByRole('button', { name: 'New schedule' }));
    await waitFor(() => expect(api.workflows.listAll).toHaveBeenCalledTimes(1));
    expect(api.skills.list).toHaveBeenCalledTimes(1);
    expect(api.conversations.listAll).not.toHaveBeenCalled();
    const delivery = screen.getByDisplayValue('Preferred channel');
    await fireEvent.change(delivery, { target: { value: 'specific_conversation' } });
    await waitFor(() => expect(api.conversations.listAll).toHaveBeenCalledTimes(1));
  });

  it('keeps schedules visible after a label registry fails and refresh does not reload registries', async () => {
    vi.mocked(api.agents.listAll).mockRejectedValue(new Error('Agent registry unavailable'));
    const view = render(SchedulesWorkspace);
    expect(await screen.findByText('Daily review')).toBeVisible();
    await view.rerender({ projectId: 'engineering' });
    await waitFor(() => expect(api.schedules.list).toHaveBeenCalledWith({ project_id: 'engineering' }));
    expect(api.agents.listAll).toHaveBeenCalledTimes(1);
  });

  it('renders schedule status before configuration choices or runs and preserves the profile', async () => {
    let resolveAgents!: (items: Agent[]) => void;
    vi.mocked(api.agents.listAll).mockReturnValue(new Promise((resolve) => { resolveAgents = resolve; }));
    vi.mocked(api.schedules.runs).mockReturnValue(pending());
    render(ScheduleDetailWorkspace, { id: schedule.schedule_id });
    expect(await screen.findByRole('heading', { name: 'Daily review' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument();
    expect(api.conversations.listAll).not.toHaveBeenCalled();
    resolveAgents([{
      agent_id: 'reviewer', name: 'Reviewer', agent_profiles: { careful: {} }
    } as unknown as Agent]);
    await fireEvent.click(await screen.findByRole('button', { name: 'Edit schedule' }));
    await screen.findByRole('button', { name: 'Save changes' });
    expect(screen.getByDisplayValue('careful')).toBeInTheDocument();
    expect(api.workflows.listAll).toHaveBeenCalledTimes(1);
  });
});
