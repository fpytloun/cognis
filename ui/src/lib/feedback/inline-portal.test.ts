import { afterEach, expect, it, vi } from 'vitest';
import { inlinePortal } from './inline-portal';

let destroy: (() => void) | undefined;
afterEach(() => { destroy?.(); destroy = undefined; document.body.replaceChildren(); });

function setup() {
  const owner = document.createElement('div'), host = document.createElement('div');
  const editor = document.createElement('textarea');
  editor.value = 'Preserved draft';
  owner.append(editor);
  document.body.append(owner, host);
  return { owner, host, editor };
}

it('preserves the editor when leaving inline mode and removes it on teardown', () => {
  const { owner, host, editor } = setup();
  const action = inlinePortal(editor, { inline: true, active: true, host: () => host });
  destroy = action.destroy;
  expect(editor.parentNode).toBe(host);
  expect(editor.hidden).toBe(false);
  action.update({ inline: false, active: true, host: () => host });
  expect(editor.parentNode).toBe(owner);
  expect(editor.value).toBe('Preserved draft');
  action.destroy(); destroy = undefined;
  expect(editor.isConnected).toBe(false);
  expect(owner.childNodes).toHaveLength(0);
});

it('never resurrects an editor already detached by its framework owner', () => {
  const { owner, editor } = setup();
  const action = inlinePortal(editor, { inline: false, active: true, host: () => null });
  editor.remove();
  action.destroy();
  expect(editor.isConnected).toBe(false);
  expect(owner.childNodes).toHaveLength(0);
});

it('hides unavailable or inactive source editors rather than floating them elsewhere', () => {
  const { owner, host, editor } = setup();
  const action = inlinePortal(editor, { inline: true, active: true, host: () => null });
  destroy = action.destroy;
  expect(editor.parentNode).toBe(owner);
  expect(editor.hidden).toBe(true);
  expect(editor.inert).toBe(true);
  action.update({ inline: true, active: true, host: () => host });
  expect(editor.parentNode).toBe(host);
  expect(editor.hidden).toBe(false);
  action.update({ inline: true, active: false, host: () => host });
  expect(editor.hidden).toBe(true);
  expect(editor.inert).toBe(true);
});

it('follows a remounted native source while preserving draft input', async () => {
  const { owner, host, editor } = setup();
  let current: HTMLElement | null = host;
  const action = inlinePortal(editor, { inline: true, active: true, host: () => current });
  destroy = action.destroy;
  host.remove(); current = null;
  await vi.waitFor(() => expect(editor.parentNode).toBe(owner));
  expect(editor.hidden).toBe(true);
  current = document.createElement('div'); document.body.append(current);
  await vi.waitFor(() => expect(editor.parentNode).toBe(current));
  expect(editor.value).toBe('Preserved draft');
  expect(editor.hidden).toBe(false);
});
