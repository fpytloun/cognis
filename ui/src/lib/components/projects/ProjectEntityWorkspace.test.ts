import { cleanup, render, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ProjectEntityWorkspace from './ProjectEntityWorkspace.svelte';
import { getDashboardLauncher, type DashboardLaunchInput } from '$lib/dashboard/launcher';

const mocks = vi.hoisted(() => ({
  setAuth: null as null | ((email: string | null) => void),
  loadPreferences: vi.fn(async () => {}),
}));
vi.mock('$lib/stores/auth', async () => {
  const { writable, get } = await import('svelte/store');
  const state = writable({ user: { email: 'owner@example.com' } as { email: string } | null });
  mocks.setAuth = email => state.set({ user: email ? { email } : null });
  return { auth: { subscribe: state.subscribe, getSnapshot: () => get(state) } };
});
vi.mock('$lib/stores/userPreferences', async () => {
  const actual = await vi.importActual<typeof import('$lib/stores/userPreferences')>(
    '$lib/stores/userPreferences',
  );
  const { writable, get } = await import('svelte/store');
  const defaults = get(actual.userPreferences);
  return {
    ...actual,
    userPreferences: writable({
      ...defaults, display: { ...defaults.display, dashboard_workspace_windows: false },
    }),
    loadUserPreferences: mocks.loadPreferences,
  };
});

beforeEach(() => {
  mocks.setAuth?.('owner@example.com');
  vi.stubGlobal('matchMedia', vi.fn((media: string) => ({
    media, matches: false, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
    dispatchEvent: () => false,
  })));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const input = (kind: DashboardLaunchInput['kind'], project_id = 'project-a'): DashboardLaunchInput => ({
  kind, project_id, id: `selected-${kind}`, title: 'Selected result', href: '/tasks/selected-task',
});
async function mount() {
  const onNavigate = vi.fn(async () => {});
  const view = render(ProjectEntityWorkspace, {
    projectId: 'project-a', userEmail: 'owner@example.com',
    kind: null, entityId: null, canMutateSchedule: false, agents: [],
    onNavigate, onClose: vi.fn(), onRefresh: vi.fn(async () => {}),
  });
  await waitFor(() => expect(getDashboardLauncher()?.projectId).toBe('project-a'));
  return { ...view, onNavigate, launcher: getDashboardLauncher()! };
}

describe('project Spotlight launcher lifecycle', () => {
  it('registers the active project while workspace preferences are still loading', async () => {
    mocks.loadPreferences.mockImplementationOnce(() => new Promise<void>(() => {}));
    const view = await mount();
    await view.launcher.open(input('task'));
    expect(view.onNavigate).toHaveBeenCalledWith('task', 'selected-task');
  });
  it('uses project navigation for every supported entity kind, even with modal fallback', async () => {
    const view = await mount();
    for (const kind of ['task', 'conversation', 'schedule', 'artifact', 'deliverable'] as const) {
      await view.launcher.open(input(kind));
      expect(view.onNavigate).toHaveBeenLastCalledWith(kind, `selected-${kind}`);
    }
  });
  it('rejects foreign project inputs without navigating', async () => {
    const view = await mount();
    await expect(view.launcher.open(input('task', 'project-b'))).rejects.toThrow();
    expect(view.onNavigate).not.toHaveBeenCalled();
  });
  it('retires retained handles when the project unmounts', async () => {
    const view = await mount();
    view.unmount();
    await expect(view.launcher.open(input('task'))).rejects.toThrow();
    expect(getDashboardLauncher()).toBeNull();
  });
  it('retires the project launcher on account changes', async () => {
    const view = await mount();
    mocks.setAuth?.('other@example.com');
    await waitFor(() => expect(getDashboardLauncher()).toBeNull());
    await expect(view.launcher.open(input('task'))).rejects.toThrow();
    expect(view.onNavigate).not.toHaveBeenCalled();
  });
});
