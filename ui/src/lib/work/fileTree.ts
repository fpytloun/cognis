export type FileChangeStatus = 'added' | 'modified' | 'deleted' | 'renamed';

export interface WorkFileDiff {
  path: string;
  diff: string;
  old_path?: string | null;
  status?: string | null;
  binary?: boolean;
  generated?: boolean;
  truncated?: boolean;
  path_id?: string | null;
  root_name?: string | null;
  root_id?: string | null;
  additions?: number | null;
  deletions?: number | null;
  content_truncated?: boolean;
  preview_omitted?: boolean;
  source_workstream?: {
    key: string;
    title: string;
    agent_id: string;
    status: string;
  } | null;
}

export interface FileTreeCounts {
  files: number;
  additions: number;
  deletions: number;
}

interface FileTreeNodeBase {
  id: string;
  name: string;
  path: string;
  depth: number;
  counts: FileTreeCounts;
}

export interface FileTreeFolder extends FileTreeNodeBase {
  kind: 'folder';
  children: FileTreeNode[];
}

export interface FileTreeFile extends FileTreeNodeBase {
  kind: 'file';
  status: FileChangeStatus;
  diff: WorkFileDiff;
  diffs: WorkFileDiff[];
  binary: boolean;
  generated: boolean;
  truncated: boolean;
}

export type FileTreeNode = FileTreeFolder | FileTreeFile;

export interface VisibleFileTreeNode {
  node: FileTreeNode;
  parentId: string | null;
}

function hasTextPreview(diff: WorkFileDiff): boolean {
  return (
    !diff.preview_omitted
    && !diff.generated
    && diff.binary !== true
    && !/^Binary files /m.test(diff.diff)
    && diff.diff.trim().length > 0
  );
}

export function combineFileDiffHistory(file: FileTreeFile): WorkFileDiff | null {
  const textDiffs = file.diffs.filter(hasTextPreview);
  const latest = textDiffs.at(-1);
  if (!latest) return null;

  return {
    ...latest,
    path: file.path,
    diff: textDiffs
      .map((event) => event.diff.trimEnd())
      .filter(Boolean)
      .join('\n'),
    truncated: textDiffs.some(
      (event) => event.truncated === true || event.content_truncated === true
    ),
    content_truncated: textDiffs.some((event) => event.content_truncated === true),
  };
}

function emptyCounts(): FileTreeCounts {
  return { files: 0, additions: 0, deletions: 0 };
}

function countLines(diff: string): Pick<FileTreeCounts, 'additions' | 'deletions'> {
  let additions = 0;
  let deletions = 0;
  for (const line of diff.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) additions += 1;
    if (line.startsWith('-') && !line.startsWith('---')) deletions += 1;
  }
  return { additions, deletions };
}

function diffCounts(diff: WorkFileDiff): Pick<FileTreeCounts, 'additions' | 'deletions'> {
  if (typeof diff.additions === 'number' && typeof diff.deletions === 'number') {
    return { additions: diff.additions, deletions: diff.deletions };
  }
  return countLines(diff.diff);
}

export function inferFileStatus(diff: WorkFileDiff): FileChangeStatus {
  const explicit = diff.status?.toLowerCase();
  if (explicit === 'added' || explicit === 'modified' || explicit === 'deleted' || explicit === 'renamed') {
    return explicit;
  }
  if (diff.old_path && diff.old_path !== diff.path) return 'renamed';
  if (/^--- \/dev\/null$/m.test(diff.diff)) return 'added';
  if (/^\+\+\+ \/dev\/null$/m.test(diff.diff)) return 'deleted';
  return 'modified';
}

function nodeId(kind: FileTreeNode['kind'], path: string, identity = path): string {
  return `${kind}:${encodeURIComponent(identity)}`;
}

function normalizePath(path: string): string[] {
  return path.replaceAll('\\', '/').split('/').filter((part) => part.length > 0 && part !== '.');
}

function pathKind(path: string): 'absolute' | 'drive' | 'relative' {
  if (/^[a-zA-Z]:[\\/]/.test(path)) return 'drive';
  if (path.startsWith('/')) return 'absolute';
  return 'relative';
}

/** The display-only common directory. Different roots must remain separate. */
export function commonFileTreeRoot(diffs: WorkFileDiff[]): string | null {
  if (diffs.length < 2) return null;
  const kinds = new Set(diffs.map((diff) => pathKind(diff.path)));
  const rootIds = new Set(diffs.map((diff) => diff.root_id).filter(Boolean));
  if (kinds.size !== 1 || rootIds.size > 1) return null;
  if (kinds.has('relative') && rootIds.size === 0) return null;
  const parts = diffs.map((diff) => normalizePath(diff.path));
  let prefix = parts[0].slice(0, -1);
  for (const path of parts.slice(1)) {
    const max = Math.min(prefix.length, Math.max(0, path.length - 1));
    let length = 0;
    while (length < max && prefix[length] === path[length]) length += 1;
    prefix = prefix.slice(0, length);
  }
  if (!prefix.length) return null;
  const first = diffs[0].path;
  const joined = prefix.join('/');
  if (pathKind(first) === 'drive') return `${first.slice(0, 2)}/${joined}`;
  return pathKind(first) === 'absolute' ? `/${joined}` : joined;
}

