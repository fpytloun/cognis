import { describe, expect, it } from 'vitest';

import {
  applyUploadOutcomes,
  chunkIngestionPlan,
  chunkRetryableOutcomes,
  includedItems,
  initialOutcomeStates,
  markBatchFailed,
  markBatchCancelled,
  markBatchUploading,
  planIngestion,
  retryableOutcomes,
  totalPlanSize,
  type IngestionSourceFile
} from './ingestion';
import type { KnowledgebaseDocumentUploadOutcome } from '$lib/types/api';

function makeFile(name: string, sizeBytes = 100, type = 'text/markdown'): File {
  return new File([new Uint8Array(sizeBytes)], name, { type });
}

function source(path: string, sizeBytes = 100): IngestionSourceFile {
  return { file: makeFile(path.split('/').pop() ?? path, sizeBytes), path };
}

describe('planIngestion', () => {
  it('marks unsupported extensions as skipped with a reason', () => {
    const plan = planIngestion([source('archive.zip')], new Set(), 'skip');
    expect(plan[0].action).toBe('skip');
    expect(plan[0].supported).toBe(false);
    expect(plan[0].skipReason).toMatch(/unsupported/i);
  });

  it('marks oversized files as skipped even if the extension is supported', () => {
    const plan = planIngestion([source('big.md', 30 * 1024 * 1024)], new Set(), 'skip');
    expect(plan[0].action).toBe('skip');
    expect(plan[0].oversized).toBe(true);
  });

  it('skips conflicting paths under the skip policy', () => {
    const plan = planIngestion([source('notes/todo.md')], new Set(['notes/todo.md']), 'skip');
    expect(plan[0].conflict).toBe(true);
    expect(plan[0].action).toBe('skip');
    expect(plan[0].resolvedPath).toBe('notes/todo.md');
  });

  it('replaces conflicting paths under the replace policy', () => {
    const plan = planIngestion([source('notes/todo.md')], new Set(['notes/todo.md']), 'replace');
    expect(plan[0].action).toBe('replace');
    expect(plan[0].resolvedPath).toBe('notes/todo.md');
  });

  it('leaves keep_both path assignment authoritative to the backend', () => {
    const plan = planIngestion([source('notes/todo.md')], new Set(['notes/todo.md']), 'keep_both');
    expect(plan[0].action).toBe('keep_both');
    expect(plan[0].resolvedPath).toBe('notes/todo.md');
  });

  it('treats duplicate paths within the same batch as conflicts', () => {
    const plan = planIngestion([source('a.md'), source('a.md')], new Set(), 'keep_both');
    expect(plan[0].conflict).toBe(false);
    expect(plan[1].conflict).toBe(true);
    expect(plan[1].resolvedPath).toBe('a.md');
  });

  it.each([
    ['skip', ['a.md']],
    ['replace', ['a.md', 'a.md']],
    ['keep_both', ['a.md', 'a.md']]
  ] as const)('plans duplicate selection paths safely for %s', (policy, expectedIncludedPaths) => {
    const plan = planIngestion([source('./a.md'), source('a.md')], new Set(), policy);
    expect(includedItems(plan).map((item) => item.resolvedPath)).toEqual(expectedIncludedPaths);
    const batches = chunkIngestionPlan(plan, 1_000_000, 25);
    for (const batch of batches) {
      const paths = batch.map((item) => item.resolvedPath);
      expect(new Set(paths).size).toBe(paths.length);
    }
  });

  it('normalizes backslashes and leading ./ in picked paths', () => {
    const plan = planIngestion([source('./folder\\file.md')], new Set(), 'skip');
    expect(plan[0].path).toBe('folder/file.md');
  });
});

describe('chunkIngestionPlan', () => {
  it('splits included items into batches bounded by count', () => {
    const sources = Array.from({ length: 5 }, (_, i) => source(`f${i}.md`));
    const plan = planIngestion(sources, new Set(), 'skip');
    const batches = chunkIngestionPlan(plan, 10 * 1024 * 1024, 2);
    expect(batches.map((batch) => batch.length)).toEqual([2, 2, 1]);
  });

  it('splits included items into batches bounded by total bytes', () => {
    const sources = [source('a.md', 60), source('b.md', 60), source('c.md', 60)];
    const plan = planIngestion(sources, new Set(), 'skip');
    const batches = chunkIngestionPlan(plan, 100, 50);
    expect(batches).toHaveLength(3);
  });

  it('excludes skipped items entirely', () => {
    const plan = planIngestion([source('a.zip'), source('b.md')], new Set(), 'skip');
    const batches = chunkIngestionPlan(plan, 100, 50);
    expect(batches.flat().map((item) => item.path)).toEqual(['b.md']);
  });

  it('uses a backend max_batch_files limit of 25', () => {
    const plan = planIngestion(
      Array.from({ length: 26 }, (_, index) => source(`nested/f${index}.md`)),
      new Set(),
      'skip'
    );
    expect(chunkIngestionPlan(plan, 1_000_000, 25).map((batch) => batch.length)).toEqual([25, 1]);
  });
});

