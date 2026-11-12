import { render, screen } from '@testing-library/svelte';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import type { MessageTimelineItem } from '$lib/timeline-render-model';
import ChatMessage from './ChatMessage.svelte';

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
});

const message: MessageTimelineItem = {
  id: 'channel-message', kind: 'message', role: 'user', content: 'A message',
  seq: 1, timestamp: '2026-10-28T20:00:00Z', html: '<p>A message</p>',
};

describe('managed conversation message authorship', () => {
  it('labels a remote participant and renders a distinct participant bubble', () => {
    const { container } = render(ChatMessage, { item: {
      ...message, messageMetadata: { channel: 'web', sender: 'Sofia Novak', untrusted: true },
    } });
    expect(screen.getByText('Sofia Novak')).toBeInTheDocument();
    expect(container.querySelector('[data-message-origin="participant"]')).toBeInTheDocument();
    expect(screen.queryByText('Private instruction · Not shared')).not.toBeInTheDocument();
  });

  it('explicitly marks private managing instructions as not shared', () => {
    const { container } = render(ChatMessage, { item: {
      ...message, messageMetadata: { private_controller_instruction: true },
    } });
    expect(screen.getByText('Private instruction · Not shared')).toBeInTheDocument();
    expect(container.querySelector('[data-message-origin="private-instruction"]')).toBeInTheDocument();
    expect(screen.queryByText('Remote participant')).not.toBeInTheDocument();
  });

  it('keeps assistant messages on the normal assistant presentation', () => {
    const { container } = render(ChatMessage, { item: { ...message, role: 'assistant' } });
    expect(container.querySelector('[data-message-role="assistant"]')).toBeInTheDocument();
    expect(container.querySelector('[data-message-origin]')).not.toBeInTheDocument();
  });
});
