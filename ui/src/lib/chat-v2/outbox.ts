import type { AttachmentRef } from '$lib/types/api';
import type { IncludedFeedback } from '$lib/feedback/types';
import type { ChatMode } from './types';
import type { SendMessageV2Response } from './types';
import { documentCoordination, CoordinationUnavailableError, StaleDocumentError, type DocumentCoordination, type DocumentLease } from '$lib/document-coordination';
import { scopedTransaction } from '$lib/offline/scoped-idb';
import { snapshotPersistedMetadata } from './persisted-metadata';
import { normalizeSubmissionInput, sameSubmissionInput, type SubmissionBinding } from './submission-input';

export type OutboxStatus = 'preparing' | 'pending' | 'sending' | 'acked' | 'failed';

export interface OutboxEntry {
  submission?: SubmissionBinding;
  /** Older unowned entries are preserved, but never automatically sent by a new account. */
  owner?: string;
  /** Admission receipt lets another window settle the same txn without resending. */
  admitted?: SendMessageV2Response;
  client_txn_id: string;
  client_message_id: string;
  conversation_id: string;
  content: string;
  attachments: AttachmentRef[];
  chat_mode?: ChatMode | null;
  feedback?: IncludedFeedback | null;
  status: OutboxStatus;
  created_at: string;
  updated_at: string;
  last_error?: string | null;
  /** False after a permanent rejection; automatic retries skip the entry. */
  retryable?: boolean;
}

export interface ChatV2Outbox {
  reserve(entry: OutboxEntry, binding: SubmissionBinding): Promise<OutboxEntry>;
  activate(clientTxnId: string, attachments: AttachmentRef[]): Promise<OutboxEntry>;
  put(entry: OutboxEntry): Promise<void>;
  get(clientTxnId: string): Promise<OutboxEntry | null>;
  list(conversationId?: string): Promise<OutboxEntry[]>;
  update(clientTxnId: string, patch: Partial<Omit<OutboxEntry, 'client_txn_id' | 'created_at'>>): Promise<void>;
  delete(clientTxnId: string): Promise<void>;
  clearAcked(): Promise<void>;
  /** True only when entries survive a reload (not an in-memory fallback). */
  isDurable(): boolean;
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

  constructor(indexedDBFactory: IDBFactory = indexedDB, private readonly name = DB_NAME,
    private readonly lease?: DocumentLease) {
    this.indexedDB = indexedDBFactory;
  }

  isDurable(): boolean {
    return this.memoryFallback === null;
  }

  async put(entry: OutboxEntry): Promise<void> {
    entry = snapshotPersistedMetadata(entry);
    if (entry.submission || entry.status === 'preparing') throw new Error('Submission bindings require reservation');
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
    patch = snapshotPersistedMetadata(patch);
    if ('submission' in patch || 'client_message_id' in patch || 'conversation_id' in patch
      || 'content' in patch || 'chat_mode' in patch || 'feedback' in patch || patch.status === 'preparing') {
      throw new Error('Immutable outbox input cannot be updated');
    }
    await this.withFallback(
      async () => {
        const db = await this.db();
        await this.lease?.validate();
        const { store, done, guard } = scopedTransaction(db, STORE_NAME, 'readwrite', this.lease);
        const request = store.get(clientTxnId) as IDBRequest<OutboxEntry | undefined>;
        request.onsuccess = () => guard(() => {
          const existing = request.result;
          if (existing?.status === 'preparing') throw new Error('Submission preparation requires activation');
          if (existing?.submission && existing.status === 'sending' && patch.status === 'pending') return;
          if (existing && existing.status !== 'acked' && !existing.admitted) store.put({ ...existing, ...patch,
            client_txn_id: existing.client_txn_id, owner: existing.owner,
            created_at: existing.created_at, updated_at: new Date().toISOString() });
        });
        await done;
      },
      (outbox) => outbox.update(clientTxnId, patch)
    );
  }

