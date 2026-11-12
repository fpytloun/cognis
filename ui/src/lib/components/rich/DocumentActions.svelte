<script lang="ts">
  import { onDestroy, type Snippet } from 'svelte';
  import DownloadMenu from './DownloadMenu.svelte';
  import ShareMenu from './ShareMenuLoader.svelte';
  import ViewerIcon from './ViewerIcon.svelte';
  import type { ShareLinkOptions } from './share-options';
  import { anchoredMenu } from '$lib/actions/anchored-menu';
  import './document-actions.css';
  import Maximize from 'lucide-svelte/icons/maximize';
  import Ellipsis from 'lucide-svelte/icons/ellipsis';
  import Download from 'lucide-svelte/icons/download';
  import ExternalLink from 'lucide-svelte/icons/external-link';
  import Search from 'lucide-svelte/icons/search';
  import { canOpenDeliverableWindow, deliverableWindowUrl, openDeliverableWindow } from '$lib/deliverable-window';
  import { isCompactViewport } from '$lib/stores/viewport';

  export let openFullView: (() => void) | undefined = undefined;
  export let onSearch: (() => void) | undefined = undefined;
  export let content = '';
  export let copyDocument: () => void = copyContent;
  export let copied = false;
  export let copyDisabled = false;
  export let pdfUrl = '';
  export let htmlUrl = '';
  export let markdownUrl = '';
  export let internalUrl = '';
  export let standaloneUrl = '';
  export let createLink: ((options: ShareLinkOptions) => Promise<string>) | null | undefined = undefined;
  export let collapsible = false;
  export let inlineDisclosure = false;
  export let expanded = true;
  export let controlsId: string | undefined = undefined;
  export let toggleExpanded: () => void = () => {};
  export let extraActions: Snippet<[boolean?]> | undefined = undefined;
  export let revisionControls: Snippet | undefined = undefined;
  export let audioControls: Snippet | undefined = undefined;
  export let menuActions: Snippet | undefined = undefined;
  let more: HTMLDetailsElement;
  let copyError = '';
  let copyTimer: ReturnType<typeof setTimeout> | undefined;
  $: canOpenWindow = !$isCompactViewport && canOpenDeliverableWindow() && !!deliverableWindowUrl(internalUrl);
  async function copyContent() {
    copyError = '';
    try {
      await navigator.clipboard.writeText(content);
      copied = true;
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => copied = false, 1500);
    } catch { copyError = 'Could not copy document. Check clipboard permissions and try again.'; }
  }
  onDestroy(() => clearTimeout(copyTimer));

  // Touch browsers can blur the summary before delivering the menu item's
  // click. Dismiss on outside pointer-down or Escape, not focusout.
  function dismiss(event: PointerEvent) {
    if (more?.open && event.target instanceof Node && !more.contains(event.target)) more.open = false;
  }
  function escape(event: KeyboardEvent) {
    if (event.key !== 'Escape' || !more?.open) return;
    event.stopPropagation();
    more.open = false;
    more.querySelector('summary')?.focus();
  }
  function run(action: () => void) {
    more.open = false;
    more.querySelector('summary')?.focus();
    action();
  }
</script>

