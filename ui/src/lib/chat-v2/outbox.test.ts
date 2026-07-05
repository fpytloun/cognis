import { describe, expect, it } from 'vitest';

import {
  ChatV2OutboxUnavailableError,
  IndexedDbChatV2Outbox,
  MemoryChatV2Outbox,
  createIndexedDbChatV2Outbox,
  type OutboxEntry
} from './outbox';

function entry(overrides: Partial<OutboxEntry> = {}): OutboxEntry {
  return {
    client_txn_id: 'txn-1',
    client_message_id: 'client-1',
    conversation_id: 'conv-1',
    content: 'hello',
    attachments: [],
    status: 'pending',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}

function cloneEntry(value: OutboxEntry): OutboxEntry {
  return { ...value, attachments: [...value.attachments] };
}

function successRequest<T>(result: T): IDBRequest<T> {
  const request = {
    result,
    error: null,
    onsuccess: null,
    onerror: null
  } as unknown as IDBRequest<T>;
  queueMicrotask(() => request.onsuccess?.call(request, new Event('success')));
  return request;
}

class FakeObjectStore {
  readonly indexNames = {
    contains: (name: string) => this.indexes.has(name)
  } as DOMStringList;

  private readonly indexes = new Set<string>();

  constructor(private readonly entries: Map<string, OutboxEntry>) {}

  put(value: OutboxEntry): IDBRequest<IDBValidKey> {
    this.entries.set(value.client_txn_id, cloneEntry(value));
    return successRequest<IDBValidKey>(value.client_txn_id);
  }

  get(clientTxnId: string): IDBRequest<OutboxEntry | undefined> {
    const value = this.entries.get(clientTxnId);
    return successRequest(value ? cloneEntry(value) : undefined);
  }

  getAll(): IDBRequest<OutboxEntry[]> {
    return successRequest([...this.entries.values()].map(cloneEntry));
  }

  delete(clientTxnId: string): IDBRequest<undefined> {
    this.entries.delete(clientTxnId);
    return successRequest(undefined);
  }

  createIndex(name: string): IDBIndex {
    this.indexes.add(name);
    return this.index(name);
  }

  index(_name: string): IDBIndex {
    return {
      getAll: (conversationId?: string) =>
        successRequest(
          [...this.entries.values()]
            .filter((item) => !conversationId || item.conversation_id === conversationId)
            .map(cloneEntry)
        )
    } as unknown as IDBIndex;
  }
}

class FakeDb {
  readonly entries = new Map<string, OutboxEntry>();
  private store: FakeObjectStore | null = null;

  readonly objectStoreNames = {
    contains: (name: string) => name === 'outbox' && this.store !== null
  } as DOMStringList;

  createObjectStore(_name: string): IDBObjectStore {
    this.store = new FakeObjectStore(this.entries);
    return this.store as unknown as IDBObjectStore;
  }

  transaction(_name: string): IDBTransaction {
    this.store ??= new FakeObjectStore(this.entries);
    return {
      objectStore: () => this.store as unknown as IDBObjectStore
    } as unknown as IDBTransaction;
  }
}

class FakeIndexedDbFactory {
  readonly db = new FakeDb();

  open(_name: string, _version?: number): IDBOpenDBRequest {
    const request = {
      result: this.db as unknown as IDBDatabase,
      transaction: this.db.transaction('outbox'),
      error: null,
      onsuccess: null,
      onerror: null,
      onupgradeneeded: null
    } as unknown as IDBOpenDBRequest;
    queueMicrotask(() => {
      request.onupgradeneeded?.call(request, new Event('upgradeneeded') as IDBVersionChangeEvent);
      request.onsuccess?.call(request, new Event('success'));
    });
    return request;
  }
}

describe('Chat v2 outbox', () => {
  it('stores, updates, filters, and deletes memory entries', async () => {
    const outbox = new MemoryChatV2Outbox();
    await outbox.put(entry());
    await outbox.put(
      entry({
        client_txn_id: 'txn-2',
        client_message_id: 'client-2',
        conversation_id: 'conv-2',
        created_at: '2026-01-01T00:00:01Z'
      })
    );

    expect(await outbox.get('txn-1')).toMatchObject({ content: 'hello', status: 'pending' });
    expect((await outbox.list('conv-1')).map((item) => item.client_txn_id)).toEqual(['txn-1']);

    await outbox.update('txn-1', { status: 'failed', last_error: 'network' });
    expect(await outbox.get('txn-1')).toMatchObject({ status: 'failed', last_error: 'network' });

    await outbox.delete('txn-1');
    expect(await outbox.get('txn-1')).toBeNull();
  });

  it('clears acknowledged entries only', async () => {
    const outbox = new MemoryChatV2Outbox();
    await outbox.put(entry({ client_txn_id: 'acked', status: 'acked' }));
    await outbox.put(entry({ client_txn_id: 'pending', status: 'pending' }));

    await outbox.clearAcked();

    expect((await outbox.list()).map((item) => item.client_txn_id)).toEqual(['pending']);
  });

  it('stores, indexes, updates, deletes, and clears entries through IndexedDB', async () => {
    const indexedDB = new FakeIndexedDbFactory();
    const outbox = new IndexedDbChatV2Outbox(indexedDB as unknown as IDBFactory);

    await outbox.put(entry({ client_txn_id: 'txn-1', conversation_id: 'conv-1' }));
    await outbox.put(entry({ client_txn_id: 'txn-2', conversation_id: 'conv-2' }));

    expect((await outbox.list('conv-1')).map((item) => item.client_txn_id)).toEqual(['txn-1']);

    await outbox.update('txn-1', { status: 'acked' });
    expect(await outbox.get('txn-1')).toMatchObject({ status: 'acked' });

    await outbox.clearAcked();
    expect(await outbox.get('txn-1')).toBeNull();
    expect(await outbox.get('txn-2')).toMatchObject({ conversation_id: 'conv-2' });

    await outbox.delete('txn-2');
    expect(await outbox.list()).toEqual([]);
  });

  it('fails explicitly when IndexedDB is unavailable', () => {
    const original = globalThis.indexedDB;
    try {
      Object.defineProperty(globalThis, 'indexedDB', {
        configurable: true,
        value: undefined
      });

      expect(() => createIndexedDbChatV2Outbox()).toThrow(ChatV2OutboxUnavailableError);
    } finally {
      Object.defineProperty(globalThis, 'indexedDB', {
        configurable: true,
        value: original
      });
    }
  });
});
