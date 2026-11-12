import { deferredDefinition } from './deferred-definition';
export const imageLightbox = deferredDefinition(() => import('../ImageLightbox.svelte'));
export const codeBlockModal = deferredDefinition(() => import('../CodeBlockModal.svelte'));
export const fileDiffViewer = deferredDefinition(() => import('../FileDiffViewer.svelte'));
export const deliverableAudio = deferredDefinition(() => import('../DeliverableAudio.svelte'));
export const assistantDeliverable = deferredDefinition(() => import('../AssistantDeliverableBlock.svelte'));
export const taskOutcomeCard = deferredDefinition(() => import('../chat-v2/TaskOutcomeCard.svelte'));
export const dashboardEntityModal = deferredDefinition(() => import('../dashboard/DashboardEntityModal.svelte'));
