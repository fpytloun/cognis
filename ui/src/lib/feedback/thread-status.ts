import type { FeedbackEntryVersion, FeedbackThread } from './types';

/** Where a comment thread stands between the reviewer and the agent. */
export type CommentStatus = 'not_sent' | 'sent' | 'answered' | 'resolved' | 'delivery_unknown' | 'not_delivered';

/** An entry the reviewer can send: anything not written by the agent. */
export function sendableEntry(entry: FeedbackEntryVersion): boolean {
  return entry.author.kind !== 'agent';
}

export function entrySent(entry: FeedbackEntryVersion): boolean {
  return entry.delivery.some(item => item.delivery === 'accepted' || item.delivery === 'pending');
}

/**
 * The newest human version decides. Editing keeps an entry's position but creates a version with
 * its own time, so order by version time: an agent entry only answers a human version it follows.
 * A resolved thread is resolved regardless of who resolved it.
 */
export function commentStatus(thread: Pick<FeedbackThread, 'state' | 'entries'>): CommentStatus {
  if (thread.state === 'resolved') return 'resolved';
  const time = (entry: FeedbackEntryVersion) => Date.parse(entry.created_at);
  const humans = thread.entries.filter(sendableEntry);
  const latestAgent = Math.max(-Infinity, ...thread.entries.filter(entry => !sendableEntry(entry)).map(time));
  if (!humans.length) return latestAgent > -Infinity ? 'answered' : 'not_sent';
  const latest = humans.reduce((newest, entry) => time(entry) >= time(newest) ? entry : newest);
  const deliveries = latest.delivery;
  const sent = deliveries.some(item => item.delivery === 'accepted' || item.delivery === 'pending');
  if (sent && latestAgent > time(latest)) return 'answered';
  if (sent) return 'sent';
  if (deliveries.some(item => item.delivery === 'uncertain')) return 'delivery_unknown';
  if (deliveries.some(item => item.delivery === 'rejected')) return 'not_delivered';
  return 'not_sent';
}

/** Statuses waiting on the reviewer: something to send, an answer to read, or a failed send. */
export function needsReviewer(status: CommentStatus): boolean {
  return status === 'not_sent' || status === 'answered' || status === 'delivery_unknown' || status === 'not_delivered';
}
