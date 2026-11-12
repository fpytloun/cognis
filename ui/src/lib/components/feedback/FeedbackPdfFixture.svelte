<script lang="ts">
  import PdfAttachmentViewer from '../PdfAttachmentViewer.svelte';
  import FeedbackHost from './FeedbackHost.svelte';
  import FeedbackIndicator from './FeedbackIndicator.svelte';
  import FeedbackWorkspace from './FeedbackWorkspace.svelte';
</script>

<main class="fixed bottom-[10px] left-[252px] right-[276px] top-[112px] flex min-h-0 flex-col text-foreground">
  <FeedbackHost source={{ kind: 'document', content_ref: 'art_87d6238b098f4798919dc4b299894379' }}>
    <header class="fixed left-[252px] right-[276px] top-[56px] flex h-14 items-center justify-between">
      <h1>PDF source visibility fixture</h1><FeedbackIndicator />
    </header>
    <FeedbackWorkspace>
      <PdfAttachmentViewer url="/pdf-visibility-fixture.pdf" />
    </FeedbackWorkspace>
  </FeedbackHost>
</main>
