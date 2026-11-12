import { describe, expect, it } from 'vitest';
import { annotationDiffRows, selectDiffLines } from './diff';

const diff = '@@ -10,3 +10,3 @@ function\n first\n-old\n+new\n last\n';

describe('exact diff annotation evidence', () => {
  it('keeps old and new side ranges separate and supports reverse selection', () => {
    const rows = annotationDiffRows({ path: 'src/a.ts', diff });
    expect(selectDiffLines(rows, 'old', 12, 10)).toMatchObject({ start_line: 10, end_line: 12, quote: 'first\nold\nlast' });
    expect(selectDiffLines(rows, 'new', 11, 12).quote).toBe('new\nlast');
  });

  it('does not invent source line zero before a hunk', () => {
    expect(annotationDiffRows({ path: 'a', diff: 'unified diff unavailable' })).toEqual([]);
    const rows = annotationDiffRows({ path: 'a', diff: '@@ -0,0 +1,2 @@\n+a\n+b\n' });
    expect(selectDiffLines(rows, 'new', 1, 2).quote).toBe('a\nb');
    expect(() => selectDiffLines(rows, 'old', 1, 1)).toThrow();
  });

  it('refuses malformed, incomplete, binary and truncated evidence', () => {
    for (const input of [
      { diff: '@@ -1,2 +1,2 @@\n a\n' },
      { diff: '@@ -1 +1 @@\n-a\n' },
      { diff: '@@ -1 +1 @@\n a\nextra' },
      { diff, truncated: true }, { diff, content_truncated: true },
      { diff, preview_omitted: true }, { diff, binary: true },
    ]) expect(annotationDiffRows({ path: 'a', ...input })).toEqual([]);
  });

  it('rejects cross-hunk, missing, duplicate and excessive ranges', () => {
    const rows = annotationDiffRows({ path: 'a', diff: '@@ -1 +1 @@\n a\n@@ -2 +2 @@\n b\n' });
    expect(() => selectDiffLines(rows, 'new', 1, 2)).toThrow('one complete hunk');
    expect(() => selectDiffLines(rows, 'new', 1, 3)).toThrow();
    expect(() => selectDiffLines(rows, 'new', 0, 1)).toThrow();
    const large = annotationDiffRows({ path: 'a', diff: `@@ -1 +1 @@\n ${'x'.repeat(2049)}\n` });
    expect(() => selectDiffLines(large, 'new', 1, 1)).toThrow('2 KiB');
  });
});
