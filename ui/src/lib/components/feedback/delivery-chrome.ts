import { deferredDefinition } from '../ui/deferred-definition';
/** SPA journal recovery must never enter hosted owner or public viewer graphs. */
export const deliveryChrome = deferredDefinition(() => import('./FeedbackDelivery.svelte'));
