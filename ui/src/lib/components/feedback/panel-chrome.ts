import { deferredDefinition } from '../ui/deferred-definition';
/** Owner panel code is safe for both SPA and hosted owner viewers. */
export const panelChrome = deferredDefinition(() => import('./FeedbackPanel.svelte'));
export const inlineChrome = deferredDefinition(() => import('./FeedbackInline.svelte'));
