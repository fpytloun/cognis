import { describe, expect, it } from 'vitest';

import { LatestRequestGate, searchResetAction } from './request-gate';

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe('LatestRequestGate', () => {
  it.each(['success', 'error'] as const)(
    'blocks a delayed stale %s after a filter reset starts a new request',
    async (outcome) => {
      const gate = new LatestRequestGate();
      const oldTicket = gate.start();
      const oldRequest = deferred<string>();
      const visibleState: string[] = [];
      let visibleError: string | null = null;

      const oldCompletion = oldRequest.promise.then(
        (value) => {
          if (gate.isCurrent(oldTicket)) visibleState.push(value);
        },
        () => {
          if (gate.isCurrent(oldTicket)) visibleError = 'stale append error';
        },
      );

      gate.invalidate();
      const filterTicket = gate.start();
      if (outcome === 'success') oldRequest.resolve('stale row');
      else oldRequest.reject(new Error('invalid_cursor'));
      await oldCompletion;

      expect(oldTicket.controller.signal.aborted).toBe(true);
      expect(gate.isCurrent(filterTicket)).toBe(true);
      expect(visibleState).toEqual([]);
      expect(visibleError).toBeNull();
    },
  );
});

describe('searchResetAction', () => {
  it('restores the current scope after changed input reverts before debounce', () => {
    expect(searchResetAction('', false, 'needle')).toBe('invalidate');
    expect(searchResetAction('', true, '   ')).toBe('restore');
  });

  it('does not invalidate pagination for no-op whitespace input', () => {
    expect(searchResetAction('', false, '   ')).toBe('none');
  });

  it('reschedules the latest changed input after synchronous invalidation', () => {
    expect(searchResetAction('', true, 'ne')).toBe('reschedule');
  });
});
