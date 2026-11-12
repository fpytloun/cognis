import { describe, expect, it } from 'vitest';
import { savedCaptureShapes } from './saved-capture';
import type { FeedbackLocator } from './types';

describe('saved HTML observation coordinates', () => {
  it('subtracts document scroll and normalizes independently of unequal raster dimensions', () => {
    const locator: Extract<FeedbackLocator, { kind: 'html' }> = {
      kind: 'html', element_key: null, selector_path: null, range: null,
      viewport: { width_css_px: 800, height_css_px: 600, scroll_x_css_px: 120, scroll_y_css_px: 900, device_pixel_ratio: 2 },
      render_state_digest: 'a'.repeat(64), capture_id: null,
      shapes: [
        { kind: 'rectangle', x: 320, y: 1050, width: 400, height: 300 },
        { kind: 'pin', point: { x: 520, y: 1200 } },
        { kind: 'arrow', from: { x: 120, y: 900 }, to: { x: 920, y: 1500 } },
      ],
    };
    const shapes = savedCaptureShapes(locator);
    expect(shapes).toEqual([
      { kind: 'rectangle', x: 0.25, y: 0.25, width: 0.5, height: 0.5 },
      { kind: 'pin', point: { x: 0.5, y: 0.5 } },
      { kind: 'arrow', from: { x: 0, y: 0 }, to: { x: 1, y: 1 } },
    ]);
    // A nonuniform 1600x900 raster maps the normalized center to its true center.
    const pin = shapes[1];
    if (pin.kind !== 'pin') throw new Error('Expected transformed pin');
    expect({ x: pin.point.x * 1600, y: pin.point.y * 900 }).toEqual({ x: 800, y: 450 });
    expect(locator.shapes[0]).toEqual({ kind: 'rectangle', x: 320, y: 1050, width: 400, height: 300 });
  });
});
