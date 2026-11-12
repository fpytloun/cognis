<script lang="ts">
  import { onMount } from 'svelte';
  import RichIcon from './RichIcon.svelte';
  import ViewerIcon from './ViewerIcon.svelte';
  import DownloadMenu from './DownloadMenu.svelte';
  import { viewerTheme, setViewerTheme, type ViewerTheme } from './viewer-theme';

  export let title = '';
  export let surface: 'embedded' | 'standalone' = 'standalone';
  export let exportMode = false;
  export let customControls = false;
  export let identity: unknown = undefined;
  export let pdfUrl = '';
  export let htmlUrl = '';
  export let markdownUrl = '';
  export let copied = false;
  export let onCopy: () => void;
  export let onClose: (() => void) | null = null;
  export let progress = 0;
  export let showProgress = true;
  export let showTocControl = false;
  export let tocOpen = false;
  export let tocLayout: 'sidebar' | 'drawer' | 'auto' = 'auto';
  export let onOpenToc: () => void = () => {};
  let wideViewport = false;
  let nextTheme: ViewerTheme;
  $: tocOnLeft = tocLayout === 'sidebar' || (tocLayout === 'auto' && wideViewport);
  $: tocTriggerVisible = showTocControl && (!tocOnLeft || !tocOpen);
  $: currentTheme = $viewerTheme === 'app' ? 'system' : $viewerTheme ?? 'system';
  $: nextTheme = currentTheme === 'system' ? 'light' : currentTheme === 'light' ? 'dark' : 'system';
  $: themeLabel = `Theme: ${currentTheme}. Switch to ${nextTheme} theme`;
  onMount(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(min-width: 960px)');
    const update = () => wideViewport = query.matches;
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  });
  $: authored = identity && typeof identity === 'object' ? identity as Record<string, unknown> : {};
  $: label = typeof authored.label === 'string' ? authored.label.trim() : '';
  const icons = new Set(['activity', 'alert', 'calendar', 'check', 'clock', 'external', 'info', 'trend_down', 'trend_up', 'arrow_up_right']);
  $: authoredIcon = authored.icon && typeof authored.icon === 'object'
    ? authored.icon as Record<string, unknown>
    : {};
  $: icon = typeof authoredIcon.name === 'string' && icons.has(authoredIcon.name)
    ? authoredIcon.name
    : '';
  $: iconAlt = typeof authoredIcon.alt === 'string' ? authoredIcon.alt.trim() : '';
</script>

