<script lang="ts">
  import { renderInlineMarkdown } from '$lib/markdown';
  import { safeImageUrl, safeUrl, type RichMediaUrlFor } from '$lib/rich-deliverable';

  export let media: unknown;
  export let fallbackAlt = '';
  export let mediaUrlFor: RichMediaUrlFor = () => '';
  export let loading: 'eager' | 'lazy' = 'eager';
  export let decoding: 'sync' | 'async' = 'sync';
  // Callers that always want a specific placement regardless of what the
  // author set on the media object (e.g. HeroBlock always wants a
  // full-bleed background banner) can force it here; omit to defer to
  // `record.placement` as before.
  export let placementOverride: 'top' | 'background' | 'leading' | '' = '';

  $: record = media && typeof media === 'object' && !Array.isArray(media) ? media as Record<string, unknown> : {};
  $: mediaKey = typeof record.key === 'string' ? record.key : '';
  $: mediaSource = mediaKey ? mediaUrlFor(mediaKey) : record.src ?? record.url ?? record.href;
  $: src = typeof mediaSource === 'string' && mediaSource ? safeImageUrl(mediaSource) : '';
  $: href = safeUrl(record.link ?? record.source_url ?? record.href);
  $: alt = typeof record.alt === 'string' ? record.alt : fallbackAlt;
  $: credit = typeof record.credit === 'string' ? record.credit : '';
  $: placement = placementOverride
    || (['top', 'background', 'leading'].includes(String(record.placement)) ? String(record.placement) : 'top');
  $: aspectRatio = typeof record.aspect_ratio === 'string' || typeof record.aspectRatio === 'string'
    ? String(record.aspect_ratio ?? record.aspectRatio) : '16 / 9';
  $: focalPoint = typeof record.focal_point === 'string' || typeof record.focalPoint === 'string'
    ? String(record.focal_point ?? record.focalPoint) : 'center';
  $: width = Number.isInteger(Number(record.width)) && Number(record.width) > 0 ? Number(record.width) : undefined;
  $: height = Number.isInteger(Number(record.height)) && Number(record.height) > 0 ? Number(record.height) : undefined;
  // Bindable so callers that build a media-driven layout (e.g. CardBlock's
  // `visual` variant overlay) can detect a broken image after the fact and
  // fall back to their normal non-media layout instead of leaving an
  // overlay treatment (transparent background, light-on-dark text) with
  // nothing behind it.
  export let failed = false;
  // Bindable read-only signal: true only once an image source exists and
  // has not failed to load. Callers can gate media-dependent layout on this
  // instead of the raw `media` prop, which does not know about resolvability
  // or load failure.
  export let hasVisibleMedia = false;
  $: hasVisibleMedia = Boolean(src) && !failed;
</script>

{#if src && !failed}
  <figure
    class:leading={placement === 'leading'}
    class:background={placement === 'background'}
    class="rich-media"
    style:aspect-ratio={aspectRatio}
    data-rich-media-placement={placement}
  >
    {#if href}
      <a href={href} target="_blank" rel="noreferrer" aria-label={alt || 'Open image source'}>
        <img src={src} {alt} {width} {height} {loading} {decoding} style:object-position={focalPoint} on:error={() => failed = true} />
      </a>
    {:else}
      <img src={src} {alt} {width} {height} {loading} {decoding} style:object-position={focalPoint} on:error={() => failed = true} />
    {/if}
    {#if credit}<figcaption>{@html renderInlineMarkdown(credit)}</figcaption>{/if}
  </figure>
{/if}

<style>
  .rich-media {
    position: relative;
    min-width: 0;
    margin: 0 0 1rem;
    overflow: hidden;
    border-radius: 1rem;
    /* Placeholder shown while an image loads or if it fails; tokenized so an
       empty media slot matches the surrounding surface in both themes. */
    background: var(--rich-surface-raised);
  }

  .rich-media.leading {
    width: clamp(5rem, 20%, 9rem);
    float: left;
    margin: 0 1rem .6rem 0;
  }

  .rich-media.background {
    margin: -1rem -1rem 1rem;
    border-radius: 1rem 1rem 0 0;
    opacity: .82;
  }

  .rich-media :global(a) { display: block; height: 100%; }
  .rich-media img { display: block; width: 100%; height: 100%; object-fit: cover; }
  .rich-media figcaption {
    position: absolute;
    right: .55rem;
    bottom: .45rem;
    max-width: calc(100% - 1rem);
    border-radius: .35rem;
    background: rgb(2 6 23 / .68);
    color: rgb(226 232 240);
    padding: .2rem .4rem;
    font-size: .68rem;
  }

  @media (max-width: 390px) {
    .rich-media.leading { width: 5rem; margin-right: .75rem; }
  }
</style>
