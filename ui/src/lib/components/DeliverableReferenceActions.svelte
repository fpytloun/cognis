<script lang="ts">
  import { getContext, onDestroy } from 'svelte';
  import { readable, type Readable } from 'svelte/store';
  import { api } from '$lib/api/client';
  import type { AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';
  import { loadConversationDeliverable } from '$lib/conversation-deliverables';
  import { DELIVERABLE_SHARING_CONTEXT } from '$lib/deliverable-permissions';
  import { appDocumentAudioClient } from '$lib/app-document-audio';
  import DocumentActions from './rich/DocumentActions.svelte';
  import DocumentFavoriteButton from './DocumentFavoriteButton.svelte';
  import DeferredPrivateView from './ui/DeferredPrivateView.svelte';
  import { assistantDeliverable, deliverableAudio } from './ui/private-view-definitions';
  import FeedbackHost from './feedback/FeedbackHost.svelte';
  import DocumentCommentCount from './DocumentCommentCount.svelte';

  export let item: AssistantDeliverableTimelineItem;
  export let conversationId = '';
  let open = false;
  let copied = false;
  let error = '';
  let alive = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const sharingPermission = getContext<Readable<boolean>>(DELIVERABLE_SHARING_CONTEXT) ?? readable(true);
  $: id = item.deliverable_id;
  $: download = `/api/v1/deliverables/${encodeURIComponent(id)}`;
  $: internalUrl = `/library/deliverable/${encodeURIComponent(id)}`;

  async function copyDocument() {
    error = '';
    try {
      const row = await loadConversationDeliverable(id, conversationId);
      if (!alive) return;
      await navigator.clipboard.writeText(row.content);
      if (!alive) return;
      copied = true;
      clearTimeout(timer);
      timer = setTimeout(() => copied = false, 1500);
    } catch {
      if (alive) error = 'Could not copy document. Try again.';
    }
  }
  onDestroy(() => { alive = false; clearTimeout(timer); });
</script>

{#key id}
<DocumentCommentCount {id} />
<FeedbackHost source={{ kind: 'document', content_ref: id }}>
<DocumentActions
  openFullView={() => { open = true; }}
  {copyDocument} {copied} {internalUrl}
  pdfUrl={`${download}/download.pdf`}
  htmlUrl={`${download}/download.html`}
  markdownUrl={['rich', 'markdown'].includes(item.format ?? '') ? `${download}/download.md` : ''}
  createLink={$sharingPermission ? async options => (await api.artifactDocuments.publicLink('deliverable', id, { ...options, mode: options?.mode ?? 'view' })).url : null}
>
  {#snippet extraActions(showLabel = false)}<DocumentFavoriteButton kind="deliverable" {id} {showLabel} />{/snippet}
  {#snippet audioControls()}
    <DeferredPrivateView definition={deliverableAudio} label="audio controls"
      viewProps={{ sourceRef: id, client: appDocumentAudioClient, rich: item.format === 'rich' }} />
  {/snippet}
</DocumentActions>
</FeedbackHost>
{/key}
{#if error}<span role="alert">{error}</span>{/if}
{#if open}
  <DeferredPrivateView definition={assistantDeliverable}
    viewProps={{ item, conversationId, fullscreenOnly: true, onClose: () => { open = false; } }}
    label="document" modal onClose={() => { open = false; }} />
{/if}
