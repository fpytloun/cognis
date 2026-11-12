<script lang="ts">
  import { api } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import AgentAccessPicker from './AgentAccessPicker.svelte';
  import Plus from 'lucide-svelte/icons/plus';
  import X from 'lucide-svelte/icons/x';
  import type { DocumentAccess } from '$lib/artifact-authoring';
  import type { LibraryItem } from '$lib/library';

  let { item, canManage }: { item: LibraryItem; canManage: boolean } = $props();
  let access = $state<DocumentAccess | null>(null);
  let selected = $state<string[]>([]);
  let writers = $state<string[]>([]);
  let open = $state(false);
  let busy = $state(false);
  let error = $state('');
  let loading = $state(false);
  let retry = $state(0);
  const recipients = $derived(access?.agents.filter((a) => a.explicit || a.inherited || a.agent_id === access?.owner_agent_id) ?? []);
  const owner = $derived(access?.agents.find((a) => a.agent_id === access?.owner_agent_id));
  const others = $derived(recipients.filter((a) => a.agent_id !== access?.owner_agent_id));
  const humanName = $derived(item.owner_email === $auth.user?.email ? ($auth.user?.name || 'You') : (item.owner_email || 'Document owner'));
  const dirty = $derived(!!access && (
    [...selected].sort().join('\0') !== access.agents.filter(a => a.explicit).map(a => a.agent_id).sort().join('\0')
    || writers.filter(id => selected.includes(id)).sort().join('\0') !== access.agents.filter(a => a.explicit_permission === 'write').map(a => a.agent_id).sort().join('\0')
  ));

  $effect(() => {
    const current = item;
    const permitted = canManage;
    void retry;
    let cancelled = false;
    access = null; error = ''; loading = permitted;
    if (permitted) void api.artifactDocuments.sharing(current.kind, current.id)
      .then((value) => {
        if (!Array.isArray(value.agents)) throw new Error('Could not load agent access. Please retry.');
        if (!cancelled) access = value;
      })
      .catch((caught) => { if (!cancelled) error = caught instanceof Error ? caught.message : 'Could not load agent access.'; })
      .finally(() => { if (!cancelled) loading = false; });
    return () => { cancelled = true; };
  });
  function show() {
    if (!access || loading) return;
    selected = access?.agents.filter(a => a.explicit).map(a => a.agent_id) ?? [];
    writers = access.agents.filter(a => a.explicit_permission === 'write').map(a => a.agent_id);
    open = true;
  }
  function close() {
    if (!busy && (!dirty || confirm('Discard agent access changes?'))) open = false;
  }
  async function save() {
    busy = true; error = '';
    try {
      access = await api.artifactDocuments.setSharing(item.kind, item.id, selected, writers.filter(id => selected.includes(id)));
      open = false;
    } catch (caught) { error = caught instanceof Error ? caught.message : 'Could not save agent access.'; }
    finally { busy = false; }
  }
</script>

<section aria-label="Agent access" class="space-y-3">
  <h3 class="text-xs font-medium text-muted">Owner &amp; agent access</h3>
  <div class="flex flex-wrap items-center gap-1.5" data-testid="document-access-avatars">
    {#if owner}
      <span title={`${owner.name} · Owner`} aria-label={`${owner.name} · Owner`}>
        <AgentAvatar name={owner.name} avatarUrl={owner.avatar_url} class="!h-9 !w-9" draggable={false} />
      </span>
    {:else}
      <span title={`${item.agent_name || humanName} · Owner`} aria-label={`${item.agent_name || humanName} · Owner`}>
        <AgentAvatar name={item.agent_name || humanName} class="!h-9 !w-9" draggable={false} />
      </span>
    {/if}
    {#each others as agent (agent.agent_id)}
      {@const source = [
        ...(agent.inherited ? [`${agent.inherited_sources?.includes('project') ? 'Project' : 'Inherited'}: ${agent.inherited_permission === 'write' ? 'read + write' : 'read'}`] : []),
        ...(agent.explicit ? [`Explicit: ${agent.explicit_permission === 'write' ? 'read + write' : 'read'}`] : []),
      ].join(' · ')}
      <span title={`${agent.name} · ${source}`} aria-label={`${agent.name} · ${source}`}>
        <AgentAvatar name={agent.name} avatarUrl={agent.avatar_url} class="!h-9 !w-9" draggable={false} />
      </span>
    {/each}
    {#if canManage}
      <Button variant="secondary" size="icon-mobile" aria-label="Manage agent access" title="Share with agents" onclick={show} disabled={loading || !access} class="!h-10 !w-10 !rounded-xl">
        <Plus size={17} />
      </Button>
    {/if}
  </div>
  <p class="text-xs text-quiet">{owner?.name || access?.owner_agent_id || humanName} owns this document.</p>
  <p class="text-xs text-muted">Revision author: {item.author_agent_id ?? item.author_email ?? 'Unknown'}</p>
  {#if loading}<p role="status" class="text-xs text-quiet">Loading access…</p>{/if}
  {#if error && !open}<p role="alert" class="text-xs text-danger">{error}</p><Button variant="ghost" size="sm" onclick={() => retry++}>Retry</Button>{/if}
  {#if !canManage}<p class="text-xs text-quiet">Only the owner can manage agent access.</p>{/if}
</section>

{#if open}
  <BlockingDialog label="Share with agents" onClose={close} dismissible={!busy} backdropDismissible={!dirty} panelClass="max-w-xl" bodyClass="space-y-4">
    {#snippet header()}
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0"><h2 class="text-lg font-semibold text-strong">Share with agents</h2><p class="mt-1 truncate text-sm text-muted">{item.title || 'Untitled document'}</p></div>
        <Button variant="ghost" size="icon" aria-label="Close agent access" disabled={busy} onclick={close}><X size={18} /></Button>
      </div>
    {/snippet}
    <p class="text-sm text-muted">Choose Read or Read + write for this document and its revisions. Project access remains inherited.</p>
    {#if access}<AgentAccessPicker agents={access.agents} bind:selected bind:writers permissions ownerId={access.owner_agent_id} disabled={busy} />
    {:else if loading}<p role="status" class="text-sm text-muted">Loading agents…</p>{/if}
    {#if error}<p role="alert" class="text-sm text-danger">{error}</p><Button size="sm" variant="ghost" onclick={() => retry++}>Retry loading</Button>{/if}
    {#snippet footer()}
      <div class="flex justify-end gap-2">
        <Button variant="ghost" onclick={close} disabled={busy}>Cancel</Button>
        <Button onclick={save} disabled={busy || !access || !dirty}>{busy ? 'Saving…' : 'Save agent access'}</Button>
      </div>
    {/snippet}
  </BlockingDialog>
{/if}
