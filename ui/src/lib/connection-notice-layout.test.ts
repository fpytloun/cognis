import { readFileSync } from 'node:fs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { usageGeometry } from '../../e2e/usage-geometry';
import { observeConnectionNotice } from './connection-notice-layout';

afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

describe('connection notice shell reservation', () => {
  it('tracks appearance, resize/expansion and hiding without double-counting safe areas', () => {
    let notify: () => void = () => {};
    const disconnect = vi.fn();
    const observe = vi.fn();
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { notify = callback; }
      observe = observe;
      disconnect = disconnect;
    });
    const slot = document.createElement('div');
    let height = 0;
    slot.getBoundingClientRect = () => new DOMRect(0, 0, 390, height);
    const reserve = vi.fn();
    const cleanup = observeConnectionNotice(slot, reserve);
    height = 55.5; notify();
    height = 91; notify();
    height = 0; notify();
    expect(reserve.mock.calls.map(([value]) => value)).toEqual([0, 56, 91, 0]);
    expect(observe).toHaveBeenCalledWith(slot);
    cleanup();
    expect(disconnect).toHaveBeenCalledOnce();
    expect(reserve).toHaveBeenLastCalledWith(0);
  });

  it('offsets the fixed header, not notice visibility or the existing content reservation', () => {
    const source = readFileSync('src/routes/(app)/+layout.svelte', 'utf8');
    const header = source.match(/<header bind:this=\{mobileHeaderEl\}[^>]*>/)?.[0] ?? '';
    expect(header).toContain('top: var(--app-shell-notice-height, 0px)');
    expect(header).toContain('app-keyboard-stable-header');
    expect(header).not.toContain('top-0');
    expect(source).toContain('bind:this={connectionNoticeSlotEl}');
    expect(source).toContain('observeConnectionNotice(element,');
    expect(source).toContain("setShellOffsetVariable('--app-shell-top-offset', shouldReserve ? mobileHeaderEl?.offsetHeight ?? 0 : 0)");
    expect(source).toContain('pt-[var(--app-shell-top-offset,0px)]');
    expect(header).toContain('env(safe-area-inset-left)');
  });
});

describe('notice-versus-header native geometry coverage', () => {
  it.each([
    { headerTop: 0, noticeHeight: 60, headerVisible: true, overlap: true },
    { headerTop: 60, noticeHeight: 60, headerVisible: true, overlap: false },
    { headerTop: 0, noticeHeight: 0, headerVisible: true, overlap: false },
    { headerTop: 0, noticeHeight: 60, headerVisible: false, overlap: false },
  ])('detects actual notice overlap only: %j', ({ headerTop, noticeHeight, headerVisible, overlap }) => {
    vi.stubGlobal('innerWidth', 390);
    vi.stubGlobal('innerHeight', 844);
    const root = document.createElement('section');
    root.className = 'usage';
    const notice = document.createElement('div');
    notice.dataset.testid = 'connection-notice-slot';
    const header = document.createElement('header');
    header.className = 'app-keyboard-stable-header';
    header.style.position = 'fixed';
    for (const node of [root, notice, header]) {
      node.style.visibility = 'visible';
      node.style.opacity = '1';
      node.style.display = node === header && !headerVisible ? 'none' : 'block';
    }
    root.getBoundingClientRect = () => new DOMRect(0, 104, 390, 740);
    notice.getBoundingClientRect = () => new DOMRect(0, 0, 390, noticeHeight);
    header.getBoundingClientRect = () => new DOMRect(0, headerTop, 390, 44);
    document.body.append(notice, header, root);
    const result = usageGeometry('.usage');
    expect(result.noticeHeaderOverlap !== null).toBe(overlap);
    expect(result.covered).toBe(0);
  });
});
