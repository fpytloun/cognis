<script lang="ts">
  import { page } from '$app/state';
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import { richDeliverableVisualScenarios } from '$lib/components/rich/rich-deliverable.fixture';
  import { SUPPORTED_RICH_BLOCK_TYPES } from '$lib/rich-deliverable';

  type RichBlock = Record<string, unknown> & { type?: unknown; blocks?: unknown[]; children?: unknown[] };
  const chartVariants: Record<string, RichBlock> = {
    line: {
      type: 'chart',
      title: 'Zoomies by weekday',
      description: 'Late-evening laps are the preferred form of cardio.',
      spec_version: 'cognis.chart.v1',
      chart_type: 'line',
      series: [{ id: 'zoomies', label: 'Zoomie intensity', points: [{ x: 'Mon', y: 2 }, { x: 'Tue', y: 5 }, { x: 'Wed', y: 3 }, { x: 'Thu', y: 7 }] }],
      x_axis: { type: 'category', label: 'Weekday' },
      y_axis: { type: 'linear', label: 'Laps', min: 0, max: 8 },
      legend_position: 'none',
      palette_token: 'warm'
    },
    bar: {
      type: 'chart',
      title: 'Most contested sleeping surfaces',
      description: 'A laptop keyboard performs unexpectedly well.',
      spec_version: 'cognis.chart.v1',
      chart_type: 'bar',
      series: [{ id: 'claims', label: 'Cat claims', points: [{ x: 'Keyboard', y: 9 }, { x: 'Sunny chair', y: 7 }, { x: 'Laundry', y: 6 }, { x: 'Human lap', y: 8 }] }],
      x_axis: { type: 'category', label: 'Surface' },
      y_axis: { type: 'linear', label: 'Claims', min: 0, max: 10 },
      legend_position: 'none',
      palette_token: 'cool'
    },
    donut: {
      type: 'chart',
      title: 'Preferred afternoon activities',
      description: 'A pie-style donut makes the nap majority unambiguous.',
      spec_version: 'cognis.chart.v1',
      chart_type: 'donut',
      series: [{ id: 'activities', label: 'Minutes', points: [{ x: 'Nap', y: 64 }, { x: 'Birdwatch', y: 16 }, { x: 'Snack', y: 12 }, { x: 'Mischief', y: 8 }] }],
      x_axis: { type: 'category', label: 'Activity' },
      y_axis: { type: 'linear', label: 'Share' },
      legend_position: 'right',
      palette_token: 'categorical'
    },
    stacked_bar: {
      type: 'chart',
      title: 'Treat negotiation outcomes',
      description: 'Two cats, one cupboard, several strong opinions.',
      spec_version: 'cognis.chart.v1',
      chart_type: 'stacked_bar',
      series: [
        { id: 'muchi', label: 'Muchi', points: [{ x: 'Morning', y: 3 }, { x: 'Afternoon', y: 1 }, { x: 'Evening', y: 4 }] },
        { id: 'guest-cat', label: 'Guest cat', points: [{ x: 'Morning', y: 1 }, { x: 'Afternoon', y: 2 }, { x: 'Evening', y: 1 }] }
      ],
      x_axis: { type: 'category', label: 'Time' },
      y_axis: { type: 'linear', label: 'Successful negotiations', min: 0, max: 6 },
      legend_position: 'bottom',
      palette_token: 'categorical'
    }
  };

  // Additional `card` variants that documentation/e2e coverage wants a
  // dedicated screenshot for, keyed like `chartVariants` above. Only used
  // when `?card=<key>` is present; without it, `card` falls back to the
  // first card found in `every-block-reference` (unchanged default).
  const cardVariants: Record<string, RichBlock> = {
    visual: {
      type: 'card',
      variant: 'visual',
      icon: 'activity',
      eyebrow: 'Image-forward',
      title: 'Visual card with media',
      summary: 'The image becomes a full-bleed background with a legibility gradient behind the title and summary.',
      media: {
        href: '/docs/rich-deliverables/pulse-editorial-river.jpg',
        alt: 'Mist over a river beneath the Czech Central Highlands at sunrise',
        credit: 'Generated editorial fixture image',
        aspect_ratio: '16 / 7',
        focal_point: '50% 50%'
      }
    }
  };

  const referenceScenario = richDeliverableVisualScenarios.find(
    (scenario) => scenario.id === 'every-block-reference'
  );

  function findBlock(blocks: unknown[], type: string): RichBlock | null {
    for (const candidate of blocks) {
      if (!candidate || typeof candidate !== 'object') continue;
      const block = candidate as RichBlock;
      if (block.type === type) return block;
      for (const children of [block.blocks, block.children]) {
        if (Array.isArray(children)) {
          const found = findBlock(children, type);
          if (found) return found;
        }
      }
    }
    return null;
  }

  let requestedType = $derived(page.url.searchParams.get('block') ?? '');
  let requestedChartVariant = $derived(page.url.searchParams.get('chart') ?? 'line');
  let requestedCardVariant = $derived(page.url.searchParams.get('card') ?? '');
  let validType = $derived(SUPPORTED_RICH_BLOCK_TYPES.has(requestedType)
    ? requestedType
    : (Array.from(SUPPORTED_RICH_BLOCK_TYPES)[0] ?? 'hero'));
  let block = $derived(validType === 'chart'
    ? (chartVariants[requestedChartVariant] ?? chartVariants.line)
    : validType === 'card' && cardVariants[requestedCardVariant]
      ? cardVariants[requestedCardVariant]
      : findBlock(referenceScenario?.payload.blocks ?? [], validType));
  let payload = $derived({
    blocks: block ? [block] : [],
    assets: referenceScenario?.payload.assets ?? [],
    sources: referenceScenario?.payload.sources ?? [],
    datasets: referenceScenario?.payload.datasets ?? []
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
    <div data-testid="rich-deliverable-block-fixture" data-block-type={validType}>
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
