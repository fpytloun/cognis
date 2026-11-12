import { afterEach, describe, expect, it, vi } from 'vitest';
import { paintTextRanges, unionIntervals } from './text-paint';
afterEach(() => { vi.unstubAllGlobals(); document.body.replaceChildren(); });
describe('text highlight paint ownership', () => {
  it('unions duplicate and transitively overlapping intervals without mutating inputs', () => {
    const values = [{ start: 8, end: 12 }, { start: 0, end: 4 }, { start: 3, end: 9 }, { start: 0, end: 4 }];
    expect(unionIntervals(values)).toEqual([{ start: 0, end: 12 }]);
    expect(values[0]).toEqual({ start: 8, end: 12 });
  });
  it('cleans only its own registry and style without changing content or selection', () => {
    const entries = new Map<string, unknown>();
    vi.stubGlobal('CSS', { highlights: entries }); vi.stubGlobal('Highlight', class { constructor(readonly range: Range) {} });
    const root = document.createElement('p'); root.innerHTML = '<em>Syntax</em> colors'; document.body.append(root);
    const original = root.innerHTML, text = root.firstChild!.firstChild!;
    const range = document.createRange(); range.setStart(text, 0); range.setEnd(text, 6);
    getSelection()!.removeAllRanges(); getSelection()!.addRange(range);
    const stop = paintTextRanges(root, [range])!, other = paintTextRanges(root, [range])!;
    expect(entries.size).toBe(2); expect(root.innerHTML).toBe(original); expect(getSelection()!.toString()).toBe('Syntax');
    expect(document.querySelector('[data-feedback-highlight-style]')!.textContent).not.toContain('::selection');
    stop(); stop();
    expect(entries.size).toBe(1); expect(document.querySelectorAll('[data-feedback-highlight-style]')).toHaveLength(1);
    other();
    expect(entries.size).toBe(0); expect(document.querySelectorAll('[data-feedback-highlight-style]')).toHaveLength(0);
  });
  it('declines unsupported browsers and excludes replaced syntax nodes', () => {
    vi.stubGlobal('CSS', {});
    const root = document.createElement('p'); root.textContent = 'Old syntax'; document.body.append(root);
    const range = document.createRange(); range.selectNodeContents(root.firstChild!);
    expect(paintTextRanges(root, [range])).toBeNull();
    const registry = { set: vi.fn(), delete: vi.fn() }, build = vi.fn();
    vi.stubGlobal('CSS', { highlights: registry });
    vi.stubGlobal('Highlight', class { constructor(...ranges: Range[]) { build(ranges); } });
    root.replaceChildren(document.createTextNode('New syntax'));
    paintTextRanges(root, [range])!();
    expect(build).toHaveBeenCalledWith([]);
  });
});
