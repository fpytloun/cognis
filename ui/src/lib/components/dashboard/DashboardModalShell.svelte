<script lang="ts">
  import type { Snippet } from 'svelte';
  import X from 'lucide-svelte/icons/x';
  import ExternalLink from 'lucide-svelte/icons/external-link';

  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import SessionDetailsButton from '$lib/components/session/SessionDetailsButton.svelte';

  let {
    title,
    status = null,
    externalLabel = 'Open full page',
    dismissible = true,
    embedded = false,
    onExternal,
    inspectorOpen,
    inspectorControlsId,
    onToggleInspector,
    onClose,
    onEscape,
    testId,
    children,
  } = $props<{
    title: string;
    status?: string | null;
    externalLabel?: string;
    dismissible?: boolean;
    embedded?: boolean;
    onExternal?: ((event: MouseEvent) => void) | undefined;
    inspectorOpen?: boolean;
    inspectorControlsId?: string;
    onToggleInspector?: () => void;
    onClose: () => void;
    onEscape?: (() => boolean) | undefined;
    testId: string;
    children: Snippet;
  }>();
</script>

{#snippet body()}
  <div class="flex h-full min-h-0 flex-col overflow-hidden overscroll-none" data-testid={`${testId}-scroll-shell`}>
    {@render children()}
  </div>
{/snippet}

{#if embedded}
  <div class="flex h-full min-h-0 flex-col overflow-hidden" data-testid={testId}>
    {@render body()}
  </div>
{:else}
  <BlockingDialog
    open
    {onClose}
    label={title}
    viewportClass="!items-stretch !px-0 lg:!items-center lg:!px-[max(1rem,env(safe-area-inset-left))]"
    viewportStyle="padding-top: var(--dashboard-safe-area-top, env(safe-area-inset-top)); padding-bottom: 0;"
    panelClass="h-full max-w-none rounded-none border-x-0 lg:h-[calc(100dvh-2rem)] lg:max-h-[64rem] lg:w-[min(96vw,100rem)] lg:max-w-[100rem] lg:rounded-3xl lg:border"
    bodyClass="!overflow-hidden !p-0"
    {dismissible}
    {onEscape}
  >
  {#snippet header()}
    <div class="flex min-w-0 flex-1 items-center justify-between gap-3" data-testid={testId}>
      <div class="min-w-0">
        <h2 class="truncate text-lg font-semibold text-white">{title}</h2>
        {#if status}<p class="mt-0.5 text-xs uppercase tracking-wide text-slate-400">{status}</p>{/if}
      </div>
      <div class="flex shrink-0 items-center gap-1">
        {#if onToggleInspector && inspectorControlsId}
          <SessionDetailsButton
            open={inspectorOpen === true}
            ariaControls={inspectorControlsId}
            testId={`${testId}-inspector`}
            onclick={onToggleInspector}
          />
        {/if}
        {#if onExternal}
          <Button aria-label={externalLabel} size="icon" variant="ghost" data-testid={`${testId}-external-link`} onclick={onExternal}>
            <ExternalLink class="h-4 w-4" />
          </Button>
        {/if}
        <Button aria-label="Close" size="icon" variant="ghost" data-testid={`${testId}-close`} onclick={onClose}>
          <X class="h-4 w-4" />
        </Button>
      </div>
    </div>
  {/snippet}
    {#snippet children()}
      {@render body()}
    {/snippet}
  </BlockingDialog>
{/if}
