import { afterEach, describe, expect, it, vi } from 'vitest';
import { canOpenDeliverableWindow, deliverableWindowUrl, openDeliverableWindow } from './deliverable-window';

afterEach(() => { delete window.__cognisNative; delete window.webkit; vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe('isolated deliverable windows', () => {
  it('routes native deliverables through the identifier-only bridge, not window.open', () => {
    const send = vi.fn(), open = vi.spyOn(window, 'open').mockReturnValue(null);
    const generation = '8d6feafb-2aac-4b41-ac82-42b2314563bb';
    window.__cognisNative = { version: 1, generation, server: window.location.origin };
    window.webkit = { messageHandlers: { cognis: { postMessage: send } } };
    openDeliverableWindow('/library/deliverable/dlv_revision?unrelated=ignored');
    expect(send).toHaveBeenCalledExactlyOnceWith({
      version: 1, generation, type: 'open-resource-window', payload: { kind: 'deliverable', id: 'dlv_revision' }
    });
    expect(open).not.toHaveBeenCalled();
  });
  it('keeps the authenticated revision URL and adds window mode', () => {
    const url = deliverableWindowUrl('/library/deliverable/dlv_revision');
    expect(url?.pathname).toBe('/library/deliverable/dlv_revision');
    expect(url?.searchParams.get('window')).toBe('1');
    expect(url?.origin).toBe(window.location.origin);
  });
  it('does not open public, cross-origin, artifact or unrelated routes', () => {
    for (const url of ['/library/artifact/art_image', '/chat/conversation', '/shared/report', 'https://external.test/library/deliverable/report', '']) {
      expect(deliverableWindowUrl(url)).toBeNull();
    }
  });
  it('opens desktop windows with opener isolation and popup sizing', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    expect(canOpenDeliverableWindow()).toBe(true);
    openDeliverableWindow('/library/deliverable/dlv_revision');
    expect(open).toHaveBeenCalledWith(
      `${window.location.origin}/library/deliverable/dlv_revision?window=1`,
      'cognis-deliverable-dlv_revision',
      expect.stringContaining('popup=yes,noopener,noreferrer,width='),
    );
  });
  it('rejects coarse-pointer mobile/tablet environments', () => {
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(any-pointer: coarse)' }) as MediaQueryList);
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    expect(canOpenDeliverableWindow()).toBe(false);
    openDeliverableWindow('/library/deliverable/dlv_revision');
    expect(open).not.toHaveBeenCalled();
  });
});
