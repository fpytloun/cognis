import type { FeedbackPoint, FeedbackShape } from '$lib/feedback/types';
/** Axis-preserving transform (scale/translate or multiples of 90 degrees). */
export function mapShapes(shapes: readonly FeedbackShape[], transform: (point: FeedbackPoint) => FeedbackPoint): FeedbackShape[] {
  return shapes.map(shape => {
    if (shape.kind === 'pin') return { ...shape, point: transform(shape.point) };
    if (shape.kind === 'arrow') return { ...shape, from: transform(shape.from), to: transform(shape.to) };
    if (shape.kind === 'freehand') return { ...shape, points: shape.points.map(transform) };
    const from = transform({ x: shape.x, y: shape.y });
    const to = transform({ x: shape.x + shape.width, y: shape.y + shape.height });
    return { ...shape, x: Math.min(from.x, to.x), y: Math.min(from.y, to.y), width: Math.abs(from.x - to.x), height: Math.abs(from.y - to.y) };
  });
}
