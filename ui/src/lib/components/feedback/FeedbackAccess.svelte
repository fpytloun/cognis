<script lang="ts">
  import { captureSharingLease, getDocumentSharing } from '$lib/document-sharing-context';
  import type { FeedbackShare, ResolvedFeedbackSource } from '$lib/feedback/types';
  import { feedbackApi } from '$lib/feedback/api';
  import Button from '$lib/components/ui/Button.svelte';
  import Copy from 'lucide-svelte/icons/copy';
  import Check from 'lucide-svelte/icons/check';
  const sharing = getDocumentSharing();
  let { source, onBusy = () => {} }: { source: ResolvedFeedbackSource; onBusy?: (value: boolean) => void } = $props();
  let shares = $state<FeedbackShare[]>([]);
  let error = $state('');
  let busy = $state(false);
  let copied = $state('');
  let recoveredUrl = $state('');
  let uncertain = $state(false);
  let epoch = 0;
  // A revoked link can never work again, so it is treated as deleted rather than listed.
  const visible = $derived(shares.filter(share => !share.revoked));
  async function load() {
    const current = ++epoch;
    busy = true; onBusy(true);
    try {
      const guard = captureSharingLease(sharing);
      await guard.validate();
      const page = await feedbackApi.shares(source.subject_id);
      await guard.validate();
      if (current === epoch) { shares = page.items; uncertain = false; error = ''; }
    } catch { if (current === epoch) error = 'Could not load public links.'; }
    finally { if (current === epoch) { busy = false; onBusy(false); } }
  }
  $effect(() => {
    void source.subject_id;
    shares = []; recoveredUrl = ''; copied = '';
    void load();
    return () => { epoch++; onBusy(false); };
  });
  async function mutate(action: () => Promise<unknown>) {
    if (busy || uncertain) return;
    busy = true; onBusy(true); error = '';
    const current = epoch;
    try { const guard = captureSharingLease(sharing); await guard.validate(); await action(); await guard.validate(); if (current === epoch) await load(); }
    catch { if (current === epoch) { uncertain = true; error = 'The change was not confirmed. Reload links before trying again.'; } }
    finally { if (current === epoch) { busy = false; onBusy(false); } }
  }
  async function copyLink(share: FeedbackShare) {
    if (busy) return;
    busy = true; onBusy(true); error = ''; recoveredUrl = ''; copied = '';
    const current = epoch;
    try {
      const guard = captureSharingLease(sharing);
      await guard.validate();
      const result = await feedbackApi.copyShare(share.share_id);
      await guard.validate();
      if (current !== epoch) return;
      if (!result.share_token) throw new Error('Link unavailable');
      recoveredUrl = `${location.origin}/feedback#${encodeURIComponent(result.share_token)}`;
      try { await navigator.clipboard.writeText(recoveredUrl); copied = share.share_id; }
      catch { error = 'Select the link below and copy it manually.'; }
    } catch { if (current === epoch) error = 'This link could not be recovered. Reload links; no replacement was created.'; }
    finally { if (current === epoch) { busy = false; onBusy(false); } }
  }
</script>
<section class="mt-3 space-y-3" aria-label="Existing public links">
  {#if busy && !shares.length}<p role="status" class="text-sm text-muted">Loading links…</p>{/if}
  {#each visible as share (share.share_id)}
    <article class="space-y-2 rounded-lg border border-separator p-3" aria-label={`Public link ${share.share_id}`}>
      <p class="break-all text-xs text-muted">{share.link_mode === 'download' ? 'Download' : 'Viewer'} · Expires {new Date(share.expires_at).toLocaleString()}</p>
      <div class="flex flex-wrap items-center gap-2">
        {#if share.link_mode !== 'download'}
          <label class="flex min-h-10 flex-1 items-center gap-2 text-sm">
            <input type="checkbox" checked={share.allow_comments} disabled={busy || uncertain}
              onchange={event => {
                const checked = event.currentTarget.checked;
                void mutate(() => feedbackApi.updateShare(share, checked));
              }} />Allow comments
          </label>
        {/if}
        <Button variant="secondary" size="icon-mobile" aria-label="Copy public link"
          title={share.can_copy ? 'Copy link' : 'The original token is unavailable, expired or revoked'}
          disabled={busy || !share.can_copy} onclick={() => copyLink(share)}>
          {#if copied === share.share_id}<Check size={16} />{:else}<Copy size={16} />{/if}
        </Button>
        <Button variant="secondary" disabled={busy || uncertain} onclick={() => mutate(() => feedbackApi.revokeShare(share))}>Revoke link</Button>
      </div>
      {#if !share.can_copy}<p class="text-xs text-muted">This older or expired link cannot be recovered. Creating another link does not change the original.</p>{/if}
    </article>
  {:else}{#if !busy}<p class="text-sm text-muted">No managed public links yet.</p>{/if}{/each}
  {#if recoveredUrl}
    <label class="block space-y-1 text-xs">Public link URL
      <input readonly value={recoveredUrl} onfocus={event => event.currentTarget.select()} class="min-h-10 w-full rounded-lg border border-control-border bg-surface px-2" />
    </label>
  {/if}
  {#if error}<p role="alert" class="text-sm text-danger">{error}</p>{/if}
  <Button variant="secondary" onclick={load} disabled={busy}>Reload links</Button>
</section>
