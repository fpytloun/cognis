import { describe, expect, it, vi } from 'vitest';
import { standaloneCloseHandler } from './standalone-navigation';

function browser(installed = true, historyLength = 2, appleStandalone = false) {
  const back = vi.fn();
  const assign = vi.fn();
  return {
    back, assign,
    window: {
      matchMedia: () => ({ matches: installed }),
      navigator: { standalone: appleStandalone },
      location: { origin: 'https://cognis.fpy.cz', assign },
      history: { length: historyLength, back },
    } as unknown as Window,
  };
}

describe('standalone PWA close', () => {
  it('does not add a close action to an ordinary browser view', () => {
    expect(standaloneCloseHandler(browser(false).window, '')).toBeNull();
  });
  it('returns to same-origin app history when it is available', () => {
    const state = browser();
    standaloneCloseHandler(state.window, 'https://cognis.fpy.cz/conversations')!();
    expect(state.back).toHaveBeenCalledOnce();
    expect(state.assign).not.toHaveBeenCalled();
  });
  it.each(['', 'https://outside.test/', 'https://cognis.fpy.cz/api/v1/artifacts/preview', 'javascript:alert(1)'])(
    'uses app home rather than an unsafe or preview referrer: %s', referrer => {
      const state = browser();
      standaloneCloseHandler(state.window, referrer)!();
      expect(state.assign).toHaveBeenCalledWith('/');
      expect(state.back).not.toHaveBeenCalled();
    },
  );
  it('returns home for a direct launch with no usable history, including Apple standalone', () => {
    const state = browser(false, 1, true);
    standaloneCloseHandler(state.window, 'https://cognis.fpy.cz/conversations')!();
    expect(state.assign).toHaveBeenCalledWith('/');
  });
});
