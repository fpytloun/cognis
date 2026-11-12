import type { Renderer, Token, Tokens, TokensList } from 'marked';

export interface MarkdownSourceMap {
  source: string;
  html: string;
  text: string;
  /** UTF-16 raw-source interval for each rendered UTF-16 unit; null is generated text. */
  units: ({ start: number; end: number } | null)[];
  barriers: { start: number; end: number }[];
}

/** Track the canonical preprocessor's line-preserving edits, not a second parser. */
export function preprocessingUnits(source: string, rendered: string): (number | null)[] {
  const original = source.split('\n'), changed = rendered.split('\n');
  if (original.length !== changed.length) return Array(rendered.length).fill(null);
  const result: (number | null)[] = [];
  let offset = 0;
  original.forEach((line, index) => {
    const next = changed[index];
    let prefix = 0, suffix = 0;
    while (prefix < line.length && prefix < next.length && line[prefix] === next[prefix]) prefix++;
    while (suffix < line.length - prefix && suffix < next.length - prefix
      && line[line.length - 1 - suffix] === next[next.length - 1 - suffix]) suffix++;
    for (let i = 0; i < next.length; i++) {
      result.push(i < prefix ? offset + i : i >= next.length - suffix ? offset + line.length - next.length + i : null);
    }
    if (index < original.length - 1) result.push(offset + line.length);
    offset += line.length + 1;
  });
  return result;
}

type Located = { start: number; end: number };
type NestedToken = Token & { text?: string; tokens?: Token[]; items?: Token[] };

