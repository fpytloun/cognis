import type { CompositeInput, DocumentDrafts } from '$lib/offline/document-drafts';
import type { AttachmentRef } from '$lib/types/api';
import { snapshotPersistedMetadata } from './persisted-metadata';
import type { DocumentLease } from '$lib/document-coordination';

export interface RecoveryScope {
  conversationId: string;
  generation: number;
  lease: DocumentLease;
  candidates: LegacyComposerInput[];
}
/** A captured selection is valid only in the view and owner that discovered it. */
export function canSelectRecovery(
  captured: RecoveryScope | null, current: RecoveryScope | null,
  conversationId: string, generation: number, alive: boolean, candidate: LegacyComposerInput,
): boolean {
  if (!alive || !captured || captured !== current
    || captured.conversationId !== conversationId || captured.generation !== generation
    || !captured.candidates.includes(candidate)) return false;
  try { captured.lease.assertCurrent(); return true; } catch { return false; }
}

export interface LegacyComposerInput {
  source: 'full' | 'compact';
  input: Omit<CompositeInput, 'input_revision'>;
}
export interface ComposerHydration {
  input: CompositeInput;
  legacy: LegacyComposerInput[];
}
export function emptyComposerInput(): CompositeInput {
  return { input_revision: crypto.randomUUID(), text: '', attachments: [], chat_mode: null };
}
export function selectLegacyInput(
  candidate: LegacyComposerInput, chosenMode: CompositeInput['chat_mode'] = null,
): CompositeInput {
  return snapshotPersistedMetadata({ ...candidate.input, chat_mode: chosenMode, input_revision: crypto.randomUUID() });
}
/** Read-only discovery. No historical mode is inferred for legacy input. */
export function hydrateComposer(drafts: DocumentDrafts, conversationId: string): ComposerHydration {
  const canonical = drafts.readComposite(`cognis-chat-draft:${conversationId}`);
  if (canonical) return {
    input: canonical.consumed ? emptyComposerInput() : snapshotPersistedMetadata({
      input_revision: canonical.input_revision, text: canonical.text,
      attachments: canonical.attachments, chat_mode: canonical.chat_mode,
      ...(canonical.feedback ? { feedback: canonical.feedback } : {}),
    }), legacy: [],
  };
  const full = drafts.read(`cognis-chat-draft:${conversationId}`) ?? '';
  const compact = drafts.read(`cognis:chat-v2-draft:${conversationId}`) ?? '';
  const raw = drafts.read(`cognis:chat-v2-draft-attachments:${conversationId}`);
  const attachments: unknown = raw ? JSON.parse(raw) : [];
  if (!Array.isArray(attachments) || attachments.some((ref) =>
    !ref || typeof ref.artifact_id !== 'string')) throw new Error('Legacy attachment recovery is malformed; input preserved.');
  const legacy: LegacyComposerInput[] = [];
  if (full) legacy.push({ source: 'full', input: { text: full, attachments: [], chat_mode: null } });
  if (compact || attachments.length) legacy.push({ source: 'compact', input: {
    text: compact, attachments: attachments as AttachmentRef[], chat_mode: null,
  } });
  return { input: legacy.length === 1
    ? { ...legacy[0].input, input_revision: crypto.randomUUID() } : emptyComposerInput(), legacy };
}
