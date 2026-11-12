<script lang="ts">
  import { feedbackApi, type FeedbackHistoryItem } from '$lib/feedback/api';
  import { libraryHref } from '$lib/library';
  import ConversationLink from '$lib/components/ConversationLink.svelte';
  let { contentRef }: { contentRef: string } = $props();
  let subject = $state<string | null>(null);
  let version = $state(0);
  let preferred = $state<string | null>(null);
  let history = $state<FeedbackHistoryItem[]>([]);
  let managing = $state(false);
  let conversationLink = $state('');
  let error = $state('');
  let disclose = $state(false);
  $effect(() => {
    const ref = contentRef;
    let active = true;
    subject = null; preferred = null; history = [];
    void feedbackApi.resolve({ kind: 'document', content_ref: ref }, { kind: 'standalone' }).then(async source => {
      const [summary, events] = await Promise.all([feedbackApi.summary(source.subject_id), feedbackApi.history(source.subject_id)]);
      if (!active) return;
      subject = source.subject_id; version = summary.version; preferred = summary.preferred_followup;
      disclose = source.capabilities.disclose; history = events.items.filter(item => item.result_ref);
    }).catch(() => { /* Optional continuity never blocks the selected document. */ });
    return () => { active = false; };
  });
  async function save() {
    if (!subject) return;
    try {
      let conversation: string | null = null;
      if (conversationLink.trim()) {
        const parsed = new URL(conversationLink, location.origin);
        const match = /^\/chat\/([^/]+)\/?$/.exec(parsed.pathname);
        if (parsed.origin !== location.origin || !match) throw new Error('Paste an internal conversation link.');
        conversation = decodeURIComponent(match[1]);
      }
      await feedbackApi.followup(subject, version, conversation);
      const summary = await feedbackApi.summary(subject);
      preferred = summary.preferred_followup; version = summary.version; managing = false;
    } catch (caught) { error = caught instanceof Error ? caught.message : 'Follow-up preference could not be saved.'; }
  }
</script>
{#if preferred || history.length || (subject && disclose)}
  <nav class="flex flex-wrap items-center gap-x-3 border-b border-separator px-3 py-1 text-xs" aria-label="Document follow-up and results">
    {#if preferred}<ConversationLink class="inline-flex min-h-11 items-center text-accent" conversationId={preferred}>Open follow-up</ConversationLink>{/if}
    {#each history as item}{#if item.result_ref}<a class="inline-flex min-h-11 items-center text-accent" href={libraryHref({ kind: item.result_ref.startsWith('dlv_') ? 'deliverable' : 'artifact', id: item.result_ref })}>Result revision · {new Date(item.created_at).toLocaleDateString()}</a>{/if}{/each}
    {#if disclose}<button type="button" class="min-h-11 text-muted" onclick={() => managing = !managing}>Change follow-up</button>{/if}
  </nav>
{/if}
{#if managing}
  <div class="space-y-2 border-b border-separator p-3">
    <label class="block text-sm">Internal conversation link<input class="mt-1 min-h-11 w-full rounded border border-separator bg-surface p-2" bind:value={conversationLink} /></label>
    <p class="text-xs text-muted">Leave empty to clear the preference. This does not send feedback or change its source.</p>
    <button type="button" class="min-h-11 text-accent" onclick={save}>Save follow-up</button>
    {#if error}<p role="alert" class="text-sm text-danger">{error}</p>{/if}
  </div>
{/if}
