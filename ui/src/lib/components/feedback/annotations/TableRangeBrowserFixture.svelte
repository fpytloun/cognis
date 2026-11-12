<script lang="ts">
  import TableRangeHarness from './TableRangeHarness.svelte';
  import type { FeedbackAnnotationAdapter, FeedbackSurfaceBinding, FeedbackTargetView, FeedbackLocator, ScopedCanonicalFeedbackText } from '$lib/feedback/types';
  import { textDigest } from '$lib/feedback/annotations/digest';
  import AgentCommentReplies from '../AgentCommentReplies.svelte';
  import FeedbackHost from '../FeedbackHost.svelte';
  import { CommentWorkspace, provideCommentWorkspace } from '$lib/feedback/comment-workspace';
  let { showReply = false }: { showReply?: boolean } = $props();
  provideCommentWorkspace(new CommentWorkspace());
  const block = { id: 'release-table', type: 'table', title: '**Release checks**', columns: ['name', 'count'],
    rows: [['Zulu **source** text', 3], { values: ['Alpha source text', 4] }, { name: 'Beta source text', count: 5 }] };
  let target = $state.raw<FeedbackTargetView | null>(null);
  let mode = $state<'comment' | 'view'>('comment');
  let located = $state('');
  const adapters = new Map<string, FeedbackAnnotationAdapter>();
  const binding: FeedbackSurfaceBinding = {
    source: { subject_id: 'table-fixture-subject', source_token: 'table-fixture-token',
      source: { kind: 'document', content_ref: 'table-fixture-document' }, content_digest: 'a'.repeat(64),
      availability: 'available', permitted_locator_kinds: ['rich'], context: { kind: 'standalone' },
      lineage_id: null, revision: 1, latest_content_ref: null,
      capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false },
      title: 'Release data', format: 'rich', canonical_text: null },
    context: { kind: 'standalone' }, get mode() { return mode; }, tool: 'text',
    supported_locator_kinds: ['rich'], completeness: 'available', highlights: [], selected_thread_id: null,
    async resolveTextScope(scope): Promise<ScopedCanonicalFeedbackText> {
      if (scope.kind !== 'rich_field') throw new Error('Fixture supports rich fields only.');
      let value: unknown = block;
      for (const part of scope.field_path) value = (value as Record<string, unknown>)[part];
      if (typeof value !== 'string') throw new Error('Fixture field is not text.');
      const text_digest = await textDigest(value);
      return { scope, content_digest: 'a'.repeat(64),
        descriptor: { representation: 'feedback-text-v1', text_digest, length: value.length, available_ranges: [[0, value.length]] },
        text_page: { representation: 'feedback-text-v1', text_digest, text: value, start: 0, end: value.length, total_length: value.length } };
    },
    onTargetChange(value) { target = value; }, onAnchorCandidate() {}, onSelectThread() {}, onInteractionChange() {},
    registerAdapter(id, adapter) { adapters.set(id, adapter); return () => { adapters.delete(id); }; },
  };
  async function locate(field_path: string[], exact: string) {
    const locator: FeedbackLocator = { kind: 'rich', block_id: block.id, identity: 'semantic',
      item_key: null, field_path, range: { representation: 'feedback-text-v1', text_digest: await textDigest(exact),
        exact, start: 0, end: exact.length, prefix: '', suffix: '' } };
    located = 'not_located';
    for (const adapter of adapters.values()) {
      const result = await adapter.locate(locator);
      if (result.status === 'exact') { located = result.status; break; }
    }
  }
</script>
<main class="fixture" class:review={showReply} data-deliverable-id="table-fixture-document">
  <h1>Exact table selection fixture</h1>
  <p>Local synthetic source · real table renderer and annotation adapter · no persistence or send</p>
  {#if !showReply}
  <button onclick={() => { mode = mode === 'comment' ? 'view' : 'comment'; }}>Toggle commenting</button>
  <button onclick={() => { for (const adapter of adapters.values()) adapter.clearSelection(); }}>Clear selection</button>
  <button onclick={() => locate(['rows', '0', '0'], 'Zulu **source** text')}>Locate saved cell</button>
  <button onclick={() => locate(['title'], block.title)}>Locate saved title</button>
  <output aria-label="Saved anchor location">{located}</output>
  {/if}
  {#if showReply}
    <FeedbackHost source={binding.source.source} context={{ kind: 'chat', conversation_id: 'table-fixture-conversation' }}>
      <TableRangeHarness binding={undefined} {block} />
    </FeedbackHost>
  {:else}<TableRangeHarness {binding} {block} />{/if}
  {#if showReply}
    <div class="mt-8">
      <AgentCommentReplies replies={[{ callId: 'table-fixture-reply', commentId: 'table-fixture-thread',
        body: 'The release engineer owns these checks. **The rollout remains blocked** until they pass.', resolved: false }]} />
    </div>
  {:else}
  <output aria-label="Exact candidate">{JSON.stringify(target?.candidate.locator ?? null)}</output>
  <output aria-label="Selected rect count">{target?.geometry()?.rects.length ?? 0}</output>
  <div class="scroll-space">Native scrolling remains available below the table.</div>
  {/if}
</main>
<style>
  .fixture { max-width: 800px; height: calc(100dvh - 120px); overflow: auto; margin: 60px auto; padding: 24px; color: #e2e8f0; }
  .fixture.review { height: auto; overflow: visible; margin: 24px auto; }
  h1 { font-size: 20px; }
  p { margin-block: 12px 32px; }
  button { border: 1px solid #64748b; padding: 8px; margin: 8px 8px 32px 0; }
  output { display: block; margin-top: 24px; overflow-wrap: anywhere; }
  .scroll-space { height: 1600px; padding-top: 100px; }
</style>
