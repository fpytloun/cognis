<script lang="ts">
  import { renderInlineMarkdown, renderInlineMarkdownNoLinks, stripMarkdown } from '$lib/markdown';
  import { blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';
  import { objectList } from '../block-helpers';

  export let block: RichBlock;
  export let type = 'incident_timeline';

  $: entries = objectList(block.items ?? block.entries ?? block.timeline ?? block.data);
  $: checklist = objectList(block.checklist ?? block.remediation ?? block.actions);
</script>

<section class="rich-incident" data-rich-block-type={type}>
  <div class="rich-incident-heading">
    <div>
      {#if blockText(block, 'eyebrow')}<span>{@html renderInlineMarkdown(blockText(block, 'eyebrow'))}</span>{/if}
      {#if blockTitle(block)}<h4>{@html renderInlineMarkdown(blockTitle(block))}</h4>{/if}
      {#if blockText(block, 'description')}<p>{@html renderInlineMarkdown(blockText(block, 'description'))}</p>{/if}
    </div>
    <div class="rich-incident-pills">
      {#if blockText(block, 'severity')}<strong class="tone-{blockText(block, 'severity').toLowerCase()}">{@html renderInlineMarkdown(blockText(block, 'severity'))}</strong>{/if}
      {#if blockText(block, 'status')}<strong>{@html renderInlineMarkdown(blockText(block, 'status'))}</strong>{/if}
      {#if blockText(block, 'owner')}<strong>{@html renderInlineMarkdown(blockText(block, 'owner'))}</strong>{/if}
    </div>
  </div>

  {#if entries.length > 0}
    <ol class="rich-incident-timeline">
      {#each entries as entry, index}
        <li class="tone-{String(entry.tone ?? entry.severity ?? entry.status ?? 'neutral').toLowerCase()}">
          <span>{@html renderInlineMarkdown(String(entry.time ?? entry.timestamp ?? entry.step ?? index + 1))}</span>
          <details open={entry.open === true || index === 0}>
            <!-- <summary> is an interactive disclosure trigger; use the
                 "no links" renderer so an authored markdown link never
                 produces invalid nested interactive content here. -->
            <summary>
              <strong>{@html renderInlineMarkdownNoLinks(String(entry.title ?? entry.label ?? `Entry ${index + 1}`))}</strong>
              <em>{@html renderInlineMarkdownNoLinks(String(entry.status ?? entry.severity ?? ''))}</em>
            </summary>
            {#if entry.content || entry.description}<p>{@html renderInlineMarkdown(String(entry.content ?? entry.description))}</p>{/if}
            <div class="rich-incident-meta">
              {#if entry.owner}<span>Owner: {@html renderInlineMarkdown(String(entry.owner))}</span>{/if}
              {#if entry.duration}<span>Duration: {@html renderInlineMarkdown(String(entry.duration))}</span>{/if}
            </div>
          </details>
        </li>
      {/each}
    </ol>
  {/if}

  {#if checklist.length > 0}
    <div class="rich-incident-checklist">
      <h5>{@html renderInlineMarkdown(String(block.checklist_title ?? 'Remediation checklist'))}</h5>
      {#each checklist as item, index}
        <label class:done={item.done === true || item.checked === true || item.status === 'done'}>
          <input type="checkbox" checked={item.done === true || item.checked === true || item.status === 'done'} aria-label={stripMarkdown(String(item.title ?? item.label ?? `Checklist item ${index + 1}`))} />
          <span>{@html renderInlineMarkdown(String(item.title ?? item.label ?? item.action ?? `Checklist item ${index + 1}`))}</span>
          {#if item.owner}<em>{@html renderInlineMarkdown(String(item.owner))}</em>{/if}
          {#if item.status}<strong>{@html renderInlineMarkdown(String(item.status))}</strong>{/if}
        </label>
      {/each}
    </div>
  {/if}
</section>

<style>
  .rich-incident {
    break-inside: avoid;
    border: 1px solid var(--rich-line);
    border-radius: var(--rich-radius-lg);
    background:
      linear-gradient(145deg, var(--rich-surface), var(--rich-surface-solid)),
      radial-gradient(circle at 10% 0%, color-mix(in srgb, var(--rich-tone-danger-fg) 14%, transparent), transparent 34%);
    box-shadow: 0 20px 60px var(--rich-shadow-lg), inset 0 1px 0 var(--rich-inset-highlight);
    padding: clamp(1rem, 2vw, 1.45rem);
  }

  .rich-incident-heading {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1rem;
  }

  .rich-incident-heading span {
    color: var(--rich-accent-soft);
    font-size: 0.72rem;
    font-weight: 850;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .rich-incident-heading h4,
  .rich-incident-checklist h5 {
    margin: 0.15rem 0 0;
    color: var(--rich-text);
    letter-spacing: -0.035em;
  }

  .rich-incident-heading p,
  .rich-incident-timeline p {
    color: var(--rich-muted);
    line-height: 1.6;
  }

  .rich-incident-pills,
  .rich-incident-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
  }

  .rich-incident-pills strong,
  .rich-incident-meta span,
  .rich-incident-checklist em,
  .rich-incident-checklist strong {
    border: 1px solid var(--rich-tone-info-border);
    border-radius: 999px;
    background: var(--rich-tone-info-bg);
    color: var(--rich-tone-info-fg);
    padding: 0.28rem 0.5rem;
    font-size: 0.7rem;
    font-style: normal;
    font-weight: 800;
  }

  .rich-incident-pills :is(.tone-p0, .tone-p1, .tone-danger, .tone-critical) {
    border-color: var(--rich-tone-danger-border);
    background: var(--rich-tone-danger-bg);
    color: var(--rich-tone-danger-fg);
  }

  .rich-incident-timeline {
    display: grid;
    gap: 0.8rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .rich-incident-timeline li {
    display: grid;
    grid-template-columns: minmax(4rem, auto) 1fr;
    gap: 0.8rem;
    border: 1px solid var(--rich-line);
    border-radius: var(--rich-radius-sm);
    background: var(--rich-surface-raised);
    box-shadow: inset 0 1px 0 var(--rich-inset-highlight);
    padding: 0.8rem;
  }

  .rich-incident-timeline li > span {
    color: var(--rich-accent-soft);
    font-size: 0.8rem;
    font-weight: 850;
    font-variant-numeric: tabular-nums;
  }

  .rich-incident-timeline summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    cursor: pointer;
    color: var(--rich-text);
  }

  .rich-incident-timeline summary em {
    color: var(--rich-muted);
    font-size: 0.75rem;
    font-style: normal;
    text-transform: uppercase;
  }

  .rich-incident-timeline :is(.tone-danger, .tone-p0, .tone-p1, .tone-critical) {
    border-color: var(--rich-tone-danger-border);
  }

  .rich-incident-timeline .tone-warning {
    border-color: var(--rich-tone-warning-border);
  }

  .rich-incident-timeline :is(.tone-success, .tone-positive, .tone-resolved) {
    border-color: var(--rich-tone-success-border);
  }

  .rich-incident-checklist {
    display: grid;
    gap: 0.55rem;
    margin-top: 1rem;
    border-top: 1px solid var(--rich-line);
    padding-top: 1rem;
  }

  .rich-incident-checklist label {
    display: grid;
    grid-template-columns: auto 1fr auto auto;
    gap: 0.55rem;
    align-items: center;
    border: 1px solid var(--rich-line);
    border-radius: var(--rich-radius-sm);
    background: var(--rich-surface-solid);
    color: var(--rich-text);
    padding: 0.65rem;
  }

  .rich-incident-checklist label.done span {
    color: var(--rich-muted);
    text-decoration: line-through;
  }
</style>

