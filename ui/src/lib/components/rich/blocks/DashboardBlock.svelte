<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks } from '$lib/markdown';
  import { blockDescription, blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { blockTone, objectList, valueText } from '../block-helpers';
  import RichIcon from '../RichIcon.svelte';

  export let block: RichBlock;
  export let type = 'dashboard';

  $: cards = objectList(block.metrics ?? block.items ?? block.cards ?? block.data ?? block.blocks);

  function sparklinePoints(value: unknown): string {
    const values = Array.isArray(value)
      ? value.map((item) => Number(typeof item === 'object' && item !== null && !Array.isArray(item) ? (item as Record<string, unknown>).value : item)).filter(Number.isFinite)
      : [];
    if (values.length === 0) return '';
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || 1;
    return values
      .map((item, index) => {
        const x = values.length === 1 ? 48 : (index / (values.length - 1)) * 96;
        const y = 36 - ((item - min) / span) * 30;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(' ');
  }
</script>

<section class="rich-dashboard" data-rich-block-type={type}>
  <div class="rich-dashboard-heading">
    <div>
      {#if blockText(block, 'eyebrow')}<span>{@html renderInlineMarkdown(blockText(block, 'eyebrow'))}</span>{/if}
      {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
      {#if blockDescription(block)}<p>{@html renderInlineMarkdown(blockDescription(block))}</p>{/if}
    </div>
    {#if blockText(block, 'status')}
      <strong class="rich-status-pill tone-{blockTone(block)}">{@html renderInlineMarkdown(blockText(block, 'status'))}</strong>
    {/if}
  </div>

  <div class="rich-dashboard-grid">
    {#each cards as card, index}
      <article class="rich-dashboard-card tone-{String(card.tone ?? card.status ?? 'neutral')}">
        <div class="rich-dashboard-card-top">
          <span>
            {#if card.icon}<RichIcon icon={card.icon} />{/if}
            {@html renderInlineMarkdown(String(card.label ?? card.title ?? card.name ?? `Metric ${index + 1}`))}
          </span>
          {#if card.status}<strong>{@html renderInlineMarkdown(String(card.status))}</strong>{/if}
        </div>
        <div class="rich-dashboard-value">
          <strong>{@html renderInlineMarkdown(valueText(card.value ?? card.current ?? card.count ?? ''))}</strong>
          {#if card.delta}<em>{@html renderInlineMarkdown(String(card.delta))}</em>{/if}
        </div>
        {#if sparklinePoints(card.sparkline ?? card.trend)}
          <svg class="rich-sparkline" viewBox="0 0 96 40" role="img" aria-label="Sparkline">
            <polyline points={sparklinePoints(card.sparkline ?? card.trend)} />
          </svg>
        {/if}
        {#if card.description || card.explanation || card.summary || card.dek || card.drilldown}
          <details>
            <summary>{@html renderInlineMarkdownNoLinks(String(card.detail_label ?? 'Details'))}</summary>
            {#if card.description || card.explanation || card.summary || card.dek}
              <p>{@html renderInlineMarkdown(String(card.description ?? card.explanation ?? card.summary ?? card.dek))}</p>
            {/if}
            {#if Array.isArray(card.drilldown)}
              <ul>
                {#each card.drilldown as drilldown}
                  <li>{@html renderInlineMarkdown(valueText(drilldown))}</li>
                {/each}
              </ul>
            {/if}
          </details>
        {/if}
      </article>
    {/each}
  </div>
</section>

<style>
  .rich-dashboard {
    break-inside: avoid;
    border: 1px solid var(--rich-line);
    border-radius: var(--rich-radius-lg);
    background:
      linear-gradient(145deg, var(--rich-surface), var(--rich-surface-solid)),
      radial-gradient(circle at 12% 0%, color-mix(in srgb, var(--rich-tone-success-fg) 16%, transparent), transparent 34%);
    box-shadow: 0 20px 60px var(--rich-shadow-lg), inset 0 1px 0 var(--rich-inset-highlight);
    padding: clamp(1rem, 2vw, 1.45rem);
  }

  .rich-dashboard-heading {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .rich-dashboard-heading span {
    color: var(--rich-accent-soft);
    font-size: 0.72rem;
    font-weight: 850;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .rich-dashboard-heading h4 {
    margin: 0.15rem 0 0;
    color: var(--rich-text);
    letter-spacing: -0.035em;
  }

  .rich-dashboard-heading p,
  .rich-dashboard-card p,
  .rich-dashboard-card li {
    color: var(--rich-muted);
    line-height: 1.55;
  }

  .rich-dashboard-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 13rem), 1fr));
    gap: 0.85rem;
  }

  .rich-dashboard-card {
    min-height: 100%;
    border: 1px solid var(--rich-line);
    border-radius: var(--rich-radius-md);
    background: var(--rich-surface-raised);
    box-shadow: inset 0 1px 0 var(--rich-inset-highlight);
    transition: border-color .16s ease, transform .16s ease;
    padding: 0.9rem;
  }

  .rich-dashboard-card:is(.tone-success, .tone-positive, .tone-warning, .tone-danger, .tone-critical, .tone-info) {
    border-color: var(--rich-tone-border);
    background:
      linear-gradient(160deg, color-mix(in srgb, var(--rich-tone-bg) 70%, var(--rich-surface-raised)), var(--rich-surface-raised));
  }

  .rich-status-pill:is(.tone-success, .tone-positive, .tone-warning, .tone-danger, .tone-critical, .tone-info) {
    border-color: var(--rich-tone-border);
    background: var(--rich-tone-bg);
    color: var(--rich-tone-fg);
  }

  .rich-dashboard-card-top,
  .rich-dashboard-value {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.75rem;
  }

  .rich-dashboard-card-top span,
  .rich-dashboard-card-top strong,
  .rich-status-pill {
    color: var(--rich-accent-soft);
    font-size: 0.72rem;
    font-weight: 850;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .rich-status-pill {
    align-self: flex-start;
    border: 1px solid var(--rich-tone-info-border);
    border-radius: 999px;
    background: var(--rich-tone-info-bg);
    padding: 0.35rem 0.6rem;
  }

  .rich-dashboard-value strong {
    color: var(--rich-text);
    font-size: clamp(1.7rem, 3vw, 2.45rem);
    letter-spacing: -0.055em;
    font-variant-numeric: tabular-nums;
  }

  .rich-dashboard-value em {
    color: var(--rich-tone-success-fg);
    font-style: normal;
    font-weight: 800;
  }

  .rich-sparkline {
    width: 100%;
    height: 3rem;
    margin-top: 0.5rem;
    overflow: visible;
  }

  .rich-sparkline polyline {
    fill: none;
    stroke: var(--rich-accent);
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 3;
    filter: drop-shadow(0 0 4px color-mix(in srgb, var(--rich-accent) 55%, transparent));
  }

  .rich-dashboard-card details {
    margin-top: 0.65rem;
  }

  .rich-dashboard-card summary {
    cursor: pointer;
    color: var(--rich-accent-soft);
    font-size: 0.8rem;
    font-weight: 800;
  }
</style>

