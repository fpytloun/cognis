<script lang="ts">
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';

  const payload = {
    metadata: { toc: true },
    sources: [{ id: 'source', title: 'Shared source', snippet: 'Instance-local preview.' }],
    blocks: [
      { type: 'section', title: 'Overview', content: 'First section.' },
      { type: 'section', title: 'Details', content: 'Second section.' },
      { type: 'section', title: 'Results', content: 'Third section.' },
      {
        type: 'research_answer',
        title: 'Evidence',
        paragraphs: [{ text: 'Claim.', citations: ['source'] }],
      },
      {
        type: 'markdown',
        title: 'Internal navigation',
        content: '[Jump to overview](#overview)',
      },
    ],
  };
</script>

<main data-testid="rich-deliverable-multi-fixture">
  <RichDeliverable title="First report" content="Fallback" {payload} instanceId="shared-durable-id" surface="embedded" />
  <RichDeliverable title="Second report" content="Fallback" {payload} instanceId="shared-durable-id" surface="embedded" />
</main>

<style>
  main {
    display: grid;
    gap: 2rem;
    margin: 0 auto;
    max-width: 90rem;
    padding: 2rem;
  }
</style>
