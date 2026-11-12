<script lang="ts">
  import { onMount, type ComponentProps } from 'svelte';
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import type { ShareLinkOptions } from '$lib/components/rich/share-options';
  import { fetchHostedAppearance, type ViewerAppearance } from '$lib/components/rich/viewer-theme';
  import { defaultDocumentAudioClient, type DocumentAudioClient } from '$lib/document-audio';
  import { installSelectionCopy } from '$lib/selection-copy';
  import { installFind } from '$lib/find';
  let FeedbackHost = $state<typeof import('$lib/components/feedback/FeedbackHost.svelte')['default'] | null>(null);
  let PublicFeedbackHost = $state<typeof import('$lib/components/feedback/PublicFeedbackHost.svelte')['default'] | null>(null);
  let { viewerProps, feedbackShareToken = '' }: { viewerProps: ComponentProps<typeof RichDeliverable>; feedbackShareToken?: string } = $props();
  let ownerFeedback = $state(false);
  let createShare = $state<((options: ShareLinkOptions) => Promise<string>) | null>(null);
  let audioEnabled = $state(false);
  let accountAppearance = $state<ViewerAppearance | null>(null);
  let refresh = $state(0);
  let previousRef = '';
  let previousClient: DocumentAudioClient | undefined;
  // Keep native WebKit copy from promoting colon-prefixed prose to a URL.
  // This also applies to anonymous viewers and downloaded offline HTML.
  onMount(() => installSelectionCopy());
  onMount(() => installFind());
  $effect(() => {
    void refresh;
    const abort = new AbortController();
    const id = viewerProps.instanceId;
    const client = viewerProps.audioClient ?? defaultDocumentAudioClient;
    if (id !== previousRef || client !== previousClient || viewerProps.exportMode) {
      audioEnabled = false;
      createShare = null;
      previousRef = id ?? '';
      previousClient = client;
    }
    if (viewerProps.exportMode || feedbackShareToken) {
      accountAppearance = null;
      ownerFeedback = false;
      if (!viewerProps.exportMode && feedbackShareToken) {
        void import('$lib/components/feedback/PublicFeedbackHost.svelte').then(module => {
          if (!abort.signal.aborted) PublicFeedbackHost = module.default;
        });
      }
      return;
    }
    const options = { credentials: 'same-origin', cache: 'no-store', signal: abort.signal } as const;
    // Public tokens are never authority for private recordings or generation.
    if (id && /^(dlv|art|att|doc|img)_[0-9a-f]{32}$/.test(id)) {
      client.context(id, { signal: abort.signal })
        .then(context => { if (!abort.signal.aborted) audioEnabled = context.supported; })
        .catch(() => { /* Optional audio must not block public viewing. */ });
    }
    fetchHostedAppearance(abort.signal)
      .then(appearance => {
        if (!abort.signal.aborted) accountAppearance = appearance;
      })
      .catch(() => { /* Keep verified appearance on transient network failure. */ });
    if (id && /^dlv_[A-Za-z0-9_]+$/.test(id)) {
      fetch(`/api/v1/deliverables/${encodeURIComponent(id)}/viewer-context`, options)
      .then(async response => {
        if (!response.ok) {
          if (!abort.signal.aborted) createShare = null;
          return;
        }
        const context = await response.json();
        if (abort.signal.aborted) return;
        const module = await import('$lib/components/feedback/FeedbackHost.svelte');
        if (abort.signal.aborted) return;
        FeedbackHost = module.default;
        ownerFeedback = true;
        if (context.share_link_url !== `/api/v1/deliverables/${encodeURIComponent(id)}/share-link`) return;
        createShare = async options => {
          const result = await fetch(context.share_link_url, {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(options),
          });
          if (!result.ok) throw new Error('Could not create share link');
          return (await result.json()).url;
        };
      })
      .catch(() => { /* Optional owner authentication must not block public viewing. */ });
    }
    return () => abort.abort();
  });
</script>

<svelte:window onfocus={() => refresh += 1} />
{#snippet documentView()}
<RichDeliverable {...viewerProps} {audioEnabled} {accountAppearance} shareLinkCallback={createShare} />
{/snippet}
{#if !viewerProps.exportMode && feedbackShareToken && PublicFeedbackHost}
  <PublicFeedbackHost capability={feedbackShareToken} contentRef={viewerProps.instanceId ?? ''} />
{:else if !viewerProps.exportMode && ownerFeedback && viewerProps.instanceId && FeedbackHost}
  <FeedbackHost source={{ kind: 'document', content_ref: viewerProps.instanceId }} standalone>{@render documentView()}</FeedbackHost>
{:else}
  {@render documentView()}
{/if}
