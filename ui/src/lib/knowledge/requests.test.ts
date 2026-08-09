import { describe, expect, it } from 'vitest';

import { LatestRequestController } from './requests';

describe('LatestRequestController', () => {
  it('aborts and invalidates an older request when a newer request starts', () => {
    const requests = new LatestRequestController();
    const first = requests.begin();
    const second = requests.begin();
    expect(first.controller.signal.aborted).toBe(true);
    expect(requests.isCurrent(first)).toBe(false);
    expect(requests.isCurrent(second)).toBe(true);
  });

  it('prevents a stale finally block from finishing the current request', () => {
    const requests = new LatestRequestController();
    const first = requests.begin();
    const second = requests.begin();
    expect(requests.finish(first)).toBe(false);
    expect(requests.isCurrent(second)).toBe(true);
    expect(requests.finish(second)).toBe(true);
  });
});
