import type { AttachmentRef } from '$lib/types/api';
import type { ChatMode } from './types';

export type OutboxStatus = 'pending' | 'sending' | 'acked' | 'failed';

export interface OutboxEntry {
  client_txn_id: string;
  client_message_id: string;
  conversation_id: string;
  content: string;
  attachments: AttachmentRef[];
  chat_mode?: ChatMode | null;
  status: OutboxStatus;
  created_at: string;
  updated_at: string;
  last_error?: string | null;
}

export interface ChatV2Outbox {
  put(entry: OutboxEntry): Promise<void>;
  get(clientTxnId: string): Promise<OutboxEntry | null>;
  list(conversationId?: string): Promise<OutboxEntry[]>;
  update(clientTxnId: string, patch: Partial<Omit<OutboxEntry, 'client_txn_id' | 'created_at'>>): Promise<void>;
  delete(clientTxnId: string): Promise<void>;
  clearAcked(): Promise<void>;
}

const DB_NAME = 'cognis-chat-v2';
const DB_VERSION = 1;
const STORE_NAME = 'outbox';
const CONVERSATION_INDEX = 'conversation_id';

export class ChatV2OutboxUnavailableError extends Error {
  constructor(message = 'IndexedDB is not available for Chat v2 outbox persistence') {
    super(message);
    this.name = 'ChatV2OutboxUnavailableError';
  }
}

export class IndexedDbChatV2Outbox implements ChatV2Outbox {
  private readonly indexedDB: IDBFactory;
  private dbPromise: Promise<IDBDatabase> | null = null;

  constructor(indexedDBFactory: IDBFactory = indexedDB) {
    this.indexedDB = indexedDBFactory;
  }

  async put(entry: OutboxEntry): Promise<void> {
    const db = await this.db();
    await requestToPromise(transactionStore(db, 'readwrite').put(entry));
  }

  async get(clientTxnId: string): Promise<OutboxEntry | null> {
    const db = await this.db();
    const value = await requestToPromise<OutboxEntry | undefined>(
      transactionStore(db, 'readonly').get(clientTxnId)
    );
    return value ?? null;
  }

  async list(conversationId?: string): Promise<OutboxEntry[]> {
    const db = await this.db();
    const store = transactionStore(db, 'readonly');
    const source: IDBObjectStore | IDBIndex = conversationId
      ? store.index(CONVERSATION_INDEX)
      : store;
    const request = conversationId ? source.getAll(conversationId) : source.getAll();
    return requestToPromise<OutboxEntry[]>(request);
  }

  async update(
    clientTxnId: string,
    patch: Partial<Omit<OutboxEntry, 'client_txn_id' | 'created_at'>>
  ): Promise<void> {
    const existing = await this.get(clientTxnId);
    if (!existing) return;
    await this.put({
      ...existing,
      ...patch,
      client_txn_id: existing.client_txn_id,
      created_at: existing.created_at,
      updated_at: new Date().toISOString()
    });
  }

  async delete(clientTxnId: string): Promise<void> {
    const db = await this.db();
    await requestToPromise(transactionStore(db, 'readwrite').delete(clientTxnId));
  }

  async clearAcked(): Promise<void> {
    const entries = await this.list();
    await Promise.all(entries.filter((entry) => entry.status === 'acked').map((entry) => this.delete(entry.client_txn_id)));
  }

  private db(): Promise<IDBDatabase> {
    this.dbPromise ??= openDatabase(this.indexedDB);
    return this.dbPromise;
  }
}

export class MemoryChatV2Outbox implements ChatV2Outbox {
  private readonly entries = new Map<string, OutboxEntry>();

  async put(entry: OutboxEntry): Promise<void> {
    this.entries.set(entry.client_txn_id, { ...entry, attachments: [...entry.attachments] });
  }

  async get(clientTxnId: string): Promise<OutboxEntry | null> {
    const entry = this.entries.get(clientTxnId);
    return entry ? { ...entry, attachments: [...entry.attachments] } : null;
  }

  async list(conversationId?: string): Promise<OutboxEntry[]> {
    return [...this.entries.values()]
      .filter((entry) => !conversationId || entry.conversation_id === conversationId)
      .map((entry) => ({ ...entry, attachments: [...entry.attachments] }))
      .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.client_txn_id.localeCompare(b.client_txn_id));
  }

  async update(
    clientTxnId: string,
    patch: Partial<Omit<OutboxEntry, 'client_txn_id' | 'created_at'>>
  ): Promise<void> {
    const existing = this.entries.get(clientTxnId);
    if (!existing) return;
    this.entries.set(clientTxnId, {
      ...existing,
      ...patch,
      client_txn_id: existing.client_txn_id,
      created_at: existing.created_at,
      updated_at: new Date().toISOString()
    });
  }

  async delete(clientTxnId: string): Promise<void> {
    this.entries.delete(clientTxnId);
  }

  async clearAcked(): Promise<void> {
    for (const [id, entry] of this.entries) {
      if (entry.status === 'acked') this.entries.delete(id);
    }
  }
}

export function createIndexedDbChatV2Outbox(): IndexedDbChatV2Outbox {
  if (typeof indexedDB === 'undefined') {
    throw new ChatV2OutboxUnavailableError();
  }
  return new IndexedDbChatV2Outbox(indexedDB);
}

function openDatabase(indexedDBFactory: IDBFactory): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDBFactory.open(DB_NAME, DB_VERSION);
    request.onerror = () => reject(request.error ?? new Error('Failed to open Chat v2 IndexedDB outbox'));
    request.onsuccess = () => resolve(request.result);
    request.onupgradeneeded = () => {
      const db = request.result;
      const store = db.objectStoreNames.contains(STORE_NAME)
        ? request.transaction?.objectStore(STORE_NAME)
        : db.createObjectStore(STORE_NAME, { keyPath: 'client_txn_id' });
      if (store && !store.indexNames.contains(CONVERSATION_INDEX)) {
        store.createIndex(CONVERSATION_INDEX, CONVERSATION_INDEX, { unique: false });
      }
    };
  });
}

function transactionStore(db: IDBDatabase, mode: IDBTransactionMode): IDBObjectStore {
  return db.transaction(STORE_NAME, mode).objectStore(STORE_NAME);
}

function requestToPromise<T = unknown>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onerror = () => reject(request.error ?? new Error('IndexedDB request failed'));
    request.onsuccess = () => resolve(request.result);
  });
}
