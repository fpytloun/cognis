const encoder = new TextEncoder();

export function canonicalText(text: string): string {
  return text.replace(/\r\n?/g, '\n');
}

export function isTextBoundary(text: string, offset: number): boolean {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > text.length) return false;
  if (offset === 0 || offset === text.length) return true;
  return !(text.charCodeAt(offset - 1) >= 0xd800 && text.charCodeAt(offset - 1) <= 0xdbff
    && text.charCodeAt(offset) >= 0xdc00 && text.charCodeAt(offset) <= 0xdfff);
}

function contextSlice(text: string, start: number, end: number): string {
  if (!isTextBoundary(text, start)) start += 1;
  if (!isTextBoundary(text, end)) end -= 1;
  return text.slice(start, end);
}

export function textPosition(text: string, offset: number) {
  if (!isTextBoundary(text, offset)) throw new Error('The selection splits a character or exceeds the available text.');
  const before = text.slice(0, offset);
  return { line: before.split('\n').length, column: offset - before.lastIndexOf('\n') };
}

/** Offsets are UTF-16 units in LF-normalized, unformatted source text. */
export function selectText(text: string, start: number, end: number, available?: readonly { start: number; end: number }[]) {
  if (text.includes('\r')) throw new Error('Selections require LF-normalized canonical text.');
  if (!isTextBoundary(text, start) || !isTextBoundary(text, end) || end <= start) {
    throw new Error('Select a complete text range.');
  }
  // One available interval must contain the whole selection; missing gaps are not selectable.
  if (available && !available.some(range => start >= range.start && end <= range.end)) {
    throw new Error('The selection includes text unavailable in this preview.');
  }
  const exact = text.slice(start, end);
  if (encoder.encode(exact).byteLength > 2048) throw new Error('Select at most 2 KiB of text.');
  const first = textPosition(text, start);
  const last = textPosition(text, end);
  return {
    start, end, exact,
    prefix: contextSlice(text, Math.max(0, start - 64), start),
    suffix: contextSlice(text, end, Math.min(text.length, end + 64)),
    start_line: first.line, start_column: first.column,
    end_line: last.line, end_column: last.column,
  };
}

/**
 * Read only a dedicated canonical text container, never chrome or pretty-printed
 * JSON. Highlight spans are transparent. Rendered Markdown needs a source map.
 */
export function selectionOffsets(root: HTMLElement, selection: Selection | null) {
  if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  return rangeOffsets(root, range);
}
export function rangeOffsets(root: HTMLElement, range: Range) {
  if (!root.contains(range.startContainer) || !root.contains(range.endContainer)) return null;
  const prefix = root.ownerDocument.createRange();
  prefix.selectNodeContents(root);
  prefix.setEnd(range.startContainer, range.startOffset);
  const through = root.ownerDocument.createRange();
  through.selectNodeContents(root);
  through.setEnd(range.endContainer, range.endOffset);
  return { start: canonicalText(prefix.toString()).length, end: canonicalText(through.toString()).length };
}

export function domTextRange(root: HTMLElement, start: number, end: number): Range | null {
  const text = root.textContent ?? '';
  if (!isTextBoundary(text, start) || !isTextBoundary(text, end) || end <= start) return null;
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const range = root.ownerDocument.createRange();
  let offset = 0;
  let started = false;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const length = node.textContent?.length ?? 0;
    if (!started && start <= offset + length) {
      range.setStart(node, start - offset);
      started = true;
    }
    if (started && end <= offset + length) {
      range.setEnd(node, end - offset);
      return range;
    }
    offset += length;
  }
  return null;
}

/** A derived proposal only: callers retain the original anchor and audit mapping. */
export function mapUniqueQuote(
  target: string, quote: { exact: string; prefix: string; suffix: string },
): { start: number; end: number } | null {
  if (!quote.exact) return null;
  let found: { start: number; end: number } | null = null;
  let cursor = 0;
  while (cursor <= target.length - quote.exact.length) {
    const start = target.indexOf(quote.exact, cursor);
    if (start < 0) break;
    const end = start + quote.exact.length;
    if (isTextBoundary(target, start) && isTextBoundary(target, end)
      && target.slice(0, start).endsWith(quote.prefix)
      && target.slice(end).startsWith(quote.suffix)) {
      if (found) return null;
      found = { start, end };
    }
    cursor = start + 1;
  }
  return found;
}

/**
 * Find rendered text in canonical text while ignoring whitespace differences (line breaks,
 * paragraph gaps, list indentation). `context` is the rendered text immediately before the
 * selection and disambiguates repeated quotes. Returns canonical offsets or null.
 * When the target is raw Markdown, inline markup (`*`, `_`, `` ` ``, `~`) that rendering removed
 * is skipped too; the returned range still covers the authored source text.
 * With `ignoreSpace`, whitespace is dropped entirely, for views that only reformat structure
 * (for example indented JSON shown for minified source).
 */
export function looseQuoteRange(target: string, quote: string, context = '', ignoreSpace = false): { start: number; end: number } | null {
  if (ignoreSpace) return quoteRange(target, quote, context, false, true);
  return quoteRange(target, quote, context, false) ?? quoteRange(target, quote, context, true);
}
function quoteRange(target: string, quote: string, context: string, markup: boolean, ignoreSpace = false): { start: number; end: number } | null {
  const squash = (value: string) => {
    let text = '';
    const index: number[] = [];
    let space = false;
    for (let i = 0; i < value.length; i++) {
      if (markup && /[*_`~]/.test(value[i])) continue;
      if (/\s/.test(value[i])) { space = !ignoreSpace && text.length > 0; continue; }
      if (space) { text += ' '; index.push(i); space = false; }
      text += value[i]; index.push(i);
    }
    return { text, index };
  };
  const haystack = squash(target), needle = squash(quote).text;
  if (!needle) return null;
  const hits: number[] = [];
  for (let at = haystack.text.indexOf(needle); at >= 0; at = haystack.text.indexOf(needle, at + 1)) hits.push(at);
  if (!hits.length) return null;
  let hit = hits[0];
  if (hits.length > 1) {
    const before = squash(context).text;
    let best = -1;
    for (const candidate of hits) {
      const prefix = haystack.text.slice(0, candidate).trimEnd();
      let shared = 0;
      while (shared < before.length && shared < prefix.length
        && before[before.length - 1 - shared] === prefix[prefix.length - 1 - shared]) shared++;
      if (shared > best) { best = shared; hit = candidate; } else if (shared === best) return null;
    }
  }
  let start = haystack.index[hit], end = haystack.index[hit + needle.length - 1] + 1;
  if (markup) {
    // Keep emphasis and code markers that wrap the quoted text inside the authored range.
    while (start > 0 && /[*_`~]/.test(target[start - 1])) start--;
    while (end < target.length && /[*_`~]/.test(target[end])) end++;
  }
  return isTextBoundary(target, start) && isTextBoundary(target, end) ? { start, end } : null;
}
