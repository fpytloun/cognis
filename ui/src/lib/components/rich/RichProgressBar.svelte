<script lang="ts">
  import { progressPercent } from './block-helpers';

  export let value: unknown;
  export let max: unknown = 100;
  export let label = '';
  export let tone = 'neutral';

  $: percent = progressPercent(value, max);
  $: numericValue = Number(value);
  $: numericMax = Number.isFinite(Number(max)) && Number(max) > 0 ? Number(max) : 100;
  $: ariaValue = Number.isFinite(numericValue) ? Math.max(0, Math.min(numericMax, numericValue)) : undefined;
</script>

<div class="rich-progress tone-{tone}">
  <div
    class="rich-progress-track"
    role="progressbar"
    aria-label={label || 'Progress'}
    aria-valuenow={ariaValue}
    aria-valuemin={0}
    aria-valuemax={numericMax}
  >
    <div class="rich-progress-fill" style:width={`${percent}%`}></div>
  </div>
  {#if label}<span class="rich-progress-label">{label}</span>{/if}
</div>

<style>
  .rich-progress {
    display: flex;
    align-items: center;
    gap: var(--rich-space-2);
    width: 100%;
  }

  .rich-progress-track {
    flex: 1;
    overflow: hidden;
    min-width: 3rem;
    height: 0.4rem;
    border-radius: var(--rich-radius-pill);
    background: color-mix(in srgb, var(--rich-line) 70%, transparent);
  }

  .rich-progress-fill {
    height: 100%;
    border-radius: inherit;
    background: var(--rich-tone-fg, var(--rich-accent));
    transition: width 200ms ease;
  }

  .tone-success .rich-progress-fill,
  .tone-positive .rich-progress-fill { background: var(--rich-tone-success-accent); }
  .tone-warning .rich-progress-fill { background: var(--rich-tone-warning-accent); }
  .tone-danger .rich-progress-fill,
  .tone-critical .rich-progress-fill { background: var(--rich-tone-danger-accent); }
  .tone-info .rich-progress-fill { background: var(--rich-accent); }

  .rich-progress-label {
    flex: 0 0 auto;
    color: var(--rich-muted);
    font-size: var(--rich-fs-2xs);
    font-weight: var(--rich-fw-semibold);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
</style>
