import { describe, expect, it } from 'vitest';

import { shouldSendComposerMessage } from './composer-keyboard';

function event(overrides: Partial<KeyboardEvent> = {}): Pick<KeyboardEvent, 'ctrlKey' | 'isComposing' | 'key' | 'metaKey' | 'shiftKey'> {
  return {
    key: 'Enter',
    ctrlKey: false,
    isComposing: false,
    metaKey: false,
    shiftKey: false,
    ...overrides,
  };
}

describe('shouldSendComposerMessage', () => {
  it('sends with Enter when the physical-keyboard preference is enabled', () => {
    expect(shouldSendComposerMessage(event(), {
      enterToSend: true,
      softwareKeyboardOpen: false,
    })).toBe(true);
  });

  it('keeps Enter as a newline while the software keyboard is open', () => {
    expect(shouldSendComposerMessage(event(), {
      enterToSend: true,
      softwareKeyboardOpen: true,
    })).toBe(false);
  });

  it('uses Ctrl or Meta with Enter when Enter-to-send is disabled', () => {
    const options = { enterToSend: false, softwareKeyboardOpen: false };

    expect(shouldSendComposerMessage(event(), options)).toBe(false);
    expect(shouldSendComposerMessage(event({ ctrlKey: true }), options)).toBe(true);
    expect(shouldSendComposerMessage(event({ metaKey: true }), options)).toBe(true);
  });

  it('never sends for Shift+Enter or during IME composition', () => {
    const options = { enterToSend: true, softwareKeyboardOpen: false };

    expect(shouldSendComposerMessage(event({ shiftKey: true }), options)).toBe(false);
    expect(shouldSendComposerMessage(event({ isComposing: true }), options)).toBe(false);
  });
});
