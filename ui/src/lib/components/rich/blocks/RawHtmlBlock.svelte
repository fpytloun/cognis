<script lang="ts">
  import { sanitizeHtml } from '$lib/markdown';
  import { blockText, type RichBlock } from '$lib/rich-deliverable';

  export let block: RichBlock;

  // `raw_html` is a client-only synthetic block type synthesized in
  // AssistantDeliverableBlock.svelte for `format: "html"` deliverables --
  // the content is already HTML, not markdown, so it must NOT go through
  // `renderMarkdown` (which escapes embedded raw HTML tags rather than
  // rendering them). Sanitize and render directly instead.
  $: html = sanitizeHtml(blockText(block));
</script>

<div class="rich-markdown" data-rich-block-type="raw_html">{@html html}</div>
