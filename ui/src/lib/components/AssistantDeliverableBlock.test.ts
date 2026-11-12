import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';
import type { Deliverable } from '$lib/types/api';
import AssistantDeliverableBlock from './AssistantDeliverableBlock.svelte';
import { readable } from 'svelte/store';
import { DELIVERABLE_SHARING_CONTEXT } from '$lib/deliverable-permissions';

const mocks = vi.hoisted(() => ({
  getDeliverable: vi.fn(),
  revisions: vi.fn(),
}));

vi.mock('$lib/api/client', () => ({
  api: {
    deliverables: {
      get: mocks.getDeliverable,
      revisions: mocks.revisions,
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

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => { resolve = nextResolve; });
  return { promise, resolve };
}

describe('AssistantDeliverableBlock', () => {
  it('renders inline content in the initial render without a detail request', () => {
    const loader = vi.fn();
    render(AssistantDeliverableBlock, {
      item: item({ deliverable: deliverable() }),
      loadDeliverable: loader,
      conversational: true,
    });
    expect(screen.queryByText('Loading deliverable…')).toBeNull();
    expect(screen.getByRole('heading', { name: 'Final report' })).toBeTruthy();
    expect(loader).not.toHaveBeenCalled();
    expect(mocks.getDeliverable).not.toHaveBeenCalled();
  });

  it('renders the actual rich body synchronously, not an empty renderer boundary', () => {
    const loader = vi.fn();
    render(AssistantDeliverableBlock, {
      item: item({ format: 'rich', deliverable: deliverable({
        format: 'rich',
        rich_payload: { version: 1, blocks: [{ type: 'markdown', content: '# Immediate rich body' }] },
      }) }),
      loadDeliverable: loader,
      conversational: true,
    });
    expect(screen.getByRole('heading', { name: 'Immediate rich body' })).toBeTruthy();
    expect(loader).not.toHaveBeenCalled();
  });

  it('ignores mismatched inline data and loads the requested revision', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable());
    render(AssistantDeliverableBlock, {
      item: item({ deliverable: deliverable({ deliverable_id: 'dlv-other' }) }),
    });
    await screen.findByRole('heading', { name: 'Final report' });
    expect(mocks.getDeliverable).toHaveBeenCalledWith('dlv-1', { accessorConversationId: '' });
  });

  it('preserves the selected historical revision when inline data refreshes', async () => {
    const current = deliverable({ lineage_id: 'lineage-1', revision: 2, latest_revision: 2,
      revision_count: 2, latest_deliverable_id: 'dlv-1', is_latest: true });
    const previous = deliverable({ deliverable_id: 'dlv-old', content: '# Historical body',
      lineage_id: 'lineage-1', revision: 1, latest_revision: 2, revision_count: 2,
      latest_deliverable_id: 'dlv-1', is_latest: false });
    mocks.revisions.mockResolvedValue({ revisions: [{
      deliverable_id: 'dlv-old', revision: 1, title: 'Previous', format: 'markdown',
      status: 'approved', step_run_id: null, task_id: null, conversation_id: null,
      created_at: null, is_latest: false,
    }] });
    const loadRevision = vi.fn().mockResolvedValue(previous);
    const { rerender } = render(AssistantDeliverableBlock, {
      item: item({ deliverable: current }), loadRevision, conversational: true,
    });
    await screen.findByRole('heading', { name: 'Final report' });
    await fireEvent.click(await screen.findByTestId('deliverable-revision-toggle'));
    await fireEvent.click(await screen.findByRole('option'));
    await screen.findByRole('heading', { name: 'Historical body' });
    await rerender({
      item: item({ deliverable: { ...current, title: 'Refreshed' } }),
      loadRevision, conversational: true,
    });
    expect(screen.getByRole('heading', { name: 'Historical body' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Final report' })).toBeNull();
  });

  it('renders conversational content once with document actions and revision history after the body', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable({ revision: 2, latest_revision: 2 }));
    const { container } = render(AssistantDeliverableBlock, { item: item(), conversational: true });
    const root = await screen.findByTestId('rich-deliverable');
    expect(root).toHaveClass('conversational');
    expect(screen.getAllByRole('heading', { name: 'Final report' })).toHaveLength(1);
    expect(screen.queryByTestId('rich-deliverable-toolbar')).toBeNull();
    const footer = screen.getByTestId('chat-document-footer');
    expect(within(footer).getByRole('button', { name: 'Open fullscreen' })).toHaveTextContent('');
    expect(await within(footer).findByTestId('deliverable-revision-toggle')).toBeTruthy();
    expect(within(footer).getByTitle('More document actions').closest('details')).not.toHaveAttribute('open');
    await fireEvent.click(within(footer).getByTitle('More document actions'));
    expect(within(footer).getByRole('link', { name: 'Open in Library ↗' })).toBeTruthy();
    await fireEvent.keyDown(window, { key: 'Escape' });
    expect(within(footer).getByTitle('More document actions')).toHaveFocus();
    const body = screen.getByTestId('rich-deliverable-inline-document');
    expect(body.compareDocumentPosition(footer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await fireEvent.click(within(footer).getByTitle('Download document'));
    expect(within(footer).getByRole('link', { name: 'PDF' })).toBeTruthy();
    await fireEvent.click(within(footer).getByRole('button', { name: 'Open fullscreen' }));
    const full = screen.getByTestId('rich-deliverable-full-view');
    expect(full).not.toHaveClass('conversational');
    expect(within(full).getByTestId('rich-viewer-chrome')).toBeTruthy();
    await fireEvent.click(within(full).getByRole('button', { name: 'Close' }));
    expect(container.querySelector('script')).toBeNull();
  });

  it('keeps a collapsed conversational document identifiable and expandable', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable());
    render(AssistantDeliverableBlock, { item: item(), conversational: true, collapsedByDefault: true, allowSharing: false });
    await screen.findByTestId('rich-deliverable');
    expect(screen.queryByTestId('rich-deliverable-inline-document')).toBeNull();
    expect(screen.getByText('Markdown report')).toBeTruthy();
    await vi.dynamicImportSettled();
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    await vi.dynamicImportSettled();
    const sharing = within(await screen.findByRole('dialog', { name: 'Share document' }));
    expect(sharing.getByLabelText('Link access and expiration')).toHaveValue('private');
    expect(sharing.getAllByRole('option')).toHaveLength(1);
    expect(sharing.queryByRole('button', { name: 'Create public link' })).toBeNull();
    await fireEvent.click(sharing.getByRole('button', { name: 'Done' }));
    // A collapsed card is read through fullscreen; there is no second in-place expand.
    await fireEvent.click(screen.getByTitle('More document actions'));
    expect(screen.queryByRole('button', { name: 'Expand document' })).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Open fullscreen' }));
    expect(within(screen.getByTestId('rich-deliverable-full-view')).getByRole('heading', { name: 'Final report' })).toBeTruthy();
    expect(screen.queryByTestId('rich-deliverable-inline-document')).toBeNull();
  });

  it('does not offer public sharing when a parent workspace denies it', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable());
    render(AssistantDeliverableBlock, {
      props: { item: item() },
      context: new Map([[DELIVERABLE_SHARING_CONTEXT, readable(false)]])
    });
    await screen.findByTestId('rich-deliverable');
    await vi.dynamicImportSettled();
    await fireEvent.click(screen.getByRole('button', { name: 'Share link' }));
    await vi.dynamicImportSettled();
    const sharing = within(await screen.findByRole('dialog', { name: 'Share document' }));
    expect(sharing.getByLabelText('Link access and expiration')).toHaveValue('private');
    expect(sharing.getAllByRole('option')).toHaveLength(1);
    expect(sharing.queryByRole('button', { name: 'Create public link' })).toBeNull();
    await fireEvent.click(sharing.getByRole('button', { name: 'Done' }));
    expect(screen.getByTitle('Download document')).toBeInTheDocument();
  });
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
    await fireEvent.click(screen.getByTitle('Download document'));
    expect(screen.getByRole('link', { name: 'Single HTML' }))
      .toHaveAttribute('href', '/api/v1/deliverables/dlv-1/download.html');
    expect(screen.getByRole('link', { name: 'Markdown' }))
      .toHaveAttribute('href', '/api/v1/deliverables/dlv-1/download.md');
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
    await fireEvent.click(screen.getByTitle('Download document'));
    expect(screen.queryByRole('link', { name: 'Markdown' })).toBeNull();
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

  it('collapses only the inline body while shared footer actions and full view remain available', async () => {
    mocks.getDeliverable.mockResolvedValue(deliverable());
    render(AssistantDeliverableBlock, { item: item(), collapsedByDefault: true });

    const root = await screen.findByTestId('rich-deliverable');
    expect(screen.queryByRole('button', { name: 'Expand document' })).toBeNull();
    expect(screen.queryByTestId('rich-deliverable-inline-document')).toBeNull();
    expect(screen.getByTestId('chat-document-footer')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Open fullscreen' })).toBeVisible();

    await fireEvent.click(screen.getByRole('button', { name: 'Open fullscreen' }));
    const fullView = screen.getByTestId('rich-deliverable-full-view');
    expect(within(fullView).getByText('Validated')).toBeVisible();
    await fireEvent.click(within(fullView).getByRole('button', { name: 'Close' }));
    expect(screen.queryByTestId('rich-deliverable-inline-document')).toBeNull();
    expect(root.querySelector('.assistant-deliverable-card')).toBeNull();
  });

  it('resets collapsed state and content when the deliverable item changes', async () => {
    mocks.getDeliverable
      .mockResolvedValueOnce(deliverable())
      .mockResolvedValueOnce(deliverable({
        deliverable_id: 'dlv-2',
        title: 'Second report',
        content: '# Second body\n\nReplacement content',
      }));
    const { rerender } = render(AssistantDeliverableBlock, {
      item: item(),
      collapsedByDefault: true,
    });
    await screen.findByTestId('rich-deliverable');
    expect(screen.queryByTestId('rich-deliverable-inline-document')).toBeNull();

    await rerender({
      item: item({
        id: 'assistant-deliverable:dlv-2',
        deliverable_id: 'dlv-2',
        title: 'Second report',
      }),
      collapsedByDefault: true,
    });
    await screen.findByRole('heading', { name: 'Second report' });
    expect(screen.queryByTestId('rich-deliverable-inline-document')).toBeNull();
    expect(mocks.getDeliverable).toHaveBeenLastCalledWith('dlv-2', expect.anything());
  });

  it('keeps the current deliverable visible during same-ID revalidation', async () => {
    const refresh = deferred<Deliverable>();
    const firstLoader = vi.fn(async () => deliverable());
    const secondLoader = vi.fn(() => refresh.promise);
    const { rerender } = render(AssistantDeliverableBlock, {
      item: item(),
      loadDeliverable: firstLoader,
    });
    await screen.findByRole('heading', { name: 'Final report' });

    await rerender({ item: item(), loadDeliverable: secondLoader });

    expect(screen.getByRole('heading', { name: 'Final report' })).toBeInTheDocument();
    expect(screen.queryByText('Loading deliverable…')).not.toBeInTheDocument();
    refresh.resolve(deliverable());
  });

  it('uses measured inline container width for TOC layout', async () => {
    let triggerResize = (_width: number) => {};
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) {
        triggerResize = (width) => callback(
          [{ contentRect: { width } } as ResizeObserverEntry],
          {} as ResizeObserver,
        );
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    mocks.getDeliverable.mockResolvedValue(deliverable({
      content: '# Report\n\n## One\n\nA\n\n## Two\n\nB\n\n## Three\n\nC',
    }));
    render(AssistantDeliverableBlock, { item: item() });
    const root = await screen.findByTestId('rich-deliverable');
    expect(root).toHaveAttribute('data-inline-toc-layout', 'drawer');

    triggerResize(1200);
    await waitFor(() => expect(root).toHaveAttribute('data-inline-toc-layout', 'sidebar'));
    expect(root.querySelector('.rich-inline-document')).toHaveClass('inline-toc-sidebar');
    expect(screen.queryByRole('button', { name: 'Open table of contents' })).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Close table of contents' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Open table of contents' }));
    expect(root.querySelector('.rich-inline-document')).toHaveClass('inline-toc-sidebar');

    triggerResize(760);
    await waitFor(() => expect(root).toHaveAttribute('data-inline-toc-layout', 'drawer'));
    expect(root.querySelector('.rich-inline-document')).not.toHaveClass('inline-toc-sidebar');
    vi.unstubAllGlobals();
  });
});
