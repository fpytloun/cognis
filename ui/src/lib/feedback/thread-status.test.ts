import { expect, it } from 'vitest';
import { commentStatus, needsReviewer } from './thread-status';
import type { FeedbackEntryVersion } from './types';

function entry(author: 'verified_user' | 'agent', at: string, delivery: FeedbackEntryVersion['delivery'][number]['delivery'] | null = null, version = 1): FeedbackEntryVersion {
  return { entry_id: crypto.randomUUID(), entry_version_id: crypto.randomUUID(), thread_id: 't', version, body: 'Comment',
    audience: 'internal', created_at: `2026-11-10T${at}:00Z`, reply_to_entry_id: null,
    author: { kind: author, label: author === 'agent' ? 'Forge' : 'Reviewer', is_self: author !== 'agent' },
    delivery: delivery ? [{ submission_id: crypto.randomUUID(), manifest_hash: 'a'.repeat(64), delivery }] : [] };
}
const open = (...entries: FeedbackEntryVersion[]) => ({ state: 'open' as const, entries });

it('follows a comment from not sent through sent to answered and resolved', () => {
  expect(commentStatus(open(entry('verified_user', '08:00')))).toBe('not_sent');
  expect(commentStatus(open(entry('verified_user', '08:00', 'accepted')))).toBe('sent');
  expect(commentStatus(open(entry('verified_user', '08:00', 'accepted'), entry('agent', '08:05')))).toBe('answered');
  expect(commentStatus({ state: 'resolved', entries: [entry('verified_user', '08:00')] })).toBe('resolved');
});

it('does not count an answer to an earlier version of an edited comment', () => {
  // Editing keeps the entry before the agent reply but gives the new version a later time.
  expect(commentStatus(open(entry('verified_user', '09:00', null, 2), entry('agent', '08:05')))).toBe('not_sent');
  expect(commentStatus(open(entry('verified_user', '09:00', 'accepted', 2), entry('agent', '08:05')))).toBe('sent');
  expect(commentStatus(open(entry('verified_user', '09:00', 'accepted', 2), entry('agent', '08:05'), entry('agent', '09:30')))).toBe('answered');
});

it('surfaces failed or unknown delivery and counts only what waits on the reviewer', () => {
  expect(commentStatus(open(entry('verified_user', '08:00', 'uncertain')))).toBe('delivery_unknown');
  expect(commentStatus(open(entry('verified_user', '08:00', 'rejected')))).toBe('not_delivered');
  expect(['not_sent', 'sent', 'answered', 'resolved', 'delivery_unknown', 'not_delivered'].filter(status =>
    needsReviewer(status as Parameters<typeof needsReviewer>[0]))).toEqual(['not_sent', 'answered', 'delivery_unknown', 'not_delivered']);
});
