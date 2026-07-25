import { describe, expect, it } from 'vitest';

import type { WebConfigStatus } from '$lib/types/api';
import {
  createWebBackendEditValue,
  webBackendConfigured,
  webBackendStatusLabel
} from '$lib/web-backends';

function webConfig(overrides: Partial<WebConfigStatus> = {}): WebConfigStatus {
  return {
    backend: 'direct',
    search_backend: 'direct',
    fetch_backend: 'direct',
    fetch_fallback_browser: true,
    browser_fetch_session_idle_seconds: 60,
    browser_fetch_wait_timeout_seconds: 30,
    browser_fetch_navigation_timeout_seconds: 60,
    browser_fetch_wait_until: 'domcontentloaded',
    browser_fetch_network_idle_after_dom_seconds: 3,
    browser_fetch_headed_fallback_enabled: false,
    tavily_configured: true,
    tavily_enabled: false,
    brave_configured: false,
    brave_enabled: true,
    searxng_url: 'https://search.example.com',
    searxng_engines: 'google,bing',
    searxng_categories: 'general',
    searxng_language: 'en-US',
    searxng_configured: true,
    searxng_enabled: true,
    available_backends: ['browser', 'direct', 'searxng'],
    available_search_backends: ['direct', 'searxng'],
    available_fetch_backends: ['direct', 'browser'],
    ...overrides
  };
}

describe('web backend editor state', () => {
  it('preserves disabled configured state without exposing an API key', () => {
    const config = webConfig();

    expect(createWebBackendEditValue('tavily', config)).toMatchObject({
      backend: 'tavily',
      enabled: false,
      apiKey: ''
    });
    expect(webBackendConfigured('tavily', config)).toBe(true);
    expect(webBackendStatusLabel('tavily', config)).toBe('Disabled');
  });

  it('loads all editable SearXNG defaults', () => {
    const value = createWebBackendEditValue('searxng', webConfig());

    expect(value).toEqual({
      backend: 'searxng',
      enabled: true,
      apiKey: '',
      searxngUrl: 'https://search.example.com',
      searxngEngines: 'google,bing',
      searxngCategories: 'general',
      searxngLanguage: 'en-US'
    });
  });

  it('distinguishes missing configuration from a disabled backend', () => {
    const config = webConfig();

    expect(webBackendStatusLabel('brave', config)).toBe('Not configured');
    expect(webBackendConfigured('brave', config)).toBe(false);
  });
});
