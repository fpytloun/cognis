import { describe, expect, it } from 'vitest';
import { mapShapes } from './shapes';
import { pdfDisplayPoint, pdfPagePoint } from './geometry';
import type { FeedbackShape } from '../types';
describe('geometry projection without mutation', () => {
  const shapes: FeedbackShape[] = [
    { kind: 'pin', point: { x: 0.5, y: 0.5 } }, { kind: 'rectangle', x: 0.2, y: 0.3, width: 0.2, height: 0.4 },
    { kind: 'ellipse', x: 0.2, y: 0.3, width: 0.2, height: 0.4 },
    { kind: 'arrow', from: { x: 0, y: 0 }, to: { x: 1, y: 1 } }, { kind: 'freehand', points: [{ x: 0, y: 0 }, { x: 1, y: 1 }] },
  ];
  it.each([0, 90, 180, 270])('roundtrips every shape through PDF page points at %d degrees', rotation => {
    const original = JSON.stringify(shapes), page = { width: 600, height: 800 };
    const mapped = mapShapes(shapes, point => pdfPagePoint(point, page, rotation));
    const restored = mapShapes(mapped, point => pdfDisplayPoint(point, page, rotation));
    restored.forEach((shape, i) => {
      if (shape.kind === 'rectangle' || shape.kind === 'ellipse') {
        const expected = shapes[i] as typeof shape;
        expect(shape.x).toBeCloseTo(expected.x); expect(shape.y).toBeCloseTo(expected.y);
        expect(shape.width).toBeCloseTo(expected.width); expect(shape.height).toBeCloseTo(expected.height);
      } else expect(shape).toEqual(shapes[i]);
    });
    expect(JSON.stringify(shapes)).toBe(original);
  });
});
