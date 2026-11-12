/**
 * Minimal JSON syntax highlighter.
 *
 * Produces an HTML string with ``<span>`` elements tagged with classes
 * the app stylesheet knows how to colour. Only handles JSON input; if the
 * input is not valid JSON, callers fall back to rendering as plain text.
 *
 * This intentionally avoids a heavy library like Prism or highlight.js —
 * JSON grammar is small and we only need five token types. The tokenizer
 * is allocation-friendly (one pass over the string) and HTML-escapes
 * every literal piece it emits.
 */

const ESC_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

function escapeHtml(input: string): string {
  return input.replace(/[&<>"']/g, (ch) => ESC_MAP[ch] ?? ch);
}

/**
 * Returns true when ``input`` can be parsed as JSON. Leading/trailing
 * whitespace is tolerated, but the outer value must be an object, array,
 * or JSON primitive.
 */
export function looksLikeJson(input: string): boolean {
  const trimmed = input.trim();
  if (trimmed.length === 0) return false;
  const first = trimmed[0];
  if (first !== '{' && first !== '[' && first !== '"' && !/[-0-9tfn]/.test(first)) {
    return false;
  }
  try {
    JSON.parse(trimmed);
    return true;
  } catch {
    return false;
  }
}

/**
 * Re-serialize any parseable JSON with 2-space indentation so the
 * highlighted output is readable even if the source was single-line.
 */
export function prettyPrintJson(input: string): string {
  try {
    return JSON.stringify(JSON.parse(input), null, 2);
  } catch {
    return input;
  }
}

/**
 * Indent JSON by changing whitespace outside strings only. Every token keeps its original bytes
 * (string escapes, number spelling), so the displayed text still matches the source text once
 * whitespace is ignored. This lets comments anchor to the exact source while it reads pretty.
 */
export function reindentJson(input: string, indent = '  '): string {
  let out = '', depth = 0, inString = false, escaped = false;
  const line = () => `\n${indent.repeat(depth)}`;
  for (let i = 0; i < input.length; i++) {
    const char = input[i];
    if (inString) {
      out += char;
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (/\s/.test(char)) continue;
    if (char === '"') { inString = true; out += char; continue; }
    if (char === '{' || char === '[') {
      let next = i + 1;
      while (next < input.length && /\s/.test(input[next])) next++;
      const close = char === '{' ? '}' : ']';
      // Keep empty containers compact: {} and [].
      if (input[next] === close) { out += char + close; i = next; continue; }
      depth++; out += char + line(); continue;
    }
    if (char === '}' || char === ']') { depth = Math.max(0, depth - 1); out += line() + char; continue; }
    if (char === ',') { out += char + line(); continue; }
    if (char === ':') { out += ': '; continue; }
    out += char;
  }
  return out;
}

/**
 * Tokenise the already-pretty-printed JSON text and wrap each token in
 * a classified ``<span>``. The caller is responsible for making sure
 * the input is valid JSON (use ``looksLikeJson`` to gate it).
 */
export function highlightJson(input: string): string {
  // Pattern covers, in order: strings (keys or values), numbers,
  // booleans/null, and structural punctuation. Unmatched characters
  // (whitespace, commas outside these categories) fall through and are
  // emitted as-is.
  const pattern =
    /("(?:\\.|[^"\\])*")(\s*:)?|(-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|\b(true|false|null)\b|([{}[\],:])/g;

  let out = '';
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(input)) !== null) {
    // Emit any raw characters (whitespace, fallthrough) between matches.
    if (match.index > lastIndex) {
      out += escapeHtml(input.slice(lastIndex, match.index));
    }
    const [, stringToken, colonAfterString, numberToken, constToken, punctToken] = match;

    if (stringToken !== undefined) {
      // A string followed by ":" is a key; anything else is a value.
      const cls = colonAfterString ? 'json-key' : 'json-string';
      out += `<span class="${cls}">${escapeHtml(stringToken)}</span>`;
      if (colonAfterString) {
        out += `<span class="json-punct">${escapeHtml(colonAfterString)}</span>`;
      }
    } else if (numberToken !== undefined) {
      out += `<span class="json-number">${escapeHtml(numberToken)}</span>`;
    } else if (constToken !== undefined) {
      const cls = constToken === 'null' ? 'json-null' : 'json-boolean';
      out += `<span class="${cls}">${escapeHtml(constToken)}</span>`;
    } else if (punctToken !== undefined) {
      out += `<span class="json-punct">${escapeHtml(punctToken)}</span>`;
    }

    lastIndex = pattern.lastIndex;
  }

  if (lastIndex < input.length) {
    out += escapeHtml(input.slice(lastIndex));
  }

  return out;
}
