<script lang="ts">
  import AlertCircle from 'lucide-svelte/icons/alert-circle';
import CheckCircle2 from 'lucide-svelte/icons/check-circle-2';
import Info from 'lucide-svelte/icons/info';
import TriangleAlert from 'lucide-svelte/icons/triangle-alert';
import X from 'lucide-svelte/icons/x';

  import Button from '$lib/components/ui/Button.svelte';
  import { removeToast, toastStore, type ToastItem } from '$lib/stores/toasts';

  const variants = {
    success: {
      className: 'border-success-border bg-success-subtle text-success',
      icon: CheckCircle2
    },
    error: {
      className: 'border-danger-border bg-danger-subtle text-danger',
      icon: AlertCircle
    },
    warning: {
      className: 'border-accent/40 bg-accent-subtle text-accent',
      icon: TriangleAlert
    },
    info: {
      className: 'border-accent/40 bg-accent-subtle text-accent',
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
    class="app-floating-top-overlay pointer-events-none z-[80] flex w-auto max-w-[22rem] flex-col gap-3 sm:left-auto sm:w-[22rem]"
    data-testid="toast-viewport"
  >
    {#each $toastStore as toast (toast.id)}
      {@const config = variantConfig(toast)}
      <div
        class={`pointer-events-auto flex w-full items-start gap-3 rounded-2xl border px-4 py-3 text-left shadow-card backdrop-blur ${config.className}`}
        data-toast-variant={toast.variant}
      >
        <svelte:component this={config.icon} class="mt-0.5 h-5 w-5 shrink-0" />
        <div class="min-w-0 flex-1">
          {#if toast.title}
            <p class="font-medium">{toast.title}</p>
          {/if}
          {#if toast.message}
            <p class={`line-clamp-3 break-words text-sm leading-6 ${toast.title ? 'mt-1 opacity-90' : ''}`}>{toast.message}</p>
          {/if}
          {#if toast.action}
            <a class="mt-1 inline-block text-sm font-medium underline underline-offset-2" href={toast.action.href} onclick={() => removeToast(toast.id)}>{toast.action.label}</a>
          {/if}
        </div>
        <Button class="-mr-2 -mt-1 shrink-0" size="icon" variant="ghost" aria-label="Dismiss notification" onclick={() => removeToast(toast.id)}>
          <X class="h-4 w-4" />
        </Button>
      </div>
    {/each}
  </div>
{/if}