describe('totalPlanSize / includedItems', () => {
  it('sums only included (non-skipped) file sizes', () => {
    const plan = planIngestion([source('a.zip', 500), source('b.md', 300)], new Set(), 'skip');
    expect(includedItems(plan)).toHaveLength(1);
    expect(totalPlanSize(plan)).toBe(300);
  });
});

describe('outcome state helpers', () => {
  it('seeds outcome states with skip reasons pre-filled for skipped items', () => {
    const plan = planIngestion([source('a.zip'), source('b.md')], new Set(), 'skip');
    const states = initialOutcomeStates(plan);
    expect(states[0].status).toBe('skipped');
    expect(states[0].error).toMatch(/unsupported/i);
    expect(states[1].status).toBe('pending');
  });

  it('applies upload outcomes matched by resolved path, only for the given batch', () => {
    const plan = planIngestion([source('a.md'), source('b.md')], new Set(), 'skip');
    let states = initialOutcomeStates(plan);
    states = markBatchUploading(states, [plan[0]]);
    expect(states[0].status).toBe('uploading');
    expect(states[1].status).toBe('pending');

    const outcomes: KnowledgebaseDocumentUploadOutcome[] = [{
      source_path: 'a.md', filename: 'a.md', status: 'created', artifact_id: 'art_1',
      kb_artifact_id: 'kba_1', job_id: 'job_1', error_code: null, message: null
    }];
    states = applyUploadOutcomes(states, [plan[0]], outcomes);
    expect(states[0].status).toBe('created');
    expect(states[1].status).toBe('pending');
  });

  it('marks a batch failed with a shared error message when the request itself throws', () => {
    const plan = planIngestion([source('a.md')], new Set(), 'skip');
    let states = initialOutcomeStates(plan);
    states = markBatchFailed(states, plan, 'network error');
    expect(states[0].status).toBe('failed');
    expect(states[0].error).toBe('network error');
  });

  it('marks only the active batch cancelled and preserves prior successful outcomes', () => {
    const plan = planIngestion([source('a.md'), source('b.md')], new Set(), 'skip');
    let states = initialOutcomeStates(plan);
    states = applyUploadOutcomes(states, [plan[0]], [{
      source_path: 'server/a.md', filename: 'a.md', status: 'created', artifact_id: 'art_1',
      kb_artifact_id: 'kba_1', job_id: 'job_1', error_code: null, message: null
    }]);
    states = markBatchUploading(states, [plan[1]]);
    states = markBatchCancelled(states, [plan[1]]);
    expect(states.map((state) => state.status)).toEqual(['created', 'cancelled']);
    expect(states[0].resolvedPath).toBe('server/a.md');
  });

  it('surfaces only failed items as retryable', () => {
    const plan = planIngestion([source('a.md'), source('b.md')], new Set(), 'skip');
    let states = initialOutcomeStates(plan);
    states = markBatchFailed(states, [plan[0]], 'boom');
    states = applyUploadOutcomes(states, [plan[1]], [{
      source_path: 'b.md', filename: 'b.md', status: 'created', artifact_id: 'art_2',
      kb_artifact_id: 'kba_2', job_id: 'job_2', error_code: null, message: null
    }]);
    expect(retryableOutcomes(states).map((s) => s.path)).toEqual(['a.md']);
  });

  it('chunks more than 25 failed retries and keeps duplicate paths out of each request', () => {
    const sources = [
      ...Array.from({ length: 26 }, (_, index) => source(`nested/f${index}.md`)),
      source('nested/f0.md')
    ];
    const plan = planIngestion(sources, new Set(), 'replace');
    let states = initialOutcomeStates(plan);
    states = markBatchFailed(states, plan, 'temporary failure');

    const batches = chunkRetryableOutcomes(states, 1_000_000, 25);

    expect(batches.flat()).toHaveLength(27);
    expect(batches.length).toBeGreaterThan(1);
    for (const batch of batches) {
      expect(batch.length).toBeLessThanOrEqual(25);
      const paths = batch.map((item) => item.resolvedPath);
      expect(new Set(paths).size).toBe(paths.length);
    }
  });
});
