import { describe, expect, it } from 'vitest';
import { numberedResultIndex } from './shortcuts';

const key = {
  code: 'Digit1', key: '!', metaKey: false, ctrlKey: true, shiftKey: true, altKey: false,
};

describe('numbered Spotlight shortcuts', () => {
  it('uses Control+Shift and physical number keys, including shifted punctuation', () => {
    for (let digit = 1; digit <= 9; digit++) {
      expect(numberedResultIndex({ ...key, code: `Digit${digit}`, key: '!@#$%^&*('[digit - 1] }))
        .toBe(digit - 1);
    }
  });
  it('supports numeric keys without code', () => {
    expect(numberedResultIndex({ ...key, code: '', key: '9' })).toBe(8);
  });
  it('leaves browser tabs, Mac screenshots and unrelated modifiers alone', () => {
    expect(numberedResultIndex({ ...key, shiftKey: false, key: '1' })).toBeNull();
    expect(numberedResultIndex({ ...key, ctrlKey: false })).toBeNull();
    expect(numberedResultIndex({ ...key, ctrlKey: false, metaKey: true, code: 'Digit3', key: '#' })).toBeNull();
    expect(numberedResultIndex({ ...key, metaKey: true })).toBeNull();
    expect(numberedResultIndex({ ...key, altKey: true })).toBeNull();
    expect(numberedResultIndex({ ...key, code: 'Digit0', key: ')' })).toBeNull();
    expect(numberedResultIndex({ ...key, code: 'KeyA', key: 'A' })).toBeNull();
  });
});
