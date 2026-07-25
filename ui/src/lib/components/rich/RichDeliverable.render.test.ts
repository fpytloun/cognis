import { fireEvent, render as renderComponent, screen, within } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';

import { SUPPORTED_RICH_BLOCK_TYPES, type RichBlock } from '$lib/rich-deliverable';
import RichDeliverable from './RichDeliverable.svelte';
import { dailyPulseScenario } from './daily-pulse.fixture';
import { richDeliverableVisualFixture } from './rich-deliverable.fixture';

function render(_: typeof RichDeliverable, props: { payload: unknown; surface?: 'embedded' | 'standalone' } & Record<string, unknown>) {
  return renderComponent(RichDeliverable, { surface: 'standalone', ...props });
}

describe('RichDeliverable component rendering', () => {
  it.each(['title', 'label', 'name'] as const)(
    'uses a leading hero %s as the single document identity',
    (alias) => {
      const { container } = render(RichDeliverable, {
        title: 'Toolbar fallback',
        content: 'Fallback',
        payload: { blocks: [{ type: 'hero', [alias]: 'Canonical identity' }] },
      });

      expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
      expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Canonical identity');
      expect(container.querySelector('[data-testid="rich-deliverable-toolbar"]')).toHaveClass('actions-only');
      expect(screen.queryByText('Toolbar fallback')).toBeNull();
    },
  );

  it('renders the pulse contract with one H1, neutral chrome, sources, and chart fallback', () => {
    const { container } = render(RichDeliverable, {
      title: dailyPulseScenario.title,
      content: dailyPulseScenario.content,
      payload: dailyPulseScenario.payload,
    });

    const deliverable = container.querySelector('[data-testid="rich-deliverable"]');
    expect(deliverable?.getAttribute('data-presentation')).toBe('pulse');
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.queryByTestId('rich-deliverable-toc')).toBeNull();
    expect(screen.queryByText(/Figure 1\./)).toBeNull();
    const image = screen.getByRole('img', { name: /Lovosice a České středohoří/ });
    expect(image).toHaveAttribute('width', '1600');
    expect(image).toHaveAttribute('height', '900');
    expect(screen.getByText(/Cognis acceptance fixture/)).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Úterý 14. července' })).toBeTruthy();
    expect(screen.getByText('Odeslat rozhodnutí')).toBeTruthy();
    expect(screen.getByLabelText('Aktuální čas 08:20')).toBeTruthy();
    expect(screen.getByLabelText('Todoist úkoly')).toHaveTextContent('Potvrdit prioritu');
    expect(screen.getByText('Před delší cestou stačí ověřit jedinou trasu.')).toBeTruthy();
    expect(screen.getAllByText('Chart data table').length).toBeGreaterThan(0);
    expect(screen.getAllByRole('link', { name: 'Lovosice po hodinách' })).toSatisfy(
      (links: HTMLElement[]) => links.every((link) => link.getAttribute('href') === 'https://example.org/pulse/weather'),
    );
  });

  it('renders a quiet day agenda empty state without inferred events or tasks', () => {
    render(RichDeliverable, {
      content: 'Fallback',
      payload: { blocks: [{ type: 'day_agenda', title: 'Dnes', now: '08:00', items: [] }] },
    });

    expect(screen.getByText('No timed events scheduled today.')).toBeTruthy();
    expect(screen.queryByLabelText('Todoist úkoly')).toBeNull();
  });

  it('renders canonical day agenda provenance safely instead of compatibility freshness', () => {
    render(RichDeliverable, {
      content: 'Fallback',
      payload: {
        blocks: [{
          type: 'day_agenda',
          title: 'Dnes',
          freshness: 'legacy must not leak',
          source: {
            label: 'Google Calendar',
            url: 'javascript:alert(1)',
            refreshed_at: '07:10 CEST',
          },
        }],
      },
    });

    const footer = screen.getByText((_, element) =>
      element?.tagName === 'FOOTER' && element.textContent?.includes('Google Calendar') === true);
    expect(footer).toHaveTextContent('Google Calendar · updated 07:10 CEST');
    expect(screen.queryByRole('link', { name: 'Google Calendar' })).toBeNull();
    expect(screen.queryByText(/legacy must not leak/)).toBeNull();
  });

  it('renders timestamp-only figure provenance without requiring a source', () => {
    render(RichDeliverable, {
      content: 'Fallback',
      payload: {
        blocks: [
          {
            type: 'figure',
            src: '/fixtures/daily-pulse-lovosice.svg',
            alt: 'Fixture',
            timestamp: '07:10 CEST',
          },
        ],
      },
    });
    expect(screen.getByText(/07:10 CEST/)).toBeTruthy();
  });

  it('renders manifest-backed nested card and figure media through private and public resolvers', () => {
    const mediaKey = 'media_0123456789abcdef01234567';
    const payload = {
      media_manifest: { [mediaKey]: { artifact_ref: 'art_0123456789abcdef' } },
      blocks: [{
        type: 'section',
        blocks: [
          { type: 'card', media: { key: mediaKey, alt: 'Private card media' } },
          {
            type: 'figure',
            media: {
              key: mediaKey,
              alt: 'Public figure media',
              source_label: 'Media source',
              source_url: 'https://example.org/source',
            },
          },
          { type: 'card', media: { key: 'media_ffffffffffffffffffffffff', alt: 'Unknown media' } },
        ],
      }],
    };
    const privateView = render(RichDeliverable, {
      content: 'Fallback',
      instanceId: 'dlv/private',
      payload,
    });
    const privateCard = within(privateView.container).getByRole('img', { name: 'Private card media' });
    expect(privateCard).toHaveAttribute(
      'src',
      'http://localhost:3000/api/v1/deliverables/dlv%2Fprivate/media/media_0123456789abcdef01234567',
    );
    expect(within(privateView.container).queryByRole('img', { name: 'Unknown media' })).toBeNull();

    const publicMediaUrlFor = (key: string) =>
      `/api/v1/deliverables/share/public-token/media/${encodeURIComponent(key)}`;
    const publicView = render(RichDeliverable, {
      content: 'Fallback',
      payload,
      mediaUrlFor: publicMediaUrlFor,
    });
    const publicFigure = within(publicView.container).getByRole('img', { name: 'Public figure media' });
    expect(publicFigure).toHaveAttribute(
      'src',
      'http://localhost:3000/api/v1/deliverables/share/public-token/media/media_0123456789abcdef01234567',
    );
    expect(within(publicView.container).getByRole('link', { name: 'Media source' })).toHaveAttribute(
      'href',
      'https://example.org/source',
    );
    expect(JSON.stringify(payload)).not.toContain('/media/');
    expect(JSON.stringify(payload)).not.toContain('X-Amz-Signature');
  });

  it.each(['embedded', 'standalone'] as const)(
    'renders authorized accordion item media once in the disclosure body on the %s surface',
    async (surface) => {
      const authorizedKey = 'media_0123456789abcdef01234567';
      const missingKey = 'media_ffffffffffffffffffffffff';
      const { container } = render(RichDeliverable, {
        content: 'Fallback',
        instanceId: 'dlv/item-media',
        surface,
        payload: {
          media_manifest: {
            [authorizedKey]: { artifact_ref: 'art_0123456789abcdef' },
          },
          blocks: [{
            type: 'accordion',
            items: [
              {
                type: 'section',
                title: 'Authorized article',
                summary: 'Collapsed summary',
                content: 'Expanded article body.',
                href: 'https://example.org/article',
                media: {
                  key: authorizedKey,
                  alt: 'Authorized article media',
                  credit: 'Fixture publisher',
                  source_url: 'https://example.org/image-source',
                  url: 'https://attacker.invalid/untrusted-image.png',
                },
              },
              {
                type: 'section',
                title: 'Missing article',
                media: { key: missingKey, alt: 'Missing article media' },
              },
            ],
          }],
        },
      });

      const summary = screen.getByText('Authorized article').closest('summary');
      const details = summary?.closest('details');
      const image = screen.getByRole('img', { name: 'Authorized article media' });

      expect(summary).not.toBeNull();
      expect(details).not.toBeNull();
      expect(summary?.querySelector('img')).toBeNull();
      expect(details?.querySelector('.rich-panel-context')?.contains(image)).toBe(true);
      expect(image).toHaveAttribute(
        'src',
        `http://localhost:3000/api/v1/deliverables/dlv%2Fitem-media/media/${authorizedKey}`,
      );
      expect(image).not.toHaveAttribute('src', 'https://attacker.invalid/untrusted-image.png');
      expect(image).toHaveAttribute('loading', 'lazy');
      expect(image).toHaveAttribute('decoding', 'async');
      expect(image).not.toBeVisible();
      expect(within(container).getAllByRole('img', { name: 'Authorized article media', hidden: true })).toHaveLength(1);
      expect(within(container).queryByRole('img', { name: 'Missing article media', hidden: true })).toBeNull();
      expect(screen.getByText('Fixture publisher')).not.toBeVisible();
      expect(screen.getByRole('link', { name: 'Authorized article media', hidden: true }))
        .toHaveAttribute('href', 'https://example.org/image-source');
      expect(screen.getByRole('link', { name: 'Open source', hidden: true }))
        .toHaveAttribute('href', 'https://example.org/article');

      await fireEvent.click(summary as HTMLElement);

      expect(details).toHaveAttribute('open');
      expect(image).toBeVisible();
      expect(screen.getByText('Fixture publisher')).toBeVisible();
      expect(screen.getByText('Expanded article body.')).toBeVisible();
      expect(within(container).getAllByRole('img', { name: 'Authorized article media' })).toHaveLength(1);
    },
  );

  it('renders tabs, modal, and gallery item media through their normal child paths without duplicates', () => {
    const mediaKey = 'media_0123456789abcdef01234567';
    const { container } = render(RichDeliverable, {
      content: 'Fallback',
      instanceId: 'dlv/container-media',
      payload: {
        media_manifest: {
          [mediaKey]: { artifact_ref: 'art_0123456789abcdef' },
        },
        blocks: [
          {
            type: 'tabs',
            items: [{
              type: 'section',
              title: 'Tab article',
              media: { key: mediaKey, alt: 'Tab item media' },
            }],
          },
          {
            type: 'modal',
            title: 'Modal details',
            items: [{
              type: 'card',
              title: 'Modal card',
              media: { key: mediaKey, alt: 'Modal item media' },
            }],
          },
          {
            type: 'gallery',
            items: [{
              type: 'figure',
              title: 'Gallery figure',
              media: { key: mediaKey, alt: 'Gallery item media' },
            }],
          },
        ],
      },
    });

    for (const alt of ['Tab item media', 'Modal item media', 'Gallery item media']) {
      expect(within(container).getAllByRole('img', { name: alt, hidden: true })).toHaveLength(1);
    }
    expect(screen.getByRole('img', { name: 'Tab item media' })).toBeVisible();
    expect(screen.getByRole('img', { name: 'Gallery item media' })).toBeVisible();
    expect(screen.getByRole('img', { name: 'Modal item media', hidden: true })).not.toBeVisible();
  });

  it('renders the visual fixture without falling back to unsupported blocks', () => {
    render(RichDeliverable, {
      payload: richDeliverableVisualFixture.payload,
      content: richDeliverableVisualFixture.content,
      title: richDeliverableVisualFixture.title,
      // "Open full view" is only shown for surface="embedded" (see below);
      // this test asserts on that button so it must set surface explicitly.
      surface: 'embedded',
    });

    expect(screen.queryByText(richDeliverableVisualFixture.title)).toBeNull();
    expect(screen.getByRole('heading', {
      level: 1,
      name: 'Managed, policy-driven agent tools beat naïve MCP for production workflows',
    })).toBeTruthy();
    expect(screen.getAllByText('Format comparison').length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { name: 'Markdown fallback typography' })).toBeTruthy();
    expect(screen.getByRole('columnheader', { name: 'Parallel?' })).toBeTruthy();
    expect(screen.getByRole('cell', { name: 'parallel with B2' })).toBeTruthy();
    expect(screen.getByText('Confidence')).toBeTruthy();
    expect(screen.getAllByText('Sources used').length).toBeGreaterThan(0);
    expect(screen.queryByText(/Unsupported block:/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Open full view' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Copy document' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Raw/debug' })).toBeNull();
  });

  it('renders generic visual contract fields safely with media, icons, links, and editorial disclosure', async () => {
    render(RichDeliverable, {
      content: 'Fallback',
      payload: {
        blocks: [
          {
            type: 'card',
            variant: 'feature',
            icon: 'activity',
            title: 'Linked card',
            summary: 'A visible summary',
            href: 'https://example.org/card',
            media: { href: 'https://example.org/image.png', alt: 'Card media', placement: 'top', credit: 'Example' },
          },
          { type: 'metric', icon: 'unknown_icon', label: 'Safe icon', value: '42', timestamp: 'Now', delta: '+1' },
          {
            type: 'accordion',
            items: [{
              type: 'section',
              title: 'Editorial story',
              summary: 'Summary remains visible',
              content: 'Expanded context appears only once.',
            }],
          },
          {
            type: 'chart',
            title: 'Baseline chart',
            spec_version: 'cognis.chart.v1',
            chart_type: 'line',
            series: [{
              id: 'value',
              label: 'Value',
              points: [{ x: '2026-07-01', y: 1 }, { x: '2026-07-02', y: 3 }],
            }],
            x_axis: { type: 'time' },
            y_axis: { type: 'linear' },
          },
        ],
      },
    });

    expect(screen.getByRole('link', { name: 'Linked card' })).toHaveAttribute('href', 'https://example.org/card');
    expect(screen.getByRole('img', { name: 'Card media' })).toBeTruthy();
    expect(screen.getByText('Example')).toBeTruthy();
    expect(document.querySelector('[data-rich-icon="activity"]')).toBeTruthy();
    expect(document.querySelector('[data-rich-icon="unknown_icon"]')).toBeNull();
    expect(screen.getByText('Summary remains visible')).toBeTruthy();
    expect(screen.getByText('Expanded context appears only once.')).not.toBeVisible();
    await fireEvent.click(screen.getByText('Editorial story'));
    expect(screen.getByText('Expanded context appears only once.')).toBeTruthy();
    expect(screen.getByTestId('rich-chart-baseline')).toBeTruthy();
  });

  it('renders card variant "visual" with media as a full-bleed background overlay', () => {
    render(RichDeliverable, {
      content: 'Fallback',
      payload: {
        blocks: [{
          type: 'card',
          variant: 'visual',
          title: 'Visual card',
          summary: 'Image-forward summary',
          media: { href: 'https://example.org/visual.png', alt: 'Visual card media' },
        }],
      },
    });

    const card = document.querySelector('[data-rich-card-variant="visual"]');
    expect(card).toHaveClass('has-media');
    expect(screen.getByRole('img', { name: 'Visual card media' })).toBeTruthy();
    expect(screen.getByText('Visual card')).toBeTruthy();
    expect(screen.getByText('Image-forward summary')).toBeTruthy();
  });

  it('falls back to the flat card layout for card variant "visual" without authored media', () => {
    render(RichDeliverable, {
      content: 'Fallback',
      payload: {
        blocks: [{
          type: 'card',
          variant: 'visual',
          title: 'Visual card, no media',
          summary: 'Degrades to the standard flat layout',
        }],
      },
    });

    const card = document.querySelector('[data-rich-card-variant="visual"]');
    expect(card).not.toHaveClass('has-media');
    expect(screen.queryByRole('img')).toBeNull();
    expect(screen.getByText('Visual card, no media')).toBeTruthy();
  });

  it('falls back to the flat card layout for card variant "visual" once the image fails to load', async () => {
    render(RichDeliverable, {
      content: 'Fallback',
      payload: {
        blocks: [{
          type: 'card',
          variant: 'visual',
          title: 'Visual card, broken media',
          summary: 'Falls back once the browser reports the load failure',
          media: { href: 'https://example.org/broken.png', alt: 'Broken media' },
        }],
      },
    });

    const card = document.querySelector('[data-rich-card-variant="visual"]');
    expect(card).toHaveClass('has-media');
    const image = screen.getByRole('img', { name: 'Broken media' });

    await fireEvent.error(image);

    expect(card).not.toHaveClass('has-media');
    expect(screen.queryByRole('img', { name: 'Broken media' })).toBeNull();
    expect(screen.getByText('Visual card, broken media')).toBeTruthy();
  });

  it('renders kv blocks and toggles the full view without raw/debug controls', async () => {
    render(RichDeliverable, {
      payload: {
        blocks: [{ type: 'kv', title: 'Facts', items: [{ label: 'Scope', value: 'conversation' }] }],
        metadata: { eyebrow: 'Test' },
      },
      content: 'Fallback text',
      title: 'Rich report',
      surface: 'embedded',
    });

    expect(screen.getByText('Facts')).toBeTruthy();
    expect(screen.getByText('Scope')).toBeTruthy();
    expect(screen.getByText('conversation')).toBeTruthy();

    await fireEvent.click(screen.getByRole('button', { name: 'Open full view' }));
    expect(screen.getByTestId('rich-deliverable-full-view')).toBeTruthy();
    await fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByTestId('rich-deliverable-full-view')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Raw/debug' })).toBeNull();
    expect(screen.queryByTestId('rich-deliverable-raw')).toBeNull();
  });

  it('dispatches every supported block type without using unsupported fallback', () => {
    const blockFor = (type: string): RichBlock => {
      const base = { type, title: `${type} title`, content: `${type} content` };
      if (['section', 'stack', 'columns', 'grid', 'card_grid'].includes(type)) {
        return { ...base, blocks: [{ type: 'markdown', content: `${type} child` }] };
      }
      if (type === 'tabs' || type === 'accordion' || type === 'modal') {
        return { ...base, items: [{ type: 'markdown', title: `${type} child`, content: `${type} child` }] };
      }
      if (type === 'callout') return { ...base, tone: 'success' };
      if (type === 'metric') return { ...base, value: 42, delta: '+2%', description: 'metric description' };
      if (type === 'kv' || type === 'key_value') return { ...base, items: [{ label: 'Key', value: 'Value' }] };
      if (type === 'timeline' || type === 'steps') return { ...base, items: [{ title: 'Step', content: 'Done' }] };
      if (type === 'quote') return { ...base, quote: 'quoted text', byline: 'Author' };
      if (type === 'figure') return { ...base, url: 'https://example.com/image.png', alt: 'Example image', caption: 'Caption' };
      if (type === 'gallery') return { ...base, items: [{ url: 'https://example.com/image.png', caption: 'Gallery item' }] };
      if (type === 'table' || type === 'comparison_matrix' || type === 'decision_matrix') return { ...base, columns: ['name', 'score'], rows: [{ name: 'A', score: 1 }] };
      if (type === 'research_answer') return { ...base, paragraphs: [{ text: 'Answer', citations: ['s1'] }], sources: [{ id: 's1', title: 'Source' }] };
      if (type === 'evidence_report' || type === 'claim_cards') return { ...base, claims: [{ title: 'Claim', confidence: 'high', evidence: [{ text: 'Evidence' }] }] };
      if (type === 'chart') return { ...base, rows: [{ label: 'A', value: 1 }] };
      if (type === 'mermaid') return { ...base, source: 'graph TD; A-->B' };
      if (type === 'link' || type === 'link_preview') return { ...base, url: 'https://example.com', site: 'Example' };
      if (type === 'source_list') return { ...base, sources: [{ title: 'Source', url: 'https://example.com' }] };
      return base;
    };
    const supportedTypes = Array.from(SUPPORTED_RICH_BLOCK_TYPES) as string[];
    const blocks = supportedTypes.map(blockFor);
    const { container } = render(RichDeliverable, { payload: { blocks }, content: 'Fallback text' });

    expect(container.querySelector('[data-rich-block-type="unsupported"]')).toBeNull();
    for (const type of supportedTypes) {
      expect(container.querySelector(`[data-rich-block-type="${type}"]`)).toBeTruthy();
    }
  });

  it.each(['content', 'summary'] as const)(
    'renders callout body from %s without ever leaving it empty',
    (field) => {
      render(RichDeliverable, {
        payload: {
          blocks: [{ type: 'callout', title: 'Important caveat', [field]: 'Read this carefully.' }],
        },
        content: 'Fallback text',
      });

      expect(screen.getByText('Important caveat')).toBeTruthy();
      expect(screen.getByText('Read this carefully.')).toBeTruthy();
    },
  );

  it('renders metric description from summary/dek aliases', () => {
    render(RichDeliverable, {
      payload: {
        blocks: [
          {
            type: 'metric',
            label: 'Error budget',
            value: '12%',
            summary: 'Consumed faster than the weekly pace.',
          },
        ],
      },
      content: 'Fallback text',
    });

    expect(screen.getByText('Consumed faster than the weekly pace.')).toBeTruthy();
  });

  it('renders dashboard item description from summary/dek aliases', () => {
    render(RichDeliverable, {
      payload: {
        blocks: [
          {
            type: 'dashboard',
            title: 'Fleet health',
            metrics: [{ label: 'Availability', value: 99.9, summary: 'Stable across all regions.' }],
          },
        ],
      },
      content: 'Fallback text',
    });

    expect(screen.getByText('Stable across all regions.')).toBeTruthy();
  });

  it('renders a standalone action block as an action-variant card, not the unsupported fallback', () => {
    const { container } = render(RichDeliverable, {
      payload: {
        blocks: [
          {
            type: 'action',
            icon: 'check',
            title: 'Rotate the credential',
            content: 'Do this before the next deploy.',
          },
        ],
      },
      content: 'Fallback text',
    });

    expect(screen.getByText('Rotate the credential')).toBeTruthy();
    expect(screen.getByText('Do this before the next deploy.')).toBeTruthy();
    expect(container.querySelector('[data-rich-block-type="action"]')).toBeTruthy();
    expect(container.querySelector('[data-rich-card-variant="action"]')).toBeTruthy();
    expect(container.querySelector('[data-rich-block-type="unsupported"]')).toBeNull();
  });

  it('lets an explicit action block variant override the action default', () => {
    const { container } = render(RichDeliverable, {
      payload: {
        blocks: [{ type: 'action', variant: 'status', title: 'Follow up', content: 'Track next week.' }],
      },
      content: 'Fallback text',
    });

    expect(container.querySelector('[data-rich-card-variant="status"]')).toBeTruthy();
    expect(container.querySelector('[data-rich-card-variant="action"]')).toBeNull();
  });

  it('lets an unconstrained grid/columns block auto-fit instead of forcing a single column', () => {
    // Regression test: GridBlock.svelte previously computed a default
    // column count of 1 whenever the author didn't specify `columns`
    // (Math.max(1, Math.min(4, Number(undefined) || 0)) === 1), which
    // hardcoded --rich-columns: 1 and overrode the CSS auto-fit fallback
    // (`repeat(var(--rich-columns, auto-fit), ...)`). Every generic grid or
    // columns block -- a row of metrics, a card grid -- silently rendered
    // as a single full-width vertical stack regardless of viewport, no
    // matter how many items it held.
    const { container } = render(RichDeliverable, {
      payload: {
        blocks: [
          {
            type: 'grid',
            blocks: [
              { type: 'metric', label: 'A', value: 1 },
              { type: 'metric', label: 'B', value: 2 },
            ],
          },
        ],
      },
      content: 'Fallback text',
    });

    const grid = container.querySelector('[data-rich-block-type="grid"]') as HTMLElement;
    expect(grid).toBeTruthy();
    expect(grid.style.getPropertyValue('--rich-columns')).toBe('');
  });

  it('honors an explicit author-specified column count on a grid block', () => {
    const { container } = render(RichDeliverable, {
      payload: {
        blocks: [
          {
            type: 'grid',
            columns: 3,
            blocks: [{ type: 'metric', label: 'A', value: 1 }],
          },
        ],
      },
      content: 'Fallback text',
    });

    const grid = container.querySelector('[data-rich-block-type="grid"]') as HTMLElement;
    expect(grid.style.getPropertyValue('--rich-columns')).toBe('3');
  });

  it('clamps an out-of-range explicit column count into 1-4', () => {
    const { container } = render(RichDeliverable, {
      payload: {
        blocks: [
          { type: 'grid', columns: 9, blocks: [{ type: 'metric', label: 'A', value: 1 }] },
        ],
      },
      content: 'Fallback text',
    });

    const grid = container.querySelector('[data-rich-block-type="grid"]') as HTMLElement;
    expect(grid.style.getPropertyValue('--rich-columns')).toBe('4');
  });

  it('renders every canonical item-backed container through chat traversal', () => {
    const types = ['tabs', 'accordion', 'modal', 'gallery'];
    render(RichDeliverable, {
      payload: {
        blocks: types.map((type) => ({
          type,
          blocks: [{ type: 'markdown', content: `${type} block marker` }],
          items: [
            type === 'gallery'
              ? { type: 'figure', title: `${type} item marker`, url: 'https://example.com/gallery.png' }
              : { type: 'markdown', title: `${type} item`, content: `${type} item marker` },
          ],
        })),
      },
      content: 'Fallback text',
    });

    for (const type of types) {
      expect(screen.getByText(`${type} block marker`)).toBeTruthy();
      expect(screen.getByText(`${type} item marker`)).toBeTruthy();
    }
  });

  it('renders chart, mermaid, and divider children exactly once after primary content', () => {
    const { container } = render(RichDeliverable, {
      payload: {
        blocks: [
          {
            type: 'chart',
            title: 'Parent chart',
            rows: [{ label: 'A', value: 1 }],
            children: [{ type: 'markdown', content: 'Chart child marker' }],
          },
          {
            type: 'mermaid',
            title: 'Parent diagram',
            source: 'graph TD; A-->B',
            blocks: [{ type: 'markdown', content: 'Mermaid child marker' }],
          },
          {
            type: 'divider',
            children: [{ type: 'markdown', content: 'Divider child marker' }],
          },
        ],
      },
      content: 'Fallback text',
    });

    for (const marker of ['Chart child marker', 'Mermaid child marker', 'Divider child marker']) {
      expect(screen.getAllByText(marker)).toHaveLength(1);
    }
    const divider = container.querySelector('[data-rich-block-type="divider"]');
    expect(divider?.firstElementChild?.tagName).toBe('HR');
    expect(divider?.textContent).toContain('Divider child marker');
  });

  it('renders Mermaid code aliases and resolves structured source-list references', () => {
    const { container } = render(RichDeliverable, {
      payload: {
        sources: [
          {
            id: 'sweet',
            title: 'Pseudogenization of a Sweet-Receptor Gene',
            url: 'https://doi.org/10.1371/journal.pgen.0010003',
          },
        ],
        blocks: [
          {
            type: 'mermaid',
            title: 'How a cat maps a doorway',
            code: 'flowchart LR\n  Air --> Whiskers',
          },
          {
            type: 'source_list',
            title: 'Further reading',
            sources: [{ source_id: 'sweet', label: 'Why cats do not taste sweetness' }],
          },
        ],
      },
      content: 'Fallback text',
    });

    expect(container.querySelector('[data-mermaid-source]')?.textContent)
      .toContain('Air --> Whiskers');
    const source = screen.getByRole('link', { name: 'Why cats do not taste sweetness' });
    expect(source.getAttribute('href')).toBe('https://doi.org/10.1371/journal.pgen.0010003');
    expect(screen.queryByText('Source 1')).toBeNull();
  });

  it('keeps Escape scoped to the rich full-view overlay', async () => {
    let bubbled = false;
    const handleKeydown = () => {
      bubbled = true;
    };
    document.addEventListener('keydown', handleKeydown);
    try {
      render(RichDeliverable, {
        payload: {
          blocks: [{ type: 'markdown', content: 'Overlay content' }],
        },
        content: 'Fallback text',
        title: 'Rich report',
        surface: 'embedded',
      });

      await fireEvent.click(screen.getByRole('button', { name: 'Open full view' }));
      await fireEvent.keyDown(screen.getByTestId('rich-deliverable-full-view'), { key: 'Escape' });

      expect(screen.queryByTestId('rich-deliverable-full-view')).toBeNull();
      expect(bubbled).toBe(false);
    } finally {
      document.removeEventListener('keydown', handleKeydown);
    }
  });

  it('renders unknown blocks with graceful fallback details', () => {
    render(RichDeliverable, {
      payload: { blocks: [{ type: 'future_block', custom: true }] },
      content: 'Fallback text',
    });

    expect(screen.getByText('Unsupported block: future_block')).toBeTruthy();
  });

  it('renders markdown fallback in full view when rich blocks are empty', async () => {
    render(RichDeliverable, {
      payload: { blocks: [] },
      content: '# Fallback report\n\n**Visible fallback markdown**',
      title: 'Fallback report',
      surface: 'embedded',
    });

    await fireEvent.click(screen.getByRole('button', { name: 'Open full view' }));

    const fullView = screen.getByTestId('rich-deliverable-full-view');
    expect(within(fullView).getByText('Visible fallback markdown')).toBeTruthy();
  });

  it('keeps one active anchor namespace and preserves legacy aliases in full view', async () => {
    const { container } = render(RichDeliverable, {
      payload: {
        metadata: { toc: true },
        blocks: [
          { type: 'section', title: 'Overview' },
          { type: 'section', title: 'Overview' },
        ],
      },
      content: 'Fallback',
      title: 'Anchors',
      surface: 'embedded',
    });
    const duplicateIds = (root: ParentNode) => {
      const ids = Array.from(root.querySelectorAll<HTMLElement>('[id]')).map((element) => element.id);
      return ids.filter((id, index) => ids.indexOf(id) !== index);
    };

    expect(duplicateIds(container)).toEqual([]);
    const namespace = container.querySelector<HTMLElement>('[data-rich-instance]')?.dataset.richInstance;
    expect(namespace).toBeTruthy();
    expect(container.querySelector(`#${namespace}-overview`)).toBeTruthy();
    expect(container.querySelector(`#${namespace}-overview-2`)).toBeTruthy();
    expect(container.querySelector(`#${namespace}-rich-section-0`)).toBeTruthy();

    await fireEvent.click(screen.getByRole('button', { name: 'Open full view' }));

    // The full-view overlay is portaled to document.body (see `use:portal`
    // in RichDeliverable.svelte) so it can escape the component's isolated
    // stacking context and always render above surrounding app chrome
    // (sidebar, composer, etc). Its content is therefore no longer inside
    // `container` once open — assert against the portaled node instead.
    const fullView = screen.getByTestId('rich-deliverable-full-view');
    expect(duplicateIds(document.body)).toEqual([]);
    expect(fullView.querySelectorAll(`#${namespace}-overview`)).toHaveLength(1);
    expect(fullView.querySelectorAll(`#${namespace}-rich-section-0`)).toHaveLength(1);
    expect(within(fullView).getAllByText('Overview').length).toBeGreaterThanOrEqual(2);
  });

  it('uses document-wide citation numbers across research blocks', () => {
    render(RichDeliverable, {
      payload: {
        sources: [
          { id: 'a', title: 'Source A' },
          { id: 'b', title: 'Source B' },
        ],
        blocks: [
          { type: 'research_answer', title: 'First', paragraphs: [{ text: 'First claim', citations: ['b', 'b'] }] },
          { type: 'research_answer', title: 'Second', paragraphs: [{ text: 'Second claim', citations: ['a', 'b'] }] },
        ],
      },
      content: 'Fallback',
    });

    expect(screen.getAllByRole('button', { name: /Citation 1: Source B/ })).toHaveLength(2);
    expect(screen.getByRole('button', { name: /Citation 2: Source A/ })).toBeTruthy();
  });

  it('renders one citation for equivalent inline DOI aliases', () => {
    render(RichDeliverable, {
      payload: {
        blocks: [{
          type: 'research_answer',
          title: 'Finding',
          paragraphs: [{
            text: 'Claim',
            citations: [
              { title: 'First alias', doi: 'HTTPS://DOI.ORG/10.1000/Test' },
              { title: 'Second alias', doi: 'doi:10.1000/test', href: 'https://example.com/alias' },
            ],
          }],
        }],
      },
      content: 'Fallback',
    });

    expect(screen.getAllByRole('button', { name: /Citation 1:/ })).toHaveLength(1);
    expect(screen.queryByRole('button', { name: /Citation 2:/ })).toBeNull();
  });

  it('normalizes rendered Markdown H1/H3 headings and anchors effective targets', () => {
    const { container } = render(RichDeliverable, {
      payload: {
        metadata: { toc: { enabled: true, depth: 3 } },
        blocks: [
          { type: 'markdown', content: '# Primary Markdown\n\nBody' },
          { type: 'markdown', content: '### Tertiary Markdown\n\nBody' },
          {
            type: 'section',
            title: 'Parent',
            children: [{ type: 'markdown', content: '# Nested Markdown\n\nBody' }],
          },
          { type: 'markdown', title: 'Summary', content: 'Paragraph only' },
        ],
      },
      content: 'Fallback',
    });
    const namespace = container.querySelector<HTMLElement>('[data-rich-instance]')?.dataset.richInstance;

    expect(container.querySelector(`h2#${namespace}-primary-markdown`)?.textContent).toBe('Primary Markdown');
    expect(container.querySelector(`h2#${namespace}-tertiary-markdown`)?.textContent).toBe('Tertiary Markdown');
    expect(container.querySelector(`h3#${namespace}-nested-markdown`)?.textContent).toBe('Nested Markdown');
    expect(container.querySelectorAll(`#${namespace}-primary-markdown`)).toHaveLength(1);
    expect(container.querySelector(`#${namespace}-primary-markdown`)?.getAttribute('tabindex')).toBe('-1');
    expect(container.querySelector(`h2#${namespace}-summary`)?.textContent).toBe('Summary');
    expect(container.querySelector(`#${namespace}-summary`)?.getAttribute('tabindex')).toBe('-1');
    expect(container.querySelectorAll(`#${namespace}-summary`)).toHaveLength(1);
  });

  it('allocates collision-free IDs and gives titled Markdown ownership of its heading', () => {
    const { container } = render(RichDeliverable, {
      payload: {
        metadata: { toc: { enabled: true, depth: 3 } },
        sources: [{ id: 'source', title: 'Source' }],
        blocks: [
          { type: 'section', id: 'rich-section-0', title: 'Legacy collision' },
          { type: 'section', id: 'reference-1', title: 'Reference collision' },
          { type: 'section', id: 'references-heading', title: 'Bibliography collision' },
          { type: 'section', id: 'cite-1-1', title: 'Citation collision' },
          { type: 'section', id: 'rich-citation-6-0-0', title: 'Popover collision' },
          { type: 'section', id: 'toc', title: 'TOC collision' },
          { type: 'figure', id: 'figure-1', title: 'Figure collision' },
          { type: 'table', id: 'table-1', title: 'Table collision' },
          { type: 'section', id: 'mermaid-0', title: 'Mermaid ID collision' },
          { type: 'mermaid', title: 'Actual Mermaid', source: 'flowchart LR\n  A --> B' },
          { type: 'section', id: 'duplicate', title: 'Duplicate one' },
          { type: 'section', id: 'duplicate', title: 'Duplicate two' },
          {
            type: 'markdown',
            id: 'summary',
            title: 'Summary',
            content: '# Content heading\n\nParagraph\n\n### Detail heading\n\nMore.',
          },
          {
            type: 'research_answer',
            title: 'Evidence',
            paragraphs: [{ text: 'Claim', citations: ['source'] }],
          },
        ],
      },
      content: 'Fallback',
    });
    const ids = Array.from(container.querySelectorAll<HTMLElement>('[id]')).map((element) => element.id);
    const namespace = container.querySelector<HTMLElement>('[data-rich-instance]')?.dataset.richInstance;

    expect(new Set(ids).size).toBe(ids.length);
    for (const id of [
      'section-rich-section-0',
      'section-reference-1',
      'section-references-heading',
      'section-cite-1-1',
      'section-rich-citation-6-0-0',
      'section-toc',
      'section-figure-1',
      'section-table-1',
      'section-mermaid-0',
      'duplicate',
      'duplicate-2',
    ]) expect(container.querySelector(`#${namespace}-${id}`)).toBeTruthy();
    expect(container.querySelector('[data-mermaid-id]')?.getAttribute('data-mermaid-id')).toBe(
      `${namespace}-mermaid-0`
    );
    expect(container.querySelector(`h2#${namespace}-summary`)?.textContent).toBe('Summary');
    expect(container.querySelector(`h3#${namespace}-content-heading`)?.textContent).toBe('Content heading');
    expect(container.querySelector(`h4#${namespace}-detail-heading`)?.textContent).toBe('Detail heading');
    expect(container.querySelector(`h2#${namespace}-summary`)?.nextElementSibling?.tagName).toBe('H3');
  });

  it('namespaces two identical deliverables and keeps controls local to each instance', async () => {
    const payload = {
      metadata: { toc: true },
      sources: [{ id: 'source', title: 'Source' }],
      blocks: [
        { type: 'section', title: 'Overview' },
        { type: 'section', title: 'Details' },
        { type: 'section', title: 'Results' },
        {
          type: 'research_answer',
          title: 'Evidence',
          paragraphs: [{ text: 'Claim', citations: ['source'] }],
        },
      ],
    };
    const first = render(RichDeliverable, { payload, content: 'Fallback', instanceId: 'same-deliverable' });
    const second = render(RichDeliverable, { payload, content: 'Fallback', instanceId: 'same-deliverable' });
    const roots = Array.from(document.querySelectorAll<HTMLElement>('[data-rich-instance]'));
    const namespaces = roots.map((root) => root.dataset.richInstance);
    const ids = Array.from(document.querySelectorAll<HTMLElement>('[id]')).map((element) => element.id);

    expect(new Set(namespaces).size).toBe(2);
    expect(new Set(ids).size).toBe(ids.length);
    for (const root of roots) {
      const namespace = root.dataset.richInstance;
      const tocButton = within(root).getAllByRole('button', { name: /Overview$/ }).at(-1)!;
      await fireEvent.click(tocButton);
      expect(root.querySelector(`#${namespace}-overview`)).toHaveFocus();
      const citation = within(root).getByRole('button', { name: /Citation 1: Source/ });
      await fireEvent.click(citation);
      const controlledId = citation.getAttribute('aria-controls');
      expect(controlledId).toContain(namespace);
      expect(root.querySelector(`#${controlledId}`)).toBeTruthy();
    }
    first.unmount();
    second.unmount();
  });

  it('keeps desktop TOC navigation instance-local', async () => {
    const payload = {
      metadata: { toc: true },
      blocks: [
        { type: 'section', title: 'Overview' },
        { type: 'section', title: 'Details' },
        { type: 'section', title: 'Results' },
      ],
    };
    const first = render(RichDeliverable, { payload, content: 'Fallback', instanceId: 'same-deliverable' });
    const second = render(RichDeliverable, { payload, content: 'Fallback', instanceId: 'same-deliverable' });
    const roots = Array.from(document.querySelectorAll<HTMLElement>('[data-rich-instance]'));
    const secondToc = within(roots[1]).getByTestId('rich-deliverable-toc');

    await fireEvent.click(within(secondToc).getByRole('button', { name: 'Overview' }));
    const secondNamespace = roots[1].dataset.richInstance;
    expect(roots[1].querySelector(`#${secondNamespace}-overview`)).toHaveFocus();
    expect(roots[0].querySelector(`#${secondNamespace}-overview`)).toBeNull();

    first.unmount();
    second.unmount();
  });

  it('mounts compact desktop TOCs with one hamburger trigger per embedded deliverable, no duplicates', () => {
    const payload = {
      metadata: { toc: true },
      blocks: [
        { type: 'section', title: 'Overview' },
        { type: 'section', title: 'Details' },
        { type: 'section', title: 'Results' },
      ],
    };
    const mounted = Array.from({ length: 5 }, (_, index) => render(RichDeliverable, {
      payload,
      content: 'Fallback',
      instanceId: `embedded-${index}`,
      surface: 'embedded',
    }));

    expect(screen.queryAllByTestId('rich-deliverable-toc')).toHaveLength(5);
    expect(screen.queryAllByRole('navigation', { name: 'Table of contents' })).toHaveLength(5);
    expect(screen.queryByText('Contents')).toBeNull();
    // The hamburger trigger is no longer restricted to surface="standalone"
    // (that restriction was the bug: embedded chat deliverables had no way
    // to open their TOC at all). It is width-driven via CSS, not surface --
    // one trigger per embedded instance, none of them duplicated.
    expect(screen.queryAllByRole('button', { name: 'Open table of contents' })).toHaveLength(5);

    mounted.forEach((component) => component.unmount());
  });

  it('renders title-only hierarchical TOC and a safe inline figure', () => {
    const safeSvg = 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 320 100%22%3E%3Crect width=%22320%22 height=%22100%22 fill=%22%23eee%22/%3E%3C/svg%3E';
    const { container } = render(RichDeliverable, {
      payload: {
        metadata: { toc: { enabled: true, depth: 3 }, publication: true },
        blocks: [
          {
            type: 'section',
            title: 'Parent',
            children: [{ type: 'section', title: 'Nested' }],
          },
          { type: 'section', title: 'Second' },
          { type: 'section', title: 'Third' },
          {
            type: 'figure',
            title: 'Pipeline',
            src: safeSvg,
            alt: 'Three-stage renderer pipeline',
            caption: 'Canonical payload reaches chat and PDF.',
          },
        ],
      },
      content: 'Fallback',
    });
    const desktopToc = container.querySelector('[data-testid="rich-deliverable-toc"] nav');
    const image = screen.getByRole('img', { name: 'Three-stage renderer pipeline' });

    expect(desktopToc?.querySelectorAll('small')).toHaveLength(0);
    expect(desktopToc?.querySelector('li[data-level="2"] > ol li[data-level="3"]')?.textContent).toContain('Nested');
    expect(image.getAttribute('src')).toBe(safeSvg);
    const caption = screen.getByText(/Figure 1\./).closest('figcaption');
    expect(caption?.textContent).toBe('Figure 1. Canonical payload reaches chat and PDF.');
    expect(caption?.querySelector('strong')?.textContent).toBe('Figure 1.');
    expect(screen.getByText(/Canonical payload reaches chat and PDF/)).toBeTruthy();
  });

  it('renders one document identity and one TOC navigation for a long report', () => {
    render(RichDeliverable, {
      title: 'Outer publication title',
      content: 'Fallback',
      payload: {
        metadata: { toc: true },
        blocks: [
          { type: 'hero', title: 'System review', subtitle: 'Ten-section review' },
          ...Array.from({ length: 10 }, (_, index) => ({
            type: 'markdown',
            title: `Section ${index + 1}`,
            content: `Finding ${index + 1}`,
          })),
        ],
      },
    });

    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.queryByText('Outer publication title')).toBeNull();
    expect(screen.getAllByRole('navigation', { name: 'Table of contents' })).toHaveLength(1);
    expect(screen.getAllByTestId('rich-deliverable-toc')).toHaveLength(1);
    expect(screen.queryByText('Rich deliverable')).toBeNull();
  });

  it('preserves SvelteKit history state when TOC navigation replaces the fragment', async () => {
    const kitState = { index: 7, scroll: { x: 0, y: 120 }, form: null };
    history.replaceState(kitState, '', '/fixture?mode=full');
    render(RichDeliverable, {
      title: 'History report',
      content: 'Fallback',
      surface: 'embedded',
      payload: {
        metadata: { toc: true },
        blocks: [
          { type: 'hero', title: 'History report' },
          { type: 'markdown', title: 'Overview', content: 'Detailed overview.' },
          { type: 'markdown', title: 'Evaluation', content: 'Detailed evaluation.' },
        ],
      },
    });

    const root = screen.getByTestId('rich-deliverable');
    root.dispatchEvent(new CustomEvent('rich-toc-request', { bubbles: true }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Evaluation' }));

    expect(location.pathname).toBe('/fixture');
    expect(location.search).toBe('?mode=full');
    expect(location.hash).toMatch(/^#rich-.+-evaluation$/);
    expect(history.state).toEqual(kitState);
  });

  it('keeps the canonical desktop TOC mounted after repeated navigation', async () => {
    render(RichDeliverable, {
      title: 'Contextual lifecycle report',
      content: 'Fallback',
      surface: 'embedded',
      payload: {
        metadata: { toc: true },
        blocks: [
          { type: 'hero', title: 'Contextual lifecycle report' },
          { type: 'markdown', title: 'Overview', content: 'Detailed overview.' },
          { type: 'markdown', title: 'Evaluation', content: 'Detailed evaluation.' },
        ],
      },
    });

    await fireEvent.click(await screen.findByRole('button', { name: 'Overview' }));
    expect(screen.queryByTestId('rich-deliverable-toc')).not.toBeNull();

    await fireEvent.click(await screen.findByRole('button', { name: 'Evaluation' }));
    expect(screen.queryByTestId('rich-deliverable-toc')).not.toBeNull();
    expect(location.hash).toMatch(/^#rich-.+-evaluation$/);
  });

  it('omits TOC chrome for a short report', () => {
    render(RichDeliverable, {
      title: 'Short report',
      content: 'Fallback',
      payload: {
        blocks: [{ type: 'markdown', title: 'Summary', content: 'One concise result.' }],
      },
    });

    expect(screen.queryByTestId('rich-deliverable-toc')).toBeNull();
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
  });

  it('renders inline markdown links/emphasis in prose fields that used to show raw syntax', () => {
    render(RichDeliverable, {
      title: 'Inline markdown',
      content: 'Fallback',
      payload: {
        blocks: [
          {
            type: 'card',
            title: 'Card',
            summary: 'See [ČTK](https://example.com/ctk) for the **latest** figures.',
          },
          { type: 'quote', quote: 'This is **quoted** with a [link](https://example.com/q).' },
          {
            type: 'table',
            columns: ['label', 'value'],
            rows: [{ label: 'Row', value: 'See [source](https://example.com/cell)' }],
          },
        ],
      },
    });

    expect(screen.getByRole('link', { name: 'ČTK' })).toHaveAttribute('href', 'https://example.com/ctk');
    expect(screen.queryByText('[ČTK](https://example.com/ctk)')).toBeNull();
    expect(screen.getByRole('link', { name: 'link' })).toHaveAttribute('href', 'https://example.com/q');
    expect(screen.getByRole('link', { name: 'source' })).toHaveAttribute('href', 'https://example.com/cell');
    expect(screen.queryByText(/\[source\]/)).toBeNull();
  });

  it('renders a hero media reference as a full-bleed background banner', () => {
    const { container } = render(RichDeliverable, {
      title: 'Hero banner',
      content: 'Fallback',
      payload: {
        blocks: [
          {
            type: 'hero',
            title: 'Ainews commentary',
            subtitle: 'Published article recap',
            media: { href: 'https://example.org/banner.jpg', alt: 'Cover banner' },
          },
        ],
      },
    });

    expect(container.querySelector('.rich-hero.has-media')).toBeTruthy();
    expect(screen.getByRole('img', { name: 'Cover banner' })).toHaveAttribute(
      'src',
      'https://example.org/banner.jpg',
    );
  });

  it('does not render a media figure when the hero has no media reference', () => {
    const { container } = render(RichDeliverable, {
      title: 'Hero without media',
      content: 'Fallback',
      payload: { blocks: [{ type: 'hero', title: 'No banner here' }] },
    });

    expect(container.querySelector('.rich-hero.has-media')).toBeNull();
    expect(container.querySelector('.rich-hero img')).toBeNull();
  });

  it('shows "Open full view" and "Open standalone page" only for surface="embedded"', () => {
    const { unmount } = render(RichDeliverable, {
      title: 'Standalone surface',
      content: 'Fallback',
      surface: 'standalone',
      standaloneUrl: '/view',
      payload: { blocks: [{ type: 'markdown', content: 'Body' }] },
    });

    expect(screen.queryByRole('button', { name: 'Open full view' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Open standalone page' })).toBeNull();
    unmount();

    render(RichDeliverable, {
      title: 'Embedded surface',
      content: 'Fallback',
      surface: 'embedded',
      standaloneUrl: '/view',
      payload: { blocks: [{ type: 'markdown', content: 'Body' }] },
    });

    expect(screen.getByRole('button', { name: 'Open full view' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open standalone page' })).toBeTruthy();
  });
});
