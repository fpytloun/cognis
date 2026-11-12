<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import { providerMonitoring, publishProviderMonitors } from '$lib/provider-monitor';
  import { providerCards } from '$lib/provider-cards';
  import ChevronLeft from 'lucide-svelte/icons/chevron-left';
  import ChevronRight from 'lucide-svelte/icons/chevron-right';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';
  import type { DashboardIssuesResponse, ProviderMonitor, Project } from '$lib/types/api';
  import ProjectAvatar from '$lib/components/projects/ProjectAvatar.svelte';
  import AccessibleTabs from '$lib/components/ui/AccessibleTabs.svelte';
  import ProviderHealthLine from '$lib/components/ProviderHealthLine.svelte';
  import IssuesStrip from './IssuesStrip.svelte';
  import InboxList from '$lib/components/InboxList.svelte';
  import { inbox } from '$lib/stores/inbox';
  import { useSectionConnectivity } from '$lib/connectivity-section.svelte';

  let { issues, projects = [], loading = false, error = null, onRetry, onDismiss, onMonitorChange = () => {} }: {
    projects?: Project[];
    issues: DashboardIssuesResponse | null; loading?: boolean; error?: string | null;
    onRetry: () => void; onDismiss: () => void | Promise<void>;
    onMonitorChange?: () => void;
  } = $props();
  let providers = $state<ProviderMonitor[]>([]);
  let providerError = $state('');
  const net = useSectionConnectivity({ hasError: () => Boolean(providerError), retry: () => refresh() });
  let selected = $state<string | null>(null);
  const collapsedKey = 'cognis.dashboardHealth.collapsed';
  const selectedKey = 'cognis.dashboardHealth.selected';
  let collapsed = $state(false);
  let signature = '';
  let scroller = $state<HTMLDivElement>();
  let cardIndex = $state(0);
  const cards = $derived(providerCards(providers.map((provider) => $providerMonitoring[provider.provider_id] ?? provider)));
  const hasNotifications = $derived(Boolean(error || issues?.issues.length || $inbox.unreadCount || $inbox.error));
  const hasProviders = $derived(providers.length > 0 || Boolean(providerError));
  const tabs = $derived([
    ...(hasNotifications ? [{ id: 'notifications', label: 'Notifications', count: (issues?.issues.length ?? 0) + $inbox.unreadCount }] : []),
    ...(projects.length ? [{ id: 'projects', label: 'Projects', count: projects.length }] : []),
    ...(hasProviders ? [{ id: 'providers', label: 'Providers', count: providers.length }] : [])
  ]);
  const active = $derived(tabs.some((tab) => tab.id === selected) ? selected! : tabs[0]?.id);

  function setCollapsed(value: boolean): void {
    collapsed = value;
    try { window.localStorage.setItem(collapsedKey, String(value)); }
    catch { /* Keep disclosure usable when browser storage is unavailable. */ }
  }

  function selectTab(id: string): void {
    selected = id;
    try { window.localStorage.setItem(selectedKey, id); }
    catch { /* In-memory selection still works without client storage. */ }
    if (collapsed) setCollapsed(false);
  }

  function syncCard(): void {
    if (!scroller) return;
    const nodes = Array.from(scroller.querySelectorAll<HTMLElement>('[data-provider-card]'));
    let closest = 0;
    let distance = Infinity;
    nodes.forEach((node, index) => {
      const delta = Math.abs(node.offsetLeft - scroller!.scrollLeft);
      if (delta < distance) { distance = delta; closest = index; }
    });
    cardIndex = closest;
  }

  function cycle(direction: number): void {
    if (!scroller || !cards.length) return;
    const index = (cardIndex + direction + cards.length) % cards.length;
    const node = scroller.querySelectorAll<HTMLElement>('[data-provider-card]')[index];
    if (!node) return;
    cardIndex = index;
    scroller.scrollTo({
      left: node.offsetLeft,
      behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'
    });
  }

  async function refresh(): Promise<void> {
    const owner = auth.getSnapshot().user?.email;
    try {
      const values = await api.llmProviders.monitorList();
      if (owner !== auth.getSnapshot().user?.email) return;
      const nextSignature = JSON.stringify(values.map((value) => [value.provider_id, value.checked_at, value.alert_severity, value.error]));
      if (signature !== nextSignature) { signature = nextSignature; onMonitorChange(); }
      providers = values;
      publishProviderMonitors(values);
      providerError = '';
    } catch { if (owner === auth.getSnapshot().user?.email) providerError = 'Provider status unavailable.'; }
  }

  onMount(() => {
    try { selected = window.localStorage.getItem(selectedKey); }
    catch { /* Default to the first available tab. */ }
    try { collapsed = window.localStorage.getItem(collapsedKey) === 'true'; }
    catch { /* Use the expanded default when browser storage is unavailable. */ }
    void refresh();
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 30_000);
    const resume = () => { if (!document.hidden) void refresh(); };
    document.addEventListener('visibilitychange', resume);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', resume); };
  });
