<script lang="ts">
  import { cn } from '$lib/utils';

  let { name, avatarUrl = null, class: className = '' } = $props<{
    name: string;
    avatarUrl?: string | null;
    class?: string;
  }>();

  let imgFailed = $state(false);

  function initials(): string {
    return name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part: string) => part[0]?.toUpperCase() ?? '')
      .join('');
  }

  function handleError() {
    imgFailed = true;
  }

  // Reset error state when URL changes
  $effect(() => {
    if (avatarUrl) {
      imgFailed = false;
    }
  });
</script>

{#if avatarUrl && !imgFailed}
  <img alt={name} class={cn('h-10 w-10 rounded-2xl object-cover', className)} src={avatarUrl} onerror={handleError} />
{:else}
  <div class={cn('flex h-10 w-10 items-center justify-center rounded-2xl bg-sky-500/20 text-sm font-semibold text-sky-200', className)}>
    {initials() || '?'}
  </div>
{/if}
