import { describe, expect, it } from 'vitest';
import { DEFAULT_USER_PREFERENCES, normalizeUserPreferences } from './user-preferences';

describe('dashboard workspace window preference', () => {
  it('defaults legacy and invalid payloads to enabled', () => {
    expect(DEFAULT_USER_PREFERENCES.display.dashboard_workspace_windows).toBe(true);
    expect(normalizeUserPreferences({
      display: { theme: 'dark', language: 'en' },
      chat: {},
    }).display.dashboard_workspace_windows).toBe(true);
  });

  it('round-trips an explicit enabled value', () => {
    expect(normalizeUserPreferences({
      display: {
        theme: 'system',
        language: 'auto',
        dashboard_workspace_windows: true,
      },
      chat: {},
    }).display.dashboard_workspace_windows).toBe(true);
  });

  it('preserves an explicit disabled value', () => {
    expect(normalizeUserPreferences({
      display: { dashboard_workspace_windows: false },
    }).display.dashboard_workspace_windows).toBe(false);
  });
});

describe('chat composer preference', () => {
  it('defaults legacy payloads to Enter-to-send', () => {
    expect(DEFAULT_USER_PREFERENCES.chat.enter_to_send).toBe(true);
    expect(normalizeUserPreferences({
      display: {},
      chat: {},
    }).chat.enter_to_send).toBe(true);
  });

  it('preserves an explicit Enter-as-newline preference', () => {
    expect(normalizeUserPreferences({
      display: {},
      chat: { enter_to_send: false },
    }).chat.enter_to_send).toBe(false);
  });
});
