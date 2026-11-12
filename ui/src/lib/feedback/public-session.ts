import type { PublicFeedbackAccess, PublicGuestSession } from './public-api';

const key = 'cognis:public-feedback-session';
export interface PublicFeedbackSession { access: PublicFeedbackAccess; guest: PublicGuestSession | null }
export function readPublicFeedbackSession(): PublicFeedbackSession | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw || raw.length > 32768) return null;
    const value = JSON.parse(raw) as PublicFeedbackSession;
    if (!value.access?.capability || !value.access.source?.content_ref || !value.access.share_id
      || (value.access.expires_at && Date.parse(value.access.expires_at) <= Date.now())) return null;
    if (value.guest && Date.parse(value.guest.expires_at) <= Date.now()) value.guest = null;
    return value;
  } catch { return null; }
}
export function savePublicFeedbackSession(value: PublicFeedbackSession): void {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* In-memory viewing remains available. */ }
}
export function clearPublicFeedbackSession(): void { try { sessionStorage.removeItem(key); } catch { /* Storage may be disabled. */ } }
export function guestDraftKey(share: string, guest: string): string {
  return `cognis:public-feedback-draft:${encodeURIComponent(share)}:${encodeURIComponent(guest)}`;
}
