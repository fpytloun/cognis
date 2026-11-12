import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import ChatHeaderMenuPanel from './ChatHeaderMenuPanel.svelte';

afterEach(() => { cleanup(); document.body.replaceChildren(); });

it('keeps commenting in mobile overflow and closes the menu before executing an action', async () => {
  const trigger = document.createElement('button');
  document.body.append(trigger);
  const onClose = vi.fn();
  const run = vi.fn(() => expect(onClose).toHaveBeenCalledOnce());
  render(ChatHeaderMenuPanel, { trigger, onClose, items: [
    { label: 'Enable commenting', icon: 'comment', checked: false, run },
    { label: 'Show comments', icon: 'comments', run: vi.fn() },
    { label: 'Search conversation', icon: 'search', run: vi.fn() },
  ] });
  const toggle = screen.getByRole('menuitemcheckbox', { name: 'Enable commenting' });
  expect(toggle).toHaveAttribute('aria-checked', 'false');
  expect(screen.getByRole('menuitem', { name: 'Show comments' })).toBeVisible();
  for (const item of screen.getAllByRole('menuitem')) expect(item.querySelector('svg')).not.toBeNull();
  expect(toggle.querySelector('svg')).not.toBeNull();
  expect(screen.queryByRole('menuitem', { name: 'Navigation' })).toBeNull();
  expect(screen.queryByRole('menuitem', { name: /separate window/i })).toBeNull();
  await fireEvent.click(toggle);
  expect(run).toHaveBeenCalledOnce();
});

it('shows the current commenting state and respects read-only actions', async () => {
  const trigger = document.createElement('button');
  document.body.append(trigger);
  const run = vi.fn();
  render(ChatHeaderMenuPanel, { trigger, onClose: vi.fn(), items: [
    { label: 'Disable commenting', icon: 'comment', checked: true, disabled: true, run },
  ] });
  const toggle = screen.getByRole('menuitemcheckbox', { name: 'Disable commenting' });
  expect(toggle).toHaveAttribute('aria-checked', 'true');
  expect(toggle).toBeDisabled();
  toggle.click();
  expect(run).not.toHaveBeenCalled();
});
