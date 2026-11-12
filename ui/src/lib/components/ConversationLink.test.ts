import { cleanup, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import { afterEach, expect, it } from 'vitest';
import ConversationLink from './ConversationLink.svelte';

const children = createRawSnippet(() => ({ render: () => '<span>Open conversation</span>' }));
afterEach(() => {
  cleanup();
  delete window.__cognisNative;
  delete window.webkit;
});
it('retains a real browser link and does not consume modified navigation', () => {
  render(ConversationLink, { conversationId: 'conv_review', children });
  const link = screen.getByRole('link', { name: 'Open conversation' });
  expect(link).toHaveAttribute('href', '/chat/conv_review');
  for (const init of [{}, { ctrlKey: true }, { metaKey: true }, { shiftKey: true }]) {
    const event = new MouseEvent('click', { ...init, bubbles: true, cancelable: true });
    link.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
  }
});
it('opens a native conversation window using the admitted host generation', () => {
  const commands: unknown[] = [];
  window.__cognisNative = { version: 1, generation: '12345678-1234-1234-1234-123456789abc',
    server: window.location.origin, surface: 'resource' };
  window.webkit = { messageHandlers: { cognis: { postMessage: value => commands.push(value) } } };
  render(ConversationLink, { conversationId: 'conv_review', children });
  const link = screen.getByRole('link', { name: 'Open conversation' });
  const event = new MouseEvent('click', { bubbles: true, cancelable: true });
  link.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
  expect(commands).toEqual([{ version: 1, generation: '12345678-1234-1234-1234-123456789abc',
    type: 'open-window', payload: { conversationId: 'conv_review' } }]);
  link.dispatchEvent(new MouseEvent('click', { ctrlKey: true, bubbles: true, cancelable: true }));
  expect(commands).toHaveLength(1);
});
