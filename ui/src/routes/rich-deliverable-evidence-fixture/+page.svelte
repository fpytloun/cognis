<script lang="ts">
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import { richDeliverableEvidenceFixture } from '$lib/components/rich/rich-deliverable-evidence.fixture';
</script>

<svelte:head>
  <title>Rich Evidence Fixture · Cognis</title>
</svelte:head>

<main class="evidence-fixture-page" data-testid="rich-deliverable-evidence-fixture">
  <RichDeliverable title="Evidence interaction fixture" content="Evidence fallback content." payload={richDeliverableEvidenceFixture} surface="standalone" />
</main>

<style>
  .evidence-fixture-page {
    height: 100vh;
    min-height: 100%;
    overflow: auto;
    background:
      radial-gradient(circle at 12% 0%, rgb(56 189 248 / 0.18), transparent 28rem),
      radial-gradient(circle at 88% 10%, rgb(16 185 129 / 0.13), transparent 26rem),
      linear-gradient(180deg, #020617, #07111a 42%, #020617);
    padding: clamp(1rem, 3vw, 2.5rem);
  }

  .evidence-fixture-page :global(.rich-deliverable) {
    width: min(100%, 86rem);
    margin: 0 auto;
  }
</style>
