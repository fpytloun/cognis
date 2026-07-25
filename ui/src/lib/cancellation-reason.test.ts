import { describe, expect, it } from 'vitest';
import { cancellationOrigin, cancellationOriginLabel } from './cancellation-reason';

describe('cancellationOrigin', () => {
  it.each([
    ['cancelled', 'Stopped by user from managed conversation UI', 'Cancelled by user'],
    ['failed', 'Cancelled by parent session', 'Cancelled by agent'],
    ['failed', 'controller restart; parent recovered', 'Cancelled by controller restart'],
  ])('labels %s sessions using persisted provenance', (status, detail, expected) => {
    expect(cancellationOriginLabel(cancellationOrigin(status, detail))).toBe(expected);
  });

  it('does not relabel unrelated failures as cancellations', () => {
    expect(cancellationOrigin('failed', 'Provider request failed')).toBeNull();
  });
});
