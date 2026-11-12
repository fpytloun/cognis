import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { page as pageStore } from '$app/stores';
import type { Writable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SchedulesWorkspace from './SchedulesWorkspace.svelte';

const { listSchedules, goto } = vi.hoisted(() => ({
  listSchedules: vi.fn(),
  goto: vi.fn(),
}));

vi.mock('$app/stores', async () => {
  const { writable } = await import('svelte/store');
  return { page: writable({ url: new URL('http://localhost/schedules') }) };
});
const page = pageStore as unknown as Writable<{ url: URL }>;
vi.mock('$app/navigation', () => ({ goto }));
vi.mock('$lib/api/client', () => ({
  api: {
    schedules: { list: listSchedules },
    agents: { listAll: vi.fn().mockResolvedValue([]) },
    projects: {
      list: vi.fn().mockResolvedValue([
        { project_id: 'project-a', name: 'Project A' },
        { project_id: 'project-b', name: 'Project B' },
      ]),
    },
  },
  asApiError: (error: unknown) => error instanceof Error ? error : new Error(String(error)),
}));

beforeEach(() => {
  listSchedules.mockReset().mockResolvedValue([]);
  goto.mockReset().mockImplementation(async (path: string) => {
    page.set({ url: new URL(path, 'http://localhost') });
  });
  page.set({ url: new URL('http://localhost/schedules') });
});

afterEach(cleanup);

describe('SchedulesWorkspace project filter', () => {
  it('keeps dropdown changes, requests the project and synchronizes the URL', async () => {
    render(SchedulesWorkspace, { canMutate: false });
    const filter = await screen.findByRole('combobox', { name: 'Project filter' });
    await screen.findByRole('option', { name: 'Project A' });
    await fireEvent.change(filter, { target: { value: 'project-a' } });

    await waitFor(() => expect(listSchedules).toHaveBeenLastCalledWith({ project_id: 'project-a' }));
    await waitFor(() => expect(goto).toHaveBeenLastCalledWith(
      '/schedules?project_id=project-a',
      { replaceState: true, noScroll: true, keepFocus: true },
    ));
    expect(screen.getByRole('combobox', { name: 'Project filter' })).toHaveValue('project-a');

    await fireEvent.change(screen.getByRole('combobox', { name: 'Project filter' }), {
      target: { value: '' },
    });
    await waitFor(() => expect(listSchedules).toHaveBeenLastCalledWith({ project_id: null }));
    await waitFor(() => expect(goto).toHaveBeenLastCalledWith('/schedules', expect.any(Object)));
    expect(screen.getByRole('combobox', { name: 'Project filter' })).toHaveValue('');
  });

  it('hydrates deep links and subsequent navigation, including the legacy project parameter', async () => {
    page.set({ url: new URL('http://localhost/schedules?project=project-a') });
    render(SchedulesWorkspace, { canMutate: false });
    await waitFor(() => expect(listSchedules).toHaveBeenLastCalledWith({ project_id: 'project-a' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Project filter' })).toHaveValue('project-a'));

    page.set({ url: new URL('http://localhost/schedules?project_id=project-b') });
    await waitFor(() => expect(listSchedules).toHaveBeenLastCalledWith({ project_id: 'project-b' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Project filter' })).toHaveValue('project-b'));
  });

  it('keeps an embedded project scope authoritative over page URL parameters', async () => {
    page.set({ url: new URL('http://localhost/projects/project-a?project_id=project-b') });
    const view = render(SchedulesWorkspace, {
      projectId: 'project-a', embedded: true, canMutate: false,
    });
    await waitFor(() => expect(listSchedules).toHaveBeenLastCalledWith({ project_id: 'project-a' }));
    const filter = await screen.findByRole('combobox', { name: 'Project filter' });
    expect(filter).toBeDisabled();
    await waitFor(() => expect(filter).toHaveValue('project-a'));

    await view.rerender({ projectId: 'project-b', embedded: true, canMutate: false });
    await waitFor(() => expect(listSchedules).toHaveBeenLastCalledWith({ project_id: 'project-b' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Project filter' })).toHaveValue('project-b'));
    expect(goto).not.toHaveBeenCalled();
  });
});
