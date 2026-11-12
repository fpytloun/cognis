<script lang="ts">
  import { onMount, untrack } from 'svelte';
  import type { TimelineScope } from '$lib/chat-v2/types';
  import { onConnectivityRestored } from '$lib/connectivity';
  import { auth } from '$lib/stores/auth';
  import { usageAnalyticsEnabled } from '$lib/system';
  import { loadConversationUsage } from './conversation-usage-source';
  import { wsClient } from '$lib/ws/client';
  import { usageEventAction } from '$lib/usage/events';
  import { formatCompactDecimalMoney, moneyTitle } from '$lib/usage/decimal';
  // Charts load only when the Usage tab renders, keeping them off the chat critical path.
  const breakdownModule = () => import('./ConversationUsageBreakdown.svelte');
  import {
    ConversationUsageController,
    usageTarget,
    type UsageInclusion,
    type UsageSource,
    type UsageState,
  } from './conversation-usage';

  let { scope, active = true, visible = true, source = loadConversationUsage } = $props<{
    scope: TimelineScope;
    active?: boolean;
    visible?: boolean;
    source?: UsageSource | null;
  }>();
  let inclusion = $state<UsageInclusion>('direct');
  let documentVisible = $state(false);
  let readState = $state<UsageState>({ status: 'idle' });
  /** Each completed total refresh also refreshes the breakdown. */
  let breakdownVersion = $state(0);
  let lastSummary: unknown = null;
  $effect(() => { if (readState.status === 'ready' && readState.summary !== lastSummary) { lastSummary = readState.summary; breakdownVersion++; } });
  const target = $derived(usageTarget(scope));
  const actor = $derived($auth.initialized && $auth.status === 'authenticated' && !$auth.offline
    ? $auth.user ? `${$auth.user.email}:${$auth.user.role}` : null : null);
  const controller = new ConversationUsageController((next) => { readState = next; });

  const compactNumber = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
  function compact(value: number | null): string { return value == null ? '—' : compactNumber.format(value); }
  function exact(value: number | null): string { return value == null ? '—' : value.toLocaleString('en'); }
  function pct(value: number): string { return `${Math.round(value * 100)}%`; }
  function displayCost(cost: { amount: string | null; currency: string }): string {
    if (cost.amount == null) return '—';
    try { return formatCompactDecimalMoney(cost.amount, cost.currency); } catch { return `${cost.amount} ${cost.currency}`; }
  }
  /** Cache reads are a subset of input; split only when that holds. Shares sum to 1. */
  function tokenMix(tokens: { input: number | null; output: number | null; cacheRead: number | null }) {
    const input = tokens.input ?? 0, output = tokens.output ?? 0;
    const cached = tokens.cacheRead != null && tokens.cacheRead <= input ? tokens.cacheRead : 0;
    const total = input + output;
    if (total <= 0) return null;
    return { fresh: (input - cached) / total, cached: cached / total, output: output / total };
  }

  $effect(() => {
    const input = { target, inclusion, active: $usageAnalyticsEnabled && active && Boolean(actor), actor, visible: visible && documentVisible, source };
    untrack(() => controller.update(input));
  });

  onMount(() => {
    const visibility = () => { documentVisible = document.visibilityState === 'visible'; };
    visibility();
    document.addEventListener('visibilitychange', visibility);
    const reconnect = onConnectivityRestored(() => { void controller.refresh(); });
    const events = wsClient.subscribe((event) => {
      const action = usageEventAction(event);
      if (!action || event.type === 'reconnected') return;
      // 'clear' changes authority, so it reloads now; anything else updates in place.
      if (action === 'clear') void controller.refresh();
      else if (!('conversation_id' in event) || event.conversation_id === scope.conversation_id) controller.invalidate();
    });
    const invalidate = (event: Event) => {
      const key = (event as CustomEvent<{ scopeKey?: string }>).detail?.scopeKey;
      if (key === scope.key) controller.invalidate();
    };
    window.addEventListener('cognis:work-invalidated', invalidate);
    return () => {
      document.removeEventListener('visibilitychange', visibility);
      window.removeEventListener('cognis:work-invalidated', invalidate);
      reconnect();
      events();
      controller.destroy();
    };
  });
</script>

