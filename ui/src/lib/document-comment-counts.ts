import { api } from '$lib/api/client';
import { auth } from '$lib/stores/auth';

type Count = number | undefined;
type Listener = (count: Count, failed: boolean) => void;
const watched = new Map<string, Set<Listener>>();
const dirty = new Set<string>();
const requests = new Map<string, object>();
let scheduled = false;
let epoch = 0;
let user: string | undefined;

function publish(ref: string, count: Count, failed = false) {
  for (const listener of watched.get(ref) ?? []) listener(count, failed);
}
function schedule() {
  if (scheduled || !user || !dirty.size) return;
  scheduled = true;
  queueMicrotask(() => {
    scheduled = false;
    if (!user) return;
    const version = epoch;
    const refs = [...dirty].filter(ref => watched.has(ref));
    dirty.clear();
    for (let start = 0; start < refs.length; start += 100) {
      const batch = refs.slice(start, start + 100);
      const request = {};
      for (const ref of batch) requests.set(ref, request);
      void api.library.commentCounts(batch).then(({ counts }) => {
        if (version !== epoch) return;
        for (const ref of batch) if (requests.get(ref) === request) publish(ref, counts[ref]);
      }, () => {
        if (version !== epoch) return;
        for (const ref of batch) if (requests.get(ref) === request) publish(ref, undefined, true);
      });
    }
  });
}
function refresh() {
  epoch++;
  for (const ref of watched.keys()) {
    publish(ref, undefined);
    dirty.add(ref);
  }
  schedule();
}
auth.subscribe(value => {
  if (user === value.user?.email) return;
  user = value.user?.email;
  refresh();
});

/** Coalesce mounted badges into metadata-only reads, never one request per row. */
export function watchDocumentCommentCount(ref: string, listener: Listener): () => void {
  if (!watched.size) {
    window.addEventListener('feedback-changed', refresh);
    window.addEventListener('focus', refresh);
  }
  let listeners = watched.get(ref);
  if (!listeners) {
    listeners = new Set();
    watched.set(ref, listeners);
  }
  listeners.add(listener);
  dirty.add(ref);
  schedule();
  return () => {
    listeners.delete(listener);
    if (!listeners.size) { watched.delete(ref); dirty.delete(ref); requests.delete(ref); }
    if (!watched.size) {
      epoch++;
      window.removeEventListener('feedback-changed', refresh);
      window.removeEventListener('focus', refresh);
    }
  };
}