  async reserve(proposal: OutboxEntry, binding: SubmissionBinding): Promise<OutboxEntry> {
    const entry = snapshotPersistedMetadata(proposal);
    const captured = snapshotPersistedMetadata({ ...binding, input: normalizeSubmissionInput(binding.input) });
    if (!captured.draft_key || !captured.input_revision || captured.version !== 1) throw new Error('Invalid submission input');
    const db = await this.db();
    await this.lease?.validate();
    const { store, done, guard } = scopedTransaction(db, STORE_NAME, 'readwrite', this.lease);
    let result!: OutboxEntry;
    const request = store.index(CONVERSATION_INDEX).getAll(entry.conversation_id) as IDBRequest<OutboxEntry[]>;
    request.onsuccess = () => guard(() => {
      const matches = request.result.filter((row) => row.owner === entry.owner
        && row.submission?.draft_key === captured.draft_key
        && row.submission.input_revision === captured.input_revision);
      if (matches.length > 1) throw new Error('Duplicate submission input binding');
      const existing = matches[0];
      if (existing) {
        if (!sameSubmissionInput(existing.submission!.input, captured.input)) throw new Error('Submission input revision mismatch');
        result = existing;
      } else {
        result = { ...entry, client_txn_id: crypto.randomUUID(), client_message_id: crypto.randomUUID(),
          submission: captured, status: 'preparing', admitted: undefined };
        store.add(result);
      }
    });
    await done;
    return result;
  }

  async activate(clientTxnId: string, attachments: AttachmentRef[]): Promise<OutboxEntry> {
    attachments = snapshotPersistedMetadata(attachments);
    const db = await this.db();
    await this.lease?.validate();
    const { store, done, guard } = scopedTransaction(db, STORE_NAME, 'readwrite', this.lease);
    let result!: OutboxEntry;
    const request = store.get(clientTxnId) as IDBRequest<OutboxEntry | undefined>;
    request.onsuccess = () => guard(() => {
      const existing = request.result;
      if (!existing?.submission) throw new Error('Submission reservation unavailable');
      result = existing;
      if (existing.status === 'preparing' && !existing.admitted) {
        result = { ...existing, attachments, status: 'pending', updated_at: new Date().toISOString() };
        store.put(result);
      }
    });
    await done;
    return result;
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
    this.lease?.assertCurrent();
    this.dbPromise ??= openDatabase(this.indexedDB, this.name).catch((error) => {
      this.dbPromise = null;
      throw error;
    });
    return this.dbPromise;
  }

  private async putDb(entry: OutboxEntry): Promise<void> {
    const db = await this.db();
    await this.lease?.validate();
    const { store, done, guard } = scopedTransaction(db, STORE_NAME, 'readwrite', this.lease);
    const request = store.get(entry.client_txn_id);
    request.onsuccess = () => guard(() => { if (!request.result) store.put(entry); });
    await done;
  }

  private async getDb(clientTxnId: string): Promise<OutboxEntry | null> {
    const db = await this.db();
    await this.lease?.validate();
    const { store, done } = scopedTransaction(db, STORE_NAME, 'readonly', this.lease);
    const value = await requestToPromise<OutboxEntry | undefined>(store.get(clientTxnId));
    await done;
    return value ?? null;
  }

  private async listDb(conversationId?: string): Promise<OutboxEntry[]> {
    const db = await this.db();
    await this.lease?.validate();
    const { store, done } = scopedTransaction(db, STORE_NAME, 'readonly', this.lease);
    const source: IDBObjectStore | IDBIndex = conversationId
      ? store.index(CONVERSATION_INDEX)
      : store;
    const request = conversationId ? source.getAll(conversationId) : source.getAll();
    const result = await requestToPromise<OutboxEntry[]>(request);
    await done;
    return result;
  }

