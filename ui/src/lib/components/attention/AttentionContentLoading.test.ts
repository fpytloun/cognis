import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'svelte';
import { loginFixtureAccount } from '../../../test/verified-account';
import { auth } from '$lib/stores/auth';
import { WorkspaceManager } from '$lib/dashboard/workspace';
import { deferredDefinition, type DeferredDefinition } from '../ui/deferred-definition';
import Content from './FocusedAttentionAction.loading-test-fixture.svelte';
import AttentionActionDialog from './AttentionActionDialog.svelte';
import WorkspaceLayer from '../dashboard/WorkspaceLayer.svelte';

const loading = vi.hoisted(() => ({
  definition: null as DeferredDefinition<ComponentProps<typeof Content>> | null,
}));
vi.mock('./focused-definition', () => ({
  focusedAttentionAction: {
    peek: () => loading.definition!.peek(),
    load: () => loading.definition!.load(),
  },
}));
vi.mock('$app/navigation', () => ({ beforeNavigate: vi.fn(), goto: vi.fn() }));
vi.mock('../dashboard/WorkspaceWindow.svelte', () => import('../dashboard/WorkspaceLayer.window-test-fixture.svelte'));
vi.mock('../dashboard/DashboardEntityModal.svelte', () => import('../dashboard/WorkspaceLayer.entity-test-fixture.svelte'));
vi.mock('../dashboard/WorkspaceDock.svelte', () => import('../dashboard/WorkspaceLayer.dock-test-fixture.svelte'));

