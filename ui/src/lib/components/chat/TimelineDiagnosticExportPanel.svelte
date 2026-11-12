<script lang="ts">
  import { version } from '$app/environment';
  import { onMount } from 'svelte';
  import Download from 'lucide-svelte/icons/download';
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';
  import Button from '$lib/components/ui/Button.svelte';
  import type { ChatV2ClientState } from '$lib/chat-v2/sync-engine';
  import type { TimelineItem } from '$lib/chat-v2/types';
  import { forceRefreshTimeline } from '$lib/chat-v2/timeline-recovery';

  let { clientState, visibleItems }: { clientState: ChatV2ClientState; visibleItems: TimelineItem[] } = $props();
  let failed = $state(false);
  let diagnosticModule = $state.raw<typeof import('./diagnostic-download') | null>(null);
  let refreshing = $state(false);
  let refreshed = $state(false);
  let refreshFailed = $state(false);
  let refreshController: AbortController | undefined;
  let refreshScope: string | null = null;
  const scopeKey = $derived(clientState.scopeKey);
  $effect(() => {
    const key = scopeKey;
    if (refreshScope && refreshScope !== key) refreshing = refreshed = refreshFailed = false;
    return () => { if (key) refreshController?.abort(); };
  });
  // Load the optional exporter outside the critical bundle, but before enabling
  // the button so Safari's download remains inside the synchronous click.
  onMount(() => {
    import('./diagnostic-download').then(module => { diagnosticModule = module; })
      .catch(() => { failed = true; });
  });

  function download(): void {
    failed = false;
    try {
      if (!diagnosticModule) throw new Error('Diagnostic exporter is unavailable');
      diagnosticModule.downloadChatDiagnostic(clientState, visibleItems, version);
    } catch {
      failed = true;
    }
  }

  async function refresh(): Promise<void> {
    if (refreshing || !clientState.scope) return;
    const scope = clientState.scope;
    refreshScope = scope.key;
    const controller = new AbortController();
    refreshController = controller;
    refreshing = true;
    refreshed = refreshFailed = false;
    try {
      await forceRefreshTimeline(scope, controller.signal);
      if (!controller.signal.aborted && clientState.scopeKey === scope.key) refreshed = true;
    } catch {
      if (!controller.signal.aborted && clientState.scopeKey === scope.key) refreshFailed = true;
    } finally {
      if (!controller.signal.aborted && clientState.scopeKey === scope.key) refreshing = false;
    }
  }
</script>

<section class="mt-4 space-y-3 border-t border-separator pt-3" aria-label="Chat diagnostics" data-testid="timeline-diagnostic-export">
  <div class="space-y-1">
    <h4 class="text-xs font-medium text-foreground">Chat diagnostics</h4>
    <p class="text-xs leading-5 text-quiet">
      Export IDs and ordering metadata from this device. No message text, tool inputs or outputs are included.
      The file stays on your device unless you share it.
    </p>
  </div>
  <div class="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
  <Button
    variant="secondary"
    size="sm"
    class="min-h-[44px] w-full gap-2 sm:w-auto md:min-h-[44px]"
    disabled={!clientState.scopeKey || !diagnosticModule}
    onclick={download}
  >
    <Download class="h-3.5 w-3.5" aria-hidden="true" />
    Download chat diagnostics
  </Button>
  <Button
    variant="secondary"
    size="sm"
      class="min-h-[44px] w-full gap-2 sm:w-auto md:min-h-[44px]"
    disabled={refreshing || !clientState.scope}
    onclick={refresh}
  >
    <RefreshCw class={refreshing ? 'h-3.5 w-3.5 motion-safe:animate-spin' : 'h-3.5 w-3.5'} aria-hidden="true" />
    {refreshing ? 'Refreshing…' : 'Force refresh'}
  </Button>
  </div>
  <p class="text-xs leading-5 text-quiet">Rebuild history from the server and remove cached activity. Queued and unsent input is kept.</p>
  {#if refreshed}<p role="status" class="text-xs text-quiet">History refreshed.</p>{/if}
  {#if refreshFailed}<p role="alert" class="text-xs text-danger">Refresh failed. Your input has been kept. Try again.</p>{/if}
  {#if failed}<p role="alert" class="text-xs text-danger">Could not prepare the diagnostic file. Please try again.</p>{/if}
</section>
