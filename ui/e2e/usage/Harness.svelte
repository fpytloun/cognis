<script lang="ts">
  import UsageDashboard from '../../src/lib/usage/UsageDashboard.svelte';
  import { fixtureQuery } from '../../src/lib/usage/fixtures';
  import { createUsageDataSource } from '../../src/lib/usage/adapter';
  import { UsageController, type UsageSnapshot } from '../../src/lib/usage/controller';
  import { readUsageQuery, restoreUsageHistory, usageHistoryState, usageRequestKey, writeUsageQuery } from '../../src/lib/usage/query-state';
  import { protocolFixture } from './protocol-fixture';
  import { onMount } from 'svelte';
  import type { UsageMode, UsageQuery } from '../../src/lib/usage/model';
  let query = $state<UsageQuery>(structuredClone(fixtureQuery));
  let fixtureState = $state('ready');
  let snapshot = $state<UsageSnapshot | null>(null);
  let controller = $state<UsageController | null>(null);
  const fixture = protocolFixture(() => fixtureState);
  const source = createUsageDataSource(fixture.send);
  const authority = 'usage-fixture@test.invalid:fixture';
  let activeKey = '';
  function change(next: UsageQuery) {
    const value = $state.snapshot(next.mode === 'project' ? { ...next, projectScope: { id: 'Cognis', label: 'Cognis' }, filters: { ...next.filters, project: 'Cognis' } } : { ...next, projectScope: undefined });
    query = value;
    window.history.pushState(usageHistoryState(value, authority), '', writeUsageQuery(new URL(window.location.href), value));
  }
  $effect(() => {
    const key = JSON.stringify([usageRequestKey(query), fixtureState]);
    if (!controller || key === activeKey) return;
    activeKey = key;
    controller.replace($state.snapshot(query));
    controller.setActive(fixtureState !== 'loading' && query.surface !== 'limits');
  });
  onMount(() => {
    const owner = new UsageController(source, (value) => snapshot = value);
    controller = owner;
    Object.assign(window, { __usageFixturePackets: fixture.packets, __usageFixtureCalls: fixture.calls });
    const restore = () => {
      const url = new URL(window.location.href);
      query = restoreUsageHistory(readUsageQuery(url, new Date(), url.searchParams.get('usage_scope') === 'project' ? { id: 'Cognis', label: 'Cognis' } : undefined), window.history.state, authority);
    };
    if (new URL(window.location.href).searchParams.has('usage_surface')) restore();
    window.addEventListener('popstate', restore);
    return () => { window.removeEventListener('popstate', restore); owner.dispose(); controller = null; };
  });
</script>
<div class="fixture">FIXTURE-BACKED · public-schema synthetic protocol · no real API integration
  <label>Scope <select aria-label="Fixture scope" value={query.mode} onchange={(event) => change({ ...query, mode: event.currentTarget.value as UsageMode })}><option value="admin">Global admin</option><option value="personal">Personal</option><option value="project">Project</option></select></label>
  <label>State <select aria-label="Fixture state" bind:value={fixtureState}><option>ready</option><option>empty</option><option>expired</option><option>denied</option><option>error</option><option>loading</option></select></label>
</div>
<div class="shell"><aside><div class="brand"><img src="/brand/cognis-mark.svg" alt=""/>cognis</div><div class="nav">Workspace</div><div class="nav">Projects</div><div class="nav">Agents</div><div class="nav active">Usage</div><div class="nav">Settings</div><div class="note">Local component harness<br/>Not a deployed Preview app</div></aside>
  <main><UsageDashboard result={snapshot?.result ?? { state: 'loading' }} {query} {source} onquery={change} onrefresh={() => controller?.invalidate()} surface={query.surface ?? 'overview'} onsurface={(surface) => change({ ...query, surface })}/></main>
</div>
<style>
  :global(*) { box-sizing:border-box; }:global(body) { margin:0; font:13px system-ui,sans-serif; background:rgb(var(--color-canvas)); color:rgb(var(--color-foreground)); }
  .fixture { background:rgb(var(--color-inset)); color:rgb(var(--color-secondary)); padding:8px 18px; display:flex; align-items:center; gap:16px; font-size:10px; flex-wrap:wrap; border-bottom:1px solid rgb(var(--color-separator)); }
  .fixture label { display:flex; align-items:center; gap:6px; }.fixture select { font:inherit; padding:4px; border:1px solid rgb(var(--color-control-border)); border-radius:4px; background:rgb(var(--color-surface)); color:inherit; }
  .shell { display:flex; min-height:calc(100vh - 43px); } aside { width:180px; flex-shrink:0; padding:24px 14px; border-right:1px solid rgb(var(--color-separator)); background:rgb(var(--color-chrome)); } .brand { display:flex; align-items:center; gap:8px; font-weight:650; font-size:23px; padding:0 8px 27px; letter-spacing:-1px; }.brand img { width:22px; }.nav { padding:11px 12px; color:rgb(var(--color-secondary)); margin-bottom:4px; }.active { background:rgb(var(--color-accent) / .1); color:rgb(var(--color-accent)); border-radius:5px; font-weight:600; }.note { padding:30px 12px; font-size:10px; line-height:1.7; color:rgb(var(--color-secondary)); } main { padding:26px 30px; flex:1; min-width:0; max-width:1600px; }
  @media(max-width:900px) { aside { display:none; } main { padding:18px; } } @media(max-width:500px) { main { padding:16px 12px; }.fixture { padding:8px 12px; gap:8px; } }
</style>
