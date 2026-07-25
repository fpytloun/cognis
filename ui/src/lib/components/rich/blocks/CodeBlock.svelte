<script lang="ts">
  import hljs from 'highlight.js/lib/common';
  import { blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';

  export let block: RichBlock;

  $: language = blockText(block, 'language') || blockText(block, 'lang');
  $: source = blockText(block);
  $: highlighted = highlight(source, language);

  function highlight(value: string, requestedLanguage: string): string {
    try {
      if (requestedLanguage && hljs.getLanguage(requestedLanguage)) {
        return hljs.highlight(value, { language: requestedLanguage, ignoreIllegals: true }).value;
      }
      return value
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;');
    } catch {
      return value
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;');
    }
  }
</script>

<section class="rich-code-card" data-rich-block-type="code">
  {#if blockTitle(block)}<h4>{blockTitle(block)}</h4>{/if}
  {#if language}<p class="rich-code-language">{language}</p>{/if}
  <pre class="rich-code"><code class:hljs={Boolean(language)}>{@html highlighted}</code></pre>
</section>
