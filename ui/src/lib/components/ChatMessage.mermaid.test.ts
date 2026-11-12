import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ChatMessage from './ChatMessage.svelte';
import { renderMarkdown } from '$lib/markdown';
import type { MessageTimelineItem } from '$lib/timeline-render-model';

vi.mock('mermaid', () => ({
  default: {
    initialize() {},
    async render(_id: string, source: string) {
      if (source.includes('broken')) throw new Error('Parse error');
      return { svg: '<svg viewBox="0 0 240 80"><text>Input to output</text></svg>' };
    },
  },
}));
afterEach(() => cleanup());

const source = 'flowchart LR\n Input --&gt; Output';
const content = `\`\`\`mermaid\n${source}\n\`\`\``;
const message = (role: 'assistant' | 'user', streaming = false): MessageTimelineItem => ({
  id: `diagram-${role}`, kind: 'message', sessionId: 'sess_diagram',
  role, content, html: renderMarkdown(content), seq: 1, timestamp: null, streaming,
});

describe('chat Mermaid viewer', () => {
  it.each(['user', 'assistant'] as const)('renders %s diagrams with source copy and fullscreen controls', async role => {
    const clipboard = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: clipboard } });
    const view = render(ChatMessage, { item: message(role) });
    await waitFor(() => expect(view.container.querySelector('svg text')).toHaveTextContent('Input to output'));
    await fireEvent.click(screen.getByRole('button', { name: 'Copy diagram source' }));
    expect(await screen.findByRole('button', { name: 'Diagram copied' })).toBeInTheDocument();
    expect(clipboard.mock.calls[0][0]).toBe(source);
    await fireEvent.click(screen.getByRole('button', { name: 'Open diagram fullscreen' }));
    expect(await screen.findByRole('dialog', { name: 'Mermaid diagram fullscreen' })).toHaveTextContent('Input to output');
    await view.rerender({ item: { ...message(role), content: 'Changed', html: '<p>Changed</p>' } });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('Changed')).toBeInTheDocument();
  });

  it('waits for a streaming assistant message to finish, then replaces its source with a viewer', async () => {
    const view = render(ChatMessage, { item: message('assistant', true) });
    expect(view.container.querySelector('code.language-mermaid')).toHaveTextContent('Input --&gt; Output');
    expect(screen.queryByRole('button', { name: 'Open diagram fullscreen' })).not.toBeInTheDocument();
    await view.rerender({ item: message('assistant') });
    expect(await screen.findByRole('button', { name: 'Open diagram fullscreen' })).toBeInTheDocument();
    await waitFor(() => expect(view.container.querySelector('svg text')).toHaveTextContent('Input to output'));
  });

  it('leaves malformed Mermaid visible as source instead of losing the message', async () => {
    render(ChatMessage, { item: { ...message('user'), html: renderMarkdown('```mermaid\nbroken\n```') } });
    expect(await screen.findByText('Could not render diagram. Mermaid source:')).toBeInTheDocument();
    expect(screen.getByText('broken')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Zoom diagram in' })).toBeDisabled();
  });
});
