import { describe, expect, it } from 'vitest';

import { isNonFatalWebSocketBackpressureError } from './errors';

describe('websocket error classification', () => {
  it('treats websocket rate limits as non-fatal transport backpressure', () => {
    expect(isNonFatalWebSocketBackpressureError({ code: 'rate_limited', message: 'Too many WebSocket messages' })).toBe(true);
    expect(isNonFatalWebSocketBackpressureError({ message: 'Too many WebSocket messages' })).toBe(true);
  });

  it('does not suppress unrelated websocket errors', () => {
    expect(isNonFatalWebSocketBackpressureError({ code: 'turn_failed', message: 'Provider failed' })).toBe(false);
    expect(isNonFatalWebSocketBackpressureError({ message: 'WebSocket disconnected' })).toBe(false);
  });
});
