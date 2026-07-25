<script lang="ts">
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import type { EditableWebBackend, WebBackendEditValue } from '$lib/web-backends';

  interface Props {
    backendValue: WebBackendEditValue;
    configured: boolean;
    busy: boolean;
    onclose: () => void;
    onsave: (value: WebBackendEditValue) => void;
    onremove: () => void;
  }

  let { backendValue, configured, busy, onclose, onsave, onremove }: Props = $props();

  let draft = $state<WebBackendEditValue>({
    backend: 'tavily',
    enabled: true,
    apiKey: '',
    searxngUrl: '',
    searxngEngines: '',
    searxngCategories: '',
    searxngLanguage: ''
  });

  function backendLabel(backend: EditableWebBackend): string {
    if (backend === 'tavily') return 'Tavily';
    if (backend === 'brave') return 'Brave Search';
    return 'SearXNG';
  }

  $effect(() => {
    draft = { ...backendValue };
  });

  let canSave = $derived(
    draft.backend === 'searxng'
      ? !draft.enabled || Boolean(draft.searxngUrl.trim())
      : !draft.enabled || configured || Boolean(draft.apiKey.trim())
  );
</script>

<BlockingDialog
  label={`Edit ${backendLabel(backendValue.backend)}`}
  onClose={onclose}
  titleId="web-backend-edit-title"
  dismissible={!busy}
>
  {#snippet header()}
    <div class="flex items-center justify-between gap-3">
      <div>
        <p class="text-xs uppercase tracking-[0.25em] text-slate-400">Web search backend</p>
        <h2 class="mt-1 text-lg font-semibold text-white" id="web-backend-edit-title">
          Edit {backendLabel(backendValue.backend)}
        </h2>
      </div>
      <Button aria-label="Close backend editor" size="icon" variant="secondary" onclick={onclose} disabled={busy}>&times;</Button>
    </div>
  {/snippet}

  {#snippet children()}
    <div class="space-y-5">
      <label class="flex items-start gap-3 rounded-2xl border border-slate-800 bg-slate-900/60 px-4 py-3 text-sm text-slate-200">
        <input
          bind:checked={draft.enabled}
          class="mt-1 rounded border-slate-600 bg-slate-950 text-sky-400 focus:ring-sky-300"
          type="checkbox"
        />
        <span>
          <span class="block font-medium text-slate-100">Enabled</span>
          <span class="mt-1 block text-xs leading-5 text-slate-400">
            Disabled backends keep their configuration but are hidden from web tools and backend selectors.
          </span>
        </span>
      </label>

      {#if draft.backend === 'searxng'}
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Instance URL</span>
          <Input bind:value={draft.searxngUrl} placeholder="http://localhost:8888" />
        </label>
        <div class="grid gap-4 sm:grid-cols-2">
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Engines</span>
            <Input bind:value={draft.searxngEngines} placeholder="google,bing,duckduckgo" />
          </label>
          <label class="space-y-2 text-sm font-medium text-slate-200">
            <span>Categories</span>
            <Input bind:value={draft.searxngCategories} placeholder="general" />
          </label>
        </div>
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>Language</span>
          <Input bind:value={draft.searxngLanguage} placeholder="all or en-US" />
        </label>
        <p class="text-xs leading-5 text-slate-400">
          Engines, categories, and language are optional comma-separated defaults sent to your SearXNG instance.
        </p>
      {:else}
        <label class="space-y-2 text-sm font-medium text-slate-200">
          <span>{configured ? 'Replace API key' : 'API key'}</span>
          <Input
            bind:value={draft.apiKey}
            type="password"
            placeholder={configured ? 'Leave blank to keep the existing key' : 'Enter API key'}
          />
        </label>
        {#if configured}
          <p class="text-xs leading-5 text-slate-400">
            The existing key is encrypted and cannot be displayed. Enter a new value only when replacing it.
          </p>
        {/if}
      {/if}
    </div>
  {/snippet}

  {#snippet footer()}
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        {#if configured}
          <Button variant="danger" onclick={onremove} disabled={busy}>
            {draft.backend === 'searxng' ? 'Clear configuration' : 'Remove key'}
          </Button>
        {/if}
      </div>
      <div class="flex gap-2">
        <Button variant="secondary" onclick={onclose} disabled={busy}>Cancel</Button>
        <Button onclick={() => onsave({ ...draft })} disabled={busy || !canSave}>Save changes</Button>
      </div>
    </div>
  {/snippet}
</BlockingDialog>
