import type { FileDiff } from '$lib/diff';

/** Source rows stay local to one exact event and hunk, never combined history. */
export function annotationDiffRows(input: FileDiff & { binary?: boolean; generated?: boolean; preview_omitted?: boolean }) {
  if (input.binary || input.generated || input.preview_omitted || input.truncated || input.content_truncated
    || /^Binary files /m.test(input.diff)) return [];
  const rows: { index: number; hunk: string; oldLine: number | null; newLine: number | null; text: string }[] = [];
  let oldLine = 0;
  let newLine = 0;
  let oldRemaining = 0;
  let newRemaining = 0;
  let hunk = '';
  const lines = input.diff.replace(/\r\n?/g, '\n').split('\n');
  for (let index = 0; index < lines.length; index += 1) {
    const raw = lines[index];
    const header = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$/.exec(raw);
    if (header) {
      // An incomplete preceding hunk makes all line evidence suspect.
      if (hunk && (oldRemaining || newRemaining)) return [];
      oldLine = Number(header[1]); newLine = Number(header[3]);
      oldRemaining = Number(header[2] ?? 1); newRemaining = Number(header[4] ?? 1);
      if (![oldLine, newLine, oldRemaining, newRemaining].every(Number.isSafeInteger)
        || oldRemaining > 0 && oldLine < 1 || newRemaining > 0 && newLine < 1) return [];
      hunk = `${index}:${raw}`;
      continue;
    }
    if (!hunk) continue;
    if (raw === '\\ No newline at end of file') continue;
    if (!oldRemaining && !newRemaining) {
      if (raw === '' && index === lines.length - 1) continue;
      // A new file cannot inherit the last hunk's identity.
      return [];
    }
    if (raw.startsWith('+') && newRemaining > 0) {
      rows.push({ index, hunk, oldLine: null, newLine: newLine++, text: raw.slice(1) });
      newRemaining -= 1;
    } else if (raw.startsWith('-') && oldRemaining > 0) {
      rows.push({ index, hunk, oldLine: oldLine++, newLine: null, text: raw.slice(1) });
      oldRemaining -= 1;
    } else if (raw.startsWith(' ') && oldRemaining > 0 && newRemaining > 0) {
      rows.push({ index, hunk, oldLine: oldLine++, newLine: newLine++, text: raw.slice(1) });
      oldRemaining -= 1; newRemaining -= 1;
    } else return [];
  }
  return oldRemaining || newRemaining ? [] : rows;
}

export function selectDiffLines(
  rows: ReturnType<typeof annotationDiffRows>, side: 'old' | 'new', from: number, to: number,
) {
  if (!Number.isSafeInteger(from) || !Number.isSafeInteger(to) || from < 1 || to < 1) {
    throw new Error('Select available diff lines.');
  }
  const start = Math.min(from, to);
  const end = Math.max(from, to);
  const field = side === 'old' ? 'oldLine' : 'newLine';
  const selected = rows.filter(row => row[field] !== null && row[field]! >= start && row[field]! <= end);
  if (selected.length !== end - start + 1 || new Set(selected.map(row => row.hunk)).size !== 1
    || selected.some((row, index) => row[field] !== start + index)) {
    throw new Error('Select a contiguous range on one side of one complete hunk.');
  }
  const quote = selected.map(row => row.text).join('\n');
  if (new TextEncoder().encode(quote).byteLength > 2048) throw new Error('Select at most 2 KiB of diff context.');
  return { start_line: start, end_line: end, quote, hunk: selected[0].hunk };
}

/** Full hunk bytes, LF-normalized, without a terminal LF (not an index/header digest). */
export function diffHunkText(diff: string, hunk: string): string {
  const lines = diff.replace(/\r\n?/g, '\n').split('\n');
  const start = Number(hunk.slice(0, hunk.indexOf(':')));
  if (!Number.isSafeInteger(start) || `${start}:${lines[start]}` !== hunk) throw new Error('Diff hunk is no longer available.');
  let end = start + 1;
  while (end < lines.length && !lines[end].startsWith('@@ ')) end++;
  if (end === lines.length && lines[end - 1] === '') end--;
  return lines.slice(start, end).join('\n');
}
