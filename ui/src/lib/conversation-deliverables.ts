import { browser } from '$app/environment';
import { api } from '$lib/api/client';
import { documentCoordination, StaleDocumentError, type DocumentLease } from '$lib/document-coordination';
import type { Deliverable } from '$lib/types/api';

const MAX_ENTRIES = 64;
const MAX_BYTES = 16 * 1024 * 1024;
const MAX_AGE_MS = 5 * 60 * 1000;

interface Entry {
  value: Deliverable;
  bytes: number;
  expires: number;
}
interface Owner {
  lease: DocumentLease;
  entries: Map<string, Entry>;
  flights: Map<string, Promise<Deliverable>>;
  bytes: number;
}
let owner: Owner | null = null;
let stopInvalidation: (() => void) | undefined;
let coordination: ReturnType<typeof documentCoordination> | undefined;

function current(conversationId: string): Owner | null {
  if (!browser || !conversationId) return null;
  try {
    const next = documentCoordination();
    if (coordination !== next) {
      stopInvalidation?.();
      coordination = next;
      stopInvalidation = next.onInvalidated(() => { owner = null; });
      owner = null;
    }
    const lease = next.capture();
    if (!lease.owner) return null;
    if (owner?.lease.owner !== lease.owner || owner.lease.epoch !== lease.epoch) {
      owner = { lease, entries: new Map(), flights: new Map(), bytes: 0 };
    }
    owner.lease.assertCurrent();
    return owner;
  } catch {
    // Caching is optional; the normal API remains the authorization boundary.
    owner = null;
    return null;
  }
}

function key(id: string, conversationId: string): string {
  return JSON.stringify([conversationId, id]);
}

function remove(scope: Owner, id: string): void {
  const previous = scope.entries.get(id);
  if (previous) scope.bytes -= previous.bytes;
  scope.entries.delete(id);
}

function peek(scope: Owner, id: string): Deliverable | undefined {
  const entry = scope.entries.get(id);
  if (!entry) return;
  if (entry.expires <= Date.now()) { remove(scope, id); return; }
  scope.entries.delete(id);
  scope.entries.set(id, entry);
  return entry.value;
}

function remember(scope: Owner, id: string, value: Deliverable): void {
  if (scope.entries.get(id)?.value === value) return;
  remove(scope, id);
  const bytes = JSON.stringify(value).length * 2;
  if (bytes > MAX_BYTES) return;
  scope.entries.set(id, { value, bytes, expires: Date.now() + MAX_AGE_MS });
  scope.bytes += bytes;
  while (scope.entries.size > MAX_ENTRIES || scope.bytes > MAX_BYTES) {
    remove(scope, scope.entries.keys().next().value!);
  }
}

/** Synchronous reuse for the same verified account, login epoch and accessor. */
export function peekConversationDeliverable(id: string, conversationId: string): Deliverable | undefined {
  const scope = current(conversationId);
  return scope ? peek(scope, key(id, conversationId)) : undefined;
}

/** Seed only authorized conversation data, never a task-specific loader result. */
export function rememberConversationDeliverable(value: Deliverable, conversationId: string): void {
  const scope = current(conversationId);
  if (!scope) return;
  const id = key(value.deliverable_id, conversationId);
  // A newer inline value must not be overwritten by an older pending read.
  scope.flights.delete(id);
  remember(scope, id, value);
}

export function loadConversationDeliverable(id: string, conversationId: string): Promise<Deliverable> {
  const scope = current(conversationId);
  const load = () => api.deliverables.get(id, { accessorConversationId: conversationId });
  if (!scope) return load();
  const cacheKey = key(id, conversationId);
  const cached = peek(scope, cacheKey);
  if (cached) return Promise.resolve(cached);
  const existing = scope.flights.get(cacheKey);
  if (existing) return existing;
  // A read may finish after navigation, but never after its account is revoked.
  const pending = load().then(async value => {
    await scope.lease.validate();
    if (owner !== scope) throw new StaleDocumentError();
    if (value.deliverable_id !== id) throw new Error('Deliverable response does not match its reference');
    if (scope.flights.get(cacheKey) === pending) remember(scope, cacheKey, value);
    return value;
  }).finally(() => {
    if (scope.flights.get(cacheKey) === pending) scope.flights.delete(cacheKey);
  });
  scope.flights.set(cacheKey, pending);
  return pending;
}
