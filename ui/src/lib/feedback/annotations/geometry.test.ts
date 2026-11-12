import { describe, expect, it } from 'vitest';
import { appendStroke, boundedPoint, checkShapeBudget, clientPoint, pdfDisplayPoint, pdfPagePoint, rectangleBetween } from './geometry';

describe('annotation coordinate spaces', () => {
  it('normalizes responsive content without device-pixel scaling', () => {
    expect(clientPoint({ x: 70, y: 45 }, { left: 20, top: 20, width: 100, height: 50 })).toEqual({ x: 0.5, y: 0.5 });
    expect(clientPoint({ x: 120, y: 70 }, { left: 20, top: 20, width: 200, height: 100 })).toEqual({ x: 0.5, y: 0.5 });
    expect(rectangleBetween({ x: 8, y: 9 }, { x: 2, y: 3 }, { width: 10, height: 10 }))
      .toEqual({ x: 2, y: 3, width: 6, height: 6 });
  });

  it.each([0, 90, 180, 270])('round trips unrotated page-space at rotation %d', rotation => {
    const page = { width: 612, height: 792 };
    const display = { x: 0.2, y: 0.8 };
    const restored = pdfDisplayPoint(pdfPagePoint(display, page, rotation), page, rotation);
    expect(restored.x).toBeCloseTo(display.x);
    expect(restored.y).toBeCloseTo(display.y);
  });

  it('rejects nonfinite, outside and degenerate geometry', () => {
    for (const x of [NaN, Infinity, -1, 11]) {
      expect(() => boundedPoint({ x, y: 0 }, { width: 10, height: 10 })).toThrow();
    }
    expect(() => boundedPoint({ x: 0, y: 0 }, { width: 0, height: 10 })).toThrow();
    expect(() => pdfPagePoint({ x: 0, y: 0 }, { width: 10, height: 10 }, 45)).toThrow();
  });

  it('enforces point and shape budgets without truncating a stroke', () => {
    const points = Array.from({ length: 2048 }, () => ({ x: 0, y: 0 }));
    expect(() => appendStroke(points, { x: 1, y: 1 }, { width: 1, height: 1 })).toThrow('2,048');
    expect(points).toHaveLength(2048);
    expect(() => checkShapeBudget(65, [])).toThrow();
    expect(() => checkShapeBudget(5, [2048, 2048, 2048, 2048, 1])).toThrow();
    expect(() => checkShapeBudget(4, [2048, 2048, 2048, 2048])).not.toThrow();
  });
});
