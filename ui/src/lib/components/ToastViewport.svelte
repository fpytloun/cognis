<script lang="ts">
  import AlertCircle from 'lucide-svelte/icons/alert-circle';
import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
import Info from 'lucide-svelte/icons/info';
import TriangleAlert from 'lucide-svelte/icons/triangle-alert';

  import { removeToast, toastStore, type ToastItem } from '$lib/stores/toasts';

  const variants = {
    success: {
      className: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-50',
      icon: CheckCircle2
    },
    error: {
      className: 'border-rose-500/40 bg-rose-500/15 text-rose-50',
      icon: AlertCircle
    },
    warning: {
      className: 'border-sky-500/40 bg-sky-500/15 text-sky-50',
      icon: TriangleAlert
    },
    info: {
      className: 'border-sky-500/40 bg-sky-500/15 text-sky-50',
      icon: Info
    }
  } as const;

  function variantConfig(toast: ToastItem) {
    return variants[toast.variant];
  }
</script>

{#if $toastStore.length > 0}
  <div
    aria-live="polite"
    class="pointer-events-none fixed z-[80] flex w-[min(22rem,calc(100vw-2rem))] flex-col gap-3"
    style="top: calc(env(safe-area-inset-top, 0px) + 0.75rem); right: calc(env(safe-area-inset-right, 0px) + 0.75rem);"
  >
    {#each $toastStore as toast (toast.id)}
      {@const config = variantConfig(toast)}
      <button
        class={`pointer-events-auto flex w-full items-start gap-3 rounded-2xl border px-4 py-3 text-left shadow-card backdrop-blur transition hover:translate-y-[-1px] ${config.className}`}
        onclick={() => removeToast(toast.id)}
        type="button"
      >
        <svelte:component this={config.icon} class="mt-0.5 h-5 w-5 shrink-0" />
        <div class="min-w-0 flex-1">
          {#if toast.title}
            <p class="font-medium">{toast.title}</p>
          {/if}
          <p class={`text-sm leading-6 ${toast.title ? 'mt-1 opacity-90' : ''}`}>{toast.message}</p>
        </div>
      </button>
    {/each}
  </div>
{/if}
