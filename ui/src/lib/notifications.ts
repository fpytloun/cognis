/**
 * Browser notification utilities.
 *
 * Uses the standard Notification API (no service worker required).
 * Notifications are shown when the tab is hidden or the user is viewing
 * a different conversation.
 */

const PERMISSION_KEY = 'cognis_notification_permission_asked';

/** Whether the browser supports the Notification API. */
export function isSupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window;
}

/** Current permission state. */
export function permissionState(): NotificationPermission | 'unsupported' {
  if (!isSupported()) return 'unsupported';
  return Notification.permission;
}

/** Whether we have permission to show notifications. */
export function isGranted(): boolean {
  return isSupported() && Notification.permission === 'granted';
}

/** Whether we've already asked the user (avoid repeated prompts). */
export function hasAskedPermission(): boolean {
  if (typeof localStorage === 'undefined') return false;
  return localStorage.getItem(PERMISSION_KEY) === 'true';
}

/**
 * Request notification permission from the user.
 * Returns the resulting permission state.
 * Only asks once — subsequent calls return the cached state.
 */
export async function requestPermission(): Promise<NotificationPermission | 'unsupported'> {
  if (!isSupported()) return 'unsupported';
  if (Notification.permission !== 'default') return Notification.permission;
  try {
    const result = await Notification.requestPermission();
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(PERMISSION_KEY, 'true');
    }
    return result;
  } catch {
    return 'denied';
  }
}

/**
 * Show a browser notification.
 *
 * @param title - Notification title (e.g. agent name)
 * @param body - Notification body text
 * @param conversationId - If provided, clicking the notification navigates to this conversation
 */
export function showNotification(
  title: string,
  body: string,
  conversationId?: string,
): void {
  if (!isGranted()) return;
  try {
    const notification = new Notification(title, {
      body,
      icon: '/favicon.png',
      tag: conversationId ?? 'cognis',
      // Reuse the same tag to avoid notification spam for the same conversation
    });
    notification.onclick = () => {
      window.focus();
      if (conversationId) {
        window.location.href = `/chat/${conversationId}`;
      }
      notification.close();
    };
    // Auto-close after 10 seconds
    setTimeout(() => notification.close(), 10_000);
  } catch {
    // Notification constructor can throw in some environments
  }
}

/**
 * Show a notification for a new message in a conversation the user isn't viewing.
 * Only shows if the document is hidden or the user is on a different conversation.
 */
export function notifyIfHidden(
  title: string,
  body: string,
  conversationId: string,
  activeConversationId: string | null,
): void {
  // Don't notify if the user is actively viewing this conversation
  if (conversationId === activeConversationId && !document.hidden) return;
  showNotification(title, body, conversationId);
}
