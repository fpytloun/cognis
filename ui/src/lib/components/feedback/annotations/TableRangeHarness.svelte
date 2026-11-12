<script lang="ts">
  import FeedbackSourceProvider from '../FeedbackSourceProvider.svelte';
  import RichAnnotations from './RichAnnotations.svelte';
  import TableBlock from '../../rich/blocks/TableBlock.svelte';
  import DecisionMatrixBlock from '../../rich/blocks/DecisionMatrixBlock.svelte';
  import { annotateRichPayload } from '$lib/feedback/annotations/rich';
  import type { FeedbackSurfaceBinding } from '$lib/feedback/types';
  import type { RichBlock } from '$lib/rich-deliverable';
  import '$lib/components/rich/rich-blocks.css';
  let { binding, block = { id: 'release-table', type: 'table', columns: ['name', 'count'],
    rows: [['Zulu', 3], { values: ['Alpha', 4] }, { name: 'Beta', count: 5 }] } }: {
    binding: FeedbackSurfaceBinding | null | undefined; block?: RichBlock;
  } = $props();
  const authored = $derived((annotateRichPayload({ blocks: [block] }) as { blocks: RichBlock[] }).blocks[0]);
</script>
{#snippet content()}
  <div class="rich-deliverable">
    <RichAnnotations block={authored} index={0} semantic={true}>
      {#if block.type === 'table'}<TableBlock block={authored} />
      {:else}<DecisionMatrixBlock block={authored} />{/if}
    </RichAnnotations>
  </div>
{/snippet}
{#if binding === undefined}{@render content()}
{:else}<FeedbackSourceProvider {binding} onOpen={() => {}}>{@render content()}</FeedbackSourceProvider>{/if}