/** Renderer-local markers are removed before HTML is returned and never trusted from authored DOM. */
export class MarkdownProvenance {
  private positions = new WeakMap<object, Located>();
  private spans: ({ text: string; units: MarkdownSourceMap['units'] })[] = [];
  private nonce = `cognis-map-${crypto.randomUUID()}`;
  private barriers: Located[] = [];
  constructor(private source: string, private normalized: string, tokens: TokensList, private offsets = preprocessingUnits(source, normalized)) {
    this.sequence(tokens, 0, normalized.length);
  }
  private sequence(tokens: Token[], start: number, end: number) {
    let cursor = start;
    for (const token of tokens) {
      const at = this.normalized.indexOf(token.raw, cursor);
      if (at < cursor || at + token.raw.length > end) return;
      this.visit(token as NestedToken, at);
      cursor = at + token.raw.length;
    }
  }
  private visit(token: NestedToken, start: number) {
    const end = start + token.raw.length;
    this.positions.set(token, { start, end });
    if (token.type === 'table') { this.table(token as unknown as Tokens.Table, start); return; }
    if (!['list', 'list_item', 'heading', 'strong', 'del', 'em', 'link', 'paragraph', 'text', 'codespan', 'escape', 'space', 'br'].includes(token.type)) {
      this.block(start, end);
      return;
    }
    if (token.type === 'list' && token.items) { this.sequence(token.items, start, end); return; }
    if (!token.tokens || typeof token.text !== 'string') return;
    let prefix = 0;
    switch (token.type) {
      case 'heading': prefix = token.raw.match(/^ {0,3}#{1,6}[ \t]+/)?.[0].length ?? 0; break;
      case 'strong': case 'del': prefix = 2; break;
      case 'em': prefix = 1; break;
      case 'link': prefix = token.raw.startsWith('[') || token.raw.startsWith('<') ? 1 : 0; break;
      case 'list_item': prefix = token.raw.match(/^ {0,3}(?:[-+*]|\d+[.)])[ \t]+/)?.[0].length ?? 0; break;
      case 'paragraph': case 'text': break;
      // Indentation-stripped blockquotes and other transformed containers stay unmapped.
      default: return;
    }
    if (token.raw.slice(prefix, prefix + token.text.length) !== token.text) { this.block(start, end); return; }
    this.sequence(token.tokens, start + prefix, start + prefix + token.text.length);
  }
  /**
   * Map each cell's inline text on its own source line (header, then body rows after the
   * delimiter row). A cell whose text is not verbatim in its line (an escaped pipe) stays
   * unmapped, so selections may not include it; the rest of the table stays selectable.
   */
  private table(token: Tokens.Table, start: number) {
    const lines: { start: number; end: number }[] = [];
    let at = start;
    for (const line of token.raw.split('\n')) { lines.push({ start: at, end: at + line.length }); at += line.length + 1; }
    const rows = [token.header, ...token.rows];
    rows.forEach((cells, index) => {
      const line = lines[index === 0 ? 0 : index + 1];
      if (!line) return;
      const spans = this.cellSpans(line.start, line.end);
      // Each cell is matched only inside its own source span; an escaped pipe changes the split,
      // so a row whose spans do not match its cells stays unmapped rather than mis-anchored.
      if (spans.length !== cells.length) return;
      cells.forEach((cell, column) => {
        const span = spans[column];
        const found = cell.text ? this.normalized.indexOf(cell.text, span.start) : -1;
        if (found < span.start || found + cell.text.length > span.end) return;
        this.sequence(cell.tokens, found, found + cell.text.length);
      });
    });
  }
  /** Source spans between unescaped pipes of one table row, without the outer pipes. */
  private cellSpans(start: number, end: number): { start: number; end: number }[] {
    const pipes: number[] = [];
    for (let at = start; at < end; at++) {
      if (this.normalized[at] === '\\') { at++; continue; }
      if (this.normalized[at] === '|') pipes.push(at);
    }
    const bounds = [start - 1, ...pipes, end];
    const spans = bounds.slice(1).map((to, index) => ({ start: bounds[index] + 1, end: to }));
    // Leading and trailing pipes leave empty edge spans that are not cells.
    if (spans.length && !this.normalized.slice(spans[0].start, spans[0].end).trim()) spans.shift();
    if (spans.length && !this.normalized.slice(spans.at(-1)!.start, spans.at(-1)!.end).trim()) spans.pop();
    return spans;
  }
  private block(start: number, end: number) {
    const from = this.offsets[start], to = this.offsets[end - 1];
    if (from != null && to != null) this.barriers.push({ start: from, end: to + 1 });
  }
  private rawUnits(start: number, raw: string): MarkdownSourceMap['units'] {
    const result: MarkdownSourceMap['units'] = [];
    for (let i = 0; i < raw.length;) {
      const entity = raw.slice(i).match(/^&(?:#\d+|#x[\da-f]+|[a-z][a-z\d]+);/i)?.[0];
      const chunk = entity ?? raw[i];
      let decoded = chunk;
      if (entity) { const textarea = document.createElement('textarea'); textarea.innerHTML = entity; decoded = textarea.value; }
      const from = this.offsets[start + i], to = this.offsets[start + i + chunk.length - 1];
      const range = from != null && to != null
        && this.offsets.slice(start + i, start + i + chunk.length).every((value, index) => value === from + index)
        ? { start: from, end: to + 1 } : null;
      for (let j = 0; j < decoded.length; j++) result.push(range);
      i += chunk.length;
    }
    return result;
  }
  wrap(renderer: Renderer): void {
    const text = renderer.text.bind(renderer), codespan = renderer.codespan.bind(renderer);
    renderer.text = token => token.type === 'text' && token.tokens ? text(token)
      : this.mark(token, text(token), token.type === 'escape' ? 1 : 0);
    renderer.codespan = token => {
      const delimiter = token.raw.match(/^`+/)?.[0].length ?? 0;
      let body = token.raw.slice(delimiter, -delimiter).replace(/\n/g, ' ');
      let trim = 0;
      if (/^ .* $/.test(body) && /[^ ]/.test(body)) { body = body.slice(1, -1); trim = 1; }
      return body === token.text ? this.mark(token, codespan(token), delimiter + trim, token.text.length) : codespan(token);
    };
  }
  private mark(token: Token, html: string, skip = 0, length = token.raw.length - skip): string {
    const position = this.positions.get(token);
    if (!position) return html;
    const template = document.createElement('template');
    template.innerHTML = html;
    const text = template.content.textContent ?? '';
    const raw = token.raw.slice(skip, skip + length);
    const units = this.rawUnits(position.start + skip, raw);
    const decoded = document.createElement('textarea');
    decoded.innerHTML = raw.replace(/</g, '&lt;');
    // Never infer a map when a parser transformation has not been characterized.
    if (decoded.value.replace(/\n/g, token.type === 'codespan' ? ' ' : '\n') !== text || units.length !== text.length) {
      this.block(position.start, position.end); return html;
    }
    const index = this.spans.push({ text, units }) - 1;
    return `<span data-${this.nonce}="${index}">${html}</span>`;
  }
  finish(html: string): MarkdownSourceMap {
    const template = document.createElement('template');
    template.innerHTML = html;
    const units: MarkdownSourceMap['units'] = [];
    const walker = document.createTreeWalker(template.content, NodeFilter.SHOW_TEXT);
    const used = new Map<Element, number>();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const marker = node.parentElement?.closest(`[data-${this.nonce}]`);
      const span = marker ? this.spans[Number(marker.getAttribute(`data-${this.nonce}`))] : undefined;
      const offset = marker ? used.get(marker) ?? 0 : 0;
      const text = node.textContent ?? '';
      units.push(...(span ? span.units.slice(offset, offset + text.length) : Array(text.length).fill(null)));
      if (marker) used.set(marker, offset + text.length);
    }
    const text = template.content.textContent ?? '';
    // Remove matching closes structurally, avoiding any authored attribute trust.
    for (const marker of template.content.querySelectorAll(`[data-${this.nonce}]`)) marker.replaceWith(...marker.childNodes);
    // Serialization is presentation-only; callers use the map, never this HTML to replace a live body.
    return { source: this.source, html: template.innerHTML, text, units, barriers: this.barriers };
  }
}
