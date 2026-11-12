<script lang="ts">
  import { dev } from '$app/environment';
  import { onMount } from 'svelte';
  import { auth } from '$lib/stores/auth';
  import DeferredPrivateView from '$lib/components/ui/DeferredPrivateView.svelte';
  import { codeBlockModal, imageLightbox, deliverableAudio } from '$lib/components/ui/private-view-definitions';
  import FeedbackDiffViewer from '$lib/components/feedback/FeedbackDiffViewer.svelte';
  import FeedbackChromeMount from '$lib/components/feedback/FeedbackChromeMount.svelte';
  import { reviewChrome } from '$lib/components/feedback/review-chrome';
  import FeedbackContextProvider from '$lib/components/feedback/FeedbackContextProvider.svelte';
  import FeedbackComposerTray from '$lib/components/feedback/FeedbackComposerTray.svelte';
  import type { FeedbackContext } from '$lib/feedback/types';
  import type { DocumentAudioClient } from '$lib/document-audio';
  import AttentionActionDialog from '$lib/components/attention/AttentionActionDialog.svelte';
  let mode = $state('');
  let ready = $state(false);
  let actionId = $state('loading-attention');
  let attentionSettled = $state(0);
  let attentionUnavailable = $state(0);
  let context = $state<FeedbackContext>({ kind: 'chat', conversation_id: 'loading-browser-conversation' });
  const audio: DocumentAudioClient = {
    context: async () => ({ supported: true, reason: null, source_format: 'markdown', content_digest: null,
      source_title: 'Loading fixture', available_profiles: [], jobs: [], ui_config: null,
      can_generate: false, can_cancel: false, detail: null }),
    estimates: async () => [],
    create: async () => { throw new Error('Read-only fixture.'); },
    get: async () => { throw new Error('Read-only fixture.'); },
    cancel: async () => { throw new Error('Read-only fixture.'); },
  };
  onMount(() => { if (dev) void auth.bootstrap().then(() => { ready = true; }); });
</script>
{#if dev && ready}
  <main class="p-4">
    <button onclick={() => { mode = 'code'; }}>Open code</button>
    <button onclick={() => { mode = 'image'; }}>Open image</button>
    <button onclick={() => { mode = 'diff'; }}>Open diff</button>
    <button onclick={() => { mode = 'audio'; }}>Open audio</button>
    <button onclick={() => { mode = 'review'; }}>Open review</button>
    <button onclick={() => { mode = 'attention'; }}>Open attention</button>
    <button onclick={() => { actionId = 'changed-loading-attention'; }}>Switch action</button>
    <a href="/work-feedback-loading-fixture?attention=next">Navigate fixture</a>
    <output aria-label="Attention callbacks">{attentionSettled}/{attentionUnavailable}</output>
    <button onclick={() => { mode = ''; }}>Dismiss view</button>
    <button onclick={() => { context = { kind: 'chat', conversation_id: 'changed-loading-conversation' }; }}>Switch source</button>
    <button onclick={() => void auth.clear()}>Invalidate account</button>
    <FeedbackContextProvider {context}>
      <FeedbackComposerTray conversationId={context.kind === 'chat' ? context.conversation_id : ''} included={null} />
      {#if mode === 'code'}
        <DeferredPrivateView definition={codeBlockModal} modal label="code preview" onClose={() => { mode = ''; }}
          viewProps={{ code: 'const loaded = true;', language: 'typescript', onClose: () => { mode = ''; } }} />
      {:else if mode === 'image'}
        <DeferredPrivateView definition={imageLightbox} modal label="image preview" onClose={() => { mode = ''; }}
          viewProps={{ src: 'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="40" height="40"%3E%3Crect width="40" height="40" fill="blue"/%3E%3C/svg%3E', alt: 'Loaded image', onClose: () => { mode = ''; } }} />
      {:else if mode === 'diff'}
        <FeedbackDiffViewer diffs={[{ path: 'src/loading.ts', diff: '@@ -1 +1 @@\n-old\n+new', additions: 1, deletions: 1 }]} />
      {:else if mode === 'audio'}
        <DeferredPrivateView definition={deliverableAudio} label="audio controls" viewProps={{ sourceRef: 'loading-fixture', client: audio }} />
      {:else if mode === 'review'}
        <FeedbackChromeMount definition={reviewChrome} viewProps={{ entries: [], subjectIds: [], context, onClose: () => { mode = ''; } }} onClose={() => { mode = ''; }} />
      {:else if mode === 'attention'}
        <AttentionActionDialog {actionId} title="Fixture attention"
          onClose={() => { mode = ''; }}
          onSettled={() => { attentionSettled++; }}
          onUnavailable={() => { attentionUnavailable++; }} />
      {/if}
    </FeedbackContextProvider>
  </main>
{:else}<p>Development loading fixture.</p>{/if}