  private async deleteDb(clientTxnId: string): Promise<void> {
    const db = await this.db();
    await this.lease?.validate();
    const { store, done } = scopedTransaction(db, STORE_NAME, 'readwrite', this.lease);
    store.delete(clientTxnId);
    await done;
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
      await this.lease?.validate();
      const result = await operation();
      await this.lease?.validate();
      return result;
    } catch (error) {
      await this.lease?.validate();
      // Production send receipts must remain visible to sibling documents.
      // Per-document memory fallback cannot safely arbitrate admission.
      if (this.lease) throw error;
      console.warn('Chat v2 IndexedDB outbox unavailable; using in-memory outbox', error);
      return fallbackOperation(this.fallback());
    }
  }
}

export class MemoryChatV2Outbox implements ChatV2Outbox {
  async reserve(_entry: OutboxEntry, _binding: SubmissionBinding): Promise<OutboxEntry> {
    throw new CoordinationUnavailableError();
  }
  async activate(_id: string, _attachments: AttachmentRef[]): Promise<OutboxEntry> {
    throw new CoordinationUnavailableError();
  }
  private readonly entries = new Map<string, OutboxEntry>();

  isDurable(): boolean {
    return false;
  }

  async put(entry: OutboxEntry): Promise<void> {
    if (this.entries.has(entry.client_txn_id)) return;
    this.entries.set(entry.client_txn_id, {
      ...entry, attachments: [...(entry.attachments ?? [])]
    });
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
    if (!existing || existing.status === 'acked' || existing.admitted) return;
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

export class CoordinatedOutbox implements ChatV2Outbox {
  readonly lease: DocumentLease;
  constructor(private readonly inner: ChatV2Outbox, readonly coordination: DocumentCoordination) {
    this.lease = coordination.capture();
  }
  private async access<T>(action: () => Promise<T>): Promise<T> {
    await this.lease.validate();
    const value = await action();
    await this.lease.validate();
    return value;
  }
  isDurable() { return this.inner.isDurable(); }
  reserve(entry: OutboxEntry, binding: SubmissionBinding) {
    if (entry.owner && entry.owner !== this.lease.owner) return Promise.reject(new StaleDocumentError());
    return this.access(() => this.inner.reserve({ ...entry, owner: this.lease.owner! }, binding));
  }
  activate(id: string, attachments: AttachmentRef[]) {
    return this.access(() => this.inner.activate(id, attachments));
  }
  put(entry: OutboxEntry) {
    if (entry.owner && entry.owner !== this.lease.owner) return Promise.reject(new StaleDocumentError());
    return this.access(() => this.inner.put({ ...entry, owner: this.lease.owner! }));
  }
  get(id: string) { return this.access(() => this.inner.get(id)); }
  list(id?: string) { return this.access(() => this.inner.list(id)); }
  update(id: string, patch: Partial<Omit<OutboxEntry, 'client_txn_id' | 'created_at'>>) {
    return this.access(() => this.inner.update(id, { ...patch, owner: this.lease.owner! }));
  }
  // Per-view cleanup cannot prove that no sibling retains this transaction.
  delete(_id: string) { this.lease.assertCurrent(); return Promise.resolve(); }
  // Receipts must remain while another document may be waiting for admission.
  clearAcked() { this.lease.assertCurrent(); return Promise.resolve(); }
}

export function createMemoryChatV2Outbox(): ChatV2Outbox {
  throw new CoordinationUnavailableError();
}

export function createIndexedDbChatV2Outbox(coordination = documentCoordination()): ChatV2Outbox {
  if (typeof indexedDB === 'undefined') {
    throw new ChatV2OutboxUnavailableError();
  }
  const lease = coordination.capture();
  return new CoordinatedOutbox(
    new IndexedDbChatV2Outbox(indexedDB, coordination.storageName(DB_NAME, lease), lease), coordination);
}

function openDatabase(indexedDBFactory: IDBFactory, name: string): Promise<IDBDatabase> {
  return withIdbTimeout(new Promise((resolve, reject) => {
    const request = indexedDBFactory.open(name, DB_VERSION);
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
