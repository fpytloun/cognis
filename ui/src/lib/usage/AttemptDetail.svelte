<script lang="ts">
  import { averageMs, throughput, count, type UsageAttempt, type UsageMode } from './model';
  import CostValues from './CostValues.svelte';
  import { mediaQuantity, operationLabel } from './media';
  /** Request detail: what was asked, what ran, how long, what it cost, who called. */
  let { attempt, mode }: { attempt: UsageAttempt; mode: UsageMode } = $props();
  const unknown = { totalMs: null, samples: 0 };
  const yesNo = (value: boolean | null | undefined) => value == null ? '—' : value ? 'Yes' : 'No';
</script>

{#snippet field(label: string, value: string | null | undefined)}
  <div class="min-w-0">
    <dt class="text-xs text-secondary">{label}</dt>
    <dd class="m-0 truncate text-sm tabular-nums" title={value ?? undefined}>{value || '—'}</dd>
  </div>
{/snippet}

<section class="min-w-0" aria-label="Attempt details">
  <h3 class="m-0 mb-3 truncate text-sm font-semibold" title={attempt.id ?? undefined}>{attempt.model} · {attempt.outcome}</h3>
  {#if attempt.detail}
    {@const detail = attempt.detail}
    {#if detail.error}
      <div class="mb-3 rounded-xl border border-danger-border bg-danger-subtle px-3 py-2 text-sm">
        <b class="text-danger">{detail.error.category}{detail.error.code ? ` · ${detail.error.code}` : ''}</b>
        {#if detail.error.summary}<p class="m-0 mt-1 text-secondary">{detail.error.summary}</p>{/if}
      </div>
    {/if}
    <div class="grid gap-5 lg:grid-cols-2">
      <div>
        <h4 class="m-0 mb-2 text-xs font-semibold uppercase tracking-wide text-secondary">Request</h4>
        <dl class="m-0 grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
          {@render field('Requested model', detail.requestedModel)}
          {@render field('Provider', detail.provider)}
          {@render field('Effort', attempt.effort)}
          {@render field('State', detail.lifecycle)}
          {@render field('Operation', operationLabel(detail.inferenceOperation ?? null))}
          {@render field('Retry', yesNo(detail.retry))}
          {@render field('Fallback', yesNo(detail.fallback))}
          {@render field('Service', detail.service ? `${detail.service}${detail.operation ? ` · ${detail.operation}` : ''}` : null)}
          {@render field('Session', detail.sessionId ?? 'Session-less request')}
        </dl>
      </div>
      <div>
        <h4 class="m-0 mb-2 text-xs font-semibold uppercase tracking-wide text-secondary">Usage, cost and timing</h4>
        <dl class="m-0 grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
          {@render field('Input', count(attempt.input))}
          {@render field('Cached input', count(attempt.cacheRead))}
          {@render field('Output', count(attempt.output))}
          {#each detail.mediaUsage ?? [] as quantity (quantity.name)}
            {@render field('Media usage', mediaQuantity(quantity))}
          {/each}
          <div class="min-w-0"><dt class="text-xs text-secondary">Cost</dt><dd class="m-0 text-sm"><CostValues cost={attempt.cost} costs={attempt.costs}/></dd></div>
          {@render field('Request time', averageMs(detail.timings.providerSpan))}
          {@render field('First token', averageMs(detail.timings.providerTtft ?? unknown))}
          {@render field('Output speed', throughput(detail.timings.generation))}
          {@render field('Executor wait', averageMs(detail.timings.executorSpan))}
        </dl>
      </div>
    </div>
    {#if mode !== 'project' && detail.identifiers}
      <h4 class="m-0 mb-2 mt-5 text-xs font-semibold uppercase tracking-wide text-secondary">Client</h4>
      <dl class="m-0 grid grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-3">
        {@render field('Client', detail.identifiers.client)}
        {@render field('API key', detail.identifiers.key)}
        {@render field('Request ID', detail.identifiers.request)}
      </dl>
    {/if}
    {#if detail.parameters.length}
      <details class="mt-5">
        <summary class="cursor-pointer text-xs font-medium text-accent">Parameters ({detail.parameters.length})</summary>
        <table class="mt-2 w-full border-collapse text-sm">
          <thead><tr>
            <th scope="col" class="border-b border-separator px-2 py-1.5 text-left text-xs font-semibold text-secondary">Parameter</th>
            <th scope="col" class="border-b border-separator px-2 py-1.5 text-left text-xs font-semibold text-secondary">Requested</th>
            <th scope="col" class="border-b border-separator px-2 py-1.5 text-left text-xs font-semibold text-secondary">Effective</th>
          </tr></thead>
          <tbody>
            {#each detail.parameters as parameter (parameter.name)}
              <tr>
                <th scope="row" class="border-b border-separator/70 px-2 py-1.5 text-left font-normal">{parameter.name}</th>
                <td class="border-b border-separator/70 px-2 py-1.5">{parameter.requested ?? '—'}</td>
                <td class="border-b border-separator/70 px-2 py-1.5">{parameter.effective ?? '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </details>
    {/if}
  {:else}
    <p class="text-sm text-secondary">No further detail for this attempt.</p>
  {/if}
</section>
