import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Deliverable } from '$lib/types/api';

const fixture = vi.hoisted(() => ({
  account: 'account-a', epoch: 0, get: vi.fn(),
  invalidations: new Set<() => void>(),
}));
vi.mock('$app/environment', () => ({ browser: true }));
vi.mock('$lib/api/client', () => ({ api: { deliverables: { get: fixture.get } } }));
vi.mock('$lib/document-coordination', async importOriginal => ({
  ...await importOriginal<typeof import('$lib/document-coordination')>(),
  documentCoordination: () => coordination,
}));
const coordination = {
  onInvalidated: (callback: () => void) => {
    fixture.invalidations.add(callback);
    return () => fixture.invalidations.delete(callback);
  },
  capture: () => {
    if (!fixture.account) throw new Error('Unverified account');
    const account = fixture.account;
    const epoch = fixture.epoch;
    const assertCurrent = () => {
      if (account !== fixture.account || epoch !== fixture.epoch) throw new Error('Revoked');
    };
    return { owner: account, epoch: String(epoch), assertCurrent, validate: async () => assertCurrent() };
  },
};
import {
  loadConversationDeliverable as load,
  peekConversationDeliverable as peek,
  rememberConversationDeliverable as remember,
} from './conversation-deliverables';

function value(id = 'report'): Deliverable {
  return { deliverable_id: id, content: '# Loaded report', format: 'markdown',
    step_run_id: null, version: 1, attempt_number: 1, title: null, target: null,
    outputs: {}, status: 'approved', evaluator_feedback: null, created_at: null, updated_at: null };
}
function deferred() {
  let resolve!: (value: Deliverable) => void;
  const promise = new Promise<Deliverable>(next => { resolve = next; });
  return { promise, resolve };
}

beforeEach(() => {
  fixture.epoch++;
  fixture.account = 'account-a';
  fixture.invalidations.forEach(callback => callback());
  fixture.get.mockReset().mockResolvedValue(value());
  vi.useRealTimers();
});

describe('conversation deliverable memory', () => {
  it('reuses loaded data synchronously after A to B to A without a second read', async () => {
    const result = await load('report', 'conversation-a');
    expect(peek('report', 'conversation-b')).toBeUndefined();
    expect(peek('report', 'conversation-a')).toBe(result);
    await expect(load('report', 'conversation-a')).resolves.toBe(result);
    expect(fixture.get).toHaveBeenCalledOnce();
    expect(fixture.get).toHaveBeenCalledWith('report', { accessorConversationId: 'conversation-a' });
  });

  it('does not share bodies or in-flight reads between accessor conversations', async () => {
    await Promise.all([load('report', 'conversation-a'), load('report', 'conversation-b')]);
    expect(fixture.get).toHaveBeenCalledTimes(2);
  });

  it('deduplicates pending reads across unmount/remount', async () => {
    const pending = deferred();
    fixture.get.mockReturnValue(pending.promise);
    const first = load('report', 'conversation-a');
    expect(load('report', 'conversation-a')).toBe(first);
    pending.resolve(value());
    await first;
    expect(peek('report', 'conversation-a')).toBeTruthy();
    expect(fixture.get).toHaveBeenCalledOnce();
  });

  it('seeds inline data and does not let an older read overwrite it', async () => {
    const pending = deferred();
    fixture.get.mockReturnValue(pending.promise);
    const first = load('report', 'conversation-a');
    const inline = { ...value(), title: 'New metadata' };
    remember(inline, 'conversation-a');
    pending.resolve(value());
    await first;
    expect(peek('report', 'conversation-a')).toBe(inline);
  });

  it.each(['account', 'epoch'] as const)('rejects late results after %s changes', async field => {
    const pending = deferred();
    fixture.get.mockReturnValue(pending.promise);
    const first = load('report', 'conversation-a');
    if (field === 'account') fixture.account = 'account-b';
    else fixture.epoch++;
    fixture.invalidations.forEach(callback => callback());
    pending.resolve(value());
    await expect(first).rejects.toThrow();
    expect(peek('report', 'conversation-a')).toBeUndefined();
  });

  it('does not reuse previous account data even without an invalidation hint', async () => {
    await load('report', 'conversation-a');
    fixture.account = 'account-b';
    expect(peek('report', 'conversation-a')).toBeUndefined();
  });

  it('never caches failures and allows a subsequent authorized retry', async () => {
    fixture.get.mockRejectedValueOnce(new Error('Forbidden'));
    await expect(load('report', 'conversation-a')).rejects.toThrow('Forbidden');
    expect(peek('report', 'conversation-a')).toBeUndefined();
    await load('report', 'conversation-a');
    expect(fixture.get).toHaveBeenCalledTimes(2);
  });

  it('rejects mismatched detail responses', async () => {
    fixture.get.mockResolvedValue(value('different-report'));
    await expect(load('report', 'conversation-a')).rejects.toThrow('does not match');
    expect(peek('report', 'conversation-a')).toBeUndefined();
  });

  it('expires entries instead of retaining mutable lineage facts indefinitely', async () => {
    vi.useFakeTimers();
    await load('report', 'conversation-a');
    vi.advanceTimersByTime(6 * 60 * 1000);
    expect(peek('report', 'conversation-a')).toBeUndefined();
  });

  it('bounds retained entries and payload memory', () => {
    for (let i = 0; i < 65; i++) remember(value(`report-${i}`), 'conversation-a');
    expect(peek('report-0', 'conversation-a')).toBeUndefined();
    expect(peek('report-64', 'conversation-a')).toBeTruthy();
    remember({ ...value(), content: 'x'.repeat(9 * 1024 * 1024) }, 'conversation-a');
    expect(peek('report', 'conversation-a')).toBeUndefined();
  });

  it('falls back to authorized API reads without retaining unverified data', async () => {
    fixture.account = '';
    await load('report', 'conversation-a');
    expect(peek('report', 'conversation-a')).toBeUndefined();
    await load('report', 'conversation-a');
    expect(fixture.get).toHaveBeenCalledTimes(2);
  });
});
