import { render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';
import type { Deliverable } from '$lib/types/api';
import AssistantDeliverableBlock from './AssistantDeliverableBlock.svelte';

const mocks = vi.hoisted(() => ({
  getDeliverable: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
  api: {
    deliverables: {
      get: mocks.getDeliverable
    }
  }
}));

function item(overrides: Partial<AssistantDeliverableTimelineItem> = {}): AssistantDeliverableTimelineItem {
  return {
    id: 'assistant-deliverable:dlv-1',
    kind: 'assistant_deliverable',
    sort_key: '00000000000000000001:assistant-deliverable:dlv-1',
    source_refs: [],
    stable: true,
    status: 'complete',
    deliverable_id: 'dlv-1',
    format: 'markdown',
    title: 'Fallback title',
    ...overrides
  };
}

function deliverable(overrides: Partial<Deliverable> = {}): Deliverable {
  return {
    deliverable_id: 'dlv-1',
    step_run_id: null,
    version: 1,
    attempt_number: 1,
    content: '# Final report\n\n- **Validated** result\n\n<script>alert("x")</script>',
    format: 'markdown',
    title: 'Markdown report',
    target: null,
    outputs: {},
    rich_payload: null,
    validation_warnings: [],
    render_metadata: {},
    export_metadata: {},
    status: 'buffered',
    evaluator_feedback: null,
    created_at: null,
    updated_at: null,
    ...overrides
  };
}

describe('AssistantDeliverableBlock', () => {
  beforeEach(() => {
    mocks.getDeliverable.mockReset();
  });

  it('renders markdown deliverables through the unified RichDeliverable renderer, not raw preformatted text', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable());

    const { container } = render(AssistantDeliverableBlock, { item: item() });

    await screen.findByTestId('rich-deliverable');

    expect(screen.getByRole('heading', { name: 'Final report' })).toBeTruthy();
    expect(screen.getByText('Validated')).toBeTruthy();
    expect(container.querySelector('script')).toBeNull();
    expect(screen.queryByText('# Final report', { exact: false })).toBeNull();
    // No more bespoke card/testids -- markdown renders as a single
    // `markdown` block inside RichDeliverable like every other format.
    expect(container.querySelector('.assistant-deliverable-card')).toBeNull();
    expect(container.querySelector('.rich-markdown')).toBeTruthy();
  });

  it('renders markdown deliverables as a single document with a generated TOC using the shared RichToc component', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable({
      content: [
        '# Final report',
        '',
        'Introductory text.',
        '',
        '## Stage scope',
        '',
        'Scope details.',
        '',
        '## Evidence base',
        '',
        'Evidence details.',
        '',
        '## Validation',
        '',
        'Validation details.',
      ].join('\n'),
    }));

    const { container } = render(AssistantDeliverableBlock, { item: item() });

    const root = await screen.findByTestId('rich-deliverable');
    const namespace = root.getAttribute('data-rich-instance');
    expect(namespace).toBeTruthy();

    expect(container.querySelector('.assistant-deliverable-card')).toBeNull();
    expect(screen.getByTestId('rich-deliverable-toc')).toBeTruthy();
    const heading = container.querySelector(`#${namespace}-stage-scope`);
    expect(heading).toBeTruthy();
    expect(heading?.textContent).toBe('Stage scope');
    expect(screen.getAllByText('Evidence base').length).toBeGreaterThan(1);
  });

  it('does not expose raw/debug controls in the user-facing deliverable', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable());

    render(AssistantDeliverableBlock, { item: item() });

    await waitFor(() => expect(screen.getByTestId('rich-deliverable')).toBeTruthy());

    expect(screen.queryByRole('button', { name: 'Raw/debug' })).toBeNull();
    expect(screen.queryByTestId('rich-deliverable-raw')).toBeNull();
  });

  it('renders plain-format deliverables literally, never as interpreted markdown', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable({
      format: 'plain',
      content: '# Not a heading\n[not a link](http://example.com)',
    }));

    render(AssistantDeliverableBlock, { item: item({ format: 'plain' }) });

    await screen.findByTestId('rich-deliverable');

    expect(screen.queryByRole('heading', { name: 'Not a heading' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'not a link' })).toBeNull();
    expect(screen.getByText('# Not a heading', { exact: false })).toBeTruthy();
  });

  it('renders html-format deliverables as sanitized HTML, not escaped/markdown-parsed text', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable({
      format: 'html',
      content: '<h2>Already HTML</h2><p>Body <strong>text</strong>.</p><script>alert(1)</script>',
    }));
    const { container } = render(AssistantDeliverableBlock, { item: item({ format: 'html' }) });

    await screen.findByTestId('rich-deliverable');

    expect(screen.getByRole('heading', { name: 'Already HTML' })).toBeTruthy();
    expect(screen.getByText('text')).toBeTruthy();
    expect(container.querySelector('script')).toBeNull();
  });

  it('renders rich deliverables without a nested outer card frame', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable({
      format: 'rich',
      title: 'Rich report',
      content: 'Fallback content',
      rich_payload: {
        metadata: { eyebrow: 'Deliverable' },
        blocks: [{ type: 'markdown', content: '# Rich body\n\nReadable content.' }],
      },
    }));

    const { container } = render(AssistantDeliverableBlock, { item: item({ format: 'rich' }) });

    await screen.findByTestId('rich-deliverable');

    expect(container.querySelector('.assistant-deliverable-card')).toBeNull();
    expect(container.querySelector('.rich-deliverable.embedded')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Rich body' })).toBeTruthy();
  });
});
