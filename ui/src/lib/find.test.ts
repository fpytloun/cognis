import { afterEach, describe, expect, it, vi } from 'vitest';
import { findTarget, installFind, registerFindWorkspace, requestFind } from './find';
import { registerOverlay, resetOverlayState } from './stores/overlays';

const cleanups: Array<() => void> = [];
function input(parent: HTMLElement = document.body, options = {}) {
  const node = document.createElement('input');
  node.value = 'existing query';
  parent.append(node);
  const action = findTarget(node, options);
  cleanups.push(action.destroy);
  return node;
}
function key(options: KeyboardEventInit = {}) {
  const event = new KeyboardEvent('keydown', { key: 'f', ctrlKey: true, bubbles: true, cancelable: true, ...options });
  (document.activeElement ?? document.body).dispatchEvent(event);
  return event;
}
function start() {
  vi.spyOn(HTMLElement.prototype, 'getClientRects').mockImplementation(function () {
    return [{ width: 100, height: 30, top: 0, left: 0, right: 100, bottom: 30 }] as unknown as DOMRectList;
  });
  cleanups.push(installFind());
}
afterEach(() => {
  cleanups.splice(0).reverse().forEach((stop) => stop());
  resetOverlayState();
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe('contextual Find key delivery', () => {
  it('opens an explicitly selected inline document without changing the primary shortcut target', async () => {
    start();
    const chat = input(document.body, { primary: true });
    const root = document.createElement('section');
    const field = document.createElement('input');
    document.body.append(root); root.append(field);
    const registration = findTarget(root, { scopeOnly: true, open: async () => field });
    cleanups.push(registration.destroy);
    expect(requestFind(root)).toBe(true);
    await Promise.resolve();
    expect(document.activeElement).toBe(field);
    chat.focus();
    key();
    expect(document.activeElement).toBe(chat);
    const modal = registerOverlay({ kind: 'blocking', blocksChrome: true, root: () => chat });
    expect(requestFind(root)).toBe(false);
    modal.unregister();
  });
  it('keeps iframe-requested document find inside the current modal boundary, including deferred focus', async () => {
    start();
    const root = document.createElement('section'), field = document.createElement('input');
    document.body.append(root); root.append(field);
    let finish!: (input: HTMLInputElement) => void;
    const registration = findTarget(root, { primary: true, open: () => new Promise(resolve => { finish = resolve; }) });
    cleanups.push(registration.destroy);
    const blocked = registerOverlay({ kind: 'blocking', blocksChrome: true });
    expect(requestFind(root)).toBe(false);
    expect(document.activeElement).toBe(document.body);
    blocked.unregister();
    expect(requestFind(root)).toBe(true);
    const nested = registerOverlay({ kind: 'blocking', blocksChrome: true });
    finish(field);
    await Promise.resolve();
    expect(document.activeElement).toBe(document.body);
    nested.unregister();
    expect(requestFind(root)).toBe(true);
    finish(field);
    await Promise.resolve();
    expect(document.activeElement).toBe(field);
  });

  it('focuses and selects without input, change or submit events', () => {
    start();
    const target = input();
    const mutate = vi.fn();
    for (const type of ['input', 'change', 'submit']) target.addEventListener(type, mutate);
    expect(key().defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(target);
    expect([target.selectionStart, target.selectionEnd]).toEqual([0, target.value.length]);
    expect(target.value).toBe('existing query');
    expect(mutate).not.toHaveBeenCalled();
    target.setSelectionRange(2, 3);
    key({ ctrlKey: false, metaKey: true });
    expect(target.selectionStart).toBe(0);
  });

  it('preserves unsupported keys, composition and already handled events', () => {
    start();
    input();
    for (const options of [{ shiftKey: true }, { altKey: true }, { metaKey: true }, { isComposing: true }, { ctrlKey: false }, { key: 'Escape' }]) {
      expect(key(options).defaultPrevented).toBe(false);
      expect(document.activeElement).toBe(document.body);
    }
    const event = new KeyboardEvent('keydown', { key: 'f', ctrlKey: true, cancelable: true });
    event.preventDefault();
    window.dispatchEvent(event);
    expect(document.activeElement).toBe(document.body);
  });

  it('does not select hidden, inert, disabled or disconnected targets', () => {
    start();
    const target = input();
    for (const attribute of ['hidden', 'inert', 'disabled']) {
      target.setAttribute(attribute, '');
      expect(key().defaultPrevented).toBe(false);
      target.removeAttribute(attribute);
    }
    target.style.display = 'none';
    expect(key().defaultPrevented).toBe(false);
    target.remove();
    expect(key().defaultPrevented).toBe(false);
  });

  it('keeps overlays a hard boundary, including searchless nested sheets', () => {
    start();
    input(document.body, { primary: true });
    const root = document.createElement('section');
    document.body.append(root);
    const overlay = registerOverlay({ kind: 'sheet', blocksChrome: false, root: () => root });
    expect(key().defaultPrevented).toBe(false);
    const local = input(root);
    expect(key().defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(local);
    const nested = registerOverlay({ kind: 'blocking', blocksChrome: true });
    expect(key().defaultPrevented).toBe(false);
    nested.unregister();
    overlay.unregister();
  });

  it('fails closed for an unregistered modal rather than stealing background focus', () => {
    start();
    input(document.body, { primary: true });
    const dialog = document.createElement('section');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    document.body.append(dialog);
    expect(key().defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(document.body);
  });

  it('does not keep duplicate root listeners or pending requests after uninstall', async () => {
    start();
    const second = installFind();
    const target = input();
    const focus = vi.spyOn(target, 'focus');
    key();
    expect(focus).toHaveBeenCalledTimes(1);
    second();
  });

  it('prefers the active workspace search, falling back only to explicit dashboard primary', () => {
    start();
    const dashboard = input(document.body, { dashboardPrimary: true });
    const first = document.createElement('section');
    const second = document.createElement('section');
    document.body.append(first, second);
    let active: HTMLElement | null = first;
    cleanups.push(registerFindWorkspace(() => active));
    const local = input(first);
    const other = input(second);
    key();
    expect(document.activeElement).toBe(local);
    active = second;
    key();
    expect(document.activeElement).toBe(other);
    other.disabled = true;
    key();
    expect(document.activeElement).toBe(dashboard);
    dashboard.hidden = true;
    expect(key().defaultPrevented).toBe(false);
    active = null;
    first.hidden = true;
    second.hidden = true;
    expect(key().defaultPrevented).toBe(false);
  });

  it('prefers a focused explicit sub-scope, not an unrelated input', () => {
    start();
    const primary = input(document.body, { primary: true });
    const sidebar = document.createElement('aside');
    sidebar.dataset.findScope = '';
    document.body.append(sidebar);
    const local = input(sidebar, { scopeOnly: true });
    const button = document.createElement('button');
    sidebar.append(button);
    button.focus();
    key();
    expect(document.activeElement).toBe(local);
    primary.focus();
    key();
    expect(document.activeElement).toBe(primary);
  });

  it('cancels delayed focus when owner context or lifetime changes', async () => {
    start();
    const anchor = document.createElement('button');
    document.body.append(anchor);
    let resolve!: (node: HTMLInputElement) => void;
    let identity = 'first';
    const action = findTarget(anchor, {
      identity: () => identity,
      open: () => new Promise<HTMLInputElement>((done) => resolve = done),
    });
    cleanups.push(action.destroy);
    expect(key().defaultPrevented).toBe(true);
    const target = document.createElement('input');
    document.body.append(target);
    identity = 'second';
    resolve(target);
    await Promise.resolve();
    expect(document.activeElement).toBe(document.body);
    key();
    action.destroy();
    resolve(target);
    await Promise.resolve();
    expect(document.activeElement).toBe(document.body);
  });
});
