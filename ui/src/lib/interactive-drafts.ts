import type { AttachmentRef } from '$lib/types/api';

export type QuestionDraftAnswer = {
  selected: string[];
  custom: string;
};

export type QuestionDraftAnswers = Record<string, QuestionDraftAnswer>;

export type OptimisticUserMessageDraft = {
  conversationId: string;
  clientMessageId: string;
  content: string;
  attachments: AttachmentRef[];
  createdAt: number;
};

const QUESTION_DRAFT_PREFIX = 'cognis.chat.question-draft.v1';
const OPTIMISTIC_MESSAGES_PREFIX = 'cognis.chat.optimistic-user-messages.v1';
const QUESTION_DRAFT_TTL_MS = 24 * 60 * 60 * 1000;
const OPTIMISTIC_MESSAGE_TTL_MS = 10 * 60 * 1000;

function storage(): Storage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function getItem(store: Storage, key: string): string | null {
  try {
    return store.getItem(key);
  } catch {
    return null;
  }
}

function setItem(store: Storage, key: string, value: string): void {
  try {
    store.setItem(key, value);
  } catch {
    // Draft persistence must never break chat/task interaction.
  }
}

function removeItem(store: Storage, key: string): void {
  try {
    store.removeItem(key);
  } catch {
    // Draft cleanup is best-effort only.
  }
}

export function questionDraftStorageKey(conversationId: string, notificationId: string): string {
  return `${QUESTION_DRAFT_PREFIX}:${conversationId}:${notificationId}`;
}

export function readQuestionDraft(
  conversationId: string | null | undefined,
  notificationId: string | null | undefined,
  store: Storage | null = storage(),
  now = Date.now(),
): QuestionDraftAnswers {
  if (!conversationId || !notificationId || !store) return {};
  const key = questionDraftStorageKey(conversationId, notificationId);
  const raw = getItem(store, key);
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    const draft = parsed as Record<string, unknown>;
    const createdAt = typeof draft.createdAt === 'number' ? draft.createdAt : now;
    if (now - createdAt > QUESTION_DRAFT_TTL_MS) {
      removeItem(store, key);
      return {};
    }
    const rawAnswers = draft.answers && typeof draft.answers === 'object'
      ? draft.answers
      : parsed;
    const answers: QuestionDraftAnswers = {};
    for (const [questionId, value] of Object.entries(rawAnswers as Record<string, unknown>)) {
      if (!value || typeof value !== 'object') continue;
      const record = value as Record<string, unknown>;
      answers[questionId] = {
        selected: Array.isArray(record.selected)
          ? record.selected.filter((item): item is string => typeof item === 'string')
          : [],
        custom: typeof record.custom === 'string' ? record.custom : '',
      };
    }
    return answers;
  } catch {
    return {};
  }
}

export function writeQuestionDraft(
  conversationId: string | null | undefined,
  notificationId: string | null | undefined,
  answers: QuestionDraftAnswers,
  store: Storage | null = storage(),
  now = Date.now(),
): void {
  if (!conversationId || !notificationId || !store) return;
  setItem(
    store,
    questionDraftStorageKey(conversationId, notificationId),
    JSON.stringify({ createdAt: now, answers }),
  );
}

export function clearQuestionDraft(
  conversationId: string | null | undefined,
  notificationId: string | null | undefined,
  store: Storage | null = storage(),
): void {
  if (!conversationId || !notificationId || !store) return;
  removeItem(store, questionDraftStorageKey(conversationId, notificationId));
}

export function optimisticUserMessagesStorageKey(conversationId: string): string {
  return `${OPTIMISTIC_MESSAGES_PREFIX}:${conversationId}`;
}

function validOptimisticDraft(value: unknown, conversationId: string, now: number): OptimisticUserMessageDraft | null {
  if (!value || typeof value !== 'object') return null;
  const record = value as Record<string, unknown>;
  if (record.conversationId !== conversationId) return null;
  if (typeof record.clientMessageId !== 'string' || !record.clientMessageId) return null;
  if (typeof record.content !== 'string') return null;
  const createdAt = typeof record.createdAt === 'number' ? record.createdAt : 0;
  if (createdAt <= 0 || now - createdAt > OPTIMISTIC_MESSAGE_TTL_MS) return null;
  const attachments = Array.isArray(record.attachments)
    ? record.attachments.filter((item): item is AttachmentRef => (
        typeof item === 'object'
        && item !== null
        && typeof (item as Record<string, unknown>).artifact_id === 'string'
      ))
    : [];
  return {
    conversationId,
    clientMessageId: record.clientMessageId,
    content: record.content,
    attachments,
    createdAt,
  };
}

export function readOptimisticUserMessageDrafts(
  conversationId: string | null | undefined,
  store: Storage | null = storage(),
  now = Date.now(),
): OptimisticUserMessageDraft[] {
  if (!conversationId || !store) return [];
  const raw = getItem(store, optimisticUserMessagesStorageKey(conversationId));
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .map((item) => validOptimisticDraft(item, conversationId, now))
      .filter((item): item is OptimisticUserMessageDraft => item !== null);
  } catch {
    return [];
  }
}

function writeOptimisticUserMessageDrafts(
  conversationId: string,
  drafts: OptimisticUserMessageDraft[],
  store: Storage | null = storage(),
): void {
  if (!store) return;
  const key = optimisticUserMessagesStorageKey(conversationId);
  if (drafts.length === 0) {
    removeItem(store, key);
    return;
  }
  setItem(store, key, JSON.stringify(drafts));
}

export function saveOptimisticUserMessageDraft(
  draft: OptimisticUserMessageDraft,
  store: Storage | null = storage(),
  now = Date.now(),
): void {
  if (!store) return;
  const existing = readOptimisticUserMessageDrafts(draft.conversationId, store, now)
    .filter((item) => item.clientMessageId !== draft.clientMessageId);
  writeOptimisticUserMessageDrafts(draft.conversationId, [...existing, draft], store);
}

export function removeOptimisticUserMessageDraft(
  conversationId: string | null | undefined,
  clientMessageId: string | null | undefined,
  store: Storage | null = storage(),
  now = Date.now(),
): void {
  if (!conversationId || !clientMessageId || !store) return;
  const remaining = readOptimisticUserMessageDrafts(conversationId, store, now)
    .filter((item) => item.clientMessageId !== clientMessageId);
  writeOptimisticUserMessageDrafts(conversationId, remaining, store);
}
