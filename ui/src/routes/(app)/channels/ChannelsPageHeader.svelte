<script lang="ts">
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';
  import PlugZap from 'lucide-svelte/icons/plug-zap';

  import Button from '$lib/components/ui/Button.svelte';

  export let busy = false;
  export let onRefresh: () => void;
  export let onAdd: () => void;
</script>

<div class="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
  <div>
    <p class="text-xs uppercase tracking-[0.28em] text-amber-300/80">Channels</p>
    <h1 class="mt-2 text-3xl font-semibold text-white">External messaging connections</h1>
    <p class="mt-2 max-w-3xl text-sm text-slate-300">
      Connect agents to chat platforms, review pending sender approvals, and manage trusted channel identities without leaving Cognis.
    </p>
  </div>
  <div class="flex flex-wrap gap-2">
    <Button variant="secondary" onclick={onRefresh} disabled={busy}>
      <RefreshCw class="mr-2 h-4 w-4" /> Refresh
    </Button>
    <Button variant="primary" onclick={onAdd} disabled={busy} data-testid="channels-add-account">
      <PlugZap class="mr-2 h-4 w-4" /> Add channel
    </Button>
  </div>
</div>