<section class="usage-panel min-w-0 space-y-4 text-sm" aria-label="Usage accounting">
  {#if !$usageAnalyticsEnabled}
    <p class="text-secondary">Usage analytics is disabled in Cognis Settings.</p>
  {:else}
  <fieldset class="segmented" disabled={!target}
    title={target?.kind === 'conversation' ? 'Whole conversation history, including compaction rotations' : 'Selected execution and its continuation lane'}>
    <legend class="sr-only">Usage scope</legend>
    <label class:selected={inclusion === 'direct'} style:min-height="44px">
      <input class="sr-only" type="radio" bind:group={inclusion} value="direct" />
      {target?.kind === 'conversation' ? 'Conversation only' : 'Session only'}
    </label>
    <label class:selected={inclusion === 'inclusive'} style:min-height="44px">
      <input class="sr-only" type="radio" bind:group={inclusion} value="inclusive" />Including children
    </label>
  </fieldset>
  {#if !actor}
    <p class="text-secondary">Usage requires a verified online sign-in.</p>
  {:else if readState.status === 'loading'}
    <p role="status" class="text-quiet">Loading usage…</p>
  {:else if readState.status === 'unavailable'}
    <p class="text-secondary">{readState.reason}</p>
  {:else if readState.status === 'error'}
    <p role="alert" class="text-danger">{readState.message}</p>
    <button type="button" class="min-h-11 text-accent" onclick={() => void controller.refresh()}>Retry</button>
  {:else if readState.status === 'ready'}
    {@const summary = readState.summary}
    {@const mix = tokenMix(summary.tokens)}
    <div class="hero">
      <p class="hero-label">{summary.costs.some((cost) => cost.basis === 'provider_reported') ? 'Cost' : 'Estimated cost'}</p>
      {#each summary.costs as cost}
        <p class="hero-value tabular-nums" title={moneyTitle(cost.amount, cost.currency)}>{displayCost(cost)}</p>
        {#if cost.basis === 'provider_reported'}<p class="hero-hint">{cost.currency} · Provider-reported cost</p>
        {:else if summary.costs.length > 1}<p class="hero-hint">{cost.currency}</p>{/if}
      {:else}
        <p class="hero-value text-quiet">—</p>
        <p class="hero-hint">{summary.unpricedAttempts ? `No price for ${summary.unpricedAttempts} ${summary.unpricedAttempts === 1 ? 'call' : 'calls'}` : 'Not priced'}</p>
      {/each}
    </div>
    <dl class="stats">
      <div><dt>Input</dt><dd class="tabular-nums" title={exact(summary.tokens.input)}>{compact(summary.tokens.input)}</dd></div>
      <div><dt>Output</dt><dd class="tabular-nums" title={exact(summary.tokens.output)}>{compact(summary.tokens.output)}</dd></div>
      <div><dt>Cached</dt><dd class="tabular-nums" title={exact(summary.tokens.cacheRead)}>{compact(summary.tokens.cacheRead)}</dd></div>
      <div><dt>Calls</dt><dd class="tabular-nums">{compact(summary.attempts.total ?? summary.attempts.succeeded)}</dd></div>
    </dl>
    {#if mix}
      <figure class="mix" aria-label="Token mix">
        <div class="bar" role="img" aria-label={`Fresh input ${pct(mix.fresh)}, cached input ${pct(mix.cached)}, output ${pct(mix.output)}`}>
          {#if mix.fresh > 0}<span class="seg fresh" style:width={pct(mix.fresh)}></span>{/if}
          {#if mix.cached > 0}<span class="seg cached" style:width={pct(mix.cached)}></span>{/if}
          {#if mix.output > 0}<span class="seg output" style:width={pct(mix.output)}></span>{/if}
        </div>
        <figcaption>
          <span><i class="dot fresh"></i>Fresh input {pct(mix.fresh)}</span>
          <span><i class="dot cached"></i>Cached {pct(mix.cached)}</span>
          <span><i class="dot output"></i>Output {pct(mix.output)}</span>
        </figcaption>
      </figure>
    {/if}
    <!-- Conversations explain failed and unpriced calls in the breakdown below. -->
    {#if target?.kind !== 'conversation' && (summary.attempts.failed || summary.unpricedAttempts)}
      <p class="text-quiet">
        {[summary.attempts.failed ? `${exact(summary.attempts.failed)} failed` : '', summary.unpricedAttempts ? `${exact(summary.unpricedAttempts)} not priced` : ''].filter(Boolean).join(' · ')}
      </p>
    {/if}
    {#if target?.kind === 'conversation'}
      {#await breakdownModule() then module}
        <module.default conversationId={target.id} {inclusion} version={breakdownVersion}/>
      {/await}
    {/if}
  {/if}
  {/if}
</section>
<style>
  .segmented { display: inline-flex; gap: 2px; padding: 2px; border-radius: 9999px; background: rgb(var(--color-raised)); border: 1px solid rgb(var(--color-separator)); }
  .segmented label { display: inline-flex; align-items: center; min-height: 32px; padding: 0 0.85rem; border-radius: 9999px; font-size: 0.8125rem; color: rgb(var(--color-secondary)); cursor: pointer; white-space: nowrap; }
  .segmented label.selected { background: rgb(var(--color-surface)); color: rgb(var(--color-foreground)); font-weight: 600; box-shadow: 0 1px 2px rgb(0 0 0 / 0.2); }
  .segmented label:focus-within { outline: 2px solid rgb(var(--color-accent)); outline-offset: 1px; }
  .hero { padding: 0.25rem 0; }
  .hero-label { font-size: 0.75rem; letter-spacing: 0.06em; text-transform: uppercase; color: rgb(var(--color-quiet)); }
  .hero-value { font-size: 2rem; line-height: 2.4rem; font-weight: 600; color: rgb(var(--color-foreground)); }
  .hero-hint { font-size: 0.8125rem; color: rgb(var(--color-secondary)); }
  .stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.5rem; }
  .stats > div { border-radius: 0.75rem; background: rgb(var(--color-raised)); padding: 0.6rem 0.7rem; min-width: 0; }
  .stats dt { font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.05em; color: rgb(var(--color-quiet)); }
  .stats dd { margin-top: 0.15rem; font-size: 1.05rem; font-weight: 600; color: rgb(var(--color-foreground)); overflow: hidden; text-overflow: ellipsis; }
  .mix { margin: 0; }
  .bar { display: flex; height: 0.6rem; overflow: hidden; border-radius: 9999px; background: rgb(var(--color-raised)); }
  .seg { display: block; height: 100%; }
  .fresh { background: rgb(var(--color-accent)); }
  .cached { background: rgb(var(--color-accent) / 0.45); }
  .output { background: rgb(var(--color-success, 34 197 94)); }
  figcaption { display: flex; flex-wrap: wrap; gap: 0.25rem 0.9rem; margin-top: 0.45rem; font-size: 0.75rem; color: rgb(var(--color-secondary)); }
  .dot { display: inline-block; width: 0.5rem; height: 0.5rem; border-radius: 9999px; margin-right: 0.35rem; vertical-align: middle; }
  @media (max-width: 420px) { .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 767px), (pointer: coarse) {
    .segmented label, button { min-height: 44px; }
    button { min-width: 44px; }
  }
</style>
