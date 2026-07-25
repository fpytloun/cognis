import type { WebConfigStatus } from '$lib/types/api';

export type EditableWebBackend = 'tavily' | 'brave' | 'searxng';

export interface WebBackendEditValue {
  backend: EditableWebBackend;
  enabled: boolean;
  apiKey: string;
  searxngUrl: string;
  searxngEngines: string;
  searxngCategories: string;
  searxngLanguage: string;
}

export function createWebBackendEditValue(
  backend: EditableWebBackend,
  config: WebConfigStatus
): WebBackendEditValue {
  return {
    backend,
    enabled: config[`${backend}_enabled`],
    apiKey: '',
    searxngUrl: config.searxng_url,
    searxngEngines: config.searxng_engines,
    searxngCategories: config.searxng_categories,
    searxngLanguage: config.searxng_language
  };
}

export function webBackendConfigured(
  backend: EditableWebBackend,
  config: WebConfigStatus
): boolean {
  return config[`${backend}_configured`];
}

export function webBackendStatusLabel(
  backend: EditableWebBackend,
  config: WebConfigStatus
): string {
  if (!webBackendConfigured(backend, config)) return 'Not configured';
  return config[`${backend}_enabled`] ? 'Enabled' : 'Disabled';
}
