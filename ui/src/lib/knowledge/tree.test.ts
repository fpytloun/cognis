import { describe, expect, it } from 'vitest';

import { buildDocumentTree, collectFolderPaths, filterTree, flattenTree } from './tree';
import type { KnowledgebaseDocumentModel } from '$lib/types/api';

function doc(overrides: Partial<KnowledgebaseDocumentModel>): KnowledgebaseDocumentModel {
  return {
    doc_id: 'doc',
    knowledgebase_id: 'kb_1',
    artifact_id: 'art_1',
    display_name: 'file.md',
    source_path: null,
    mime_type: 'text/markdown',
    size_bytes: 100,
    status: 'indexed',
    chunk_count: 3,
    metadata: {},
    last_job_id: null,
    last_error: null,
    attached_at: null,
    indexed_at: null,
    ...overrides
  };
}

describe('buildDocumentTree', () => {
  it('builds nested folders from source_path and marks the tree as non-flat', () => {
    const docs = [
      doc({ doc_id: '1', display_name: 'intro.md', source_path: 'guides/intro.md' }),
      doc({ doc_id: '2', display_name: 'setup.md', source_path: 'guides/setup/setup.md' }),
      doc({ doc_id: '3', display_name: 'readme.md', source_path: 'readme.md' })
    ];

    const { tree, isFlat } = buildDocumentTree(docs);

    expect(isFlat).toBe(false);
    expect(tree.map((node) => node.name)).toEqual(['guides', 'readme.md']);

    const guides = tree[0];
    if (guides.kind !== 'folder') throw new Error('expected folder');
    expect(guides.children.map((child) => child.name)).toEqual(['setup', 'intro.md']);

    const setupFolder = guides.children.find((child) => child.kind === 'folder');
    if (!setupFolder || setupFolder.kind !== 'folder') throw new Error('expected setup folder');
    expect(setupFolder.children).toHaveLength(1);
    expect(setupFolder.children[0].path).toBe('guides/setup/setup.md');
  });

  it('falls back to a flat listing when no document has a nested source_path', () => {
    const docs = [
      doc({ doc_id: '1', display_name: 'a.md', source_path: null }),
      doc({ doc_id: '2', display_name: 'b.md', source_path: 'b.md' })
    ];

    const { tree, isFlat } = buildDocumentTree(docs);

    expect(isFlat).toBe(true);
    expect(tree.every((node) => node.kind === 'file')).toBe(true);
  });

  it('sorts folders before files, alphabetically within each group', () => {
    const docs = [
      doc({ doc_id: '1', display_name: 'zebra.md', source_path: 'zebra.md' }),
      doc({ doc_id: '2', display_name: 'alpha.md', source_path: 'alpha.md' }),
      doc({ doc_id: '3', display_name: 'x.md', source_path: 'folder/x.md' })
    ];

    const { tree } = buildDocumentTree(docs);

    expect(tree.map((node) => node.name)).toEqual(['folder', 'alpha.md', 'zebra.md']);
  });
});

describe('flattenTree', () => {
  it('returns every file node in depth-first order', () => {
    const docs = [
      doc({ doc_id: '1', source_path: 'a/one.md' }),
      doc({ doc_id: '2', source_path: 'a/two.md' }),
      doc({ doc_id: '3', source_path: 'root.md' })
    ];
    const { tree } = buildDocumentTree(docs);

    const files = flattenTree(tree);
    expect(files.map((file) => file.document.doc_id).sort()).toEqual(['1', '2', '3']);
  });
});

describe('filterTree', () => {
  it('keeps folders that contain a matching descendant and drops non-matching branches', () => {
    const docs = [
      doc({ doc_id: '1', display_name: 'setup.md', source_path: 'guides/setup.md' }),
      doc({ doc_id: '2', display_name: 'other.md', source_path: 'misc/other.md' })
    ];
    const { tree } = buildDocumentTree(docs);

    const filtered = filterTree(tree, 'setup');
    expect(filtered.map((node) => node.name)).toEqual(['guides']);
  });

  it('returns all nodes unchanged for an empty query', () => {
    const docs = [doc({ doc_id: '1', source_path: 'a.md' })];
    const { tree } = buildDocumentTree(docs);
    expect(filterTree(tree, '  ')).toBe(tree);
  });
});

describe('collectFolderPaths', () => {
  it('collects every folder path for expand-all seeding', () => {
    const docs = [doc({ doc_id: '1', source_path: 'a/b/c.md' })];
    const { tree } = buildDocumentTree(docs);
    expect(collectFolderPaths(tree)).toEqual(['a', 'a/b']);
  });
});
