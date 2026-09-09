<script lang="ts">
  import { page } from '$app/state';
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import { findRichScenarioBlock } from '$lib/rich-scenarios/blocks';
  import { getRichScenarioDocPreview, richScenarioDocPreviews } from '$lib/rich-scenarios/docs-previews';
  import { requireRichScenario } from '$lib/rich-scenarios/registry';

  let requestedPreviewId = $derived(page.url.searchParams.get('preview') ?? '');
  let requestedType = $derived(page.url.searchParams.get('block') ?? '');
  let requestedChartVariant = $derived(page.url.searchParams.get('chart') ?? 'line');
  let requestedCardVariant = $derived(page.url.searchParams.get('card') ?? '');
  let legacyPreviewId = $derived(requestedType === 'chart'
    ? `chart-${requestedChartVariant}`
    : requestedType === 'card' && requestedCardVariant
      ? `card-${requestedCardVariant}`
      : requestedType);
  let preview = $derived(
    getRichScenarioDocPreview(requestedPreviewId)
      ?? getRichScenarioDocPreview(legacyPreviewId)
      ?? richScenarioDocPreviews[0]
  );
  let scenario = $derived(requireRichScenario(preview.scenario_id));
  let validType = $derived(preview.block_type);
  let block = $derived(findRichScenarioBlock(
    scenario.payload.blocks,
    preview.block_type,
    preview.occurrence ?? 0,
  ));
  let payload = $derived({
    blocks: block ? [block] : [],
    assets: scenario.payload.assets,
    sources: scenario.payload.sources,
    datasets: scenario.payload.datasets,
    exports: scenario.payload.exports,
    metadata: {},
  });
</script>

<svelte:head>
  <title>{validType} block fixture · Cognis</title>
</svelte:head>

<main class="fixture-page" data-testid="rich-deliverable-block-fixture-page">
  <section class="fixture-shell">
    <p class="eyebrow">Rich deliverable visual QA</p>
    <h1>{validType}</h1>
    <p class="description">A deterministic isolated fixture for documentation screenshots and renderer checks.</p>
    <div data-testid="rich-deliverable-block-fixture" data-block-type={validType} data-preview={preview.id}>
      <RichDeliverable
        title={`${validType} reference`}
        content={`${validType} reference fallback.`}
        {payload}
        instanceId={`block-fixture-${validType}`}
        surface="embedded"
      />
    </div>
  </section>
</main>

<style>
  .fixture-page {
    min-height: 100%;
    background: #f8fafc;
    padding: 2rem;
  }

  .fixture-shell {
    margin: 0 auto;
    max-width: 70rem;
  }

  .eyebrow {
    color: #0369a1;
    font-size: 0.75rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  h1 {
    color: #172033;
    margin: 0.25rem 0;
  }

  .description {
    color: #475569;
    margin: 0 0 1.5rem;
  }
</style>
