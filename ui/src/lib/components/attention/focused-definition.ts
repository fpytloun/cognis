import { deferredDefinition } from '../ui/deferred-definition';

// Both eager hosts share code readiness, never action state or callbacks.
export const focusedAttentionAction = deferredDefinition(() => import('./FocusedAttentionAction.svelte'));
