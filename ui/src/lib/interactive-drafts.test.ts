import { describe, expect, it } from 'vitest';

import {
  clearQuestionDraft,
  optimisticUserMessagesStorageKey,
  questionDraftStorageKey,
  readOptimisticUserMessageDrafts,
  readQuestionDraft,
  removeOptimisticUserMessageDraft,
  saveOptimisticUserMessageDraft,
  writeQuestionDraft,
} from './interactive-drafts';

class MemoryStorage implements Storage {
  private values = new Map<string, string>();

  get length(): number {
    return this.values.size;
  }

  clear(): void {
    this.values.clear();
  }

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  key(index: number): string | null {
    return Array.from(this.values.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

class ThrowingStorage extends MemoryStorage {
  override getItem(): string | null {
    throw new Error('blocked');
  }

  override setItem(): void {
    throw new Error('blocked');
  }

  override removeItem(): void {
    throw new Error('blocked');
  }
}

describe('interactive drafts', () => {
  it('persists question answers by conversation and notification', () => {
    const storage = new MemoryStorage();

    writeQuestionDraft('conv_1', 'notif_1', {
      q1: { selected: ['yes'], custom: 'custom answer' },
    }, storage, 100);

    expect(storage.getItem(questionDraftStorageKey('conv_1', 'notif_1'))).toBeTruthy();
    expect(readQuestionDraft('conv_1', 'notif_1', storage, 100)).toEqual({
      q1: { selected: ['yes'], custom: 'custom answer' },
    });
    expect(readQuestionDraft('conv_1', 'notif_2', storage, 100)).toEqual({});

    clearQuestionDraft('conv_1', 'notif_1', storage);
    expect(readQuestionDraft('conv_1', 'notif_1', storage, 100)).toEqual({});
  });

  it('expires stale question drafts', () => {
    const storage = new MemoryStorage();

    writeQuestionDraft('conv_1', 'notif_1', {
      q1: { selected: [], custom: 'stale' },
    }, storage, 100);

    expect(readQuestionDraft('conv_1', 'notif_1', storage, 100 + 25 * 60 * 60 * 1000)).toEqual({});
    expect(storage.getItem(questionDraftStorageKey('conv_1', 'notif_1'))).toBeNull();
  });

  it('treats unavailable storage as best-effort', () => {
    const storage = new ThrowingStorage();

    expect(() => {
      writeQuestionDraft('conv_1', 'notif_1', {
        q1: { selected: [], custom: 'draft' },
      }, storage);
      clearQuestionDraft('conv_1', 'notif_1', storage);
      saveOptimisticUserMessageDraft({
        conversationId: 'conv_1',
        clientMessageId: 'cmsg_1',
        content: 'hello',
        attachments: [],
        createdAt: 100,
      }, storage, 100);
      removeOptimisticUserMessageDraft('conv_1', 'cmsg_1', storage, 100);
    }).not.toThrow();
    expect(readQuestionDraft('conv_1', 'notif_1', storage)).toEqual({});
    expect(readOptimisticUserMessageDrafts('conv_1', storage, 100)).toEqual([]);
  });

  it('persists optimistic user message drafts until canonical reconciliation removes them', () => {
    const storage = new MemoryStorage();

    saveOptimisticUserMessageDraft({
      conversationId: 'conv_1',
      clientMessageId: 'cmsg_1',
      content: 'hello',
      attachments: [],
      createdAt: 100,
    }, storage, 100);

    expect(storage.getItem(optimisticUserMessagesStorageKey('conv_1'))).toBeTruthy();
    expect(readOptimisticUserMessageDrafts('conv_1', storage, 100)).toEqual([
      {
        conversationId: 'conv_1',
        clientMessageId: 'cmsg_1',
        content: 'hello',
        attachments: [],
        createdAt: 100,
      },
    ]);

    removeOptimisticUserMessageDraft('conv_1', 'cmsg_1', storage, 100);
    expect(readOptimisticUserMessageDrafts('conv_1', storage, 100)).toEqual([]);
  });

  it('drops stale optimistic user message drafts', () => {
    const storage = new MemoryStorage();
    saveOptimisticUserMessageDraft({
      conversationId: 'conv_1',
      clientMessageId: 'cmsg_1',
      content: 'hello',
      attachments: [],
      createdAt: 100,
    }, storage, 100);

    expect(readOptimisticUserMessageDrafts('conv_1', storage, 100 + 11 * 60 * 1000)).toEqual([]);
  });
});
