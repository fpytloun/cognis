type NumberKey = Pick<KeyboardEvent, 'code' | 'key' | 'metaKey' | 'ctrlKey' | 'shiftKey' | 'altKey'>;

/** Control+Shift avoids browser tabs and Mac screenshots; code handles shifted digits. */
export function numberedResultIndex(event: NumberKey): number | null {
  if (!event.ctrlKey || event.metaKey || !event.shiftKey || event.altKey) return null;
  const digit = /^Digit([1-9])$/.exec(event.code)?.[1] ?? /^[1-9]$/.exec(event.key)?.[0];
  return digit ? Number(digit) - 1 : null;
}
