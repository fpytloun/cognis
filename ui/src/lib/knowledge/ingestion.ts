/**
 * Pure planning helpers for the generic "Add documents" ingestion wizard.
 *
 * Scope note: this is intentionally generic. A single flow handles single
 * files, multiple files, and
 * folder picks (via `webkitRelativePath`/drag-drop), all producing the same
 * `{ file, path }` shape consumed by `api.knowledgebases.documents.upload`.
 */
import type { KnowledgebaseDocumentConflictPolicy, KnowledgebaseDocumentUploadOutcome } from '$lib/types/api';

/** Conservative allowlist of extensions accepted for generic ingestion. */
export const SUPPORTED_EXTENSIONS = new Set([
  'md',
  'markdown',
  'txt',
  'text',
  'json',
  'yaml',
  'yml',
  'xml',
  'csv',
  'tsv',
  'html',
  'htm',
  'pdf',
  'docx',
  'log',
  'rst'
]);

export const MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024; // 25 MB per file
export type IngestionAction = 'add' | 'skip' | 'replace' | 'keep_both';

export interface IngestionSourceFile {
  file: File;
  /** Folder-relative path, e.g. `notes/todo.md`. Falls back to `file.name` for flat picks. */
  path: string;
}

export interface IngestionPlanItem {
  id: string;
  file: File;
  path: string;
  size: number;
  extension: string;
  supported: boolean;
  oversized: boolean;
  conflict: boolean;
  action: IngestionAction;
  resolvedPath: string;
  skipReason: string | null;
}

export interface IngestionOutcomeState extends IngestionPlanItem {
  status: KnowledgebaseDocumentUploadOutcome['status'] | 'pending' | 'uploading' | 'cancelled';
  error: string | null;
}

