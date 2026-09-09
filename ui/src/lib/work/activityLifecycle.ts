import type { WorkProjectionResponse } from '$lib/chat-v2/types';

export type ActivityMaterialization = NonNullable<WorkProjectionResponse['materialization']>;

export interface ActivityLifecyclePresentation {
  kind: 'none' | 'status' | 'warning' | 'error';
  message: string | null;
  busy: boolean;
}

export function presentActivityLifecycle(
  materialization: ActivityMaterialization | null | undefined,
): ActivityLifecyclePresentation {
  switch (materialization?.state) {
    case 'catching_up':
      return { kind: 'status', message: 'Catching up activity…', busy: true };
    case 'partial':
      return { kind: 'warning', message: 'Activity is partially available.', busy: false };
    case 'failed':
      return { kind: 'error', message: 'Activity refresh failed.', busy: false };
    case 'live':
    default:
      return { kind: 'none', message: null, busy: false };
  }
}
