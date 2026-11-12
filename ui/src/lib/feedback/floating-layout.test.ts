import { describe, expect, it } from 'vitest';
import { authorInitials, avatarClusters, floatingPlacement, intersects, targetPaint, unionRects, MARKER_HIT, MARKER_SIZE } from './floating-layout';
const bounds = { left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800 };
const quote = { left: 300, top: 200, width: 160, height: 20 };
const card = { width: 344, height: 240 };
const gap = (a: typeof quote, b: typeof quote) => Math.hypot(
  Math.max(0, b.left - (a.left + a.width), a.left - (b.left + b.width)), Math.max(0, b.top - (a.top + a.height), a.top - (b.top + b.height)));
const covered = (placed: { left: number; top: number; width: number; height: number }, rects: typeof quote[]) => rects.some(rect => intersects(rect, placed));

describe('shared floating layout', () => {
  it('never covers the target and stays next to it when nothing else is in the way', () => {
    const rects = [quote, { ...quote, top: 224 }];
    const placed = floatingPlacement(bounds, rects, card, false);
    expect(covered(placed, rects)).toBe(false);
    expect(gap(placed, { left: 300, top: 200, width: 160, height: 44 })).toBeLessThanOrEqual(24);
  });

  it('does not cover surrounding text lines or the message composer when a clear area exists', () => {
    // A paragraph column with the selected phrase mid-line and a composer at the bottom.
    const lines = Array.from({ length: 6 }, (_, index) => ({ left: 100, top: 160 + index * 24, width: 560, height: 20 }));
    const composer = { left: 100, top: 720, width: 880, height: 60 };
    const target = { left: 260, top: 184, width: 120, height: 20 };
    const placed = floatingPlacement(bounds, [target], card, false, [...lines, composer]);
    expect(covered(placed, [target, ...lines, composer])).toBe(false);
    expect(gap(placed, target)).toBeLessThanOrEqual(160);
  });

  it('never covers the selected line and stays near it instead of jumping to a far clear corner', () => {
    // The selection's line spans the whole width; the only fully clear spot is a far corner.
    const line = { left: 100, top: 600, width: 900, height: 20 };
    const ink = [line, ...Array.from({ length: 22 }, (_, index) => ({ left: 100, top: 72 + index * 24, width: 900, height: 20 }))];
    const target = { left: 260, top: 600, width: 150, height: 20 };
    const placed = floatingPlacement(bounds, [target], { width: 344, height: 200 }, false, ink);
    expect(intersects(placed, line)).toBe(false);
    // Clear space exists only at the top of the view (y < 72); the card stays next to the line.
    expect(gap(placed, target)).toBeLessThanOrEqual(24);
  });

  it('minimises covered ink when no position is fully clear', () => {
    const full = { left: 0, top: 0, right: 1000, bottom: 800, width: 1000, height: 800 };
    const lines = Array.from({ length: 30 }, (_, index) => ({ left: 0, top: index * 26, width: index < 10 ? 1000 : 300, height: 20 }));
    const placed = floatingPlacement(full, [{ left: 40, top: 52, width: 80, height: 20 }], card, false, lines);
    expect(placed.left).toBeGreaterThanOrEqual(300);
  });

  it('keeps a clear previous offset so typing does not move the card', () => {
    const first = floatingPlacement(bounds, [quote], card, false);
    const moved = floatingPlacement(bounds, [{ ...quote, top: quote.top + 40 }], { ...card, height: 260 }, false, [], first.offset);
    expect(moved.left - quote.left).toBe(first.offset!.dx);
    expect(moved.top - (quote.top + 40)).toBe(first.offset!.dy);
  });

  it('docks when the reader scrolls the target out of view and fits a narrow keyboard viewport', () => {
    expect(floatingPlacement(bounds, [{ ...quote, top: -100 }], card, false).docked).toBe(true);
    expect(floatingPlacement({ ...bounds, right: 360, bottom: 380, width: 360, height: 380 }, [quote], { width: 344, height: 220 }, true))
      .toMatchObject({ top: 160, height: 220, width: 344 });
  });

  it('paints dense structural ranges as one region and flowing text per line', () => {
    const cells = [0, 1, 2].flatMap(row => [{ left: 100, top: 100 + row * 40, width: 340, height: 38 }, { left: 442, top: 100 + row * 40, width: 400, height: 38 }]);
    expect(targetPaint(cells)).toEqual({ block: true, rects: [{ left: 100, top: 100, width: 742, height: 118 }] });
    const text = [{ left: 400, top: 100, width: 300, height: 20 }, { left: 100, top: 124, width: 120, height: 20 }];
    expect(targetPaint(text)).toEqual({ block: false, rects: text });
  });

  it('places markers on the target line in the first gap without ink, and clusters identical targets', () => {
    const paints = Array.from({ length: 12 }, () => ({ ...quote }));
    expect(unionRects(paints)).toEqual([quote]);
    const targets = paints.map((rect, index) => ({ id: String(index), author: 'Ada Lovelace', guest: false, label: 'Comment', rects: [rect] }));
    const rest = { left: 460, top: 200, width: 200, height: 20 };
    const clusters = avatarClusters(targets, bounds, [quote, rest]);
    expect(clusters).toHaveLength(1); expect(clusters[0].ids).toHaveLength(12);
    const marker = { left: clusters[0].left + (MARKER_HIT - MARKER_SIZE) / 2, top: clusters[0].top + (MARKER_HIT - MARKER_SIZE) / 2, width: MARKER_SIZE, height: MARKER_SIZE };
    expect(marker.left).toBeGreaterThanOrEqual(rest.left + rest.width);
    expect(marker.top + marker.height / 2).toBe(quote.top + quote.height / 2);
    expect(authorInitials('Ada Lovelace')).toBe('AL');
    const blocked = Array.from({ length: 50 }, (_, index) => ({ left: index * 20, top: 195, width: 20, height: 30 }));
    expect(avatarClusters(targets, bounds, blocked)).toEqual([]);
  });
});
