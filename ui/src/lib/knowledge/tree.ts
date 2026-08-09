/**
 * Pure helpers for building a generic browse tree from document source
 * paths. Falls back to a flat listing when no document carries a
 * `source_path` (e.g. individually attached files with no folder context).
 */
import type { KnowledgebaseDocumentModel } from '$lib/types/api';

export interface DocumentTreeFolder {
  kind: 'folder';
  name: string;
  path: string;
  children: DocumentTreeNode[];
}

export interface DocumentTreeFile {
  kind: 'file';
  name: string;
  path: string;
  document: KnowledgebaseDocumentModel;
}

export type DocumentTreeNode = DocumentTreeFolder | DocumentTreeFile;

function splitPath(path: string): string[] {
  return path
    .split('/')
    .map((segment) => segment.trim())
    .filter(Boolean);
}

function sortNodes(nodes: DocumentTreeNode[]): DocumentTreeNode[] {
  return [...nodes].sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === 'folder' ? -1 : 1;
    const byName = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
    if (byName !== 0) return byName;
    if (a.kind === 'file' && b.kind === 'file') return a.document.doc_id.localeCompare(b.document.doc_id);
    return a.path.localeCompare(b.path);
  });
}

function sortTree(nodes: DocumentTreeNode[]): DocumentTreeNode[] {
  return sortNodes(nodes).map((node) =>
    node.kind === 'folder' ? { ...node, children: sortTree(node.children) } : node
  );
}

/**
 * Builds a nested tree from document `source_path` values. Documents with
 * no `source_path` are treated as flat root-level files. Returns
 * `{ tree, isFlat }` where `isFlat` is true when no document has any path
 * segments (i.e. every doc sits at the root) so callers can choose a
 * simpler flat-list presentation.
 */
export function buildDocumentTree(documents: KnowledgebaseDocumentModel[]): {
  tree: DocumentTreeNode[];
  isFlat: boolean;
} {
  const root: DocumentTreeNode[] = [];
  const folderIndex = new Map<string, DocumentTreeFolder>();
  let hasNesting = false;

  for (const document of documents) {
    const segments = document.source_path ? splitPath(document.source_path) : [];
    if (segments.length > 1) hasNesting = true;

    const folderSegments = segments.slice(0, -1);
    const fileName = segments.length > 0 ? segments[segments.length - 1] : document.display_name;

    let siblings = root;
    let currentPath = '';
    for (const segment of folderSegments) {
      currentPath = currentPath ? `${currentPath}/${segment}` : segment;
      let folder = folderIndex.get(currentPath);
      if (!folder) {
        folder = { kind: 'folder', name: segment, path: currentPath, children: [] };
        folderIndex.set(currentPath, folder);
        siblings.push(folder);
      }
      siblings = folder.children;
    }

    const filePath = currentPath ? `${currentPath}/${fileName}` : document.source_path ?? document.doc_id;
    siblings.push({ kind: 'file', name: fileName, path: filePath, document });
  }

  return { tree: sortTree(root), isFlat: !hasNesting };
}

export function flattenTree(nodes: DocumentTreeNode[]): DocumentTreeFile[] {
  const files: DocumentTreeFile[] = [];
  for (const node of nodes) {
    if (node.kind === 'file') {
      files.push(node);
    } else {
      files.push(...flattenTree(node.children));
    }
  }
  return files;
}

export function filterTree(nodes: DocumentTreeNode[], query: string): DocumentTreeNode[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return nodes;

  const result: DocumentTreeNode[] = [];
  for (const node of nodes) {
    if (node.kind === 'file') {
      if (node.name.toLowerCase().includes(normalized) || node.path.toLowerCase().includes(normalized)) {
        result.push(node);
      }
      continue;
    }
    const children = filterTree(node.children, query);
    if (children.length > 0 || node.name.toLowerCase().includes(normalized)) {
      result.push({ ...node, children: children.length > 0 ? children : node.children });
    }
  }
  return result;
}

/** All folder paths in the tree, used to seed "expand all" / persisted expansion state. */
export function collectFolderPaths(nodes: DocumentTreeNode[]): string[] {
  const paths: string[] = [];
  for (const node of nodes) {
    if (node.kind === 'folder') {
      paths.push(node.path);
      paths.push(...collectFolderPaths(node.children));
    }
  }
  return paths;
}
