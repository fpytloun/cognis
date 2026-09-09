<script lang="ts">
  import { beforeNavigate, goto } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';
  import X from 'lucide-svelte/icons/x';

  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import type { AttentionActionDetail } from '$lib/types/api';
  import FocusedAttentionAction from './FocusedAttentionAction.svelte';

  let { actionId, title, onClose, onSettled, onUnavailable } = $props<{
    actionId: string;
    title: string;
    onClose: () => void;
    onSettled: (action: AttentionActionDetail) => void | Promise<void>;
    onUnavailable?: (actionId: string) => void | Promise<void>;
  }>();

  let historyEntryActive = false;
  let closing = false;
  let pendingClose: (() => void | Promise<void>) | null = null;
  let originalState: Record<string, unknown> | null = null;
  const historyToken = `attention-${Date.now()}-${Math.random().toString(36).slice(2)}`;

  function finishClose(): void {
    const callback = pendingClose ?? onClose;
    pendingClose = null;
    void callback();
  }

  function handlePopState(): void {
    if (!historyEntryActive) return;
    historyEntryActive = false;
    finishClose();
  }

  beforeNavigate(({ to, cancel }) => {
    if (!historyEntryActive || closing || !to?.url) return;
    cancel();
    closing = true;
    const targetUrl = to.url;
    const external = to.route.id === null;
    pendingClose = async () => {
      onClose();
      if (external) {
        window.location.assign(targetUrl);
      } else {
        await goto(targetUrl);
      }
    };
    window.history.back();
  });

  function dismiss(): void {
    if (closing) return;
    closing = true;
    pendingClose = onClose;
    if (historyEntryActive) window.history.back();
    else finishClose();
  }

  function settle(action: AttentionActionDetail): void {
    if (closing) return;
    closing = true;
    pendingClose = () => onSettled(action);
    if (historyEntryActive) window.history.back();
    else finishClose();
  }

  onMount(() => {
    originalState = window.history.state as Record<string, unknown> | null;
    window.history.pushState(
      {
        ...(originalState ?? {}),
        cognisAttentionActionId: actionId,
        cognisAttentionHistoryToken: historyToken,
      },
      '',
      window.location.href,
    );
    historyEntryActive = true;
    window.addEventListener('popstate', handlePopState);
  });

  onDestroy(() => {
    window.removeEventListener('popstate', handlePopState);
    if (
      historyEntryActive
      && window.history.state?.cognisAttentionHistoryToken === historyToken
    ) {
      historyEntryActive = false;
      window.history.back();
    }
  });
</script>

<BlockingDialog
  open
  label={title}
  onClose={dismiss}
  dismissible
  viewportClass="!items-stretch !p-0"
  viewportStyle="padding-top: env(safe-area-inset-top); padding-bottom: var(--app-shell-bottom-offset, 0px);"
  panelClass="h-full max-h-none w-full max-w-none rounded-none"
  bodyClass="!overflow-hidden !p-0"
>
  {#snippet header()}
    <div class="flex min-h-11 items-center justify-between gap-3">
      <div class="min-w-0">
        <p class="text-[10px] font-semibold uppercase tracking-[0.22em] text-amber-300">Action required</p>
        <h1 class="truncate text-base font-semibold text-white">{title}</h1>
      </div>
      <button
        type="button"
        class="flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-xl border border-slate-700 bg-slate-950 text-slate-200"
        aria-label={`Close ${title}`}
        data-testid="attention-dialog-close"
        onclick={dismiss}
      ><X class="h-5 w-5" /></button>
    </div>
  {/snippet}
  {#snippet children()}
    <FocusedAttentionAction {actionId} onSettled={settle} {onUnavailable} onDismiss={dismiss} />
  {/snippet}
</BlockingDialog>
