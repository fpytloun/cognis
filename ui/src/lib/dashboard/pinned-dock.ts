import type { ActivityAvatarState } from '$lib/conversation-activity';
import type { Conversation } from '$lib/types/api';

export const PINNED_AVATAR_GAP = 6;

/** Center a tucked 3rem face on the edge of its 4rem hit target at any font scale. */
export function pinnedAvatarTuckOffset(avatarSize: number): number {
  return avatarSize / 3 + avatarSize / 2;
}

/** Reading/opening metadata is not activity; message time is the durable baseline. */
export function pinnedMessageActivityAt(conversation: Conversation | null | undefined): number {
  return conversation?.last_message_at ? Date.parse(conversation.last_message_at) || 0 : 0;
}

export function hasNewPinnedNotification(previous: Conversation | undefined, next: Conversation): boolean {
  return !!previous && (next.pending_notification_types ?? []).some(
    (type) => !(previous.pending_notification_types ?? []).includes(type),
  );
}

export function aggregatePinnedActivity(states: ActivityAvatarState[]): ActivityAvatarState {
  return states.reduce((best, state) => pinnedActivityRank(state) > pinnedActivityRank(best) ? state : best, {
    active: false, background: false, attention: false, unread: false, error: false,
    tone: 'default', label: 'No new activity',
  });
}
export function pinnedActivityRank(state: ActivityAvatarState): number {
  return state.error ? 6 : state.attention ? 5 : state.active ? 4 : state.background ? 3 : state.unread ? 2 : 1;
}

export function pinnedDockGeometry(width: number, height: number, position: number, shellTop = 0, bottomReserve = 0) {
  const top = Math.max(16, shellTop + 16);
  const bottom = Math.max(top, height - 80 - bottomReserve);
  const y = top + Math.max(0, Math.min(1, position)) * (bottom - top);
  const panelHeight = Math.max(0, Math.min(640, height - top - 24));
  return {
    y, top, bottom, panelHeight,
    panelWidth: Math.min(480, Math.max(320, width - 104)),
    panelTop: Math.max(top, Math.min(y - panelHeight / 2 + 24, height - panelHeight - 16)),
    expandUp: y > height / 2,
  };
}

export function isPinnedWorkspaceRoute(pathname: string): boolean {
  return pathname === '/';
}

/** Velocity is CSS pixels/ms; slow placement has no inertia. */
export function pinnedThrow(y: number, velocity: number, top: number, bottom: number): number {
  const distance = Math.abs(velocity) >= 0.35 ? Math.sign(velocity) * Math.min(600, velocity * velocity / 0.009) : 0;
  return Math.max(top, Math.min(bottom, y + distance));
}

/** A short intentional horizontal pull fits within the right-edge hit target. */
export function pinnedEdgeGesture(dx: number, dy: number, vx: number, fresh: boolean): 'tuck' | 'reveal' | null {
  if (Math.abs(dx) < Math.abs(dy) * 1.25) return null;
  if (dx >= 12 || (fresh && dx >= 6 && vx >= 0.35)) return 'tuck';
  if (dx <= -12 || (fresh && dx <= -6 && vx <= -0.35)) return 'reveal';
  return null;
}

export function pinnedBlockRows(height: number, size: number): number {
  const slots = Math.floor(height * 0.45 / (size + PINNED_AVATAR_GAP));
  return slots >= 6 ? 6 : slots >= 4 ? 4 : 2;
}

export function pinnedAvatarPositions(
  count: number, y: number, top: number, bottom: number,
  expandUp: boolean, expanded: boolean, size = 48,
): { x: number; y: number }[] {
  const spacing = Math.min(size + PINNED_AVATAR_GAP, (bottom - top) / Math.max(1, count - 1));
  const span = Math.max(0, count - 1) * spacing;
  const anchor = expandUp ? Math.max(y, top + span) : Math.min(y, bottom - span);
  return Array.from({ length: count }, (_, index) => expanded
    ? { x: 0, y: anchor - y + (expandUp ? -1 : 1) * index * spacing }
    : { x: index ? -Math.min(index, 3) * 5 : 0, y: index ? -Math.min(index, 3) * 5 : 0 });
}
