import { describe, expect, it } from 'vitest';
import { mapPdfRange } from './pdf-layer';
describe('PDF layer canonical mapping', () => {
  it('maps repeated text by exact glyph position across extraction whitespace', () => {
    expect(mapPdfRange('SameSame', 'Same\nSame', 4, 8)).toEqual({ start: 5, end: 9 });
  });
  it('maps beyond the first canonical window and rejects any reordered or missing glyph', () => {
    const lead = 'x'.repeat(32768);
    expect(mapPdfRange(`${lead}End`, `${lead}\nEnd`, 32768, 32771)).toEqual({ start: 32769, end: 32772 });
    expect(mapPdfRange('ABC', 'ACB', 0, 1)).toBeNull();
    expect(mapPdfRange('', 'Scanned', 0, 1)).toBeNull();
  });
});
