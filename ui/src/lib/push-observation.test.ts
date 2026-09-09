import { describe, expect, it } from 'vitest';

import { PushObservationRegistry } from './push-observation';

describe('PushObservationRegistry', () => {
  it('suppresses a delayed push after an open client observed the same completion', () => {
    const registry = new PushObservationRegistry();
    registry.record('client-a', 'conversation-a', '2026-08-26T21:00:00Z');

    expect(
      registry.wasObserved(
        ['client-a'],
        'conversation-a',
        '2026-08-26T21:00:00Z',
      ),
    ).toBe(true);
  });

  it('does not suppress a newer completion or another conversation', () => {
    const registry = new PushObservationRegistry();
    registry.record('client-a', 'conversation-a', '2026-08-26T21:00:00Z');

    expect(
      registry.wasObserved(
        ['client-a'],
        'conversation-a',
        '2026-08-26T21:00:01Z',
      ),
    ).toBe(false);
    expect(
      registry.wasObserved(
        ['client-a'],
        'conversation-b',
        '2026-08-26T21:00:00Z',
      ),
    ).toBe(false);
  });

  it('ignores observations from clients that are no longer open', () => {
    const registry = new PushObservationRegistry();
    registry.record('client-a', 'conversation-a', '2026-08-26T21:00:00Z');
    registry.retainClients(['client-b']);

    expect(
      registry.wasObserved(
        ['client-a'],
        'conversation-a',
        '2026-08-26T21:00:00Z',
      ),
    ).toBe(false);
  });

  it('falls back to normal delivery when timestamps are missing or invalid', () => {
    const registry = new PushObservationRegistry();
    registry.record('client-a', 'conversation-a', 'invalid');

    expect(registry.wasObserved(['client-a'], 'conversation-a', undefined)).toBe(false);
    expect(registry.wasObserved(['client-a'], 'conversation-a', 'invalid')).toBe(false);
  });
});
