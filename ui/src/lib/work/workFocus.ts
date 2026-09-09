import type { TimelineScope } from '$lib/chat-v2/types';

export interface WorkFileFocusIdentity {
  path?: string | null;
  pathId?: string | null;
  pathGenerationId?: string | null;
}

export interface WorkInitialFocus {
  workItemId: string;
  sourceScope?: TimelineScope;
  sourceWorkstreamKey?: string | null;
  files: WorkFileFocusIdentity[];
}
