import { describe, expect, it } from 'vitest';
import {
  displaySessionStatus,
  isActiveExecutionState,
  isEffectiveSessionRunning,
  isTerminalExecutionState,
  isTerminalSessionStatus,
  updateRuntimeActiveSessions,
} from './session-status';

describe('session status', () => {
  it.each(['completed', 'failed', 'cancelled', 'terminated'])(
    'keeps %s terminal despite runtime activity',
    (status) => {
      expect(isTerminalSessionStatus(status)).toBe(true);
      expect(isEffectiveSessionRunning(status, 'ongoing', true)).toBe(false);
      expect(displaySessionStatus(status, 'ongoing', true)).not.toBe('Running');
    },
  );

  it('cannot retain a terminated child runtime override', () => {
    const current = new Set(['session-child']);
    expect(updateRuntimeActiveSessions(
      current,
      'session-child',
      true,
      'terminated',
      'ongoing',
    )).toEqual(new Set());
  });

  it('retains runtime activity only for nonterminal sessions', () => {
    expect(updateRuntimeActiveSessions(
      new Set(),
      'session-child',
      true,
      'active',
      'active',
    )).toEqual(new Set(['session-child']));
  });

  describe('server-owned execution_state (additive)', () => {
    it('maps idle to Idle even when a stale runtime overlay says active', () => {
      expect(isActiveExecutionState('idle')).toBe(false);
      expect(displaySessionStatus('active', 'ongoing', true, 'idle')).toBe('Idle');
    });

    it.each([
      ['queued', 'Queued'],
      ['running', 'Running'],
      ['waiting', 'Waiting'],
      ['recovering', 'Recovering'],
    ] as const)('maps active execution_state %s to display status %s and effective running', (state, label) => {
      expect(isActiveExecutionState(state)).toBe(true);
      expect(isTerminalExecutionState(state)).toBe(false);
      expect(isEffectiveSessionRunning('active', 'active', false, state)).toBe(true);
      expect(displaySessionStatus('active', 'active', false, state)).toBe(label);
    });

    it.each(['completed', 'failed', 'cancelled'] as const)(
      'treats terminal execution_state %s as not running',
      (state) => {
        expect(isTerminalExecutionState(state)).toBe(true);
        expect(isActiveExecutionState(state)).toBe(false);
        expect(isEffectiveSessionRunning('active', 'active', false, state)).toBe(false);
      },
    );

    it('falls back to status/activity/runtime overlays when execution_state is absent', () => {
      expect(isEffectiveSessionRunning('active', 'ongoing', false, undefined)).toBe(true);
      expect(displaySessionStatus('active', 'ongoing', false, undefined)).toBe('Running');
      expect(isEffectiveSessionRunning('active', 'active', false, null)).toBe(false);
    });

    it('lets execution_state supersede a stale coarse status/activity_state pair', () => {
      // execution_state is the more current, server-owned signal and is
      // authoritative once present.
      expect(displaySessionStatus('active', 'ongoing', false, 'failed')).toBe('Failed');
      expect(displaySessionStatus('active', 'active', false, 'cancelled')).toBe('Cancelled');
      expect(displaySessionStatus('active', 'active', false, 'completed')).toBe('Completed');
    });
  });
});
