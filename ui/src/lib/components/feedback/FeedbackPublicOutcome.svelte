<script lang="ts">
  import { feedbackApi } from '$lib/feedback/api';
  import type { FeedbackShare } from '$lib/feedback/types';
  let { subjectId, entryVersionId }: { subjectId: string; entryVersionId: string } = $props();
  let open = $state(false);
  let shares = $state<FeedbackShare[]>([]);
  let selected = $state('');
  let disposition = $state<'addressed' | 'needs_clarification' | 'not_changed'>('addressed');
  let reason = $state('');
  let error = $state('');
  let saved = $state(false);
  async function begin() {
    open = true;
    try { shares = (await feedbackApi.shares(subjectId)).items; }
    catch (caught) { error = caught instanceof Error ? caught.message : 'Public share settings unavailable.'; }
  }
</script>
<button type="button" class="min-h-11 text-xs text-accent" onclick={begin}>Publish a public outcome</button>
{#if open}
  <div class="space-y-2 border-l-2 border-separator pl-2">
    <p class="text-xs text-muted">Only this disposition and reason become public. No task IDs, private comments or result links are included.</p>
    <label class="block text-xs">Public share<select class="min-h-11 w-full rounded border border-separator bg-surface p-2" bind:value={selected}><option value="">Choose the comment's share</option>{#each shares as share}<option value={share.share_id}>{share.share_id} · {share.discussion}</option>{/each}</select></label>
    <label class="block text-xs">Outcome<select class="min-h-11 w-full rounded border border-separator bg-surface p-2" bind:value={disposition}><option value="addressed">Addressed</option><option value="needs_clarification">Needs clarification</option><option value="not_changed">Not changed</option></select></label>
    <label class="block text-xs">Public reason<textarea class="w-full rounded border border-separator bg-surface p-2" bind:value={reason} maxlength="4096"></textarea></label>
    <button type="button" class="min-h-11 text-xs text-accent" disabled={!selected || !reason.trim()} onclick={async () => { try { await feedbackApi.publishOutcome(selected, entryVersionId, disposition, reason); saved = true; } catch (caught) { error = caught instanceof Error ? caught.message : 'Outcome could not be published.'; } }}>Publish outcome</button>
    {#if saved}<p role="status" class="text-xs">Public outcome published.</p>{/if}
    {#if error}<p role="alert" class="text-xs text-danger">{error}</p>{/if}
  </div>
{/if}
