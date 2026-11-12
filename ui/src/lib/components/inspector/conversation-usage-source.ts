import type { UsageSummaryResponse } from '$lib/types/api';
import type { UsageInclusion, UsageResult, UsageSource, UsageTarget } from './conversation-usage';

/** Adapt only the exact selected population; never reconstruct membership in the UI. */
export function summaryResult(
  response: UsageSummaryResponse,
  target: UsageTarget,
  inclusion: UsageInclusion,
): UsageResult {
  const item = response.items.length === 1 ? response.items[0] : null;
  if (!item || item.selector.kind !== target.kind || item.selector.id !== target.id
    || item.inclusion !== inclusion) {
    throw new Error('Accounting response does not match the selected population.');
  }
  if (!item.available || !item.metrics || item.availability?.resolution === 'unavailable'
    || response.meta.availability?.resolution === 'unavailable') {
    return { status: 'unavailable', reason: 'Accounting is unavailable for this selection or its lineage.' };
  }
  const metrics = item.metrics;
  const meta = response.meta;
  const observed = metrics.observed_attempts ?? null;
  const hasObservations = (observed ?? metrics.finalized_attempts) > 0;
  const notes = [...meta.limitations];
  if (!hasObservations) notes.push('No accounting observations are available. This does not prove zero usage.');
  if (meta.private_resources_excluded) notes.push('Private resources are excluded.');
  if (meta.excluded_fields.length) notes.push('Some accounting fields are excluded.');
  if (metrics.partially_priced_attempts != null) notes.push(`Partially priced attempts: ${metrics.partially_priced_attempts}.`);
  if (metrics.unallocatable_cost_attempts != null) notes.push(`Attempts without an allocatable price band: ${metrics.unallocatable_cost_attempts}.`);
  if (meta.projection && meta.projection.state !== 'ready') notes.push(`Accounting projection is ${meta.projection.state}.`);
  return {
    status: 'ready',
    summary: {
      coverage: !hasObservations || meta.capture?.state === 'unknown' ? 'unknown'
        : meta.complete && !meta.accounting_degraded && meta.capture?.state !== 'gaps'
          && (!meta.projection || meta.projection.state === 'ready') ? 'complete' : 'partial',
      costs: metrics.costs.map((cost) => ({
        currency: cost.currency, basis: cost.basis, amount: cost.known_amount,
        complete: cost.quality === 'complete',
      })),
      unpricedAttempts: hasObservations ? metrics.unpriced_attempts : null,
      tokens: {
        input: metrics.tokens.input, output: metrics.tokens.output,
        cacheRead: metrics.tokens.cache_read, cacheWrite: metrics.tokens.cache_write,
        reasoning: metrics.tokens.reasoning,
      },
      attempts: {
        total: hasObservations ? observed : null,
        succeeded: hasObservations ? metrics.states.succeeded : null,
        failed: hasObservations ? metrics.states.failed : null,
      },
      representedSessions: item.physical_sessions ?? null,
      observedThrough: meta.observed_through,
      notes,
    },
  };
}

/** Default production source; the transport module is loaded only on visible demand. */
export const loadConversationUsage: UsageSource = async (target, inclusion, signal) => {
  const { fetchUsageSummaries } = await import('$lib/usage/conversation-api');
  signal.throwIfAborted();
  const response = await fetchUsageSummaries({
    items: [target], inclusion, pricing_basis: 'historical',
  }, { signal });
  signal.throwIfAborted();
  return summaryResult(response, target, inclusion);
};
