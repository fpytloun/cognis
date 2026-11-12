import { afterEach, describe, expect, it, vi } from 'vitest';
import { createDocumentFind, documentMatches } from './document-find';

afterEach(() => { document.body.replaceChildren(); vi.unstubAllGlobals(); });

describe('document find', () => {
  it('matches literal case-insensitive text with original Unicode offsets', () => {
    expect(documentMatches('İ ready READY [ready] a+b 😀ready', 'ready')).toEqual([
      { start: 2, end: 7 }, { start: 8, end: 13 }, { start: 15, end: 20 }, { start: 28, end: 33 },
    ]);
    expect(documentMatches('a+b A+B aaab', 'a+b')).toEqual([{ start: 0, end: 3 }, { start: 4, end: 7 }]);
    expect(documentMatches('anything', '')).toEqual([]);
  });

  it('finds across inline markup, excludes controls and hidden content, and clears without rewriting source', () => {
    const highlights = new Map<string, Set<Range>>();
    vi.stubGlobal('CSS', { highlights });
    vi.stubGlobal('Highlight', class extends Set<Range> { constructor(...ranges: Range[]) { super(ranges); } });
    const root = document.createElement('div');
    root.innerHTML = '<p>Release <strong style="display:inline">rea</strong>dy today.</p><p>Not rea</p><p>dy tomorrow.</p><button>ready</button><p hidden>ready</p><p data-find-ignore>ready</p>';
    document.body.append(root);
    const original = root.innerHTML;
    const finder = createDocumentFind(root, documentMatches);
    expect(finder.search('ready')).toBe(1);
    const ranges = [...highlights.values()][0];
    expect([...ranges][0].toString()).toBe('ready');
    finder.clear();
    expect(highlights.size).toBe(0);
    expect(root.innerHTML).toBe(original);
    expect(finder.search('Release ready')).toBe(1);
    finder.clear();
  });

  it('uses concatenated PDF text-run offsets instead of CSS block boundaries', () => {
    const highlights = new Map<string, Set<Range>>();
    vi.stubGlobal('CSS', { highlights });
    vi.stubGlobal('Highlight', class extends Set<Range> { constructor(...ranges: Range[]) { super(ranges); } });
    const root = document.createElement('div');
    root.innerHTML = '<span style="position:absolute;display:block">The release is </span><span style="position:absolute;display:block">rea</span><span style="position:absolute;display:block">dy for review.</span>';
    document.body.append(root);
    const finder = createDocumentFind(root, documentMatches, true);
    expect(finder.search('release is ready')).toBe(1);
    expect([...highlights.values()][0].values().next().value?.toString()).toBe('release is ready');
    finder.clear();
  });
});
