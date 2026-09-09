import { describe, expect, it, vi } from 'vitest';
import { MemoryChatV2Outbox } from './outbox';
import {
  dispatchChatComposerMessage,
  normalizeChatComposerInput,
} from './composer-dispatch';

describe('dispatchChatComposerMessage', () => {
  it.each([
    ['/plan', { kind: 'command', content: '/plan', chatMode: undefined }],
    [' /build ', { kind: 'command', content: '/build', chatMode: undefined }],
    ['/default\n', { kind: 'command', content: '/default', chatMode: undefined }],
    ['/plan investigate', { kind: 'message', content: 'investigate', chatMode: 'plan' }],
    ['/build\timplement', { kind: 'message', content: 'implement', chatMode: 'build' }],
    ['/default\ncontinue normally', { kind: 'message', content: 'continue normally', chatMode: 'default' }],
    ['ordinary content', { kind: 'message', content: 'ordinary content', chatMode: undefined }],
    ['/ask unsupported', { kind: 'message', content: '/ask unsupported', chatMode: undefined }],
  ])('normalizes %j at the shared dispatch boundary', (content, expected) => {
    expect(normalizeChatComposerInput({ content })).toEqual(expected);
  });

  it('lets one-shot directives override an input mode while ordinary text preserves it', () => {
    expect(normalizeChatComposerInput({ content: '/plan inspect', chatMode: 'build' })).toEqual({
      kind: 'message',
      content: 'inspect',
      chatMode: 'plan'
    });
    expect(normalizeChatComposerInput({ content: 'inspect', chatMode: 'build' })).toEqual({
      kind: 'message',
      content: 'inspect',
      chatMode: 'build'
    });
  });

  it('executes a normalized slash command and retries with the same transaction', async () => {
    const executeCommand = vi.fn()
      .mockRejectedValueOnce(new Error('retry'))
      .mockResolvedValueOnce({ ok: true });
    const result = await dispatchChatComposerMessage(
      { conversationId: 'conv-1', content: ' /help ', clientTxnId: 'txn-1' },
      { executeCommand: executeCommand as never }
    );
    expect(result.kind).toBe('command');
    expect(executeCommand).toHaveBeenNthCalledWith(1, 'conv-1', 'txn-1', '/help');
    expect(executeCommand).toHaveBeenNthCalledWith(2, 'conv-1', 'txn-1', '/help');
  });

  it('persists ordinary sends and clears an acknowledged outbox entry', async () => {
    const outbox = new MemoryChatV2Outbox();
    const sendMessage = vi.fn().mockResolvedValue({ admission: 'queued', queue: { messages: [] } });
    const result = await dispatchChatComposerMessage(
      {
        conversationId: 'conv-1',
        content: 'hello',
        chatMode: 'plan',
        clientTxnId: 'txn-1',
        clientMessageId: 'msg-1'
      },
      { outbox, sendMessage: sendMessage as never, now: () => '2026-01-01T00:00:00Z' }
    );
    expect(result.kind).toBe('message');
    expect(sendMessage).toHaveBeenCalledWith('conv-1', 'txn-1', expect.objectContaining({
      content: 'hello',
      client_message_id: 'msg-1',
      chat_mode: 'plan'
    }));
    expect(await outbox.list()).toEqual([]);
  });

  it.each([
    ['/plan investigate', 'investigate', 'plan'],
    ['/build\nimplement', 'implement', 'build'],
    ['/default continue', 'continue', 'default'],
  ] as const)('sends %s as normalized one-shot content and mode', async (input, content, chatMode) => {
    const outbox = new MemoryChatV2Outbox();
    const sendMessage = vi.fn().mockResolvedValue({ status: 'accepted' });
    await dispatchChatComposerMessage({
      conversationId: 'conv-1',
      content: input,
      chatMode: chatMode === 'plan' ? 'build' : 'plan',
      clientTxnId: 'txn-mode',
      clientMessageId: 'msg-mode'
    }, {
      outbox,
      sendMessage: sendMessage as never,
      now: () => '2026-01-01T00:00:00Z'
    });
    expect(sendMessage).toHaveBeenCalledWith('conv-1', 'txn-mode', {
      content,
      attachments: [],
      client_message_id: 'msg-mode',
      chat_mode: chatMode
    });
    expect(await outbox.list()).toEqual([]);
  });

  it('routes bare mode directives to the command endpoint without sending a message', async () => {
    const executeCommand = vi.fn().mockResolvedValue({ status: 'completed' });
    const sendMessage = vi.fn();
    await dispatchChatComposerMessage(
      { conversationId: 'conv-1', content: ' /plan\n', clientTxnId: 'txn-plan' },
      { executeCommand: executeCommand as never, sendMessage: sendMessage as never }
    );
    expect(executeCommand).toHaveBeenCalledWith('conv-1', 'txn-plan', '/plan');
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it('keeps unsupported slash input on the established message rejection path', async () => {
    const sendMessage = vi.fn().mockRejectedValue(new Error('slash_command_not_supported'));
    await expect(dispatchChatComposerMessage(
      {
        conversationId: 'conv-1',
        content: '/ask unsupported',
        clientTxnId: 'txn-ask',
        clientMessageId: 'msg-ask'
      },
      { sendMessage: sendMessage as never }
    )).rejects.toThrow('slash_command_not_supported');
    expect(sendMessage).toHaveBeenCalledWith('conv-1', 'txn-ask', expect.objectContaining({
      content: '/ask unsupported',
      chat_mode: undefined
    }));
  });

  it('preserves an absent one-shot chat mode for server-side persistent mode', async () => {
    const sendMessage = vi.fn().mockResolvedValue({ status: 'accepted' });
    await dispatchChatComposerMessage(
      { conversationId: 'conv-1', content: 'inherit mode', clientTxnId: 'txn-2', clientMessageId: 'msg-2' },
      { sendMessage: sendMessage as never }
    );
    expect(sendMessage).toHaveBeenCalledWith(
      'conv-1',
      'txn-2',
      expect.objectContaining({ chat_mode: undefined })
    );
  });

  it('retains a failed outbox entry with the error for retry', async () => {
    const outbox = new MemoryChatV2Outbox();
    await expect(dispatchChatComposerMessage(
      { conversationId: 'conv-1', content: 'hello', clientTxnId: 'txn-1', clientMessageId: 'msg-1' },
      { outbox, sendMessage: vi.fn().mockRejectedValue(new Error('offline')) as never }
    )).rejects.toThrow('offline');
    expect(await outbox.get('txn-1')).toMatchObject({ status: 'failed', last_error: 'offline' });
  });
});