export function buildFileTree(diffs: WorkFileDiff[]): FileTreeNode[] {
  const root: FileTreeFolder = {
    kind: 'folder',
    id: 'folder:',
    name: '',
    path: '',
    depth: 0,
    counts: emptyCounts(),
    children: [],
  };
  const folders = new Map<string, FileTreeFolder>([['', root]]);

  const commonRoot = commonFileTreeRoot(diffs);
  const commonParts = commonRoot ? normalizePath(commonRoot) : [];
  for (const diff of diffs) {
    const normalized = normalizePath(diff.path);
    const parts = commonRoot ? normalized.slice(commonParts.length) : normalized;
    if (parts.length === 0) continue;
    const rootIdentity = diff.root_id ?? '';
    let parent = root;
    for (let index = 0; index < parts.length - 1; index += 1) {
      const path = parts.slice(0, index + 1).join('/');
      const folderKey = `${rootIdentity}:${path}`;
      let folder = folders.get(folderKey);
      if (!folder) {
        folder = {
          kind: 'folder',
          id: nodeId('folder', path, folderKey),
          name: parts[index],
          path,
          depth: index + 1,
          counts: emptyCounts(),
          children: [],
        };
        folders.set(folderKey, folder);
        parent.children.push(folder);
      }
      parent = folder;
    }

    const stats = diffCounts(diff);
    const identity = diff.path_id ?? diff.path;
    const file: FileTreeFile = {
      kind: 'file',
      id: nodeId('file', diff.path, identity),
      name: parts.at(-1) ?? diff.path,
      path: diff.path,
      depth: parts.length,
      counts: { files: 1, ...stats },
      status: inferFileStatus(diff),
      diff,
      diffs: [diff],
      binary: diff.binary === true || /^Binary files /m.test(diff.diff),
      generated: diff.generated === true,
      truncated: diff.truncated === true || diff.content_truncated === true,
    };
    const previousIndex = parent.children.findIndex(
      (child) => child.kind === 'file' && child.id === file.id
    );
    if (previousIndex >= 0) {
      const previous = parent.children[previousIndex] as FileTreeFile;
      parent.children[previousIndex] = {
        ...file,
        counts: {
          files: 1,
          additions: previous.counts.additions + file.counts.additions,
          deletions: previous.counts.deletions + file.counts.deletions,
        },
        diffs: [...previous.diffs, diff],
      };
    } else {
      parent.children.push(file);
    }
  }

  function finalize(folder: FileTreeFolder): FileTreeCounts {
    folder.children.sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === 'folder' ? -1 : 1;
      return left.name.localeCompare(right.name);
    });
    folder.counts = folder.children.reduce<FileTreeCounts>((total, child) => {
      const counts = child.kind === 'folder' ? finalize(child) : child.counts;
      total.files += counts.files;
      total.additions += counts.additions;
      total.deletions += counts.deletions;
      return total;
    }, emptyCounts());
    return folder.counts;
  }

  const rootNames = new Map<string, number>();
  for (const node of root.children) {
    rootNames.set(node.name, (rootNames.get(node.name) ?? 0) + 1);
  }
  for (const node of root.children) {
    if (node.kind !== 'folder' || (rootNames.get(node.name) ?? 0) < 2) continue;
    const key = decodeURIComponent(node.id.slice('folder:'.length));
    const identity = key.split(':', 1)[0].slice(-6);
    node.name = `${node.name} · ${identity}`;
  }
  finalize(root);
  return root.children;
}

export function fileNodes(nodes: FileTreeNode[]): FileTreeFile[] {
  return nodes.flatMap((node) => node.kind === 'file' ? [node] : fileNodes(node.children));
}

export function filterFileTree(
  nodes: FileTreeNode[],
  query: string,
  statuses: ReadonlySet<FileChangeStatus> = new Set()
): FileTreeNode[] {
  const normalizedQuery = query.trim().toLowerCase();
  function filterNode(node: FileTreeNode): FileTreeNode | null {
    if (node.kind === 'file') {
      const matchesQuery = !normalizedQuery || node.path.toLowerCase().includes(normalizedQuery);
      const matchesStatus = statuses.size === 0 || statuses.has(node.status);
      return matchesQuery && matchesStatus ? node : null;
    }
    const children = node.children.map(filterNode).filter((child): child is FileTreeNode => child !== null);
    if (children.length === 0) return null;
    const counts = children.reduce<FileTreeCounts>((total, child) => ({
      files: total.files + child.counts.files,
      additions: total.additions + child.counts.additions,
      deletions: total.deletions + child.counts.deletions,
    }), emptyCounts());
    return { ...node, children, counts };
  }
  return nodes.map(filterNode).filter((node): node is FileTreeNode => node !== null);
}

export function visibleFileTreeNodes(
  nodes: FileTreeNode[],
  expanded: ReadonlySet<string>
): VisibleFileTreeNode[] {
  const visible: VisibleFileTreeNode[] = [];
  function append(items: FileTreeNode[], parentId: string | null): void {
    for (const node of items) {
      visible.push({ node, parentId });
      if (node.kind === 'folder' && expanded.has(node.id)) append(node.children, node.id);
    }
  }
  append(nodes, null);
  return visible;
}

export function defaultExpandedFolders(nodes: FileTreeNode[], expandAll = false): Set<string> {
  const result = new Set<string>();
  function visit(items: FileTreeNode[], depth: number): void {
    for (const node of items) {
      if (node.kind !== 'folder') continue;
      if (expandAll || depth === 0) result.add(node.id);
      visit(node.children, depth + 1);
    }
  }
  visit(nodes, 0);
  return result;
}
