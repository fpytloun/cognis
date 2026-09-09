<script lang="ts">
  import { goto } from '$app/navigation';
  import AlertTriangle from 'lucide-svelte/icons/alert-triangle';
  import BrainCircuit from 'lucide-svelte/icons/brain-circuit';
  import ServerCrash from 'lucide-svelte/icons/server-crash';
  import ShieldAlert from 'lucide-svelte/icons/shield-alert';

  import Button from '$lib/components/ui/Button.svelte';
  import { providerNotices } from '$lib/provider-notices';
  import { workspaceHealth } from '$lib/system';

  let { compact = false } = $props<{ compact?: boolean }>();
  const notices = $derived(providerNotices($workspaceHealth.health));

  function iconFor(id: string): typeof AlertTriangle {
    if (id === 'memory') return BrainCircuit;
    if (id === 'guardrails') return ShieldAlert;
    if (id === 'llm') return ServerCrash;
    return AlertTriangle;
  }
</script>

{#if notices.length > 0}
  <div class={compact ? 'space-y-1.5 border-t border-slate-800 px-3 py-2' : 'space-y-2'} data-testid="provider-unavailable-notices">
    {#each notices as notice (notice.id)}
      {@const NoticeIcon = iconFor(notice.id)}
      <div
        class={`rounded-xl border text-sm ${
          notice.severity === 'warning'
            ? 'border-amber-500/30 bg-amber-500/10 text-amber-100'
            : 'border-rose-500/30 bg-rose-500/10 text-rose-100'
        } ${compact ? 'px-3 py-2' : 'px-3 py-3'}`}
        data-testid={`provider-unavailable-${notice.id}`}
        role="status"
      >
        <div class="flex min-w-0 items-start gap-2">
          <NoticeIcon class="mt-0.5 h-4 w-4 shrink-0" />
          <div class="min-w-0 flex-1">
            <p class="font-medium">{notice.title}</p>
            <p class="mt-0.5 break-words opacity-90">{notice.detail}</p>
            {#if notice.reason}
              <p class="mt-1 break-words text-xs opacity-75">Reason: {notice.reason}</p>
            {/if}
          </div>
          <Button size="sm" variant="secondary" onclick={() => goto(notice.actionUrl)}>
            {notice.actionLabel}
          </Button>
        </div>
      </div>
    {/each}
  </div>
{/if}
