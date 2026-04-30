import { describe, expect, it } from 'vitest';

import { detectFileLanguage, normalizeFileDiffs, parseFileDiff } from '$lib/diff';

describe('diff helpers', () => {
  it('detects file language from extension', () => {
    expect(detectFileLanguage('/repo/cognis/core/agent_loop.py')).toMatchObject({
      language: 'python',
      label: 'Python',
      icon: 'Py',
    });
    expect(detectFileLanguage('ui/src/lib/chat.ts')).toMatchObject({
      language: 'typescript',
      label: 'TypeScript',
      icon: 'TS',
    });
  });

  it('parses unified diff line numbers and counts', () => {
    const parsed = parseFileDiff({
      path: 'example.py',
      diff: '--- example.py\n+++ example.py\n@@ -2,3 +2,4 @@\n keep\n-old = 1\n+new = 1\n+extra = True\n',
    });

    expect(parsed.additions).toBe(2);
    expect(parsed.deletions).toBe(1);
    expect(parsed.lines.find((line) => line.type === 'remove')).toMatchObject({ oldLine: 3, newLine: null });
    expect(parsed.lines.find((line) => line.type === 'add')).toMatchObject({ oldLine: null, newLine: 3 });
  });

  it('normalizes malformed diff payloads', () => {
    expect(
      normalizeFileDiffs([
        { path: 'a.ts', diff: '+const x = 1\n', truncated: true, original_size: 123 },
        { path: '', diff: '', truncated: true, omitted_count: 2 },
        { path: '', diff: '' },
        'bad',
      ]),
    ).toEqual([
      { path: 'a.ts', diff: '+const x = 1\n', truncated: true, original_size: 123, omitted_count: undefined },
      { path: '', diff: '', truncated: true, original_size: undefined, omitted_count: 2 },
    ]);
  });
});
