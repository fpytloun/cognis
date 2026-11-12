<script lang="ts">
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import X from 'lucide-svelte/icons/x';
  import Link from 'lucide-svelte/icons/link';
  import UsersRound from 'lucide-svelte/icons/users-round';
  import Copy from 'lucide-svelte/icons/copy';
  import { captureSharingLease, getDocumentSharing } from '$lib/document-sharing-context';
  const sharing = getDocumentSharing();
  import type { ShareLinkOptions } from './share-options';
  import { untrack } from 'svelte';
  import type { ResolvedFeedbackSource } from '$lib/feedback/types';
  let { createLink, internalUrl = '', onClose, allowModeChoice = true, defaultMode = 'view', onCopied, feedbackSource = null, permissionsError = '' }: {
    createLink?: ((options: ShareLinkOptions) => Promise<string>) | null; internalUrl?: string; onClose: () => void;
    allowModeChoice?: boolean; defaultMode?: 'view' | 'download'; onCopied?: () => void;
    feedbackSource?: ResolvedFeedbackSource | null; permissionsError?: string;
  } = $props();
  let mode = $state<'view' | 'download'>('view');
  $effect(() => { mode = defaultMode; });
  let expiration = $state(untrack(() => internalUrl ? 'private' : '7'));
  let tab = $state<'people' | 'links'>(untrack(() => sharing?.loadPeople && sharing.canManage ? 'people' : 'links'));
  const contentRef = $derived(sharing?.contentRef ?? (feedbackSource?.source.kind === 'document' ? feedbackSource.source.content_ref : null));
  const canSharePeople = $derived(!!contentRef && !!sharing?.loadPeople && sharing.canManage);
  const canManageLinks = $derived(!!sharing?.canManage && feedbackSource?.source.kind === 'document' && feedbackSource.capabilities.disclose);
  const canCreatePublic = $derived(!!createLink && (!feedbackSource || feedbackSource.capabilities.disclose));
  let custom = $state('');
  let busy = $state(false);
  let url = $state('');
  let error = $state('');
  let copied = $state(false);
  let comments = $state(false);
  let accessVersion = $state(0);
  let Access = $state<typeof import('../feedback/FeedbackAccess.svelte').default | null>(null);
  const canShareComments = $derived(!!sharing?.canManage && !!createLink && feedbackSource?.source.kind === 'document' && feedbackSource.capabilities.disclose);
  const isPrivate = $derived(!canSharePeople && expiration === 'private');
  $effect(() => { if (canSharePeople && expiration === 'private') expiration = '7'; });
  $effect(() => { void internalUrl; void expiration; void mode; void comments; void tab; void feedbackSource?.source_token; void canSharePeople; url = ''; error = ''; copied = false; });
  $effect(() => {
    if (!canSharePeople) return;
    try {
      const lease = captureSharingLease(sharing);
      const invalidate = () => { url = ''; onClose(); };
      lease.assertCurrent();
      lease.signal.addEventListener('abort', invalidate, { once: true });
      return () => lease.signal.removeEventListener('abort', invalidate);
    } catch { onClose(); }
  });
  $effect(() => { if (!canSharePeople && tab === 'people') tab = 'links'; });
  $effect(() => { if (isPrivate || mode === 'download' || !canShareComments) comments = false; });
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  function close() { if (!busy) onClose(); }
  async function copy() {
    try { await navigator.clipboard.writeText(url); copied = true; onCopied?.(); }
    catch { error = 'Select the link below and copy it manually.'; }
  }
  async function copyPrivate() {
    error = ''; copied = false;
    url = new URL(internalUrl, location.origin).href;
    await copy();
  }
  async function create() {
    if (isPrivate) {
      error = ''; copied = false;
      url = new URL(internalUrl, location.origin).href;
      await copy();
      return;
    }
    if (!createLink) return;
    let options: ShareLinkOptions;
    if (expiration === 'custom') {
      const date = new Date(custom);
      const duration = date.getTime() - Date.now();
      if (!custom || !Number.isFinite(duration) || duration < 60000 || duration > 90 * 86400000) {
        error = 'Choose an expiration between one minute and 90 days from now.'; return;
      }
      options = { expires_at: date.toISOString() };
    } else options = { ttl_seconds: Number(expiration) * 86400 };
    if (allowModeChoice || comments) options.mode = mode;
    if (canShareComments) options.allow_comments = comments;
    busy = true; error = ''; url = ''; copied = false;
    try { url = await createLink(options); accessVersion++; await copy(); }
    catch (caught) { error = caught instanceof Error ? caught.message : 'Could not create a public link.'; }
    finally { busy = false; }
  }
