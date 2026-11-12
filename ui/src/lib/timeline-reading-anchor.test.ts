import { describe, expect, it } from 'vitest';
import {
  captureTimelineReadingAnchor, protectTimelineReadingWindow,
  timelinePagingDirection, timelineReadingScrollTop,
  type TimelineReadingAnchor,
} from './timeline-reading-anchor';

function bounds(node: HTMLElement, top: number, height: number): void {
  node.getBoundingClientRect = () => ({ top, bottom: top + height, height } as DOMRect);
}

function fixture() {
  const viewport = document.createElement('div');
  bounds(viewport, 100, 500);
  const row = document.createElement('div');
  row.dataset.timelineRowKey = 'tool-group:session:101';
  row.dataset.timelineItemIds = JSON.stringify(['item:101', 'item:102']);
  bounds(row, 100, 40);
  viewport.append(row);
  return { viewport, row };
}

describe('canonical timeline reading anchors', () => {
  it('restores a collapsed group after prepend replaces its first-member key', () => {
    const { viewport, row } = fixture();
    const anchor = captureTimelineReadingAnchor(viewport)!;
    row.dataset.timelineRowKey = 'tool-group:session:51';
    row.dataset.timelineItemIds = JSON.stringify(['item:51', 'item:101', 'item:102']);
    bounds(row, 800, 40);
    expect(timelineReadingScrollTop(viewport, anchor)).toBe(700);
  });

  it('captures a tall last message crossing the viewport top', () => {
    const { viewport, row } = fixture();
    const leaf = document.createElement('div');
    leaf.dataset.timelineItemId = 'item:101';
    row.append(leaf);
    bounds(row, -900, 2_000);
    bounds(leaf, -900, 2_000);
    const anchor = captureTimelineReadingAnchor(viewport)!;
    expect(anchor.points[0]).toEqual({ ids: ['item:101'], top: -1_000, leaf: true });
    bounds(leaf, -1_300, 2_000);
    viewport.scrollTop = 500;
    expect(timelineReadingScrollTop(viewport, anchor)).toBe(100);
  });

  it('uses the nested canonical item, not the expanded group header', () => {
    const { viewport, row } = fixture();
    const leaf = document.createElement('div');
    leaf.dataset.timelineItemId = 'item:102';
    row.append(leaf);
    bounds(row, -400, 2_000);
    bounds(leaf, 110, 600);
    const anchor = captureTimelineReadingAnchor(viewport)!;
    bounds(row, 0, 2_000);
    bounds(leaf, 810, 600);
    expect(timelineReadingScrollTop(viewport, anchor)).toBe(700);
  });

  it('does not apply a stale height delta when canonical content is gone', () => {
    const { viewport } = fixture();
    const anchor = captureTimelineReadingAnchor(viewport)!;
    viewport.replaceChildren();
    expect(timelineReadingScrollTop(viewport, anchor)).toBeNull();
  });

  it('does not request a scroll write for an unchanged reading position', () => {
    const { viewport } = fixture();
    expect(timelineReadingScrollTop(viewport, captureTimelineReadingAnchor(viewport))).toBeNull();
  });

  it('handles Safari top and bottom overscroll without false direction reversals', () => {
    expect(timelinePagingDirection(-40, 0, 1_000)).toBe(0);
    expect(timelinePagingDirection(0, -40, 1_000)).toBe(0);
    expect(timelinePagingDirection(1_000, 1_040, 1_000)).toBe(0);
    expect(timelinePagingDirection(0, 50, 1_000)).toBe(-1);
    expect(timelinePagingDirection(100, 50, 1_000)).toBe(1);
  });
});

describe('bounded reading-window protection', () => {
  const items = Array.from({ length: 1_000 }, (_, index) => ({ id: `item:${index}` }));
  const anchor = (ids: number[]): TimelineReadingAnchor => ({
    key: 'message:assistant:item:200', top: 0, scrollTop: 0,
    points: ids.map(index => ({ ids: [`item:${index}`], top: 0, leaf: true })),
  });

  it('moves the eviction boundary instead of evicting a visible long message', () => {
    expect(protectTimelineReadingWindow({ start: 350, end: 500 }, items, anchor([200])))
      .toEqual({ start: 200, end: 350 });
  });

  it('does not grow to include an entire giant group', () => {
    const reading = anchor([200]);
    reading.points.unshift({ ids: items.slice(0, 450).map(item => item.id), top: 0, leaf: false });
    const window = protectTimelineReadingWindow({ start: 350, end: 500 }, items, reading);
    expect(window.end! - window.start).toBe(150);
  });

  it('protects a representative of each visible collapsed group during eviction', () => {
    const reading = anchor([]);
    reading.points = [{ ids: items.slice(10, 41).map(item => item.id), top: 0, leaf: false }];
    expect(protectTimelineReadingWindow({ start: 50, end: 200 }, items, reading))
      .toEqual({ start: 40, end: 190 });
    const result = protectTimelineReadingWindow({ start: 0, end: 150 }, items, {
      ...reading, points: [{ ids: items.slice(180, 201).map(item => item.id), top: 200, leaf: false }],
    });
    expect(result).toEqual({ start: 31, end: 181 });
  });

  it('retains the existing visible interval even if it exceeds the target size', () => {
    const result = protectTimelineReadingWindow({ start: 500, end: 650 }, items, anchor([300, 600]));
    expect(result).toEqual({ start: 300, end: 601 });
  });

  it('preserves the live tail only when protection still reaches it', () => {
    expect(protectTimelineReadingWindow({ start: 900, end: null }, items, anchor([950])))
      .toEqual({ start: 900, end: null });
    expect(protectTimelineReadingWindow({ start: 900, end: null }, items, anchor([800])))
      .toEqual({ start: 800, end: 900 });
  });
});
