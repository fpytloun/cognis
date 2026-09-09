import { chatV2Api } from '$lib/chat-v2/api';
import type { ChatMode, CommandV2Response, SendMessageV2Response } from '$lib/chat-v2/types';
import type { ChatV2Outbox } from '$lib/chat-v2/outbox';
import {
  isSystemSlashCommand,
  normalizeSlashCommandInput,
  parseChatModeDirectiveInput,
} from '$lib/slash-commands';
import type { AttachmentRef } from '$lib/types/api';

export interface ComposerDispatchInput {
  conversationId: string;
  content: string;
  attachments?: AttachmentRef[];
  chatMode?: ChatMode;
  clientTxnId?: string;
  clientMessageId?: string;
}

export type NormalizedComposerInput =
  | { kind: 'command'; content: string; chatMode: undefined }
  | { kind: 'message'; content: string; chatMode: ChatMode | undefined };

export type ComposerDispatchResult =
  | { kind: 'command'; response: CommandV2Response; clientTxnId: string }
  | { kind: 'message'; response: SendMessageV2Response; clientTxnId: string; clientMessageId: string };

export interface ComposerDispatchDependencies {
  executeCommand?: typeof chatV2Api.executeCommand;
  sendMessage?: typeof chatV2Api.sendMessage;
  outbox?: ChatV2Outbox | null;
  now?: () => string;
  uuid?: () => string;
}

export function normalizeChatComposerInput(
  input: Pick<ComposerDispatchInput, 'content' | 'chatMode'>
): NormalizedComposerInput {
  const content = input.content.trim();
  const directive = parseChatModeDirectiveInput(content);
  if (directive?.oneShot && directive.content) {
    return { kind: 'message', content: directive.content, chatMode: directive.mode };
  }
  if (isSystemSlashCommand(content)) {
    return {
      kind: 'command',
      content: normalizeSlashCommandInput(content),
      chatMode: undefined,
    };
  }
  return { kind: 'message', content, chatMode: input.chatMode };
}

export async function executeComposerCommandWithRetry(
  conversationId: string,
  clientTxnId: string,
  content: string,
  executeCommand: typeof chatV2Api.executeCommand = (...args) => chatV2Api.executeCommand(...args)
): Promise<CommandV2Response> {
  try {
    return await executeCommand(conversationId, clientTxnId, content);
  } catch {
    return executeCommand(conversationId, clientTxnId, content);
  }
}

export async function dispatchChatComposerMessage(
  input: ComposerDispatchInput,
  dependencies: ComposerDispatchDependencies = {}
): Promise<ComposerDispatchResult> {
  const uuid = dependencies.uuid ?? (() => crypto.randomUUID());
  const now = dependencies.now ?? (() => new Date().toISOString());
  const clientTxnId = input.clientTxnId ?? uuid();
  const normalized = normalizeChatComposerInput(input);
  if (normalized.kind === 'command') {
    const response = await executeComposerCommandWithRetry(
      input.conversationId,
      clientTxnId,
      normalized.content,
      dependencies.executeCommand
    );
    return { kind: 'command', response, clientTxnId };
  }

  const clientMessageId = input.clientMessageId ?? uuid();
  const attachments = input.attachments ?? [];
  const content = normalized.content;
  const chatMode = normalized.chatMode;
  const timestamp = now();
  let stored = false;
  if (dependencies.outbox) {
    try {
      const existing = await dependencies.outbox.get(clientTxnId);
      if (!existing) {
        await dependencies.outbox.put({
          client_txn_id: clientTxnId,
          client_message_id: clientMessageId,
          conversation_id: input.conversationId,
          content,
          attachments,
          chat_mode: chatMode,
          status: 'pending',
          created_at: timestamp,
          updated_at: timestamp
        });
      }
      stored = true;
      await dependencies.outbox.update(clientTxnId, { status: 'sending', updated_at: now() });
    } catch {
      stored = false;
    }
  }

  try {
    const sendMessage = dependencies.sendMessage ?? ((...args) => chatV2Api.sendMessage(...args));
    const response = await sendMessage(
      input.conversationId,
      clientTxnId,
      {
        content,
        attachments,
        client_message_id: clientMessageId,
        chat_mode: chatMode
      }
    );
    if (stored) {
      await dependencies.outbox?.update(clientTxnId, { status: 'acked', updated_at: now() }).catch(() => undefined);
      await dependencies.outbox?.delete(clientTxnId).catch(() => undefined);
    }
    return { kind: 'message', response, clientTxnId, clientMessageId };
  } catch (error) {
    if (stored) {
      await dependencies.outbox?.update(clientTxnId, {
        status: 'failed',
        updated_at: now(),
        last_error: error instanceof Error ? error.message : String(error)
      }).catch(() => undefined);
    }
    throw error;
  }
}
