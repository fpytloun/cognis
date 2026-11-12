export interface TextInterval { start: number; end: number }
export function unionIntervals(values: TextInterval[]): TextInterval[] {
  const result: TextInterval[] = [];
  for (const value of values.filter(value => value.end > value.start).sort((a, b) => a.start - b.start || a.end - b.end)) {
    const last = result.at(-1);
    if (last && value.start <= last.end) last.end = Math.max(last.end, value.end);
    else result.push({ ...value });
  }
  return result;
}
interface HighlightRegistry { set(name: string, value: unknown): void; delete(name: string): boolean }
interface HighlightWindow { CSS?: { highlights?: HighlightRegistry }; Highlight?: new (...ranges: Range[]) => unknown }
let sequence = 0;
/** One disposable paint registration, not one tinted element per comment. */
export function paintTextRanges(root: HTMLElement, ranges: Range[]): (() => void) | null {
  const view = root.ownerDocument.defaultView as HighlightWindow | null;
  const registry = view?.CSS?.highlights, Highlight = view?.Highlight;
  if (!registry || !Highlight) return null;
  const valid = ranges.filter(range => !range.collapsed && range.startContainer.isConnected && range.endContainer.isConnected
    && root.contains(range.startContainer) && root.contains(range.endContainer));
  const name = `cognis-feedback-${++sequence}`, style = root.ownerDocument.createElement('style');
  style.dataset.feedbackHighlightStyle = name;
  // Do not set color or ::selection: syntax colors and native selection win.
  style.textContent = `::highlight(${name}) { background-color: color-mix(in srgb, var(--color-accent, #0ea5e9) 20%, transparent); }`;
  root.ownerDocument.head.append(style); registry.set(name, new Highlight(...valid));
  let active = true;
  return () => { if (active) { active = false; registry.delete(name); style.remove(); } };
}
