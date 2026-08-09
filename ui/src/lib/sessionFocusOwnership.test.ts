import { describe, expect, it } from 'vitest';
import { focusedSessionAfterMiddleClose, middleOverlayOwnedSessionId } from './sessionFocusOwnership';

describe('middle sub-session focus ownership', () => {
  it('resets focus only when the middle overlay initiated synchronization', () => {
    expect(middleOverlayOwnedSessionId(true, false, 'session-a')).toBe('session-a');
    expect(middleOverlayOwnedSessionId(false, true, 'session-a')).toBe('session-a');
    expect(focusedSessionAfterMiddleClose('session-a', 'session-a')).toBeNull();
    expect(focusedSessionAfterMiddleClose('session-b', 'session-a')).toBe('session-b');
    expect(focusedSessionAfterMiddleClose('session-a', null)).toBe('session-a');
  });
});
