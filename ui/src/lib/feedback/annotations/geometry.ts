type Point = Readonly<{ x: number; y: number }>;
type Bounds = Readonly<{ width: number; height: number }>;

function finite(...values: number[]): void {
  if (!values.every(Number.isFinite)) throw new Error('Annotation coordinates must be finite.');
}

export function checkBounds(bounds: Bounds): void {
  finite(bounds.width, bounds.height);
  if (bounds.width <= 0 || bounds.height <= 0) throw new Error('Annotation dimensions must be positive.');
}

export function boundedPoint(point: Point, bounds: Bounds): Point {
  checkBounds(bounds);
  finite(point.x, point.y);
  if (point.x < 0 || point.y < 0 || point.x > bounds.width || point.y > bounds.height) {
    throw new Error('The annotation is outside the content.');
  }
  return { x: point.x, y: point.y };
}

export function normalizedPoint(point: Point, bounds: Bounds): Point {
  boundedPoint(point, bounds);
  return { x: point.x / bounds.width, y: point.y / bounds.height };
}

export function rectangleBetween(from: Point, to: Point, bounds: Bounds) {
  boundedPoint(from, bounds); boundedPoint(to, bounds);
  return {
    x: Math.min(from.x, to.x), y: Math.min(from.y, to.y),
    width: Math.abs(to.x - from.x), height: Math.abs(to.y - from.y),
  };
}

/** Coordinates relative to the actual content rect, not an object-fit letterbox. */
export function clientPoint(point: Point, rect: { left: number; top: number; width: number; height: number }): Point {
  finite(rect.left, rect.top);
  return normalizedPoint({ x: point.x - rect.left, y: point.y - rect.top }, rect);
}

/** Unrotated top-left page-space points; device-pixel ratio never enters this transform. */
export function pdfPagePoint(point: Point, page: Bounds, rotation: number): Point {
  boundedPoint(point, { width: 1, height: 1 });
  checkBounds(page);
  switch (rotation) {
    case 0: return { x: point.x * page.width, y: point.y * page.height };
    case 90: return { x: point.y * page.width, y: (1 - point.x) * page.height };
    case 180: return { x: (1 - point.x) * page.width, y: (1 - point.y) * page.height };
    case 270: return { x: (1 - point.y) * page.width, y: point.x * page.height };
    default: throw new Error('Unsupported PDF page rotation.');
  }
}

export function pdfDisplayPoint(point: Point, page: Bounds, rotation: number): Point {
  const normalized = normalizedPoint(point, page);
  switch (rotation) {
    case 0: return normalized;
    case 90: return { x: 1 - normalized.y, y: normalized.x };
    case 180: return { x: 1 - normalized.x, y: 1 - normalized.y };
    case 270: return { x: normalized.y, y: 1 - normalized.x };
    default: throw new Error('Unsupported PDF page rotation.');
  }
}

/** Exceeding a stroke budget is visible, not silently decimated. */
export function appendStroke(points: readonly Point[], point: Point, bounds: Bounds): Point[] {
  boundedPoint(point, bounds);
  if (points.length >= 2048) throw new Error('A stroke may contain at most 2,048 points. Finish it and start another.');
  return [...points, { x: point.x, y: point.y }];
}

export function checkShapeBudget(shapeCount: number, pointCounts: readonly number[]): void {
  if (!Number.isSafeInteger(shapeCount) || shapeCount < 1 || shapeCount > 64
    || pointCounts.some(count => !Number.isSafeInteger(count) || count < 1 || count > 2048)
    || pointCounts.reduce((sum, count) => sum + count, 0) > 8192) {
    throw new Error('The annotation exceeds the shape or point limit.');
  }
}
