import { fireEvent, render as renderComponent, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import RichDeliverable from './RichDeliverable.svelte';
import { richDeliverableDataScenario } from './rich-deliverable-data.fixture';

function render(_: typeof RichDeliverable, props: { payload: unknown; surface?: 'embedded' | 'standalone' } & Record<string, unknown>) {
  return renderComponent(RichDeliverable, { surface: 'standalone', ...props });
}

vi.mock('chart.js/auto', () => ({
  default: class ChartMock {
    data = { labels: [], datasets: [] };
    options = {};
    config = { type: 'line' };

    constructor(_canvas: HTMLCanvasElement, config: { type: string; data: unknown; options: unknown }) {
      this.config.type = config.type;
      this.data = config.data as typeof this.data;
      this.options = config.options ?? {};
    }

    update() {}
    destroy() {}
    getElementsAtEventForMode() {
      return [{ index: 0, datasetIndex: 0 }];
    }
  },
}));

describe('RichDeliverable data/dashboard rendering', () => {
  it('renders dashboard, chart controls, and incident checklist without unsupported fallback', () => {
    render(RichDeliverable, {
      title: richDeliverableDataScenario.title,
      content: richDeliverableDataScenario.content,
      payload: richDeliverableDataScenario.payload,
    });

    expect(screen.getAllByText('Service health summary').length).toBeGreaterThan(0);
    expect(screen.getByText('Availability')).toBeTruthy();
    expect(screen.getByRole('button', { name: '7D' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Requests' })).toBeTruthy();
    expect(screen.getAllByText('Retry storm remediation').length).toBeGreaterThan(0);
    expect(screen.getByText('Remediation checklist')).toBeTruthy();
    expect(screen.queryByText(/Unsupported block:/)).toBeNull();
  });

  it('supports renderer-owned chart and disclosure interactions', async () => {
    render(RichDeliverable, {
      title: richDeliverableDataScenario.title,
      content: richDeliverableDataScenario.content,
      payload: richDeliverableDataScenario.payload,
    });

    await fireEvent.click(screen.getByRole('button', { name: '30D' }));
    expect(screen.getByRole('button', { name: '30D' })).toHaveAttribute('aria-pressed', 'true');

    await fireEvent.click(screen.getByRole('button', { name: 'Errors' }));
    expect(screen.getByRole('button', { name: 'Errors' })).toHaveAttribute('aria-pressed', 'false');

    await Promise.resolve();
    const chartArea = screen.getByRole('button', { name: 'Interactive chart area' });
    await fireEvent.mouseMove(chartArea, { clientX: 10, clientY: 10 });
    expect(screen.getByTestId('rich-chart-tooltip')).toBeTruthy();
    await fireEvent.click(chartArea, { clientX: 10, clientY: 10 });
    expect(screen.getByTestId('rich-chart-pinned')).toBeTruthy();

    await fireEvent.click(screen.getAllByText('Details')[0]);
    expect(screen.getByText('API gateway: 99.98%')).toBeTruthy();

    await fireEvent.click(screen.getByText('Traffic shifted'));
    expect(screen.getByText('Requests were shifted away from the degraded provider while queue depth drained.')).toBeTruthy();
  });

  it.each(['top', 'right', 'bottom', 'none'] as const)(
    'honors the %s canonical chart legend position',
    (legendPosition) => {
      render(RichDeliverable, {
        content: 'Chart fallback',
        payload: {
          blocks: [{
            type: 'chart',
            spec_version: 'cognis.chart.v1',
            chart_type: 'line',
            series: [
              { id: 'first', label: 'First', points: [{ x: 'A', y: 1 }] },
              { id: 'second', label: 'Second', points: [{ x: 'A', y: 2 }] },
            ],
            legend_position: legendPosition,
          }],
        },
      });

      const legend = screen.queryByRole('group', { name: 'Chart series' });
      if (legendPosition === 'none') {
        expect(legend).toBeNull();
      } else {
        expect(legend?.parentElement).toHaveClass(`legend-${legendPosition}`);
        expect(screen.getByRole('button', { name: 'First' })).toBeTruthy();
        expect(screen.getByRole('button', { name: 'Second' })).toBeTruthy();
      }
    },
  );

  it('renders donut category labels in the canonical legend', () => {
    render(RichDeliverable, {
      content: 'Donut fallback',
      payload: {
        blocks: [{
          type: 'chart',
          spec_version: 'cognis.chart.v1',
          chart_type: 'donut',
          series: [{
            id: 'share',
            label: 'Share',
            points: [{ x: 'Chrome', y: 55 }, { x: 'Safari', y: 45 }],
          }],
          legend_position: 'bottom',
          palette_token: 'categorical',
        }],
      },
    });

    const legend = screen.getByRole('group', { name: 'Chart series' });
    expect(legend.querySelector('[data-chart-category="Chrome"]')?.getAttribute('style')).toContain('#38bdf8');
    expect(legend.querySelector('[data-chart-category="Safari"]')?.getAttribute('style')).toContain('#34d399');
  });
});

