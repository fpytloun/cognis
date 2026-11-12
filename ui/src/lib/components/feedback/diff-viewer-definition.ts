import { deferredDefinition } from '../ui/deferred-definition';

export const feedbackDiffViewer = deferredDefinition(() => import('./FeedbackDiffViewer.svelte'));
