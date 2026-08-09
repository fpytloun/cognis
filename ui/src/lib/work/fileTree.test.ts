import { describe, expect, it } from 'vitest';

import {
  buildFileTree,
  commonFileTreeRoot,
  combineFileDiffHistory,
  defaultExpandedFolders,
  fileNodes,
  filterFileTree,
  visibleFileTreeNodes,
  type WorkFileDiff,
} from './fileTree';

function diff(path: string, patch = '@@ -1 +1 @@\n-old\n+new', extra: Partial<WorkFileDiff> = {}): WorkFileDiff {
  return { path, diff: patch, ...extra };
}

describe('fileTree', () => {
  it('preserves every authorized full path and keeps duplicate basenames distinct', () => {
    const input = [
      diff('src/client/index.ts'),
      diff('src/server/index.ts'),
      diff('README.md'),
    ];
    const tree = buildFileTree(input);

    expect(fileNodes(tree).map((node) => node.path).sort()).toEqual(input.map((item) => item.path).sort());
    expect(fileNodes(tree).filter((node) => node.name === 'index.ts')).toHaveLength(2);
    expect(new Set(fileNodes(tree).map((node) => node.id)).size).toBe(input.length);
  });

  it('keeps same-label roots separate by root identity and trusts explicit stats', () => {
    const tree = buildFileTree([
      diff('repo/src/app.ts', '+partial', {
        path_id: 'root-a:src/app.ts',
        root_id: 'root-a',
        root_name: 'repo',
        additions: 100,
        deletions: 40,
        content_truncated: true,
      }),
      diff('repo/src/app.ts', '+other', {
        path_id: 'root-b:src/app.ts',
        root_id: 'root-b',
        root_name: 'repo',
        additions: 7,
        deletions: 3,
      }),
    ]);

    expect(tree).toHaveLength(2);
    expect(tree[0].id).not.toBe(tree[1].id);
    expect(tree.map((node) => node.name)).toEqual([
      'repo · root-a',
      'repo · root-b',
    ]);
    expect(fileNodes(tree).map((file) => file.counts)).toEqual([
      { files: 1, additions: 100, deletions: 40 },
      { files: 1, additions: 7, deletions: 3 },
    ]);
  });

  it('combines every authorized patch for a repeatedly edited path in chronological order', () => {
    const files = fileNodes(buildFileTree([
      diff('src/app.ts', '@@ -1 +1 @@\n-old\n+middle'),
      diff('src/app.ts', '@@ -1 +1 @@\n-middle\n+new'),
    ]));

    expect(files).toHaveLength(1);
    expect(files[0].diffs.map((item) => item.diff)).toEqual([
      '@@ -1 +1 @@\n-old\n+middle',
      '@@ -1 +1 @@\n-middle\n+new',
    ]);
    expect(combineFileDiffHistory(files[0])?.diff).toBe(
      '@@ -1 +1 @@\n-old\n+middle\n@@ -1 +1 @@\n-middle\n+new'
    );
    expect(files[0].counts).toEqual({ files: 1, additions: 2, deletions: 2 });
  });

  it('combines text edits while excluding unavailable binary and generated previews', () => {
    const [file] = fileNodes(buildFileTree([
      diff('src/app.ts', 'Binary files differ', { binary: true }),
      diff('src/app.ts', '@@ -1 +1 @@\n-old\n+middle'),
      diff('src/app.ts', '', { generated: true }),
      diff('src/app.ts', '@@ -2 +2 @@\n-before\n+after', { content_truncated: true }),
    ]));

    expect(combineFileDiffHistory(file)).toMatchObject({
      path: 'src/app.ts',
      diff: '@@ -1 +1 @@\n-old\n+middle\n@@ -2 +2 @@\n-before\n+after',
      truncated: true,
      content_truncated: true,
    });
  });

  it('keeps current metadata and every event across binary, generated, and text histories', () => {
    const files = fileNodes(buildFileTree([
      diff('binary-text.ts', 'Binary files differ', { binary: true }),
      diff('binary-text.ts', '@@ -1 +1 @@\n-old\n+text'),
      diff('generated-text.ts', '', { generated: true }),
      diff('generated-text.ts', '@@ -1 +1 @@\n-old\n+text'),
      diff('text-binary.ts', '@@ -1 +1 @@\n-old\n+text'),
      diff('text-binary.ts', 'Binary files differ', { binary: true }),
      diff('text-generated.ts', '@@ -1 +1 @@\n-old\n+text'),
      diff('text-generated.ts', '', { generated: true }),
    ]));
    const byPath = Object.fromEntries(files.map((file) => [file.path, file]));

    expect(byPath['binary-text.ts']).toMatchObject({ binary: false, generated: false });
    expect(byPath['generated-text.ts']).toMatchObject({ binary: false, generated: false });
    expect(byPath['text-binary.ts']).toMatchObject({ binary: true, generated: false });
    expect(byPath['text-generated.ts']).toMatchObject({ binary: false, generated: true });
    expect(files.every((file) => file.diffs.length === 2)).toBe(true);
  });

  it('aggregates file and line counts through nested folders', () => {
    const tree = buildFileTree([
      diff('src/a.ts', '@@ -1 +1,2 @@\n-old\n+new\n+extra'),
      diff('src/nested/b.ts', '@@ -1 +0,0 @@\n-removed'),
    ]);
    const src = tree[0];

    expect(src.kind).toBe('folder');
    expect(src.counts).toEqual({ files: 2, additions: 2, deletions: 2 });
  });

  it('infers additions, deletions, renames, binary, generated, and truncated metadata', () => {
    const files = fileNodes(buildFileTree([
      diff('added.ts', '--- /dev/null\n+++ b/added.ts\n+new'),
      diff('deleted.ts', '--- a/deleted.ts\n+++ /dev/null\n-old'),
      diff('new.ts', '', { old_path: 'old.ts' }),
      diff('asset.png', 'Binary files differ'),
      diff('generated.ts', '', { generated: true, truncated: true }),
    ]));

    expect(Object.fromEntries(files.map((file) => [file.path, file.status]))).toEqual({
      'added.ts': 'added',
      'asset.png': 'modified',
      'deleted.ts': 'deleted',
      'generated.ts': 'modified',
      'new.ts': 'renamed',
    });
    expect(files.find((file) => file.path === 'asset.png')?.binary).toBe(true);
    expect(files.find((file) => file.path === 'generated.ts')).toMatchObject({ generated: true, truncated: true });
  });

  it('filters by full path and status while recalculating folder counts', () => {
    const tree = buildFileTree([
      diff('src/add.ts', '--- /dev/null\n+++ b/src/add.ts\n+new'),
      diff('src/delete.ts', '--- a/src/delete.ts\n+++ /dev/null\n-old'),
      diff('docs/add.md', '--- /dev/null\n+++ b/docs/add.md\n+new'),
    ]);
    const result = filterFileTree(tree, 'src/', new Set(['added']));

    expect(fileNodes(result).map((node) => node.path)).toEqual(['src/add.ts']);
    expect(result[0].counts.files).toBe(1);
  });

  it('returns a stable keyboard-visible sequence from expansion state', () => {
    const tree = buildFileTree([diff('src/a.ts'), diff('src/nested/b.ts'), diff('root.ts')]);
    const topLevel = visibleFileTreeNodes(tree, new Set());
    const expanded = visibleFileTreeNodes(tree, defaultExpandedFolders(tree, true));

    expect(topLevel.map((item) => item.node.path)).toEqual(['src', 'root.ts']);
    expect(expanded.map((item) => item.node.path)).toEqual(['src', 'src/nested', 'src/nested/b.ts', 'src/a.ts', 'root.ts']);
  });

  it('does not invent nodes for empty or redacted paths', () => {
    expect(buildFileTree([diff(''), diff('authorized.ts')]).map((node) => node.path)).toEqual(['authorized.ts']);
  });

  it('uses the longest shared directory as a label and builds descendants below it', () => {
    const tree = buildFileTree([
      diff('/home/user/src/app/client.ts'),
      diff('/home/user/src/app/server.ts'),
    ]);
    expect(commonFileTreeRoot([
      diff('/home/user/src/app/client.ts'),
      diff('/home/user/src/app/server.ts'),
    ])).toBe('/home/user/src/app');
    expect(fileNodes(tree).map((file) => file.name).sort()).toEqual(['client.ts', 'server.ts']);
  });

  it('does not collapse mixed worktree, absolute, Windows-drive, and relative paths', () => {
    const paths = [
      diff('/home/user/repo/src/a.ts'),
      diff('C:\\repo\\src\\b.ts'),
      diff('relative/src/c.ts'),
    ];
    expect(commonFileTreeRoot(paths)).toBeNull();
    expect(fileNodes(buildFileTree(paths)).map((file) => file.path).sort()).toEqual(
      paths.map((item) => item.path).sort(),
    );
  });
});
