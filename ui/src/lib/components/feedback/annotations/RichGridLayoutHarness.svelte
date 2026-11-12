<script lang="ts">
  import RichDeliverable from '../../rich/RichDeliverable.svelte';
  import FeedbackSourceProvider from '../FeedbackSourceProvider.svelte';
  import type { FeedbackAnnotationAdapter, FeedbackLocator, FeedbackSurfaceBinding } from '$lib/feedback/types';

  let { mode = 'unbound' }: { mode?: 'unbound' | 'view' | 'comment' } = $props();
  const shortText = 'Short card body.';
  const longText = Array.from({ length: 8 }, (_, index) => `Paragraph ${index + 1} provides deliberately longer content to exercise the intrinsic height of this grid peer.`).join('\n\n');
  const payload = { title: 'Grid annotation layout regression', metadata: { presentation: 'dashboard' }, blocks: [
    { type: 'grid', id: 'peer-grid', columns: 2, children: [
      { type: 'card', id: 'short-card', title: 'Short peer', content: shortText, span: 1 },
      { type: 'card', id: 'long-card', title: 'Long peer', content: longText, span: 1 },
    ] },
  ] };
  const adapters = new Map<string, FeedbackAnnotationAdapter>();
  let candidate = $state<FeedbackLocator | null>(null);
  let located = $state('');
  const binding: FeedbackSurfaceBinding = {
    source: { subject_id: 'layout-subject', source_token: 'layout-source', source: { kind: 'document', content_ref: 'dlv_layout_fixture' },
      content_digest: 'layout-bytes', availability: 'available', permitted_locator_kinds: ['whole', 'rich'],
      capabilities: { read: true, comment: true, moderate: false, dispatch: false, disclose: false },
      canonical_text: null, context: { kind: 'standalone' }, lineage_id: null, revision: 1,
      latest_content_ref: null, title: 'Grid layout fixture', format: 'rich' },
    context: { kind: 'standalone' }, get mode() { return mode === 'comment' ? 'comment' : 'view'; },
    completeness: 'available', supported_locator_kinds: ['whole', 'rich'], selected_thread_id: null, highlights: [],
    onAnchorCandidate: value => { candidate = value.locator; },
    onInteractionChange() {}, onSelectThread() {},
    registerAdapter(id, adapter) { adapters.set(id, adapter); return () => { adapters.delete(id); }; },
    async resolveTextScope(scope) {
      const text = scope.kind === 'rich_field' && scope.block_id === 'short-card' ? shortText : longText;
      return { scope, content_digest: 'layout-bytes',
        descriptor: { representation: 'feedback-text-v1', text_digest: 'layout-field',
          length: text.length, available_ranges: [[0, text.length]] },
        text_page: { representation: 'feedback-text-v1', text_digest: 'layout-field',
          start: 0, end: text.length, total_length: text.length, text } };
    },
  };
  async function locateShort() {
    located = 'not_located';
    for (const adapter of adapters.values()) {
      const result = await adapter.locate({ kind: 'rich', block_id: 'short-card', identity: 'semantic', item_key: null, field_path: [], range: null });
      if (result.status === 'exact') { located = 'exact'; break; }
    }
  }
</script>

<FeedbackSourceProvider binding={mode === 'unbound' ? null : binding} onOpen={() => {}}>
  <RichDeliverable {payload} surface="standalone" />
</FeedbackSourceProvider>
<button data-testid="locate-short" onclick={() => void locateShort()}>Locate short peer</button>
<output data-testid="located-status">{located}</output>
<output data-testid="selected-field">{JSON.stringify(candidate)}</output>