let back: ReturnType<typeof vi.spyOn>;
beforeEach(async () => {
  await loginFixtureAccount();
  window.history.replaceState({}, '', '/');
  back = vi.spyOn(window.history, 'back').mockImplementation(() => {});
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener() {}, removeEventListener() {} });
});
afterEach(async () => {
  cleanup();
  back.mockRestore();
  window.history.replaceState({}, '', '/');
  await auth.clear();
});
function delayed() {
  let resolve!: (value: { default: typeof Content }) => void;
  const load = vi.fn(() => new Promise<{ default: typeof Content }>(done => { resolve = done; }));
  loading.definition = deferredDefinition(load);
  return { load, release: () => resolve({ default: Content }) };
}
function openWorkspace() {
  const manager = new WorkspaceManager('desktop', { x: 0, y: 0, width: 1200, height: 800 });
  manager.open({ kind: 'attention', entityId: 'loading-action', title: 'Pending attention', canonicalHref: '/' });
  return manager;
}
it('activates dialog history, focus and Escape while content is still downloading', async () => {
  const module = delayed();
  const close = vi.fn(), settled = vi.fn(), unavailable = vi.fn();
  const opener = document.createElement('button');
  document.body.append(opener); opener.focus();
  const view = render(AttentionActionDialog, {
    actionId: 'loading-action', title: 'Pending attention', onClose: close,
    onSettled: settled, onUnavailable: unavailable,
  });
  expect(window.history.state.cognisAttentionActionId).toBe('loading-action');
  await waitFor(() => expect(module.load).toHaveBeenCalledOnce());
  const closeButton = screen.getByRole('button', { name: 'Close Pending attention' });
  await waitFor(() => expect(document.activeElement).toBe(closeButton));
  await fireEvent.keyDown(window, { key: 'Escape' });
  expect(back).toHaveBeenCalledOnce();
  await fireEvent(window, new PopStateEvent('popstate'));
  expect(close).toHaveBeenCalledOnce();
  view.unmount(); module.release();
  await waitFor(() => expect(loading.definition!.peek()).toBe(Content));
  expect(screen.queryByLabelText('Action draft loading-action')).toBeNull();
  await waitFor(() => expect(document.activeElement).toBe(opener));
  opener.remove();
  expect(settled).not.toHaveBeenCalled();
  expect(unavailable).not.toHaveBeenCalled();
});
it('keeps the dialog host and history token while replacing a cold action, then retains warm DOM', async () => {
  const module = delayed();
  const callbacks = { onClose: vi.fn(), onSettled: vi.fn(), onUnavailable: vi.fn() };
  const view = render(AttentionActionDialog, { actionId: 'first-action', title: 'Pending attention', ...callbacks });
  const host = screen.getByRole('dialog', { name: 'Pending attention' });
  const token = window.history.state.cognisAttentionHistoryToken;
  await view.rerender({ actionId: 'second-action', title: 'Pending attention', ...callbacks });
  module.release();
  const input = await screen.findByLabelText('Action draft second-action');
  expect(screen.queryByLabelText('Action draft first-action')).toBeNull();
  expect(screen.getByRole('dialog', { name: 'Pending attention' })).toBe(host);
  expect(window.history.state.cognisAttentionHistoryToken).toBe(token);
  input.focus();
  await view.rerender({ actionId: 'second-action', title: 'Renamed attention', ...callbacks });
  expect(screen.getByLabelText('Action draft second-action')).toBe(input);
  expect(document.activeElement).toBe(input);
  expect(module.load).toHaveBeenCalledOnce();
});
it('keeps failed downloads retryable inside the same dialog without settling or marking unavailable', async () => {
  const load = vi.fn().mockRejectedValueOnce(new Error('Attention download failed.'))
    .mockResolvedValueOnce({ default: Content });
  loading.definition = deferredDefinition(load);
  const callbacks = { onClose: vi.fn(), onSettled: vi.fn(), onUnavailable: vi.fn() };
  render(AttentionActionDialog, { actionId: 'loading-action', title: 'Pending attention', ...callbacks });
  const host = screen.getByRole('dialog', { name: 'Pending attention' });
  const token = window.history.state.cognisAttentionHistoryToken;
  expect(await screen.findByRole('alert')).toHaveTextContent('Attention download failed.');
  expect(screen.getByRole('button', { name: 'Close Pending attention' })).toBeVisible();
  await fireEvent.click(screen.getByRole('button', { name: 'Retry attention action' }));
  expect(await screen.findByLabelText('Action draft loading-action')).toBeVisible();
  expect(screen.getByRole('dialog', { name: 'Pending attention' })).toBe(host);
  expect(window.history.state.cognisAttentionHistoryToken).toBe(token);
  expect(callbacks.onUnavailable).not.toHaveBeenCalled();
  expect(callbacks.onSettled).not.toHaveBeenCalled();
  expect(back).not.toHaveBeenCalled();
});
it('keeps the workspace layer eager and drops content when its host mode is removed', async () => {
  const module = delayed(), manager = openWorkspace();
  const settled = vi.fn(), unavailable = vi.fn();
  const view = render(WorkspaceLayer, {
    manager, deviceClass: 'desktop', onAttentionSettled: settled, onAttentionUnavailable: unavailable,
  });
  const layer = screen.getByTestId('workspace-layer');
  expect(screen.getByTestId('workspace-window-attention:loading-action')).toBeVisible();
  await waitFor(() => expect(module.load).toHaveBeenCalledOnce());
  await view.rerender({ manager, deviceClass: 'phone', onAttentionSettled: settled, onAttentionUnavailable: unavailable });
  module.release();
  await waitFor(() => expect(loading.definition!.peek()).toBe(Content));
  expect(screen.getByTestId('workspace-layer')).toBe(layer);
  expect(screen.queryByLabelText('Action draft loading-action')).toBeNull();
  expect(manager.snapshot()).toHaveLength(1);
  expect(settled).not.toHaveBeenCalled();
  expect(unavailable).not.toHaveBeenCalled();
});
it('does not mount cold workspace content after account invalidation or close its action', async () => {
  const module = delayed(), manager = openWorkspace();
  const settled = vi.fn(), unavailable = vi.fn();
  render(WorkspaceLayer, { manager, deviceClass: 'desktop', onAttentionSettled: settled, onAttentionUnavailable: unavailable });
  await waitFor(() => expect(module.load).toHaveBeenCalledOnce());
  await auth.clear();
  module.release();
  await waitFor(() => expect(loading.definition!.peek()).toBe(Content));
  expect(screen.queryByLabelText('Action draft loading-action')).toBeNull();
  expect(manager.snapshot()).toHaveLength(1);
  expect(settled).not.toHaveBeenCalled();
  expect(unavailable).not.toHaveBeenCalled();
});
it('keeps workspace download failure and retry in the same keyed window and preserves warm content', async () => {
  const load = vi.fn().mockRejectedValueOnce(new Error('Attention download failed.'))
    .mockResolvedValueOnce({ default: Content });
  loading.definition = deferredDefinition(load);
  const manager = openWorkspace(), settled = vi.fn(), unavailable = vi.fn();
  render(WorkspaceLayer, { manager, deviceClass: 'desktop', onAttentionSettled: settled, onAttentionUnavailable: unavailable });
  const host = screen.getByTestId('workspace-window-attention:loading-action');
  expect(await screen.findByRole('alert')).toHaveTextContent('Attention download failed.');
  expect(manager.snapshot()).toHaveLength(1);
  await fireEvent.click(screen.getByRole('button', { name: 'Retry attention action' }));
  const input = await screen.findByLabelText('Action draft loading-action');
  await fireEvent.input(input, { target: { value: 'Warm action draft' } });
  manager.minimize('attention:loading-action');
  manager.restore('attention:loading-action');
  await waitFor(() => expect(screen.getByTestId('workspace-window-attention:loading-action')).toBe(host));
  expect(screen.getByLabelText('Action draft loading-action')).toBe(input);
  expect(input).toHaveValue('Warm action draft');
  expect(load).toHaveBeenCalledTimes(2);
  expect(settled).not.toHaveBeenCalled();
  expect(unavailable).not.toHaveBeenCalled();
});