<header class="viewer-chrome" class:custom-controls={customControls} data-testid="rich-viewer-chrome">
  <div class="viewer-chrome-inner" data-find-toolbar>
  {#if tocTriggerVisible && tocOnLeft}
    <button class="toc-control toc-left" type="button" aria-label="Open table of contents" title="Open table of contents" aria-expanded={tocOpen} on:click={onOpenToc}><ViewerIcon name="expand" /></button>
  {/if}
  {#if label || icon}
    <span class="viewer-identity" data-testid="rich-viewer-identity">
      {#if icon}<RichIcon {icon} label={label ? '' : iconAlt} />{/if}
      {#if label}<span>{label}</span>{/if}
    </span>
  {/if}
  <div class="viewer-title-group">
    <span class="viewer-title" title={title}>{title}</span>
    {#if !exportMode}<slot name="titleActions" />{/if}
  </div>
  <nav aria-label="Viewer controls" data-find-actions>
    {#if !exportMode}
    {#if surface === 'standalone'}
    <button class="theme-control" type="button" data-theme-mode={currentTheme} aria-label={themeLabel} title={themeLabel} on:click={() => setViewerTheme(nextTheme)}>
      <ViewerIcon name={currentTheme} />
    </button>
    {/if}
    {#if !customControls}
    {#if pdfUrl || htmlUrl || markdownUrl}<DownloadMenu {pdfUrl} {htmlUrl} {markdownUrl} />{/if}
    <button class="copy-control" type="button" aria-label={copied ? 'Copied' : 'Copy document'} title={copied ? 'Copied' : 'Copy document'} on:click={onCopy}><ViewerIcon name={copied ? 'check' : 'copy'} /></button>
    {/if}
    {/if}
    {#if tocTriggerVisible && !tocOnLeft}
      <button class="toc-control" type="button" aria-label="Open table of contents" title="Open table of contents" aria-expanded={tocOpen} on:click={onOpenToc}><ViewerIcon name="contents" /></button>
    {/if}
    {#if onClose && !exportMode}
      <button type="button" aria-label="Close preview" title="Close preview" on:click={onClose}><ViewerIcon name="close" /></button>
    {/if}
    {#if !exportMode}<slot />{/if}
  </nav>
  </div>
  <div class="viewer-progress" role={showProgress ? 'progressbar' : undefined} aria-label={showProgress ? 'Reading progress' : undefined} aria-valuemin={showProgress ? 0 : undefined} aria-valuemax={showProgress ? 100 : undefined} aria-valuenow={showProgress ? progress : undefined}><span style={`width:${progress}%`}></span></div>
</header>

<style>
  @media print {
    .viewer-chrome { display: none !important; }
  }
  .viewer-chrome { position: relative; min-width: 0; padding-top: env(safe-area-inset-top); background: var(--rich-surface); color: var(--rich-text-secondary); }
  .custom-controls { --rich-action-size: 32px; --rich-action-icon-size: 18px; }
  @media (pointer: coarse) { .custom-controls { --rich-action-size: 44px; --rich-action-icon-size: 20px; } }
  .viewer-chrome-inner { box-sizing: border-box; max-width: 1320px; height: 62px; margin: auto; padding: 9px max(40px, env(safe-area-inset-right)) 9px max(40px, env(safe-area-inset-left)); display: flex; align-items: center; gap: 14px; }
  .viewer-title-group { min-width: 0; flex: 1; display: flex; align-items: center; gap: 8px; }
  .viewer-title { min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; font-size: 13.5px; line-height: 1.3; font-weight: 500; }
  .viewer-identity { display: flex; align-items: center; gap: 4px; max-width: 12rem; flex: none; font: 500 10.5px/1 var(--rich-font-mono); letter-spacing: .14em; color: var(--rich-accent); border: 1px solid color-mix(in srgb, var(--rich-accent) 35%, transparent); border-radius: 4px; padding: 5px 7px; }
  .viewer-identity > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  nav { display: flex; align-items: center; flex-shrink: 0; }
  nav { gap: 8px; }
  button { position: relative; display: inline-grid; place-items: center; width: 34px; height: 34px; padding: 0; border: 1px solid var(--rich-line); border-radius: 7px; background: transparent; color: var(--rich-text-secondary); text-decoration: none; cursor: pointer; }
  button::after { content: ''; position: absolute; width: 44px; height: 44px; }
  .theme-control, .toc-control { border: 0; flex-shrink: 0; }
  .toc-left { color: var(--rich-text-secondary); }
  .copy-control { background: #159fef; border-color: #159fef; color: #061826; }
  button:hover { filter: brightness(1.12); }
  button:focus-visible { outline: 2px solid var(--rich-accent); outline-offset: 3px; }
  .viewer-progress { height: 1px; background: var(--rich-line); }
  .viewer-progress span { display: block; height: 2px; background: var(--rich-accent); }
  @media (pointer: coarse), (max-width: 600px) { button { width: 44px; height: 44px; } }
  @media (max-width: 600px) { .viewer-chrome-inner { padding: 8px 12px; gap: 8px; flex-wrap: wrap; height: auto; min-height: 106px; } .viewer-identity { max-width: 7rem; } .viewer-title-group { flex-basis: 50%; } nav { width: 100%; gap: 2px; justify-content: flex-end; } }
  @media (max-width: 600px) {
    .custom-controls .viewer-chrome-inner { min-height: 62px; flex-wrap: nowrap; }
    .custom-controls .viewer-title-group { flex-basis: 0; }
    .custom-controls nav { width: auto; }
  }
</style>
