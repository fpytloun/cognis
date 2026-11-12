/** PDF.js owns the unrotated layer dimensions and rotates it into the viewport. */
export function preparePdfTextLayer(container: HTMLElement, scale: number, userUnit = 1): void {
  container.replaceChildren();
  container.style.setProperty('--scale-factor', String(scale));
  container.style.setProperty('--total-scale-factor', String(scale * userUnit));
  container.style.setProperty('--scale-round-x', '1px');
  container.style.setProperty('--scale-round-y', '1px');
}

/** Prove complete glyph order before mapping across extractor-only whitespace. */
export function mapPdfRange(rendered: string, canonical: string, start: number, end: number) {
  function compact(text: string) {
    const positions: number[] = [];
    for (let index = 0; index < text.length; index++) if (!/\s/.test(text[index])) positions.push(index);
    return { text: positions.map(index => text[index]).join(''), positions };
  }
  const from = compact(rendered), to = compact(canonical);
  if (!from.text || from.text !== to.text) return null;
  const first = from.positions.findIndex(position => position >= start);
  let last = from.positions.findIndex(position => position >= end);
  if (last < 0) last = from.positions.length;
  if (first < 0 || last <= first) return null;
  return { start: to.positions[first], end: to.positions[last - 1] + 1 };
}
