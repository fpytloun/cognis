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
const IDB_OPERATION_TIMEOUT_MS = 3_000;

export class ChatV2OutboxUnavailableError extends Error {
  constructor(message = 'IndexedDB is not available for Chat v2 outbox persistence') {
    super(message);
    this.name = 'ChatV2OutboxUnavailableError';
  }
}

export class IndexedDbChatV2Outbox implements ChatV2Outbox {
  private readonly indexedDB: IDBFactory;
  private dbPromise: Promise<IDBDatabase> | null = null;
  private memoryFallback: MemoryChatV2Outbox | null = null;

  constructor(indexedDBFactory: IDBFactory = indexedDB) {
    this.indexedDB = indexedDBFactory;
  }

  async put(entry: OutboxEntry): Promise<void> {
    await this.withFallback(
      () => this.putDb(entry),
      (outbox) => outbox.put(entry)
    );
  }

  async get(clientTxnId: string): Promise<OutboxEntry | null> {
    return this.withFallback(
      () => this.getDb(clientTxnId),
      (outbox) => outbox.get(clientTxnId)
    );
  }

  async list(conversationId?: string): Promise<OutboxEntry[]> {
    return this.withFallback(
      () => this.listDb(conversationId),
      (outbox) => outbox.list(conversationId)
    );
  }

  async update(
    clientTxnId: string,
    patch: Partial<Omit<OutboxEntry, 'client_txn_id' | 'created_at'>>
  ): Promise<void> {
    await this.withFallback(
      async () => {
        const existing = await this.getDb(clientTxnId);
        if (!existing) return;
        await this.putDb({
          ...existing,
          ...patch,
          client_txn_id: existing.client_txn_id,
          created_at: existing.created_at,
          updated_at: new Date().toISOString()
        });
      },
      (outbox) => outbox.update(clientTxnId, patch)
    );
  }

  async delete(clientTxnId: string): Promise<void> {
    await this.withFallback(
      () => this.deleteDb(clientTxnId),
      (outbox) => outbox.delete(clientTxnId)
    );
  }

  async clearAcked(): Promise<void> {
    await this.withFallback(
      async () => {
        const entries = await this.listDb();
        await Promise.all(entries.filter((entry) => entry.status === 'acked').map((entry) => this.deleteDb(entry.client_txn_id)));
      },
      (outbox) => outbox.clearAcked()
    );
  }

  private db(): Promise<IDBDatabase> {
    this.dbPromise ??= openDatabase(this.indexedDB).catch((error) => {
      this.dbPromise = null;
      throw error;
    });
    return this.dbPromise;
  }

  private async putDb(entry: OutboxEntry): Promise<void> {
    const db = await this.db();
    await requestToPromise(transactionStore(db, 'readwrite').put(entry));
  }

  private async getDb(clientTxnId: string): Promise<OutboxEntry | null> {
    const db = await this.db();
    const value = await requestToPromise<OutboxEntry | undefined>(
      transactionStore(db, 'readonly').get(clientTxnId)
    );
    return value ?? null;
  }

  private async listDb(conversationId?: string): Promise<OutboxEntry[]> {
    const db = await this.db();
    const store = transactionStore(db, 'readonly');
    const source: IDBObjectStore | IDBIndex = conversationId
      ? store.index(CONVERSATION_INDEX)
      : store;
    const request = conversationId ? source.getAll(conversationId) : source.getAll();
    return requestToPromise<OutboxEntry[]>(request);
  }

  private async deleteDb(clientTxnId: string): Promise<void> {
    const db = await this.db();
    await requestToPromise(transactionStore(db, 'readwrite').delete(clientTxnId));
  }

  private fallback(): MemoryChatV2Outbox {
    this.dbPromise = null;
    this.memoryFallback ??= new MemoryChatV2Outbox();
    return this.memoryFallback;
  }

  private async withFallback<T>(
    operation: () => Promise<T>,
    fallbackOperation: (outbox: MemoryChatV2Outbox) => Promise<T>
  ): Promise<T> {
    if (this.memoryFallback) {
      return fallbackOperation(this.memoryFallback);
    }
    try {
      return await operation();
    } catch (error) {
      console.warn('Chat v2 IndexedDB outbox unavailable; using in-memory outbox', error);
      return fallbackOperation(this.fallback());
    }
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
  return withIdbTimeout(new Promise((resolve, reject) => {
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
  }), 'Opening Chat v2 IndexedDB outbox');
}

function transactionStore(db: IDBDatabase, mode: IDBTransactionMode): IDBObjectStore {
  return db.transaction(STORE_NAME, mode).objectStore(STORE_NAME);
}

function requestToPromise<T = unknown>(request: IDBRequest<T>): Promise<T> {
  return withIdbTimeout(new Promise((resolve, reject) => {
    request.onerror = () => reject(request.error ?? new Error('IndexedDB request failed'));
    request.onsuccess = () => resolve(request.result);
  }), 'Chat v2 IndexedDB request');
}

function withIdbTimeout<T>(promise: Promise<T>, label: string): Promise<T> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timeoutId = setTimeout(() => {
      reject(new ChatV2OutboxUnavailableError(`${label} timed out after ${Math.round(IDB_OPERATION_TIMEOUT_MS / 1000)} seconds`));
    }, IDB_OPERATION_TIMEOUT_MS);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  });
}
