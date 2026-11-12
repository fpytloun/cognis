import type { FeedbackLocator, FeedbackShape } from './types';
import { checkBounds } from './annotations/geometry';
import { mapShapes } from './annotations/shapes';

/** Historical HTML shapes are CSS-document coordinates, not raster pixels. */
export function savedCaptureShapes(locator: Extract<FeedbackLocator, { kind: 'html' }>): FeedbackShape[] {
  const viewport = locator.viewport;
  checkBounds({ width: viewport.width_css_px, height: viewport.height_css_px });
  return mapShapes(locator.shapes, point => ({
    x: (point.x - viewport.scroll_x_css_px) / viewport.width_css_px,
    y: (point.y - viewport.scroll_y_css_px) / viewport.height_css_px,
  }));
}