</script>

<BlockingDialog label="Share document" onClose={close} dismissible={!busy} class="z-[2147483100]" panelClass="max-w-lg" bodyClass="space-y-5">
  {#snippet header()}
    <div class="flex items-center justify-between gap-4"><h2 class="text-lg font-semibold text-strong">Share document</h2>
      <Button variant="ghost" size="icon" aria-label="Close share link" onclick={close} disabled={busy}><X size={18} /></Button>
    </div>
  {/snippet}
  {#if contentRef}
    <div role="tablist" aria-label="Sharing options" class="flex flex-wrap gap-2 border-b border-separator pb-3">
      <Button class="gap-2" variant={tab === 'people' ? 'primary' : 'secondary'} role="tab" aria-selected={tab === 'people'} disabled={busy || !canSharePeople}
        onclick={() => { tab = 'people'; }}><UsersRound size={16} aria-hidden="true" />Share with people</Button>
      <Button class="gap-2" variant={tab === 'links' ? 'primary' : 'secondary'} role="tab" aria-selected={tab === 'links'} disabled={busy}
        onclick={() => { tab = 'links'; }}><Link size={16} aria-hidden="true" />Public links</Button>
    </div>
  {/if}
  {#if tab === 'people' && canSharePeople && contentRef}
    <div role="tabpanel" aria-label="Share with people" tabindex="0">
      {#await sharing!.loadPeople!()}
        <p role="status" class="text-sm text-muted">Loading sharing…</p>
      {:then module}
        <module.default {contentRef} onBusy={value => { busy = value; }} />
      {:catch}<p role="alert" class="text-sm text-danger">Could not load sharing. Close and reopen this dialog to retry.</p>{/await}
      {#if internalUrl}
        <div class="mt-4 space-y-2 border-t border-separator pt-4">
          <p class="text-xs text-muted">The private document link works only for people with access.</p>
          <Button class="gap-2" variant="secondary" disabled={busy} onclick={copyPrivate}><Copy size={16} />Copy private link</Button>
          {#if url}<input aria-label="Private link URL" readonly value={url} onfocus={event => event.currentTarget.select()} class="min-h-11 w-full rounded-lg border border-control-border bg-surface px-3 text-xs" />{/if}
          {#if copied}<p role="status" class="text-xs text-muted">Private link copied.</p>{/if}
          {#if error}<p role="alert" class="text-sm text-danger">{error}</p>{/if}
        </div>
      {/if}
    </div>
  {:else}
   <div role="tabpanel" aria-label="Links" class="space-y-5" tabindex="0">
  <p class="text-sm leading-6 text-secondary">{isPrivate ? 'Only people who already have access in Cognis can open this link. Sharing it does not grant access.' : 'Anyone with the link sees the latest version of this document until the link expires. Agent access is managed separately.'}</p>
  {#if allowModeChoice && !isPrivate && canCreatePublic}
    <label class="block space-y-2 text-sm text-secondary">Link behavior
      <select aria-label="Link behavior" class="min-h-11 w-full rounded-xl border border-control-border bg-surface px-3 text-foreground focus:border-accent focus:outline-none" bind:value={mode} disabled={busy}>
        <option value="view">View in browser</option><option value="download">Download file</option>
      </select>
    </label>
  {/if}
  {#if isPrivate || canCreatePublic}
  <label class="block space-y-2 text-sm text-secondary">Expires in
    <select aria-label="Link access and expiration" class="min-h-11 w-full rounded-xl border border-control-border bg-surface px-3 text-foreground focus:border-accent focus:outline-none" bind:value={expiration} disabled={busy}>
       {#if internalUrl && !canSharePeople}<option value="private">Private · internal access only</option>{/if}
       {#if createLink && (!feedbackSource || feedbackSource.capabilities.disclose)}<option value="1">1 day</option><option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option><option value="custom">Custom date and time</option>{/if}
    </select>
  </label>
  {:else}<p class="text-sm text-muted">Public link creation is unavailable here.</p>{/if}
  {#if expiration === 'custom'}
    <label class="block space-y-2 text-sm text-secondary">Expiration date and time
      <input type="datetime-local" class="min-h-11 w-full rounded-xl border border-control-border bg-surface px-3 text-foreground" bind:value={custom} disabled={busy} />
      <span class="block text-xs text-quiet">{timezone} · up to 90 days</span>
    </label>
  {/if}
  {#if canShareComments}
    <label class="flex min-h-11 items-center gap-3 text-sm text-secondary" title={isPrivate ? 'Available for public viewer links' : undefined}>
      <input type="checkbox" bind:checked={comments} disabled={busy || isPrivate} onchange={event => { if (event.currentTarget.checked) mode = 'view'; }} />Allow comments
    </label>
  {/if}
  {#if url}
    <div class="space-y-2 border-t border-separator pt-4">
      <label class="block text-xs text-secondary" for="public-link-result">{isPrivate ? 'Private link' : 'Public link'} · {copied ? 'Copied' : 'Ready to copy'}</label>
      <div class="flex items-center gap-2">
        <input id="public-link-result" aria-label={isPrivate ? 'Private link URL' : 'Public link URL'} readonly value={url} onfocus={(event) => event.currentTarget.select()} class="min-h-11 min-w-0 flex-1 rounded-xl border border-control-border bg-surface px-3 text-xs text-foreground" />
        <Button variant="secondary" size="icon-mobile" aria-label={isPrivate ? 'Copy private link again' : 'Copy public link'} onclick={copy}><Copy size={17} /></Button>
      </div>
    </div>
  {/if}
  {#if error}<p role="alert" class="text-sm text-danger">{error}</p>{/if}
  {#if permissionsError}<p role="alert" class="text-sm text-danger">{permissionsError}</p>{/if}
  {#if canManageLinks}
    <details class="border-t border-separator pt-3" ontoggle={async event => {
      if (event.currentTarget.open && !Access) Access = (await import('../feedback/FeedbackAccess.svelte')).default;
    }}>
      <summary class="cursor-pointer text-sm text-secondary">Existing public links</summary>
      <p class="mt-2 text-xs text-muted">Copy or revoke links that work without signing in. Internal sharing is managed separately.</p>
      {#if Access && feedbackSource}{#key accessVersion}<Access source={feedbackSource} onBusy={value => { busy = value; }} />{/key}{/if}
    </details>
  {/if}
  {#if !isPrivate}<p class="text-xs leading-5 text-quiet">{canShareComments ? 'Links always show the latest version and can be copied or revoked from existing public links.' : 'Links stay valid until expiry. Older direct links cannot be listed or revoked individually.'}</p>{/if}
   </div>
  {/if}
  {#snippet footer()}
    <div class="flex justify-end gap-2">
      <Button variant="ghost" onclick={close} disabled={busy}>Done</Button>
      {#if tab === 'links' && (isPrivate || canCreatePublic)}<Button onclick={create} disabled={busy}>{isPrivate ? 'Copy private link' : busy ? 'Creating…' : url ? 'Create another link' : 'Create public link'}</Button>{/if}
    </div>
  {/snippet}
</BlockingDialog>
