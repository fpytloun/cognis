import { describe, expect, it } from 'vitest';
import {
  promoteRootOverview,
  RootOverviewRequestEpoch,
} from './rootOverviewRequestEpoch';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe('RootOverviewRequestEpoch', () => {
  it('keeps R2 when R2 resolves before the older R1', async () => {
    const epoch = new RootOverviewRequestEpoch();
    const r1 = deferred<string>();
    const r2 = deferred<string>();
    let currentRoot = 'root';
    let rendered: string | null = null;
    const p1 = promoteRootOverview(epoch, epoch.begin('root'), () => currentRoot, () => r1.promise, (value) => { rendered = value; });
    const p2 = promoteRootOverview(epoch, epoch.begin('root'), () => currentRoot, () => r2.promise, (value) => { rendered = value; });

    r2.resolve('R2');
    await p2;
    r1.resolve('R1');
    await p1;

    expect(rendered).toBe('R2');
    void currentRoot;
  });

  it('does not let a rejected root A fallback clear or replace root B', async () => {
    const epoch = new RootOverviewRequestEpoch();
    const a = deferred<string>();
    const b = deferred<string>();
    let currentRoot: string | null = 'A';
    let rendered: string | null = 'stale-A';
    const loadA = promoteRootOverview(epoch, epoch.begin('A'), () => currentRoot, () => a.promise, (value) => { rendered = value; })
      .catch(() => {
        if (currentRoot === 'A') rendered = null;
      });

    currentRoot = 'B';
    const loadB = promoteRootOverview(epoch, epoch.begin('B'), () => currentRoot, () => b.promise, (value) => { rendered = value; });
    b.resolve('root-B');
    await loadB;
    a.reject(new Error('root A unavailable'));
    await loadA;

    expect(rendered).toBe('root-B');
  });

  it('keeps focused-child and root refresh epochs independent', async () => {
    const rootEpoch = new RootOverviewRequestEpoch();
    const childEpoch = new RootOverviewRequestEpoch();
    const root = deferred<string>();
    const child = deferred<string>();
    let rootValue: string | null = null;
    let childValue: string | null = null;

    const rootLoad = promoteRootOverview(rootEpoch, rootEpoch.begin('root'), () => 'root', () => root.promise, (value) => { rootValue = value; });
    const childLoad = promoteRootOverview(childEpoch, childEpoch.begin('child'), () => 'child', () => child.promise, (value) => { childValue = value; });
    child.resolve('child-refresh');
    await childLoad;
    root.resolve('root-refresh');
    await rootLoad;

    expect({ rootValue, childValue }).toEqual({
      rootValue: 'root-refresh',
      childValue: 'child-refresh',
    });
  });
});
