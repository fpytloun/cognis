import { fireEvent, render as renderComponent, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import RichDeliverable from './RichDeliverable.svelte';
import { requireRichScenario } from '$lib/rich-scenarios/registry';

function render(props: { payload: unknown; surface?: 'embedded' | 'standalone' } & Record<string, unknown>) {
  return renderComponent(RichDeliverable, { surface: 'standalone', ...props });
}

const capacityDashboardScenario = requireRichScenario('capacity-dashboard');

describe('dashboard presentation and canvas resolution', () => {
  it('resolves the dashboard presentation and wide canvas from metadata onto the root element', () => {
    const { container } = render({
      payload: { metadata: { presentation: 'dashboard', canvas: 'wide' }, blocks: [{ type: 'markdown', content: 'Body' }] },
    });

    const root = container.querySelector('[data-testid="rich-deliverable"]');
    expect(root).toHaveAttribute('data-presentation', 'dashboard');
    expect(root).toHaveAttribute('data-rich-canvas', 'wide');
    expect(root).toHaveClass('dashboard');
  });

  it('defaults to the default presentation and standard canvas when metadata is absent', () => {
    const { container } = render({ payload: { blocks: [{ type: 'markdown', content: 'Body' }] } });

    const root = container.querySelector('[data-testid="rich-deliverable"]');
    expect(root).toHaveAttribute('data-presentation', 'default');
    expect(root).toHaveAttribute('data-rich-canvas', 'standard');
    expect(root).not.toHaveClass('dashboard');
  });

  it('accepts compact/comfortable as density aliases of dense/airy', () => {
    const { container: compact } = render({
      payload: { metadata: { density: 'compact' }, blocks: [{ type: 'metric', title: 'M', value: 1 }] },
    });
    expect(compact.querySelector('[data-testid="rich-deliverable"]')).toHaveAttribute('data-rich-density', 'dense');

    const { container: comfortable } = render({
      payload: { metadata: { density: 'comfortable' }, blocks: [{ type: 'markdown', content: 'Body' }] },
    });
    expect(comfortable.querySelector('[data-testid="rich-deliverable"]')).toHaveAttribute('data-rich-density', 'airy');
  });

  it('uses the payload-level title only when no host title prop is supplied', () => {
    const withPayloadTitle = render({
      payload: { title: 'Payload title', blocks: [{ type: 'markdown', content: 'Body' }] },
    });
    expect(withPayloadTitle.getByRole('heading', { level: 1 })).toHaveTextContent('Payload title');
    withPayloadTitle.unmount();

    const withHostTitle = render({
      title: 'Host title',
      payload: { title: 'Payload title', blocks: [{ type: 'markdown', content: 'Body' }] },
    });
    expect(withHostTitle.getByRole('heading', { level: 1 })).toHaveTextContent('Host title');
  });

  it('opens a full view carrying the same resolved canvas as the inline document', async () => {
    render({
      payload: { metadata: { canvas: 'wide' }, blocks: [{ type: 'markdown', content: 'Body' }] },
      surface: 'embedded',
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Open full view' }));
    expect(screen.getByTestId('rich-deliverable-full-view')).toHaveAttribute('data-rich-canvas', 'wide');
  });

  it('lets a dashboard section header own the only document title while retaining actions', () => {
    const { container } = render({
      title: 'Host dashboard title',
      surface: 'embedded',
      payload: {
        metadata: { presentation: 'dashboard' },
        blocks: [{ type: 'section_header', eyebrow: 'Operations', title: 'Capacity snapshot', subtitle: 'Current system posture.' }],
      },
    });

    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.getByRole('heading', { level: 1, name: 'Capacity snapshot' })).toBeTruthy();
    expect(screen.queryByText('Host dashboard title')).toBeNull();
    expect(container.querySelector('[data-testid="rich-deliverable-toolbar"]')).toHaveClass('actions-only');
    expect(screen.getByRole('button', { name: 'Open full view' })).toBeTruthy();
  });

  it('keeps the host title when a dashboard starts with a titleless section header', () => {
    render({
      title: 'Host dashboard title',
      payload: {
        metadata: { presentation: 'dashboard' },
        blocks: [{ type: 'section_header', eyebrow: 'Operations', subtitle: 'Current system posture.' }],
      },
    });

    expect(screen.getByRole('heading', { level: 1, name: 'Host dashboard title' })).toBeTruthy();
    expect(screen.queryByTestId('rich-deliverable-toolbar')?.className).not.toContain('actions-only');
  });
});

describe('section_header block', () => {
  it('renders eyebrow, title, subtitle, and a toned status pill', () => {
    render({
      payload: {
        blocks: [{
          type: 'section_header',
          eyebrow: 'Aster platform',
          title: 'Capacity snapshot',
          subtitle: 'Sizing and scaling picture.',
          status: 'Idle',
          tone: 'success',
        }],
      },
    });

    expect(screen.getByText('Aster platform')).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Capacity snapshot' })).toBeTruthy();
    expect(screen.getByText('Sizing and scaling picture.')).toBeTruthy();
    const status = screen.getByText('Idle');
    expect(status.className).toContain('tone-success');
  });
});

describe('metric progress bar', () => {
  it('renders an accessible progressbar with value/max/label from block.progress', () => {
    render({
      payload: {
        blocks: [{
          type: 'metric', title: 'Disk used', value: '61%', tone: 'warning',
          progress: { value: 61, max: 100, label: '38.4 / 63 GB' },
        }],
      },
    });

    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '61');
    expect(bar).toHaveAttribute('aria-valuemin', '0');
    expect(bar).toHaveAttribute('aria-valuemax', '100');
    expect(screen.getByText('38.4 / 63 GB')).toBeTruthy();
  });

  it('renders no progress bar when block.progress is absent', () => {
    render({ payload: { blocks: [{ type: 'metric', title: 'Disk used', value: '61%' }] } });
    expect(screen.queryByRole('progressbar')).toBeNull();
  });

  it('keeps accessible progress values within their declared range', () => {
    render({
      payload: {
        blocks: [
          { type: 'metric', title: 'Over', value: '150%', progress: { value: 150, max: 100 } },
          { type: 'metric', title: 'Under', value: '-10%', progress: { value: -10, max: 100 } },
          { type: 'metric', title: 'Invalid', value: 'n/a', progress: { value: 'n/a', max: 100 } },
        ],
      },
    });

    const bars = screen.getAllByRole('progressbar');
    expect(bars[0]).toHaveAttribute('aria-valuenow', '100');
    expect(bars[1]).toHaveAttribute('aria-valuenow', '0');
    expect(bars[2]).not.toHaveAttribute('aria-valuenow');
  });
});

describe('generic block surface and span', () => {
  it('sets data-rich-surface only when explicitly authored', () => {
    const { container } = render({
      payload: {
        blocks: [
          { type: 'card', title: 'Explicit', surface: 'outlined' },
          { type: 'card', title: 'Default' },
        ],
      },
    });

    const cards = container.querySelectorAll('[data-rich-block-type="card"]');
    expect(cards[0]).toHaveAttribute('data-rich-surface', 'outlined');
    expect(cards[1]).not.toHaveAttribute('data-rich-surface');
  });

  it('applies a clamped grid-column span to the grid item wrapper', () => {
    const { container } = render({
      payload: {
        blocks: [{
          type: 'grid',
          blocks: [{ type: 'metric', title: 'Wide metric', value: 1, span: 9 }],
        }],
      },
    });

    const metricAnchor = container.querySelector('[data-rich-block-type="grid"] .rich-block-anchor') as HTMLElement;
    expect(metricAnchor.style.gridColumn).toBe('span 4');
  });

  it('keeps grid card peers stretchable through their RichBlockList anchors', () => {
    const { container } = render({
      payload: {
        blocks: [{
          type: 'grid',
          layout: 'equal',
          blocks: [
            { type: 'card', title: 'Short', content: 'One line.' },
            { type: 'card', title: 'Long', content: 'A deliberately longer card body that wraps onto additional lines.' },
          ],
        }],
      },
    });

    const anchors = container.querySelectorAll('[data-rich-block-type="grid"] .rich-block-anchor');
    expect(anchors).toHaveLength(2);
    expect((anchors[0].firstElementChild as HTMLElement).classList).toContain('rich-card');
    expect((anchors[1].firstElementChild as HTMLElement).classList).toContain('rich-card');
  });
});

describe('authored Markdown lists', () => {
  it('preserves ordered, unordered, and nested lists in card, callout, and section content', () => {
    const { container } = render({
      payload: {
        blocks: [
          {
            type: 'section',
            blocks: [{ type: 'markdown', content: '- Section item\n  - Nested item' }],
          },
          {
            type: 'callout',
            title: 'Callout',
            content: '1. First callout item\n2. Second callout item',
          },
          {
            type: 'card',
            title: 'Action',
            content: '1. First card item\n   - Nested card item',
          },
        ],
      },
    });

    expect(container.querySelector('.rich-panel .rich-markdown ul ul')).toHaveTextContent('Nested item');
    expect(container.querySelector('.rich-callout .rich-markdown ol')).toHaveTextContent('First callout item');
    expect(container.querySelector('.rich-card .rich-markdown ol ul')).toHaveTextContent('Nested card item');
  });
});

describe('grid layout variants', () => {
  it('tags the grid block with the requested layout attribute', () => {
    const { container } = render({
      payload: {
        blocks: [{ type: 'grid', layout: 'split-2-1', blocks: [{ type: 'markdown', content: 'A' }, { type: 'markdown', content: 'B' }] }],
      },
    });

    expect(container.querySelector('[data-rich-block-type="grid"]')).toHaveAttribute('data-rich-grid-layout', 'split-2-1');
  });

  it('defaults to auto and ignores unrecognized layout strings', () => {
    const { container } = render({
      payload: { blocks: [{ type: 'grid', layout: 'diagonal', blocks: [{ type: 'markdown', content: 'A' }] }] },
    });

    expect(container.querySelector('[data-rich-block-type="grid"]')).toHaveAttribute('data-rich-grid-layout', 'auto');
  });
});

describe('typed table cells', () => {
  it('renders text/number/code/badge/progress cells with dedicated markup and keeps scalars working', () => {
    const { container } = render({
      payload: {
        blocks: [{
          type: 'table',
          columns: ['collection', 'seen', 'latency', 'ratio', 'note'],
          rows: [{
            collection: { type: 'code', value: 'orchid.slot_control' },
            seen: { type: 'number', value: 54, label: '54×' },
            latency: { type: 'text', value: 0.2, label: '~0.2 s', emphasis: 'strong' },
            ratio: { type: 'badge', value: 'moderate', label: '88k → 1', tone: 'info' },
            note: 'plain scalar cell',
          }],
        }],
      },
    });

    expect(container.querySelector('td[data-cell-type="code"] code')).toHaveTextContent('orchid.slot_control');
    expect(container.querySelector('td[data-cell-type="number"]')).toHaveTextContent('54×');
    expect(container.querySelector('td[data-cell-type="text"] .emphasis-strong')).toHaveTextContent('~0.2 s');
    const badge = container.querySelector('td[data-cell-type="badge"] .rich-table-badge');
    expect(badge).toHaveTextContent('88k → 1');
    expect(badge?.className).toContain('tone-info');
    expect(container.querySelector('td[data-cell-type="progress"] [role="progressbar"]')).toBeNull();
    expect(screen.getByText('plain scalar cell')).toBeTruthy();
  });

  it('renders an accessible progress cell', () => {
    const { container } = render({
      payload: {
        blocks: [{
          type: 'table',
          columns: ['label', 'usage'],
          rows: [{ label: 'Cache', usage: { type: 'progress', value: 44, max: 100, label: '0.71 / 1.6 GB' } }],
        }],
      },
    });

    const bar = container.querySelector('td[data-cell-type="progress"] [role="progressbar"]');
    expect(bar).toHaveAttribute('aria-valuenow', '44');
    expect(bar).toHaveAttribute('aria-valuemax', '100');
  });

  it('sorts a typed-cell column by canonical value from the UI', async () => {
    const { container, getByRole } = render({
      payload: {
        blocks: [{
          type: 'table',
          columns: [{ key: 'collection', label: 'Collection' }, { key: 'seen', label: 'Seen' }],
          rows: [
            { collection: { type: 'code', value: 'z' }, seen: { type: 'number', value: 3 } },
            { collection: { type: 'code', value: 'a' }, seen: { type: 'number', value: 41 } },
          ],
        }],
      },
    });

    await fireEvent.click(getByRole('button', { name: /Sort by Seen/i }));
    const firstRowSeen = container.querySelector('tbody tr td[data-cell-type="number"]');
    expect(firstRowSeen).toHaveTextContent('3');
  });
});

describe('capacity-dashboard fixture scenario', () => {
  it('exists and renders without any unsupported block fallback', () => {
    expect(capacityDashboardScenario).toBeTruthy();
    const { container, queryByText } = render({
      title: capacityDashboardScenario!.title,
      content: capacityDashboardScenario!.content,
      payload: capacityDashboardScenario!.payload,
    });

    expect(queryByText(/Unsupported block:/)).toBeNull();
    expect(container.querySelector('[data-testid="rich-deliverable"]')).toHaveAttribute('data-presentation', 'dashboard');
    expect(container.querySelector('[data-testid="rich-deliverable"]')).toHaveAttribute('data-rich-canvas', 'wide');
    expect(container.querySelectorAll('[data-rich-block-type="metric"] [role="progressbar"]')).toHaveLength(4);
    expect(container.querySelector('[data-rich-role="metadata"]')).toHaveTextContent('orchid-primary');
    expect(container.querySelector('[data-rich-role="status"]')).toHaveTextContent('9 suggested indexes');
    expect(container.querySelectorAll('[data-rich-block-type="card"]')).toHaveLength(1);
  });
});
