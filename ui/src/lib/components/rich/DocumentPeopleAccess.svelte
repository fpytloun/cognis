<script lang="ts">
  import { api, ApiError } from '$lib/api/client';
  import Button from '$lib/components/ui/Button.svelte';
  import UserRound from 'lucide-svelte/icons/user-round';
  import Trash2 from 'lucide-svelte/icons/trash-2';
  import type { DocumentPeople, DocumentPersonGrant } from '$lib/document-people';
  import { captureSharingLease, getDocumentSharing } from '$lib/document-sharing-context';
  const sharing = getDocumentSharing();
  let { contentRef, onBusy = () => {} }: { contentRef: string; onBusy?: (busy: boolean) => void } = $props();
  let page = $state<DocumentPeople | null>(null);
  let principal = $state('');
  let role = $state('viewer');
  let busy = $state(false);
  let uncertain = $state(false);
  let error = $state('');
  let notice = $state('');
  let epoch = 0;
  const kind = $derived(contentRef.startsWith('dlv_') ? 'deliverable' : 'artifact');
  $effect(() => {
    void contentRef; void kind;
    page = null; error = ''; uncertain = false;
    void load();
    return () => { epoch++; onBusy(false); };
  });
  async function load() {
    const lease = ++epoch;
    busy = true; onBusy(true);
    try {
      const leaseGuard = captureSharingLease(sharing);
      await leaseGuard.validate();
      const result = await api.artifactDocuments.people(kind, contentRef);
      await leaseGuard.validate();
      if (lease !== epoch) return;
      page = result; error = ''; uncertain = false;
    } catch (caught) {
      if (lease === epoch) error = caught instanceof Error ? caught.message : 'Could not load document access.';
    } finally {
      if (lease === epoch) { busy = false; onBusy(false); }
    }
  }
  async function save(email: string, allowComments: boolean, revoked = false) {
    if (busy || uncertain || !page) return;
    const lease = ++epoch;
    const targetKind = kind;
    const targetRef = contentRef;
    const normalized = email.trim().toLowerCase();
    const existing = page.items.find(item => item.principal_email === normalized);
    busy = true; onBusy(true); error = ''; notice = '';
    try {
      const leaseGuard = captureSharingLease(sharing);
      await leaseGuard.validate();
      if (lease !== epoch || kind !== targetKind || contentRef !== targetRef) return;
      const result = await api.artifactDocuments.setPerson(targetKind, targetRef, {
        principal_email: normalized, expected_version: existing?.version ?? 0,
        allow_comments: allowComments, revoked,
      });
      await leaseGuard.validate();
      if (lease !== epoch) return;
      page = result; notice = revoked ? 'Access removed.' : 'Document access saved.';
    } catch (caught) {
      if (lease === epoch) {
        if (caught instanceof ApiError && caught.status === 422) {
          error = caught.message;
        } else {
          uncertain = true;
          error = 'Access was not confirmed. Reload access before trying again; your input is preserved.';
        }
      }
    } finally {
      if (lease === epoch) { busy = false; onBusy(false); }
    }
  }
  function changeRole(item: DocumentPersonGrant, event: Event) {
    void save(item.principal_email, (event.currentTarget as HTMLSelectElement).value === 'commenter');
  }
</script>

<section class="space-y-4" aria-label="Share with people">
  <p class="text-sm leading-6 text-secondary">Share the whole document with a signed-in Cognis user, including future revisions. This does not publish a link or share the producing conversation or agents.</p>
  {#if busy && !page}<p role="status" class="text-sm text-muted">Loading access…</p>{/if}
  <form class="space-y-3" onsubmit={event => { event.preventDefault(); void save(principal, role === 'commenter'); }}>
    <label class="block space-y-1 text-sm text-secondary">Cognis user email
      <input type="email" required autocomplete="off" bind:value={principal} disabled={busy || uncertain || !page}
        class="min-h-11 w-full rounded-lg border border-control-border bg-surface px-3 text-foreground" />
    </label>
    <div class="flex flex-wrap items-end gap-2">
      <label class="min-w-0 flex-1 space-y-1 text-sm text-secondary">Permission
        <select aria-label="New recipient permission" bind:value={role} disabled={busy || uncertain || !page}
          class="min-h-11 w-full rounded-lg border border-control-border bg-surface px-3 text-foreground">
          <option value="viewer">Can view</option><option value="commenter">Can view and comment</option>
        </select>
      </label>
      <Button type="submit" disabled={busy || uncertain || !page || !principal.trim()}>Share with person</Button>
    </div>
  </form>
  {#if page}
    <h3 class="border-t border-separator pt-4 text-sm font-medium">People with access</h3>
    {#each page.items.filter(item => !item.revoked) as item (item.principal_email)}
      <div class="flex flex-wrap items-center gap-2 rounded-lg border border-separator p-3">
        <UserRound size={16} class="shrink-0 text-muted" aria-hidden="true" />
        <span class="min-w-0 flex-1 break-all text-sm">{item.principal_email}</span>
        <select aria-label={`Permission for ${item.principal_email}`} value={item.allow_comments ? 'commenter' : 'viewer'}
          onchange={event => changeRole(item, event)} disabled={busy || uncertain}
          class="min-h-10 max-w-full rounded-lg border border-control-border bg-surface px-2 text-sm">
          <option value="viewer">Can view</option><option value="commenter">Can view and comment</option>
        </select>
        <Button variant="secondary" size="icon-mobile" aria-label={`Remove access for ${item.principal_email}`}
          onclick={() => save(item.principal_email, item.allow_comments, true)} disabled={busy || uncertain}><Trash2 size={16} /></Button>
      </div>
    {:else}<p class="text-sm text-muted">No one has been added directly. Existing project access is unchanged.</p>{/each}
  {/if}
  {#if error}<p role="alert" class="text-sm text-danger">{error}</p>{/if}
  {#if notice}<p role="status" class="text-sm text-secondary">{notice}</p>{/if}
  {#if error || uncertain}<Button variant="secondary" onclick={load} disabled={busy}>Reload access</Button>{/if}
</section>
