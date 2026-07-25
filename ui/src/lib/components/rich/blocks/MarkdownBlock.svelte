<script lang="ts">
  import { renderMarkdown } from '$lib/markdown';
  import { blockText, blockTitle, type RichBlock } from '$lib/rich-deliverable';

  export let block: RichBlock;

  $: rendered = renderMarkdown(blockText(block));
  $: publicationHtml = renderPublicationMarkdown(rendered, block);

  function renderPublicationMarkdown(value: string, current: RichBlock): string {
    const anchor = typeof current.__publication_anchor === 'string' ? current.__publication_anchor : '';
    if (!anchor) return value;
    const level = [2, 3, 4].includes(Number(current.__publication_level))
      ? Number(current.__publication_level)
      : 2;
    const title = blockTitle(current);
    const descriptors = Array.isArray(current.__publication_markdown_headings)
      ? current.__publication_markdown_headings as Array<Record<string, unknown>>
      : [];
    const linkTargets = current.__publication_link_targets && typeof current.__publication_link_targets === 'object'
      ? current.__publication_link_targets as Record<string, string>
      : {};
    let headingIndex = 0;
    const content = value.replace(/<h[1-4](?:\s[^>]*)?>([\s\S]*?)<\/h[1-4]>/gi, (_match, inner) => {
      if (!title && headingIndex === 0) {
        headingIndex += 1;
        return `<h${level} id="${anchor}" tabindex="-1">${inner}</h${level}>`;
      }
      const descriptor = descriptors.find((item) => item.index === headingIndex);
      headingIndex += 1;
      const nestedAnchor = typeof descriptor?.anchor === 'string' ? descriptor.anchor : '';
      const id = nestedAnchor ? ` id="${nestedAnchor}" tabindex="-1"` : '';
      const nestedLevel = [3, 4].includes(Number(descriptor?.level)) ? Number(descriptor?.level) : 3;
      return `<h${nestedLevel}${id}>${inner}</h${nestedLevel}>`;
    });
    const withLinks = content.replace(/href="#([^"]+)"/gi, (match, fragment) => {
      const target = linkTargets[slug(fragment)];
      return target ? `href="#${target}"` : match;
    });
    if (!title) return withLinks;
    return `<h${level} id="${anchor}" tabindex="-1">${escapeHtml(title)}</h${level}>${withLinks}`;
  }

  function escapeHtml(value: string): string {
    return value
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function slug(value: string): string {
    return value
      .normalize('NFKD')
      .replace(/[\u0300-\u036f]/g, '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-|-$/g, '') || 'section';
  }
</script>

<div class="rich-markdown" data-rich-block-type="markdown">{@html publicationHtml}</div>