</script>

{#if tabs.length}
  <section class="min-w-0" aria-label="Notifications, projects and provider health" data-testid="dashboard-health-strip">
    <div class="flex min-w-0 items-center border-b border-separator">
      <div class="min-w-0 flex-1">
        <AccessibleTabs {tabs} activeId={active ?? tabs[0].id} idPrefix="dashboard-health" ariaLabel="Dashboard health" onChange={selectTab} onReselect={(id) => { const next = !collapsed; selectTab(id); setCollapsed(next); }} edgeFade={false} />
      </div>
      <button type="button"
        class="flex h-11 w-11 shrink-0 items-center justify-center rounded text-muted hover:bg-raised/60 hover:text-foreground focus-visible:outline focus-visible:outline-accent"
        aria-label={collapsed ? 'Expand dashboard status' : 'Collapse dashboard status'}
        title={collapsed ? 'Expand dashboard status' : 'Collapse dashboard status'}
        aria-expanded={!collapsed} aria-controls={`dashboard-health-panel-${active}`}
        onclick={() => setCollapsed(!collapsed)}>
        {#if collapsed}<ChevronDown class="h-4 w-4" />{:else}<ChevronUp class="h-4 w-4" />{/if}
      </button>
    </div>
    <div id={`dashboard-health-panel-${active}`} role="tabpanel" hidden={collapsed}
      aria-labelledby={`dashboard-health-tab-${active}`}>
      {#if active === 'notifications'}
        <IssuesStrip {issues} {loading} {error} {onRetry} {onDismiss} />
        {#if $inbox.unreadCount || ($inbox.error && !net.hideErrors)}<div class="mt-2"><InboxList compact /></div>{/if}
      {:else if active === 'projects'}
        <nav class="flex min-w-0 gap-3 overflow-x-auto overscroll-x-contain border-b border-separator py-3"
          aria-label="Projects" data-testid="dashboard-project-strip">
          {#each projects as project (project.project_id)}
            <a href={`/projects/${project.project_id}`} class="flex w-20 shrink-0 flex-col items-center gap-1.5 rounded-xl p-2 text-center hover:bg-raised/60" title={project.name}>
              <ProjectAvatar name={project.name} avatarUrl={project.avatar_url} icon={project.metadata?.icon} color={project.color} class="h-10 w-10" />
              <span class="w-full truncate text-xs text-secondary">{project.name}</span>
            </a>
          {/each}
        </nav>
      {:else}
        <div class="relative min-w-0 border-b border-separator px-7 py-2 sm:px-3" data-testid="dashboard-providers">
          <div bind:this={scroller} onscroll={syncCard}
            class="relative flex min-w-0 snap-x snap-mandatory gap-6 overflow-x-auto overscroll-x-contain sm:snap-none"
            aria-label="Provider cards" data-testid="provider-card-scroller">
            {#each cards as card (card.map((provider) => provider.provider_id).join(':'))}
              <div class="min-w-0 basis-full shrink-0 snap-start snap-always sm:w-80 sm:basis-auto"
                data-provider-card data-testid="provider-card">
                <div class="flex flex-col gap-1">
                  {#each card as provider (provider.provider_id)}
                    <ProviderHealthLine providerId={provider.provider_id} />
                  {/each}
                </div>
              </div>
            {/each}
          </div>
          {#if cards.length > 1}
            <button type="button" class="absolute inset-y-0 left-0 flex w-7 items-center justify-center text-quiet hover:text-foreground focus-visible:outline focus-visible:outline-accent sm:hidden"
              aria-label="Previous provider card" onclick={() => cycle(-1)}><ChevronLeft class="h-4 w-4" /></button>
            <button type="button" class="absolute inset-y-0 right-0 flex w-7 items-center justify-center text-quiet hover:text-foreground focus-visible:outline focus-visible:outline-accent sm:hidden"
              aria-label="Next provider card" onclick={() => cycle(1)}><ChevronRight class="h-4 w-4" /></button>
            <span class="sr-only" aria-live="polite">Provider card {Math.min(cardIndex + 1, cards.length)} of {cards.length}</span>
          {/if}
          {#if providerError && !net.hideErrors}
            <button class="text-left text-xs text-warning" onclick={() => void refresh()}>{providerError} Retry</button>
          {/if}
        </div>
      {/if}
    </div>
  </section>
{/if}
