<script lang="ts">
  import { blockType, type RichBlock, type RichMediaUrlFor } from '$lib/rich-deliverable';
  import './rich-blocks.css';
  import CalloutBlock from './blocks/CalloutBlock.svelte';
  import CardBlock from './blocks/CardBlock.svelte';
  import ChartBlock from './blocks/ChartBlock.svelte';
  import CodeBlock from './blocks/CodeBlock.svelte';
  import DashboardBlock from './blocks/DashboardBlock.svelte';
  import DayAgendaBlock from './blocks/DayAgendaBlock.svelte';
  import DisclosureBlock from './blocks/DisclosureBlock.svelte';
  import DividerBlock from './blocks/DividerBlock.svelte';
  import FigureBlock from './blocks/FigureBlock.svelte';
  import GalleryBlock from './blocks/GalleryBlock.svelte';
  import GridBlock from './blocks/GridBlock.svelte';
  import HeroBlock from './blocks/HeroBlock.svelte';
  import IncidentBlock from './blocks/IncidentBlock.svelte';
  import KeyValueBlock from './blocks/KeyValueBlock.svelte';
  import LinkBlock from './blocks/LinkBlock.svelte';
  import MarkdownBlock from './blocks/MarkdownBlock.svelte';
  import MermaidBlock from './blocks/MermaidBlock.svelte';
  import MetricBlock from './blocks/MetricBlock.svelte';
  import ModalBlock from './blocks/ModalBlock.svelte';
  import QuoteBlock from './blocks/QuoteBlock.svelte';
  import RawHtmlBlock from './blocks/RawHtmlBlock.svelte';
  import DecisionMatrixBlock from './blocks/DecisionMatrixBlock.svelte';
  import EvidenceReportBlock from './blocks/EvidenceReportBlock.svelte';
  import ResearchAnswerBlock from './blocks/ResearchAnswerBlock.svelte';
  import SectionBlock from './blocks/SectionBlock.svelte';
  import SourceListBlock from './blocks/SourceListBlock.svelte';
  import TableBlock from './blocks/TableBlock.svelte';
  import TimelineBlock from './blocks/TimelineBlock.svelte';
  import UnsupportedBlock from './blocks/UnsupportedBlock.svelte';

  export let block: RichBlock;
  export let sources: Record<string, unknown>[] = [];
  export let mediaUrlFor: RichMediaUrlFor = () => '';

  $: type = blockType(block);
</script>

{#if type === 'hero'}
  <HeroBlock {block} {sources} {mediaUrlFor} />
{:else if type === 'section' || type === 'stack'}
  <SectionBlock {block} {sources} {type} {mediaUrlFor} />
{:else if type === 'columns' || type === 'grid' || type === 'card_grid'}
  <GridBlock {block} {sources} {type} {mediaUrlFor} />
{:else if type === 'tabs' || type === 'accordion'}
  <DisclosureBlock {block} {sources} type={type} {mediaUrlFor} />
{:else if type === 'modal'}
  <ModalBlock {block} {sources} {mediaUrlFor} />
{:else if type === 'markdown'}
  <MarkdownBlock {block} />
{:else if type === 'raw_html'}
  <RawHtmlBlock {block} />
{:else if type === 'callout'}
  <CalloutBlock {block} />
{:else if type === 'card'}
  <CardBlock {block} {sources} {mediaUrlFor} />
{:else if type === 'action'}
  <CardBlock
    block={{ ...block, variant: typeof block.variant === 'string' ? block.variant : 'action' }}
    {sources}
    {mediaUrlFor}
    dataBlockType="action"
  />
{:else if type === 'dashboard' || type === 'status' || type === 'status_grid'}
  <DashboardBlock {block} type={type} />
{:else if type === 'metric'}
  <MetricBlock {block} />
{:else if type === 'kv' || type === 'key_value'}
  <KeyValueBlock {block} {type} />
{:else if type === 'timeline' || type === 'steps'}
  <TimelineBlock {block} {type} />
{:else if type === 'day_agenda'}
  <DayAgendaBlock {block} />
{:else if type === 'incident_timeline' || type === 'incident_checklist' || type === 'checklist'}
  <IncidentBlock {block} type={type} />
{:else if type === 'quote'}
  <QuoteBlock {block} />
{:else if type === 'divider'}
  <DividerBlock {block} {sources} {mediaUrlFor} />
{:else if type === 'figure'}
  <FigureBlock {block} {mediaUrlFor} />
{:else if type === 'gallery'}
  <GalleryBlock {block} {sources} {mediaUrlFor} />
{:else if type === 'research_answer'}
  <ResearchAnswerBlock {block} {sources} />
{:else if type === 'evidence_report' || type === 'claim_cards'}
  <EvidenceReportBlock {block} {sources} />
{:else if type === 'decision_matrix' || type === 'comparison_matrix'}
  <DecisionMatrixBlock {block} {sources} {type} />
{:else if type === 'table'}
  <TableBlock {block} {type} />
{:else if type === 'chart'}
  <ChartBlock {block} {sources} {mediaUrlFor} />
{:else if type === 'mermaid'}
  <MermaidBlock {block} {sources} {mediaUrlFor} />
{:else if type === 'code'}
  <CodeBlock {block} />
{:else if type === 'link' || type === 'link_preview'}
  <LinkBlock {block} {type} />
{:else if type === 'source_list'}
  <SourceListBlock {block} {sources} />
{:else}
  <UnsupportedBlock {block} {type} />
{/if}