<svelte:window on:pointerdown={dismiss} on:keydown={escape} />
{#snippet collapseControl(inline: boolean)}
  <!-- Keep optional controls out of the standalone viewer's initial bundle. -->
  {#await import('$lib/components/ui/Button.svelte') then module}
    <svelte:component this={module.default} variant="ghost" size={inline ? 'icon' : 'sm'} type="button"
      class={inline ? 'document-icon-action' : ''}
      aria-label={expanded ? 'Collapse document' : 'Expand document'}
      title={expanded ? 'Collapse document' : 'Expand document'}
      aria-expanded={expanded} aria-controls={controlsId}
      onclick={() => inline ? toggleExpanded() : run(toggleExpanded)}>
      <ViewerIcon name={expanded ? 'collapse' : 'expand'} />
      {#if !inline}{expanded ? 'Collapse document' : 'Expand document'}{/if}
    </svelte:component>
  {/await}
{/snippet}
{#snippet searchControl(menu: boolean)}
  {#await import('$lib/components/ui/Button.svelte') then module}
    <svelte:component this={module.default} variant="ghost" size={menu ? 'sm' : 'icon'} type="button"
      class={menu ? '' : 'document-icon-action'} aria-label="Search in document" title="Search in document (⌘F / Ctrl+F)"
      onclick={() => menu ? run(onSearch!) : onSearch?.()}>
      <Search aria-hidden="true" />{#if menu}Search in document{/if}
    </svelte:component>
  {/await}
{/snippet}
<nav data-find-actions class="chat-document-actions" aria-label="Document actions">
  {#if inlineDisclosure && collapsible}{@render collapseControl(true)}{/if}
  {@render revisionControls?.()}
  {#if openFullView}
  <button class="document-icon-action" type="button" aria-label="Open fullscreen" title="Open fullscreen" on:click={openFullView}>
    <Maximize aria-hidden="true" />
  </button>
  {/if}
  {@render audioControls?.()}
  <!-- On phones download and share move into ⋯ so the title keeps its room. -->
  {#if !$isCompactViewport}
    {#if pdfUrl || htmlUrl || markdownUrl}<DownloadMenu {pdfUrl} {htmlUrl} {markdownUrl} />{/if}
    {#if internalUrl || createLink}<ShareMenu {internalUrl} {createLink} />{/if}
  {/if}
  <details class="chat-document-more" bind:this={more}>
    <summary class="document-icon-action" aria-label="More document actions" title="More document actions">
      <Ellipsis aria-hidden="true" />
    </summary>
    <div class="chat-document-menu" use:anchoredMenu={{ preferredSide: 'above' }}>
      {#if onSearch}{@render searchControl(true)}{/if}
      {#if $isCompactViewport}
        {#if pdfUrl}<a href={pdfUrl} download on:click={() => run(() => {})}><Download aria-hidden="true" />Download PDF</a>{/if}
        {#if htmlUrl}<a href={htmlUrl} download on:click={() => run(() => {})}><Download aria-hidden="true" />Download HTML</a>{/if}
        {#if markdownUrl}<a href={markdownUrl} download on:click={() => run(() => {})}><Download aria-hidden="true" />Download Markdown</a>{/if}
        {#if internalUrl || createLink}<ShareMenu {internalUrl} {createLink} label="Share link" />{/if}
      {/if}
      <button type="button" disabled={copyDisabled} on:click={() => run(copyDocument)}><ViewerIcon name={copied ? 'check' : 'copy'} />{copied ? 'Copied' : 'Copy document'}</button>
      {#if collapsible && !openFullView && !inlineDisclosure}{@render collapseControl(false)}{/if}
      {#if internalUrl}<a href={internalUrl}>Open in Library ↗</a>{/if}
      {#if canOpenWindow}
        <button type="button" on:click={() => run(() => openDeliverableWindow(internalUrl))}><ExternalLink aria-hidden="true" />Open in new window</button>
      {/if}
      {#if standaloneUrl && !internalUrl}<a href={standaloneUrl} aria-label="Open standalone page" target="_blank" rel="noreferrer">Open standalone page ↗</a>{/if}
      {@render extraActions?.(true)}
      {@render menuActions?.()}
    </div>
  </details>
  <span class="sr-only" role="status">{copied ? 'Document copied' : ''}</span>
  {#if copyError}<span role="alert">{copyError}</span>{/if}
</nav>

<style>
  .chat-document-actions { position: relative; display: flex; align-items: center; justify-content: flex-end; gap: 4px; margin-left: auto; font: 12px/1.5 var(--rich-font-sans, ui-sans-serif, system-ui, sans-serif); --rich-action-size: 32px; --rich-action-icon-size: 18px; --rich-action-radius: 6px; }
  .chat-document-actions > .sr-only { left: 0; top: 0; }
  .chat-document-menu :global(svg) { width: 18px; height: 18px; flex: none; }
  details { position: relative; flex: none; }
  summary { list-style: none; }
  summary::-webkit-details-marker { display: none; }
  .chat-document-menu { position: absolute; right: 0; bottom: calc(100% + .5rem); z-index: 30; width: max-content; min-width: 12rem; max-width: calc(100vw - 2rem); padding: .35rem; border: 1px solid var(--rich-line, rgb(var(--color-control-border))); border-radius: .75rem; background: var(--rich-surface-raised, rgb(var(--color-surface))); color: var(--rich-text, rgb(var(--color-foreground))); box-shadow: 0 8px 24px #0003; }
  .chat-document-menu :global(button), .chat-document-menu a { display: flex; align-items: center; justify-content: flex-start; gap: .5rem; width: 100%; min-height: 2.25rem; height: auto; padding: .4rem .6rem; border: 0; border-radius: .4rem; background: transparent; color: inherit; font: inherit; font-size: 13px; text-align: left; text-decoration: none; cursor: pointer; }
  .chat-document-menu :global(button:hover), .chat-document-menu a:hover { background: var(--rich-control-active, rgb(var(--color-surface-hover))); }
  button:focus-visible, summary:focus-visible, a:focus-visible { outline: 2px solid var(--rich-accent); outline-offset: 2px; }
  .chat-document-menu :global(select) { max-width: 100%; min-height: 2.25rem; padding: .4rem .6rem; }
  @media (pointer: coarse) {
    .chat-document-actions { --rich-action-size: 44px; --rich-action-icon-size: 20px; }
    .chat-document-menu :global(button), .chat-document-menu a { min-height: 44px; }
  }
</style>
