import { get, writable } from 'svelte/store';
import { api } from '$lib/api/client';
import { auth } from '$lib/stores/auth';
import { addToast } from '$lib/stores/toasts';
import { wsClient, wsState } from '$lib/ws/client';
import type { InboxItem } from '$lib/types/api';

interface InboxState {
  items: InboxItem[];
  unreadCount: number;
  loading: boolean;
  error: string | null;
  cursor: string | null;
  unreadOnly: boolean;
  initialized: boolean;
}

const empty = (): InboxState => ({ items: [], unreadCount: 0, loading: false, error: null, cursor: null, unreadOnly: false, initialized: false });
export const inbox = writable<InboxState>(empty());
let generation = 0;
let owner: string | null = null;
let requestGeneration = 0;

export async function refreshInbox(append = false, notify = false): Promise<void> {
  if (!owner) return;
  const email = owner;
  const epoch = generation;
  const previous = get(inbox);
  if (append && (!previous.cursor || previous.loading)) return;
  const request = ++requestGeneration;
  inbox.update((state) => ({ ...state, loading: true, error: null }));
  try {
    const page = await api.inbox.list({ cursor: append ? previous.cursor : null, unread: previous.unreadOnly });
    if (epoch !== generation || email !== owner || request !== requestGeneration) return;
    const seen = new Set(previous.items.map((item) => item.notification_id));
    if (notify && document.visibilityState === 'visible' && document.hasFocus()) {
      const fresh = page.items.filter((item) => !item.read_at && !seen.has(item.notification_id));
      // Toasts carry the short title and a link; the full text stays in the inbox.
      if (fresh.length === 1) addToast('', 'info', 6000, fresh[0].title, { label: 'Open', href: fresh[0].href });
      else if (fresh.length > 1) addToast(fresh[0].title, 'info', 6000, `${fresh.length} new notifications`, { label: 'Open inbox', href: '/notifications' });
    }
    const items = append
      ? [...previous.items, ...page.items.filter((item) => !seen.has(item.notification_id))]
      : page.items;
    inbox.set({ items, unreadCount: page.unread_count, cursor: page.next_cursor, loading: false, error: null, unreadOnly: previous.unreadOnly, initialized: true });
  } catch {
    if (epoch === generation && request === requestGeneration) {
      inbox.update((state) => ({ ...state, loading: false, error: 'Could not load notifications. Please retry.' }));
    }
  }
}

export function setInboxUnreadOnly(unreadOnly: boolean): void {
  inbox.update((state) => ({ ...state, items: [], cursor: null, unreadOnly }));
  void refreshInbox();
}

export async function markInboxRead(ids: string[]): Promise<void> {
  const epoch = generation;
  try {
    const snapshot = [...new Set(ids)];
    for (let offset = 0; offset < snapshot.length; offset += 100) {
      if (epoch !== generation) return;
      await api.inbox.markRead(snapshot.slice(offset, offset + 100));
    }
    if (epoch === generation) await refreshInbox();
  } catch {
    if (epoch === generation) addToast('Could not mark notifications as read. Please retry.', 'error');
  }
}

/** The authenticated shell owns subscriptions; account changes fence old responses. */
export function startInbox(): () => void {
  const stopAuth = auth.subscribe((state) => {
    const email = state.status === 'authenticated' ? state.user?.email ?? null : null;
    if (email === owner) return;
    owner = email;
    generation++;
    inbox.set(empty());
    if (owner) void refreshInbox();
  });
  const stopEvents = wsClient.subscribe((event) => {
    if (event.type === 'inbox_changed') void refreshInbox(false, true);
  });
  let previousStatus = '';
  const stopState = wsState.subscribe((state) => {
    if (state.status === 'connected' && previousStatus !== 'connected') void refreshInbox();
    previousStatus = state.status;
  });
  const refresh = () => { if (document.visibilityState === 'visible') void refreshInbox(); };
  window.addEventListener('focus', refresh);
  document.addEventListener('visibilitychange', refresh);
  // Reconcile missed HA hints without relying on a live socket.
  const timer = window.setInterval(refresh, 60_000);
  return () => {
    stopAuth();
    stopEvents();
    stopState();
    window.removeEventListener('focus', refresh);
    document.removeEventListener('visibilitychange', refresh);
    window.clearInterval(timer);
    owner = null;
    generation++;
    inbox.set(empty());
  };
}
