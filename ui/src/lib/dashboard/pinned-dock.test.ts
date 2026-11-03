import { describe, it, expect } from 'vitest';
import { aggregatePinnedActivity, pinnedDockGeometry, pinnedAvatarPositions, pinnedThrow, pinnedEdgeGesture, pinnedBlockRows, pinnedMessageActivityAt, hasNewPinnedNotification, PINNED_AVATAR_GAP, isPinnedWorkspaceRoute, pinnedAvatarTuckOffset } from './pinned-dock';
import type { Conversation } from '$lib/types/api';
import type { ActivityAvatarState } from '$lib/conversation-activity';
const neutral: ActivityAvatarState = { active: false, background: false, attention: false,
  unread: false, error: false, tone: 'default', label: 'Idle' };

describe('pinned dock', () => {
  it.each([36, 42, 48, 60])('centers a tucked %ipx face on either viewport edge', (size) => {
    const targetWidth = size * 4 / 3;
    expect(-targetWidth + size / 2 + pinnedAvatarTuckOffset(size)).toBeCloseTo(0);
    expect(size / 3 + size / 2 - pinnedAvatarTuckOffset(size)).toBeCloseTo(0);
  });

  it('uses message time rather than open/read/session metadata for activity ordering', () => {
    const base = { last_message_at: '2026-10-15T08:00:00Z', pending_notification_types: [] } as unknown as Conversation;
    expect(pinnedMessageActivityAt({ ...base, updated_at: '2099-01-01', active_session_updated_at: '2099-01-01' }))
      .toBe(Date.parse(base.last_message_at!));
    expect(pinnedMessageActivityAt({ ...base, last_message_at: null })).toBe(0);
    expect(hasNewPinnedNotification(base, { ...base, pending_notification_types: ['step_question'] })).toBe(true);
    expect(hasNewPinnedNotification({ ...base, pending_notification_types: ['step_question'] }, base)).toBe(false);
    expect(hasNewPinnedNotification(undefined, { ...base, pending_notification_types: ['step_question'] })).toBe(false);
  });
  it('aggregates precedence at both group and stack levels', () => {
    const unread = { ...neutral, unread: true };
    const running = { ...neutral, active: true };
    const attention = { ...neutral, attention: true, tone: 'amber' as const };
    const error = { ...neutral, error: true, tone: 'rose' as const };
    expect(aggregatePinnedActivity([unread, running])).toBe(running);
    expect(aggregatePinnedActivity([running, attention])).toBe(attention);
    expect(aggregatePinnedActivity([attention, error])).toBe(error);
    expect(aggregatePinnedActivity([aggregatePinnedActivity([unread, running]), attention])).toBe(attention);
  });
  it('clamps vertical position and opens panels inward within the safe viewport', () => {
    for (const width of [768, 1024, 1440, 2560]) {
      for (const height of [500, 900]) {
        for (const position of [-1, 0, 0.5, 1, 2]) {
          const result = pinnedDockGeometry(width, height, position, 48);
          expect(result.y).toBeGreaterThanOrEqual(64);
          expect(result.y + 48).toBeLessThanOrEqual(height);
          expect(result.panelTop + result.panelHeight).toBeLessThanOrEqual(height - 16);
          expect(result.panelWidth + 80).toBeLessThanOrEqual(width);
        }
      }
    }
  });
  it('suppresses the stack only on the main dashboard, not projects', () => {
    expect(isPinnedWorkspaceRoute('/')).toBe(true);
    expect(isPinnedWorkspaceRoute('/projects/project-one')).toBe(false);
    expect(isPinnedWorkspaceRoute('/projects')).toBe(false);
    expect(isPinnedWorkspaceRoute('/chat/chat-one')).toBe(false);
  });
  it('keeps slow placement precise and throws with bounded velocity-sensitive travel', () => {
    expect(pinnedThrow(400, 0.2, 16, 820)).toBe(400);
    expect(pinnedThrow(400, -0.8, 16, 820)).toBeLessThan(pinnedThrow(400, -0.4, 16, 820));
    expect(pinnedThrow(400, 10, 16, 820)).toBe(820);
    expect(pinnedThrow(400, -10, 16, 820)).toBe(16);
  });
  it('accepts short edge gestures without mistaking vertical movement or jitter for tucking', () => {
    expect(pinnedEdgeGesture(12, 2, 0.05, false)).toBe('tuck');
    expect(pinnedEdgeGesture(6, 1, 0.5, true)).toBe('tuck');
    expect(pinnedEdgeGesture(-12, 2, -0.05, false)).toBe('reveal');
    expect(pinnedEdgeGesture(4, 0, 1, true)).toBeNull();
    expect(pinnedEdgeGesture(12, 40, 0.5, true)).toBeNull();
    expect(pinnedEdgeGesture(6, 0, 0.5, false)).toBeNull();
  });
  it('uses display-relative 2/4/6 rows while keeping the whole block within vertical bounds', () => {
    expect(pinnedBlockRows(400, 48)).toBe(2);
    expect(pinnedBlockRows(600, 48)).toBe(4);
    expect(pinnedBlockRows(900, 48)).toBe(6);
    for (const height of [400, 600, 900]) {
      const span = (pinnedBlockRows(height, 48) - 1) * (48 + PINNED_AVATAR_GAP);
      for (const position of [0, 0.5, 1]) {
        const geometry = pinnedDockGeometry(1440, height, position, 48, span);
        expect(geometry.y + span + 48).toBeLessThanOrEqual(height - 16);
      }
    }
  });
  it('separates the same avatar slots vertically in either direction', () => {
    expect(pinnedAvatarPositions(3, 300, 64, 820, false, false))
      .toEqual([{ x: 0, y: 0 }, { x: -5, y: -5 }, { x: -10, y: -10 }]);
    expect(pinnedAvatarPositions(3, 300, 64, 820, false, true))
      .toEqual([{ x: 0, y: 0 }, { x: 0, y: 54 }, { x: 0, y: 108 }]);
    expect(pinnedAvatarPositions(3, 600, 64, 820, true, true))
      .toEqual([{ x: 0, y: 0 }, { x: 0, y: -54 }, { x: 0, y: -108 }]);
  });
  it('fits an expanded stack within the viewport even near an edge', () => {
    for (const count of [1, 2, 10]) {
      for (const y of [64, 300, 820]) {
        for (const up of [false, true]) {
          const positions = pinnedAvatarPositions(count, y, 64, 820, up, true, 60);
          for (const position of positions) {
            expect(position.x).toBe(0);
            expect(position.y + y).toBeGreaterThanOrEqual(64);
            expect(position.y + y).toBeLessThanOrEqual(820);
          }
        }
      }
    }
  });
});
