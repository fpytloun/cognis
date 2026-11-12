import { describe, expect, it } from 'vitest';
import { htmlBridgeScript, readObservation } from './html-bridge';

const instance = 'selection-frame';
const observation = () => ({ type: 'cognis-selection', instance, selector: 'html:nth-of-type(1) > body:nth-of-type(1)', element_key: null, quote: 'Selected text',
  selection_kind: 'text', start: 0, end: 13, rects: [{ left: 12, top: 20, width: 80, height: 24 }], scopes: [],
  viewport: { width_css_px: 800, height_css_px: 600, scroll_x_css_px: 0, scroll_y_css_px: 100, device_pixel_ratio: 2 } });
describe('untrusted opaque frame observation protocol', () => {
  it('accepts only bounded observations, never command or persistence fields', () => {
    expect(readObservation(observation(), instance)?.quote).toBe('Selected text');
    expect(readObservation({ ...observation(), destination: { kind: 'origin_chat' } }, instance)).toBeNull();
    expect(readObservation({ ...observation(), type: 'dispatch' }, instance)).toBeNull();
    expect(readObservation(observation(), 'replacement-frame')).toBeNull();
    expect(readObservation({ ...observation(), quote: 'x'.repeat(2049) }, instance)).toBeNull();
    expect(readObservation({ ...observation(), selector: 'a'.repeat(2049) }, instance)).toBeNull();
    expect(readObservation(null, instance)).toBeNull();
  });
  it('rejects nonfinite, negative, oversized or incomplete viewport claims', () => {
    for (const value of [Infinity, NaN, -1, 32769]) {
      const observationValue = observation(); observationValue.viewport.width_css_px = value;
      expect(readObservation(observationValue, instance)).toBeNull();
    }
    expect(readObservation({ ...observation(), viewport: {} }, instance)).toBeNull();
  });
  it('does not interpolate an instance string into executable markup', () => {
    const script = htmlBridgeScript('</script><script>alert(1)</script>');
    expect(script.match(/<script>/g)).toHaveLength(1);
    expect(script.match(/<\/script>/g)).toHaveLength(1);
    expect(script).toContain('event.source !== parent');
  });
});
