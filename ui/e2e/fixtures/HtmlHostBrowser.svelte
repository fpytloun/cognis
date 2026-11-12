<script lang="ts">
  import HtmlFeedbackHostHarness from '../../src/lib/components/feedback/HtmlFeedbackHostHarness.svelte';
  const abort = new AbortController();
  const guard = { signal: abort.signal, assertCurrent() { abort.signal.throwIfAborted(); }, validate() { abort.signal.throwIfAborted(); } };
  let preview = $state({ text: '<h1 data-feedback-key="heading">Observed heading</h1><button onclick="this.textContent=\'Action performed\'">Run action</button>', guard });
</script>
<button onclick={() => preview = { ...preview, text: '<h1>Replacement heading</h1>' }}>Replace preview</button>
<button onclick={() => abort.abort()}>Revoke preview guard</button>
<HtmlFeedbackHostHarness {preview}
  source={{ kind: 'document', content_ref: 'art_4ad53de97cfe4253931563af979f4d0b' }}
  context={{ kind: 'chat', conversation_id: 'html-browser-origin' }} />