function extensionOf(path: string): string {
  const name = path.split('/').pop() ?? path;
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

function planItemId(path: string, index: number): string {
  return `${path}#${index}`;
}

/**
 * Builds a bounded, conflict-aware ingestion plan for a batch of picked
 * files against the set of paths already present in the knowledgebase.
 */
export function planIngestion(
  sources: IngestionSourceFile[],
  existingPaths: ReadonlySet<string>,
  conflictPolicy: KnowledgebaseDocumentConflictPolicy,
  options: { supportedExtensions?: ReadonlySet<string>; maxFileSizeBytes?: number } = {}
): IngestionPlanItem[] {
  const supportedExtensions = options.supportedExtensions ?? SUPPORTED_EXTENSIONS;
  const maxFileSizeBytes = options.maxFileSizeBytes ?? MAX_FILE_SIZE_BYTES;
  const usedPaths = new Set(existingPaths);
  const seenInBatch = new Map<string, number>();

  return sources.map(({ file, path }, index) => {
    const normalizedPath = path.replace(/^\.\/+/, '').replace(/\\/g, '/');
    const extension = extensionOf(normalizedPath);
    const supported = supportedExtensions.has(extension);
    const oversized = file.size > maxFileSizeBytes;
    const duplicateInBatch = seenInBatch.has(normalizedPath);
    seenInBatch.set(normalizedPath, (seenInBatch.get(normalizedPath) ?? 0) + 1);
    const conflict = usedPaths.has(normalizedPath) || duplicateInBatch;

    let action: IngestionAction = 'add';
    let resolvedPath = normalizedPath;
    let skipReason: string | null = null;

    if (!supported) {
      action = 'skip';
      skipReason = `Unsupported file type (.${extension || 'unknown'})`;
    } else if (oversized) {
      action = 'skip';
      skipReason = `File exceeds the ${Math.round(maxFileSizeBytes / (1024 * 1024))} MB limit`;
    } else if (conflict) {
      if (conflictPolicy === 'skip') {
        action = 'skip';
        skipReason = 'Already exists in this knowledgebase';
      } else if (conflictPolicy === 'replace') {
        action = 'replace';
      } else {
        action = 'keep_both';
      }
    }

    if (action !== 'skip') {
      usedPaths.add(resolvedPath);
    }

    return {
      id: planItemId(normalizedPath, index),
      file,
      path: normalizedPath,
      size: file.size,
      extension,
      supported,
      oversized,
      conflict,
      action,
      resolvedPath,
      skipReason
    };
  });
}

/** Items that will actually be sent to the backend (excludes skipped items). */
export function includedItems(plan: IngestionPlanItem[]): IngestionPlanItem[] {
  return plan.filter((item) => item.action !== 'skip');
}

/** Splits included items into bounded batches respecting count and byte limits. */
export function chunkIngestionPlan(
  plan: IngestionPlanItem[],
  maxBatchBytes: number,
  maxBatchCount: number
): IngestionPlanItem[][] {
  const batches: IngestionPlanItem[][] = [];
  let current: IngestionPlanItem[] = [];
  let currentBytes = 0;

  for (const item of includedItems(plan)) {
    const currentPaths = new Set(current.map((entry) => entry.resolvedPath));
    const wouldOverflow =
      current.length > 0 &&
      (current.length >= maxBatchCount ||
        currentBytes + item.size > maxBatchBytes ||
        currentPaths.has(item.resolvedPath));
    if (wouldOverflow) {
      batches.push(current);
      current = [];
      currentBytes = 0;
    }
    current.push(item);
    currentBytes += item.size;
  }
  if (current.length > 0) batches.push(current);
  return batches;
}

export function totalPlanSize(plan: IngestionPlanItem[]): number {
  return includedItems(plan).reduce((sum, item) => sum + item.size, 0);
}

export function initialOutcomeStates(plan: IngestionPlanItem[]): IngestionOutcomeState[] {
  return plan.map((item) => ({
    ...item,
    status: item.action === 'skip' ? 'skipped' : 'pending',
    error: item.action === 'skip' ? item.skipReason : null
  }));
}

/** Merges a backend upload response batch into the running outcome state list, matched by resolved path. */
export function applyUploadOutcomes(
  states: IngestionOutcomeState[],
  batch: IngestionPlanItem[],
  outcomes: KnowledgebaseDocumentUploadOutcome[]
): IngestionOutcomeState[] {
  const byBatchId = new Map(batch.map((item, index) => [item.id, outcomes[index]]));
  const batchIds = new Set(batch.map((item) => item.id));

  return states.map((state) => {
    if (!batchIds.has(state.id)) return state;
    const outcome = byBatchId.get(state.id);
    if (!outcome) {
      return { ...state, status: 'failed', error: 'No upload outcome returned for this file' };
    }
    return {
      ...state,
      resolvedPath: outcome.source_path ?? state.resolvedPath,
      status: outcome.status,
      error: outcome.message ?? null
    };
  });
}

export function markBatchUploading(
  states: IngestionOutcomeState[],
  batch: IngestionPlanItem[]
): IngestionOutcomeState[] {
  const batchIds = new Set(batch.map((item) => item.id));
  return states.map((state) => (batchIds.has(state.id) ? { ...state, status: 'uploading', error: null } : state));
}

export function markBatchFailed(
  states: IngestionOutcomeState[],
  batch: IngestionPlanItem[],
  error: string
): IngestionOutcomeState[] {
  const batchIds = new Set(batch.map((item) => item.id));
  return states.map((state) => (batchIds.has(state.id) ? { ...state, status: 'failed', error } : state));
}

export function markBatchCancelled(
  states: IngestionOutcomeState[],
  batch: IngestionPlanItem[]
): IngestionOutcomeState[] {
  const batchIds = new Set(batch.map((item) => item.id));
  return states.map((state) =>
    batchIds.has(state.id) && (state.status === 'uploading' || state.status === 'pending')
      ? { ...state, status: 'cancelled', error: 'Upload cancelled' }
      : state
  );
}

/** Items eligible for a manual retry after a failed upload. */
export function retryableOutcomes(states: IngestionOutcomeState[]): IngestionOutcomeState[] {
  return states.filter((state) => state.status === 'failed');
}

export function chunkRetryableOutcomes(
  states: IngestionOutcomeState[],
  maxBatchBytes: number,
  maxBatchCount: number
): IngestionPlanItem[][] {
  return chunkIngestionPlan(retryableOutcomes(states), maxBatchBytes, maxBatchCount);
}
