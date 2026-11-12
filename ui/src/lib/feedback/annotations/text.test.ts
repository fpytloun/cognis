import { describe, expect, it } from 'vitest';
import { canonicalText, domTextRange, isTextBoundary, mapUniqueQuote, selectText, selectionOffsets } from './text';

describe('canonical annotation text', () => {
  it('uses UTF-16 offsets, LF line endings and no Unicode normalization', () => {
    const text = canonicalText('A\r\n😀e\u0301\rZ');
    expect(text).toBe('A\n😀e\u0301\nZ');
    expect(selectText(text, 2, 6)).toMatchObject({
      start: 2, end: 6, exact: '😀e\u0301', start_line: 2, start_column: 1, end_line: 2, end_column: 5,
    });
    expect(isTextBoundary(text, 3)).toBe(false);
    expect(() => selectText(text, 3, 6)).toThrow();
    expect(() => selectText(text, 2, 3)).toThrow();
  });

  it('rejects oversize, unnormalized, empty and unavailable selections without truncating', () => {
    expect(() => selectText('é'.repeat(1025), 0, 1025)).toThrow('2 KiB');
    expect(() => selectText('a\r\nb', 0, 1)).toThrow('LF');
    expect(() => selectText('abc', 1, 1)).toThrow();
    expect(() => selectText('abcdef', 0, 6, [{ start: 0, end: 2 }, { start: 4, end: 6 }])).toThrow('unavailable');
    expect(selectText('abcdef', 4, 6, [{ start: 4, end: 6 }]).exact).toBe('ef');
  });

  it('does not split surrogate pairs in contextual quotes', () => {
    const text = '😀' + 'a'.repeat(63) + 'x' + 'a'.repeat(63) + '😀';
    const result = selectText(text, 65, 66);
    expect(result.prefix).toBe('a'.repeat(63));
    expect(result.suffix).toBe('a'.repeat(63));
  });

  it('reads across highlighter spans but rejects selections outside the owning content', () => {
    const root = document.createElement('code');
    root.innerHTML = '<span>const</span> a = <span>1</span>;';
    document.body.append(root);
    const range = domTextRange(root, 3, 11)!;
    const selection = window.getSelection()!;
    selection.removeAllRanges(); selection.addRange(range);
    expect(selectionOffsets(root, selection)).toEqual({ start: 3, end: 11 });
    expect(range.toString()).toBe('st a = 1');
    expect(selectionOffsets(document.createElement('div'), selection)).toBeNull();
    expect(domTextRange(root, 0, 100)).toBeNull();
    selection.removeAllRanges(); root.remove();
  });

  it('maps only a unique quote with matching context', () => {
    expect(mapUniqueQuote('before word after', { exact: 'word', prefix: 'before ', suffix: ' after' }))
      .toEqual({ start: 7, end: 11 });
    expect(mapUniqueQuote('word word', { exact: 'word', prefix: '', suffix: '' })).toBeNull();
    expect(mapUniqueQuote('word', { exact: 'word', prefix: 'before ', suffix: '' })).toBeNull();
    expect(mapUniqueQuote('😀', { exact: '\ude00', prefix: '', suffix: '' })).toBeNull();
    expect(mapUniqueQuote('abc', { exact: '', prefix: '', suffix: '' })).toBeNull();
  });
});
