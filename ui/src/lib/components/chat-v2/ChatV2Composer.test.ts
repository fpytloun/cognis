import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import { api } from '$lib/api/client';
import ChatV2Composer from './ChatV2Composer.svelte';

function props(overrides: Record<string, unknown> = {}) {
  return {
    value: '',
    attachments: [],
    onSend: vi.fn(),
    onStop: vi.fn(),
    onFiles: vi.fn(),
    ...overrides,
  };
}

describe('ChatV2Composer', () => {
  it('uses the same attachment and microphone controls on every surface', async () => {
    const onFiles = vi.fn();
    render(ChatV2Composer, props({ onFiles }));
    const input = screen.getByLabelText('Attach files').querySelector('input') as HTMLInputElement;
    const file = new File(['content'], 'note.md', { type: 'text/markdown' });

    await fireEvent.change(input, { target: { files: [file] } });

    expect(onFiles).toHaveBeenCalledWith([file]);
    expect(screen.getByRole('button', { name: /Record voice message/ })).toBeInTheDocument();
  });

  it('queues a draft during an active turn and stops when the draft is empty', async () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    const { rerender } = render(ChatV2Composer, props({
      value: 'Follow up',
      active: true,
      onSend,
      onStop,
    }));

    await fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    expect(onSend).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: 'Queue message' })).toBeInTheDocument();

    await rerender(props({ active: true, onSend, onStop }));
    await fireEvent.click(screen.getByRole('button', { name: 'Cancel turn' }));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it('sends with Enter by default and keeps Shift+Enter as a newline', async () => {
    const onSend = vi.fn();
    render(ChatV2Composer, props({ value: 'Draft', onSend }));
    await fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' });
    expect(onSend).toHaveBeenCalledOnce();
    await fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', shiftKey: true });
    expect(onSend).toHaveBeenCalledOnce();
  });

  it('always requests an Enter key from the software keyboard', () => {
    render(ChatV2Composer, props());

    expect(screen.getByRole('textbox')).toHaveAttribute('enterkeyhint', 'enter');
  });

  it.each(['/help', '/model', '/plan', '/fork'])(
    'does not let slash suggestions swallow the complete %s command',
    async (command) => {
      const onSend = vi.fn();
      render(ChatV2Composer, props({ value: command, onSend }));
      const composer = screen.getByRole('textbox');
      await fireEvent.input(composer);
      expect(screen.getByRole('listbox', { name: 'Slash command suggestions' })).toBeInTheDocument();

      await fireEvent.keyDown(composer, { key: 'Enter' });
      expect(onSend).toHaveBeenCalledOnce();
    },
  );

  it('does not let slash suggestions swallow a modified-Enter send', async () => {
    const onSend = vi.fn();
    render(ChatV2Composer, props({ value: '/model', onSend }));
    const composer = screen.getByRole('textbox');
    await fireEvent.input(composer);
    expect(screen.getByRole('listbox', { name: 'Slash command suggestions' })).toBeInTheDocument();

    await fireEvent.keyDown(composer, { key: 'Enter', ctrlKey: true });
    expect(onSend).toHaveBeenCalledOnce();
  });

  it('closes slash suggestions when the parent clears after Enter dispatch', async () => {
    const onSend = vi.fn();
    const { rerender } = render(ChatV2Composer, props({ value: '/help', onSend }));
    const composer = screen.getByRole('textbox');
    await waitFor(() => {
      expect(screen.getByRole('listbox', { name: 'Slash command suggestions' })).toBeInTheDocument();
    });

    await fireEvent.keyDown(composer, { key: 'Enter' });
    expect(onSend).toHaveBeenCalledOnce();
    await rerender(props({ value: '', onSend }));

    expect(screen.queryByRole('listbox', { name: 'Slash command suggestions' })).toBeNull();
    expect(composer).not.toHaveAttribute('aria-activedescendant');
  });

  it('closes slash suggestions when the parent clears after pointer send', async () => {
    const onSend = vi.fn();
    const { rerender } = render(ChatV2Composer, props({ value: '/help', onSend }));
    await waitFor(() => {
      expect(screen.getByRole('listbox', { name: 'Slash command suggestions' })).toBeInTheDocument();
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(onSend).toHaveBeenCalledOnce();
    await rerender(props({ value: '', onSend }));

    expect(screen.queryByRole('listbox', { name: 'Slash command suggestions' })).toBeNull();
  });

  it('sends a touch pointer in one gesture without blurring the textarea or duplicating the click', async () => {
    const onSend = vi.fn();
    render(ChatV2Composer, props({ value: 'Touch send', onSend }));
    const composer = screen.getByRole('textbox');
    const send = screen.getByRole('button', { name: 'Send' });
    composer.focus();

    const pointerDown = await fireEvent.pointerDown(send, {
      pointerId: 7,
      pointerType: 'touch',
      isPrimary: true,
    });
    expect(pointerDown).toBe(false);
    expect(composer).toHaveFocus();
    expect(onSend).not.toHaveBeenCalled();

    await fireEvent.pointerUp(send, {
      pointerId: 7,
      pointerType: 'touch',
      isPrimary: true,
    });
    expect(onSend).toHaveBeenCalledOnce();
    expect(composer).toHaveFocus();

    await fireEvent.click(send, { detail: 1 });
    expect(onSend).toHaveBeenCalledOnce();

    const genuineMouseClick = new MouseEvent('click', {
      bubbles: true,
      cancelable: true,
      detail: 1,
    });
    Object.defineProperty(genuineMouseClick, 'pointerType', { value: 'mouse' });
    await fireEvent(send, genuineMouseClick);
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it('does not send a cancelled touch pointer gesture', async () => {
    const onSend = vi.fn();
    render(ChatV2Composer, props({ value: 'Do not send', onSend }));
    const send = screen.getByRole('button', { name: 'Send' });

    fireEvent.pointerDown(send, { pointerId: 8, pointerType: 'touch', isPrimary: true });
    await fireEvent.pointerCancel(send, { pointerId: 8, pointerType: 'touch', isPrimary: true });
    await fireEvent.pointerUp(send, { pointerId: 8, pointerType: 'touch', isPrimary: true });

    expect(onSend).not.toHaveBeenCalled();
  });

  it('aborts a dynamic request and ignores its late response after parent clear', async () => {
    let resolveRequest!: (value: {
      items: Array<{
        kind: 'parameter';
        command: string;
        value: string;
        label: string;
        description: string;
        badges: string[];
        insert_text: string;
        suffix: 'none' | 'space';
      }>;
    }) => void;
    let requestSignal: AbortSignal | undefined;
    const request = new Promise<{
      items: Array<{
        kind: 'parameter';
        command: string;
        value: string;
        label: string;
        description: string;
        badges: string[];
        insert_text: string;
        suffix: 'none' | 'space';
      }>;
    }>((resolve) => {
      resolveRequest = resolve;
    });
    const suggestionSpy = vi.spyOn(api.conversations, 'slashCommandSuggestions')
      .mockImplementation((_conversationId, _input, _limit, options) => {
        requestSignal = options?.signal;
        return request;
      });
    const { rerender } = render(ChatV2Composer, props({
      value: '/profile ',
      conversationId: 'conversation-1',
    }));
    await waitFor(() => expect(suggestionSpy).toHaveBeenCalledOnce());

    await rerender(props({ value: '', conversationId: 'conversation-1' }));
    expect(requestSignal?.aborted).toBe(true);
    resolveRequest({
      items: [{
        kind: 'parameter',
        command: '/profile',
        value: 'architect',
        label: 'Architect',
        description: 'Architecture profile',
        badges: [],
        insert_text: '/profile architect',
        suffix: 'none',
      }],
    });
    await Promise.resolve();

    expect(screen.queryByRole('listbox', { name: 'Slash command suggestions' })).toBeNull();
    suggestionSpy.mockRestore();
  });

  it.each(['pointer', 'modified Enter'] as const)(
    'aborts a pending dynamic request before %s send',
    async (sendMethod) => {
      let resolveRequest!: (value: { items: [] }) => void;
      let requestSignal: AbortSignal | undefined;
      const request = new Promise<{ items: [] }>((resolve) => {
        resolveRequest = resolve;
      });
      const suggestionSpy = vi.spyOn(api.conversations, 'slashCommandSuggestions')
        .mockImplementation((_conversationId, _input, _limit, options) => {
          requestSignal = options?.signal;
          return request;
        });
      const onSend = vi.fn();
      render(ChatV2Composer, props({
        value: '/profile ar',
        conversationId: 'conversation-1',
        onSend,
      }));
      await waitFor(() => expect(suggestionSpy).toHaveBeenCalledOnce());

      if (sendMethod === 'pointer') {
        await fireEvent.click(screen.getByRole('button', { name: 'Send' }));
      } else {
        await fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', ctrlKey: true });
      }

      expect(onSend).toHaveBeenCalledOnce();
      expect(requestSignal?.aborted).toBe(true);
      resolveRequest({ items: [] });
      await Promise.resolve();
      expect(screen.queryByRole('listbox', { name: 'Slash command suggestions' })).toBeNull();
      suggestionSpy.mockRestore();
    },
  );

  it('renders the active orbit and the stopping pulse without changing stop behavior', async () => {
    const onStop = vi.fn();
    const { container, rerender } = render(ChatV2Composer, props({ active: true, onStop }));
    expect(container.querySelector('.conversation-turn-orbit')).toBeInTheDocument();
    expect(container.querySelector('.conversation-turn-stop-pulse')).toBeNull();

    await rerender(props({ active: true, stopping: true, onStop }));
    const button = screen.getByRole('button', { name: 'Cancelling turn' });
    expect(button).toBeDisabled();
    expect(container.querySelector('.conversation-turn-orbit--stopping')).toBeInTheDocument();
    expect(container.querySelector('.conversation-turn-stop-pulse')).toBeInTheDocument();
  });
});
