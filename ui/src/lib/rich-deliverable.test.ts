import { describe, expect, it } from 'vitest';

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import {
  neutralChartConfig,
  normalizeRichDeliverable,
  privateDeliverableMediaUrl,
  resolveRichMedia,
  richBlockRenderPlan,
  safeImageUrl,
  safeUrl,
} from './rich-deliverable';
import { richDeliverableVisualScenarios } from './components/rich/rich-deliverable.fixture';
import { workflowToolPresentation } from './tool-call-summary';

const legacyChartKeys = ['data', 'rows', 'series_key', 'x_key', 'y_key', 'variant'];

function fixtureChartBlocks(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return value.flatMap(fixtureChartBlocks);
  if (!value || typeof value !== 'object') return [];
  const record = value as Record<string, unknown>;
  return [
    ...(record.type === 'chart' ? [record] : []),
    ...Object.values(record).flatMap(fixtureChartBlocks),
  ];
}

describe('rich deliverables', () => {
  it('normalizes invalid blocks for graceful fallback', () => {
    const payload = normalizeRichDeliverable({ blocks: [{ type: 'markdown', content: 'Hi' }, 'bad'], sources: 'bad' });
    expect(payload.blocks[0].type).toBe('markdown');
    expect(payload.blocks[1]).toEqual({ type: 'unknown', raw: 'bad' });
    expect(payload.sources).toEqual([]);
  });

  it('rejects executable URLs', () => {
    expect(safeUrl('javascript:alert(1)')).toBe('');
    expect(safeUrl('https://example.com/a')).toBe('https://example.com/a');
  });

  it('resolves only controller manifest media keys through the supplied private resolver', () => {
    const payload = normalizeRichDeliverable({
      media_manifest: {
        media_0123456789abcdef01234567: {
          artifact_ref: 'art_0123456789abcdef',
          mime_type: 'image/png',
        },
      },
      blocks: [
        {
          type: 'section',
          blocks: [{
            type: 'card',
            media: { key: 'media_0123456789abcdef01234567', alt: 'Editorial' },
          }],
        },
        {
          type: 'card',
          media: { key: 'media_ffffffffffffffffffffffff', alt: 'Unknown' },
        },
      ],
    });

    const mediaUrlFor = (mediaKey: string) => privateDeliverableMediaUrl('dlv/private', mediaKey);
    const resolved = resolveRichMedia(payload, mediaUrlFor);
    const nested = resolved.blocks[0].blocks?.[0] as Record<string, unknown>;
    expect(nested.media).toEqual({
      key: 'media_0123456789abcdef01234567',
      alt: 'Editorial',
      src: '/api/v1/deliverables/dlv%2Fprivate/media/media_0123456789abcdef01234567',
    });
    expect(resolved.blocks[1].media).toEqual({
      key: 'media_ffffffffffffffffffffffff',
      alt: 'Unknown',
    });
    expect(JSON.stringify(resolved.blocks)).not.toContain('art_0123456789abcdef');
  });

  it('resolves authorized item media recursively and leaves missing manifest refs inert', () => {
    const authorizedKey = 'media_0123456789abcdef01234567';
    const missingKey = 'media_ffffffffffffffffffffffff';
    const payload = normalizeRichDeliverable({
      media_manifest: {
        [authorizedKey]: { artifact_ref: 'art_0123456789abcdef' },
      },
      blocks: [{
        type: 'accordion',
        items: [
          { type: 'section', media: { key: authorizedKey, alt: 'Authorized article' } },
          { type: 'section', media: { key: missingKey, alt: 'Missing article' } },
        ],
      }],
    });

    const resolved = resolveRichMedia(
      payload,
      (mediaKey) => privateDeliverableMediaUrl('dlv/item-media', mediaKey),
    );
    const items = resolved.blocks[0].items as Record<string, unknown>[];

    expect(items[0].media).toEqual({
      key: authorizedKey,
      alt: 'Authorized article',
      src: `/api/v1/deliverables/dlv%2Fitem-media/media/${authorizedKey}`,
    });
    expect(items[1].media).toEqual({
      key: missingKey,
      alt: 'Missing article',
    });
    expect(JSON.stringify(payload)).not.toContain('/media/');
    expect(JSON.stringify(resolved.blocks)).not.toContain('art_0123456789abcdef');
  });

  it('uses the supplied public resolver without persisting signed media URLs', () => {
    const payload = normalizeRichDeliverable({
      media_manifest: {
        media_0123456789abcdef01234567: { artifact_ref: 'art_0123456789abcdef' },
      },
      blocks: [{ type: 'card', media: { key: 'media_0123456789abcdef01234567' } }],
    });
    const mediaUrlFor = (mediaKey: string) =>
      `/api/v1/deliverables/share/public-token/media/${encodeURIComponent(mediaKey)}?X-Amz-Signature=render-only`;

    const resolved = resolveRichMedia(payload, mediaUrlFor);

    expect((resolved.blocks[0].media as Record<string, unknown>).src).toContain('public-token');
    expect(JSON.stringify(payload)).not.toContain('X-Amz-Signature');
    expect(JSON.stringify(payload)).not.toContain('/media/');
  });

  it('allows inert inline figure SVG and rejects executable SVG', () => {
    const safe = 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22%3E%3Crect width=%2210%22 height=%2210%22/%3E%3C/svg%3E';
    const unsafe = 'data:image/svg+xml,%3Csvg%3E%3Cscript%3Ealert(1)%3C/script%3E%3C/svg%3E';

    expect(safeImageUrl(safe)).toBe(safe);
    expect(safeImageUrl(unsafe)).toBe('');
  });

  it('keeps rich write_deliverable tool-call presentations compact', () => {
    const presentation = workflowToolPresentation({
      toolName: 'write_deliverable',
      status: 'completed',
      arguments: {
        format: 'rich',
        content: 'Fallback',
        rich: { blocks: [{ type: 'chart', rows: [{ label: 'A', value: 1 }] }] },
      },
      result: JSON.stringify({
        status: 'buffered',
        deliverable_id: 'dlv_1',
        version: 1,
        rich: { blocks: [{ type: 'chart' }] },
        render_metadata: { schema: 'cognis.rich_deliverable.v1' },
        export_metadata: { available: ['copy'] },
      }),
    });

    expect(presentation?.kind).toBe('write_deliverable');
    if (presentation?.kind !== 'write_deliverable') throw new Error('expected write deliverable');
    expect(presentation.format).toBe('rich');
    expect(presentation.deliverableId).toBe('dlv_1');
    expect(JSON.stringify(presentation)).not.toContain('chart');
    expect(JSON.stringify(presentation)).not.toContain('cognis.rich_deliverable.v1');
    expect(JSON.stringify(presentation)).not.toContain('Fallback');
  });

  it('converts canonical chart data without accepting raw library callbacks', () => {
    const config = neutralChartConfig({
      type: 'chart',
      spec_version: 'cognis.chart.v1',
      chart_type: 'bar',
      series: [{ id: 'value', label: 'Value', points: [{ x: 'A', y: 42 }] }],
      x_axis: { type: 'category' },
      y_axis: { type: 'linear' },
      options: { onClick: 'ignored' },
    });
    expect(config?.type).toBe('bar');
    expect(config?.data.labels).toEqual(['A']);
    expect(config?.data.datasets[0].data).toEqual([42]);
    expect(JSON.stringify(config)).not.toContain('onClick');
  });

  it('builds a renderer plan for supported blocks and unknown fallback', () => {
    const plan = richBlockRenderPlan({
      blocks: [
        { type: 'section', title: 'Section', blocks: [{ type: 'markdown', content: 'Hi' }] },
        { type: 'future_block', title: 'Future' },
      ],
    });

    expect(plan).toEqual([
      { type: 'section', title: 'Section', fallback: false },
      { type: 'markdown', title: '', fallback: false },
      { type: 'future_block', title: 'Future', fallback: true },
    ]);
  });

  it('keeps visual fixture scenarios on supported rich block types', () => {
    for (const scenario of richDeliverableVisualScenarios) {
      const unsupported = richBlockRenderPlan(scenario.payload).filter((entry) => entry.fallback);
      expect(unsupported, scenario.id).toEqual([]);
    }
  });

  it('keeps every visual fixture chart on the complete canonical chart contract', () => {
    const charts = fixtureChartBlocks(richDeliverableVisualScenarios);

    expect(charts).toEqual([
      {
        type: 'chart',
        title: 'Error rate during mitigation',
        description: 'Synthetic incident curve for validating chart rendering.',
        spec_version: 'cognis.chart.v1',
        chart_type: 'line',
        series: [{
          id: 'error-rate',
          label: 'Error rate',
          points: [
            { x: '10:40', y: 2 },
            { x: '10:50', y: 9 },
            { x: '11:00', y: 6 },
            { x: '11:10', y: 1 },
            { x: '11:20', y: 0 },
          ],
        }],
        x_axis: { type: 'category', label: 'Mitigation time' },
        y_axis: { type: 'linear', label: 'Error rate', unit: '%', min: 0, max: 10 },
        legend_position: 'none',
        palette_token: 'cool',
      },
      {
        type: 'chart',
        title: 'Rich deliverable adoption',
        description: 'Synthetic weekday output mix for validating grouped canonical series.',
        spec_version: 'cognis.chart.v1',
        chart_type: 'grouped_bar',
        series: [
          {
            id: 'rich-deliverables',
            label: 'Rich deliverables',
            points: [
              { x: 'Mon', y: 14 },
              { x: 'Tue', y: 21 },
              { x: 'Wed', y: 27 },
              { x: 'Thu', y: 35 },
              { x: 'Fri', y: 49 },
            ],
          },
          {
            id: 'markdown-only',
            label: 'Markdown-only',
            points: [
              { x: 'Mon', y: 82 },
              { x: 'Tue', y: 76 },
              { x: 'Wed', y: 68 },
              { x: 'Thu', y: 57 },
              { x: 'Fri', y: 43 },
            ],
          },
        ],
        x_axis: { type: 'category', label: 'Weekday' },
        y_axis: { type: 'linear', label: 'Outputs', unit: 'deliverables', min: 0, max: 100 },
        stack: false,
        legend_position: 'bottom',
        palette_token: 'default',
      },
      {
        type: 'chart',
        title: 'Validation coverage',
        description: 'Covered validation paths in the implementation fixture.',
        spec_version: 'cognis.chart.v1',
        chart_type: 'bar',
        series: [{
          id: 'coverage',
          label: 'Coverage',
          points: [{ x: 'Covered paths', y: 3 }],
        }],
        x_axis: { type: 'category', label: 'Validation status' },
        y_axis: { type: 'linear', label: 'Paths', unit: 'paths', min: 0, max: 4 },
        legend_position: 'none',
        palette_token: 'default',
      },
      {
        type: 'chart',
        title: 'Visual system adoption',
        description: 'The SVG baseline remains useful before Chart.js enhancement.',
        spec_version: 'cognis.chart.v1',
        chart_type: 'line',
        series: [{
          id: 'adopted-surfaces',
          label: 'Adopted surfaces',
          points: [
            { x: '2026-07-13', y: 2 },
            { x: '2026-07-14', y: 4 },
            { x: '2026-07-15', y: 7 },
          ],
        }],
        x_axis: { type: 'time', label: 'Weekday' },
        y_axis: { type: 'linear', label: 'Surfaces', unit: 'surfaces', min: 0, max: 8 },
        legend_position: 'none',
        palette_token: 'cool',
      },
      {
        type: 'chart',
        title: 'Zoomies by weekday',
        description: 'A minimal line chart for design-system QA.',
        spec_version: 'cognis.chart.v1',
        chart_type: 'line',
        series: [{
          id: 'zoomies',
          label: 'Zoomie intensity',
          points: [
            { x: 'Mon', y: 1 },
            { x: 'Tue', y: 3 },
            { x: 'Wed', y: 2 },
          ],
        }],
        x_axis: { type: 'category', label: 'Weekday' },
        y_axis: { type: 'linear', label: 'Intensity', unit: 'laps', min: 0, max: 4 },
        legend_position: 'none',
        palette_token: 'default',
        source: 'The Institute of Extremely Serious Cat Science',
        observed_at: '2026-07-16T08:00:00+00:00',
      },
    ]);
    for (const chart of charts) {
      expect(chart.spec_version, String(chart.title)).toBe('cognis.chart.v1');
      expect([
        'line',
        'area',
        'bar',
        'grouped_bar',
        'stacked_bar',
        'sparkline',
        'progress',
        'range',
        'donut',
      ]).toContain(chart.chart_type);
      expect(chart.title, String(chart.title)).toEqual(expect.any(String));
      expect(chart.description, String(chart.title)).toEqual(expect.any(String));
      expect(chart.legend_position, String(chart.title)).toMatch(/^(top|right|bottom|none)$/);
      expect(chart.palette_token, String(chart.title)).toMatch(/^(default|cool|warm|categorical)$/);
      expect(chart.x_axis, String(chart.title)).toEqual(expect.objectContaining({ type: expect.any(String), label: expect.any(String) }));
      expect(chart.y_axis, String(chart.title)).toEqual(expect.objectContaining({
        type: 'linear',
        label: expect.any(String),
        unit: expect.any(String),
        min: expect.any(Number),
        max: expect.any(Number),
      }));

      const series = chart.series as Array<Record<string, unknown>>;
      expect(series.length, String(chart.title)).toBeGreaterThan(0);
      for (const item of series) {
        expect(item).toEqual(expect.objectContaining({
          id: expect.any(String),
          label: expect.any(String),
          points: expect.any(Array),
        }));
        const points = item.points as Array<Record<string, unknown>>;
        expect(points.length, String(chart.title)).toBeGreaterThan(0);
        for (const point of points) {
          expect(point).toEqual(expect.objectContaining({
            x: expect.any(String),
            y: expect.any(Number),
          }));
        }
      }
      for (const key of legacyChartKeys) expect(chart).not.toHaveProperty(key);
      const config = neutralChartConfig(chart);
      expect(config, String(chart.title)).not.toBeNull();
      expect(config?.data.datasets.length, String(chart.title)).toBe(series.length);
    }
  });

  it('supports kind aliases used by direct smoke tests and model output', () => {
    const plan = richBlockRenderPlan({
      blocks: [
        { kind: 'kv', title: 'Facts', items: [{ label: 'Scope', value: 'conversation' }] },
        { kind: 'chart', data: [{ label: 'A', value: 1 }] },
      ],
    });

    expect(plan).toEqual([
      { type: 'kv', title: 'Facts', fallback: false },
      { type: 'chart', title: '', fallback: false },
    ]);
    expect(neutralChartConfig({ kind: 'chart', data: [{ label: 'A', value: 1 }] })).toBeNull();
  });

  it('keeps user-facing controls and unknown fallback markup in the component', () => {
    const source = readFileSync(resolve(process.cwd(), 'src/lib/components/rich/RichDeliverable.svelte'), 'utf8');
    const blockSource = readFileSync(resolve(process.cwd(), 'src/lib/components/rich/blocks/UnsupportedBlock.svelte'), 'utf8');

    expect(source).toContain('Open full view');
    expect(source).not.toContain('Raw/debug');
    expect(blockSource).toContain('Unsupported block: {type}');
  });
});
