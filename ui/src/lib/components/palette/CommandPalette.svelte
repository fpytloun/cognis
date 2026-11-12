<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import Search from 'lucide-svelte/icons/search';
  import ArrowUpRight from 'lucide-svelte/icons/arrow-up-right';
  import CornerDownLeft from 'lucide-svelte/icons/corner-down-left';
  import Command from 'lucide-svelte/icons/command';
  import { portal } from '$lib/actions/portal';
  import { api } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import { usageAnalyticsEnabled } from '$lib/system';
  import { overlayStack, isTopOverlay, registerOverlay } from '$lib/stores/overlays';
  import { wsClient, isMetadataRefreshOnlyScope } from '$lib/ws/client';
  import { formatRelativeTime, formatAbsoluteTime } from '$lib/time';
  import { getDashboardLauncher } from '$lib/dashboard/launcher';
  import { addToast } from '$lib/stores/toasts';
  import { PaletteSearch } from '$lib/palette/search';
  import { PaletteThumbnails } from '$lib/palette/thumbnails';
  import { PaletteHistory } from '$lib/palette/history';
  import { numberedResultIndex } from '$lib/palette/shortcuts';
  import { identity, labels, localItems, highlighted, destinationLabel, shouldOpenWindow, normalize, resultTypes, isEntityKind, isWindowKind, type PaletteItem } from '$lib/palette/model';
  import ResultIcon from './ResultIcon.svelte';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import TypeFilter from './TypeFilter.svelte';

  const search = new PaletteSearch((query, cursor, signal, types) => api.search.palette(query, cursor, signal, types));
  const thumbnails = new PaletteThumbnails((id) => api.artifacts.signedUrl(id, 60, 'download'));
  let open = $state(false);
  let leaving = $state(false);
  let query = $state('');
  let selectedTypes = $state<PaletteItem['kind'][]>([...resultTypes]);
  const entityTypes = $derived(selectedTypes.filter(isEntityKind));
  let selected = $state('');
  let panel = $state<HTMLDivElement>();
  let input = $state<HTMLInputElement>();
  let results = $state<HTMLDivElement>();
  let scroll = $state(0);
  let reducedMotion = false;
  let closeTimer: ReturnType<typeof setTimeout> | null = null;
  let previousFocus: HTMLElement | null = null;
  let overlay: ReturnType<typeof registerOverlay> | null = null;
  let activating = false;
  const history = new PaletteHistory();
  let hasHistory = $state(false), browsingHistory = $state(false);
  let rememberOnClose = true;
  const mod = $state({ label: '⌘' });
  const suggestions = $derived(localItems(query, $auth.user?.role === 'admin', $auth.user?.role !== 'viewer', $usageAnalyticsEnabled)
    .filter(item => selectedTypes.includes(item.kind)));
  const metadataItems = $derived($search.items);
  const items = $derived.by(() => {
    const needle = normalize(query.trim());
    const exact: PaletteItem[] = [], settings: PaletteItem[] = [], actions: PaletteItem[] = [], pages: PaletteItem[] = [];
    // SQL owns the entire metadata order, including exact-title matches and
    // content diversity. Never regroup a truncated server page client-side.
    for (const item of suggestions) {
      if (needle && normalize(item.title) === needle) exact.push(item);
      else if (item.kind === 'setting') settings.push(item);
      else if (item.kind === 'action') actions.push(item);
      else if (item.kind === 'page') pages.push(item);
    }
    return needle ? [...exact, ...pages, ...actions, ...metadataItems, ...settings] : [...actions, ...metadataItems];
  });
  const activeIndex = $derived(Math.max(0, items.findIndex((item) => identity(item) === selected)));
  const active = $derived(items[activeIndex]);
  const start = $derived(Math.max(0, Math.floor(scroll / 68) - 3));
  const visible = $derived(items.slice(start, start + 18));
  const activeOption = $derived(active && activeIndex >= start && activeIndex < start + 18 ? `palette-option-${activeIndex}` : undefined);
  const statusText = $derived($search.loading ? 'Searching Cognis…' : $search.error ? 'Search unavailable' : `${items.length} results${$search.cursor ? ', more available' : ''}`);

  export async function show() {
    if (!$auth.user || $auth.status !== 'authenticated') return;
    if (open && !leaving) { input?.focus(); return; }
    if (get(overlayStack).length > 0 && !overlay) return;
    if (closeTimer) clearTimeout(closeTimer);
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    history.open($auth.user.email);
    hasHistory = history.size > 0; browsingHistory = false; rememberOnClose = true;
    query = ''; selected = ''; scroll = 0; leaving = false; open = true;
    overlay ??= registerOverlay({ kind: 'blocking', blocksChrome: true, root: () => panel ?? null });
    search.setQuery('', entityTypes);
    await tick();
    input?.focus({ preventScroll: true });
  }
  function close(restore = true, remember = true) {
    if (!open || leaving) return;
    if (remember && rememberOnClose) history.remember(query);
    search.close(); leaving = true;
    overlay?.unregister(); overlay = null;
    if (restore && previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    previousFocus = null;
    closeTimer = setTimeout(() => { open = false; leaving = false; }, reducedMotion ? 0 : 90);
  }
  function updateQuery() {
    selected = ''; scroll = 0;
    if (results) results.scrollTop = 0;
    search.setQuery(query, entityTypes);
  }
  function onInput() {
    history.reset(); browsingHistory = false; rememberOnClose = true;
    updateQuery();
  }
  function clearHistory() {
    history.clear(); hasHistory = false; browsingHistory = false; rememberOnClose = false;
    input?.focus();
  }
  function changeTypes(types: PaletteItem['kind'][]) {
    selectedTypes = types;
    updateQuery();
  }
  function select(index: number) {
    const item = items[index];
    if (!item) return;
    selected = identity(item);
    if (results) {
      const top = index * 68;
      if (top < results.scrollTop) results.scrollTop = top;
      if (top + 68 > results.scrollTop + results.clientHeight) results.scrollTop = top + 68 - results.clientHeight;
    }
    if (index >= items.length - 3) search.more(true);
  }
  async function activate(item: PaletteItem | undefined, forceRoute = false) {
    if (!item || activating) return;
    activating = true;
    const launcher = getDashboardLauncher();
    const windowed = shouldOpenWindow(item, launcher, forceRoute);
    close(false);
    try {
      if (windowed && launcher && isWindowKind(item.kind)) {
        await launcher.open({ ...item, kind: item.kind });
      } else {
        let href = item.href;
        if (item.kind === 'action') {
          const currentAgent = $page.url.searchParams.get('agent_id');
          if (currentAgent) href += `?agent_id=${encodeURIComponent(currentAgent)}`;
        }
        await goto(href);
      }
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Could not open result.', 'error');
    } finally { activating = false; }
  }
  function handleKey(event: KeyboardEvent) {
    if (event.isComposing || event.repeat) return;
    const modifier = event.metaKey || event.ctrlKey;
    if (modifier && !event.altKey && event.key.toLowerCase() === 'k' && !event.shiftKey) {
      if (get(overlayStack).length > 0 && !isTopOverlay(overlay?.id ?? null)) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (open && !leaving) close(); else void show();
      return;
    }
    if (!open || leaving || !isTopOverlay(overlay?.id ?? null)) return;
    const filter = event.target instanceof Element ? event.target.closest('[data-palette-filter]') : null;
    if (filter && event.key !== 'Tab' && (event.key !== 'Escape' || filter.getAttribute('data-open') === 'true')) return;
    if (event.target === input && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      history.reset(); browsingHistory = false;
    }
    if (event.target === input && !modifier && !event.shiftKey &&
      (event.key === 'ArrowUp' || event.key === 'ArrowDown') &&
      (event.altKey || history.browsing || (event.key === 'ArrowUp' && !query.trim()))) {
      const value = event.key === 'ArrowUp' ? history.previous(query) : history.next();
      if (value !== null || event.altKey) {
        event.preventDefault(); event.stopImmediatePropagation();
        browsingHistory = history.browsing;
        if (value !== null && value !== query) { query = value; updateQuery(); }
        return;
      }
    }
    if (event.altKey) return;
    const numberedIndex = numberedResultIndex(event);
    if (numberedIndex !== null) {
      event.preventDefault(); event.stopImmediatePropagation();
      void activate(items[numberedIndex]); return;
    }
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation(); close(); return;
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); event.stopImmediatePropagation();
      select(Math.max(0, Math.min(items.length - 1, activeIndex + (event.key === 'ArrowDown' ? 1 : -1)))); return;
    }
    if (event.key === 'Enter' && (event.target === input || modifier)) {
      event.preventDefault(); event.stopImmediatePropagation(); void activate(active, modifier); return;
    }
    if (event.key === 'Tab' && panel) {
      const controls = [...panel.querySelectorAll<HTMLElement>('input, button:not([disabled]), a[href]')]
        .filter((node) => node.tabIndex >= 0 && node.getClientRects().length > 0 && node.getAttribute('aria-disabled') !== 'true');
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }
  function onScroll() {
    if (!results) return;
    scroll = results.scrollTop;
    if (results.scrollHeight - results.scrollTop - results.clientHeight < 180) search.more(true);
  }
  function loadMore(node: HTMLElement) {
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) search.more();
    }, { root: results, rootMargin: '120px' });
    observer.observe(node);
    return { destroy() { observer.disconnect(); } };
  }
  onMount(() => {
    mod.label = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl';
    reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
    let user = $auth.status === 'authenticated' ? $auth.user?.email ?? '' : '';
    const stopAuth = auth.subscribe((state) => {
      const next = state.status === 'authenticated' ? state.user?.email ?? '' : '';
      if (next !== user) {
        search.clear(); thumbnails.clear(); close(true, false); user = next;
        history.open(next || null); hasHistory = history.size > 0;
        browsingHistory = false; query = '';
      }
    });
    const stopSocket = wsClient.subscribe((event) => {
      if (event.type === 'scope_invalidated' && (isMetadataRefreshOnlyScope(event.reason, event.metadata_only) || event.reason === 'event_store_session_invalidated')) search.invalidate();
      else if (['scope_invalidated', 'sidebar_conversation_removed', 'reconnected'].includes(event.type)) search.invalidate(true);
      else if (['conversation_updated', 'sidebar_conversation_upsert'].includes(event.type)) search.invalidate();
    });
    window.addEventListener('keydown', handleKey, true);
    return () => {
      stopAuth(); stopSocket(); search.clear(); thumbnails.clear(); overlay?.unregister();
      if (closeTimer) clearTimeout(closeTimer);
      window.removeEventListener('keydown', handleKey, true);
    };
  });
</script>

{#if open}
  <div use:portal class="palette-layer" class:leaving data-testid="command-palette">
    <button class="palette-backdrop" tabindex="-1" aria-label="Dismiss command palette" onclick={() => close()}></button>
    <div class="palette-position">
      <div bind:this={panel} class="palette-panel" role="dialog" aria-modal="true" aria-label="Command palette" tabindex="-1">
        <div class="palette-input-row">
          <Search class="search-icon" size={26} strokeWidth={1.7} />
          <input bind:this={input} bind:value={query} oninput={onInput}
            role="combobox" aria-label="Search Cognis" aria-autocomplete="list" aria-expanded="true"
            aria-controls="palette-results" aria-activedescendant={activeOption}
            placeholder="Search anything…" autocomplete="off" spellcheck="false" maxlength="160" />
          <button class="escape-key" aria-label="Close command palette" onclick={() => close()}>esc</button>
        </div>
        <div class="search-progress" class:loading={$search.loading}></div>
        <div class="palette-section-heading">
          <span>{query ? 'Search results' : 'Jump back in'}</span>
          <TypeFilter selected={selectedTypes} onchange={changeTypes} />
        </div>
        <div bind:this={results} class="palette-results" onscroll={onScroll}>
          <div id="palette-results" role="listbox" aria-label="Search results" style={`height:${items.length * 68}px;position:relative;`}>
            {#each visible as item, offset (identity(item))}
              {@const index = start + offset}
              <div class="palette-option" class:selected={index === activeIndex} id={`palette-option-${index}`}
                role="option" aria-selected={index === activeIndex} tabindex="-1"
                style={`transform:translateY(${index * 68}px)`}>
                <button class="result-main" tabindex="-1" onclick={() => void activate(item)}
                  onpointermove={(event) => { if (event.movementX || event.movementY) selected = identity(item); }}>
                  <ResultIcon {item} {thumbnails} />
                  <span class="result-copy">
                    <span class="result-title">{#each highlighted(item.title, query) as segment}{#if segment.match}<mark>{segment.text}</mark>{:else}{segment.text}{/if}{/each}</span>
                    <span class="result-subtitle">
                      {labels[item.kind]}
                      {#if item.kind === 'artifact' && item.origin === 'user'}
                        <span class="separator">·</span><span class="result-agent-name" title={item.producer_name ?? undefined}>Uploaded by {item.producer_name ?? 'user'}</span>
                      {:else if item.agent_name && item.kind !== 'agent'}
                        <span class="separator">·</span><span class="result-agent" title={item.agent_name}>
                          {#if item.kind !== 'conversation'}<span aria-hidden="true"><AgentAvatar name={item.agent_name} avatarUrl={item.agent_avatar_url} draggable={false} class="h-3.5 w-3.5 rounded-full text-[8px]" /></span>{/if}
                          <span class="result-agent-name">{item.agent_name}</span>
                        </span>
                      {:else if item.origin === 'agent'}<span class="separator">·</span>Agent-produced
                      {:else if item.origin === 'other'}<span class="separator">·</span>Source unknown{/if}
                      {#if item.status}<span class="separator">·</span>{item.status}{/if}
                      {#if item.kind === 'setting'}<span class="separator">/</span>{item.title}{/if}
                    </span>
                    {#if item.updated_at}<time class="result-updated" datetime={item.updated_at} title={formatAbsoluteTime(item.updated_at)}>Updated {formatRelativeTime(item.updated_at)}</time>{/if}
                  </span>
                </button>
                {#if index < 9}<kbd class="number-hint">{mod.label === '⌘' ? '⌃⇧' : 'Ctrl+Shift+'}{index + 1}</kbd>{/if}
                {#if item.kind !== 'action'}
                  <a class="redirect-button" href={item.href} aria-label={`Open ${item.title} in ${destinationLabel(item)}`}
                    title={`Open in ${destinationLabel(item)} (${mod.label}↵)`}
                    onclick={(event) => { if (event.button === 0 && !event.shiftKey && !event.metaKey && !event.ctrlKey) { event.preventDefault(); void activate(item, true); } }}>
                    <ArrowUpRight size={17} strokeWidth={1.6} />
                  </a>
                {/if}
              </div>
            {/each}
          </div>
          {#if !items.length && !$search.loading && !$search.error}
            <div class="palette-empty"><Search size={28} strokeWidth={1.3} /><strong>No matches for “{query}”</strong><span>Try another name or a shorter search.</span></div>
          {/if}
          {#if $search.cursor && !$search.capped && !$search.loading && !$search.error}
            {#if $search.paused}
              <button class="load-more" onclick={() => search.more(true)}>Search more names</button>
            {:else}
              {#key $search.cursor}<button class="load-more" use:loadMore onclick={() => search.more(true)}>More results</button>{/key}
            {/if}
          {/if}
          {#if $search.capped}<p class="palette-notice">Refine your search to find more results.</p>{/if}
          {#if $search.error}<div class="palette-notice" role="status">{$search.error} <button onclick={() => search.retry()}>Retry</button></div>{/if}
          {#if $search.loading && !items.length}<p class="palette-notice">Searching names across Cognis…</p>{/if}
        </div>
        <div class="palette-footer">
          <div><kbd>↑</kbd><kbd>↓</kbd><span>{browsingHistory ? 'history · → results' : !query.trim() && hasHistory ? 'history / results' : 'navigate'}</span><kbd><CornerDownLeft size={11} /></kbd><span>open</span></div>
          <div class="footer-redirect"><kbd>{mod.label}<CornerDownLeft size={11} /></kbd><span>go to page</span></div>
          {#if hasHistory}<button type="button" class="history-clear" onclick={clearHistory} aria-label="Clear search history">Clear history</button>{/if}
          <Command size={14} strokeWidth={1.5} class="footer-brand" />
        </div>
        <span class="sr-only" role="status" aria-live="polite">{statusText}</span>
      </div>
    </div>
  </div>
{/if}

<style>
  /* Keep the standard filter last: the production minifier coalesces aliases. */
  .palette-layer{--foreground:rgb(var(--color-foreground));--muted:rgb(var(--color-muted));--separator:rgb(var(--color-separator));--elevated:rgb(var(--color-elevated));--accent:rgb(var(--color-accent));position:fixed;inset:0;z-index:200;color:var(--foreground);-webkit-backdrop-filter:blur(9px);backdrop-filter:blur(9px);}
  .palette-backdrop{position:absolute;inset:0;width:100%;height:100%;background:rgb(3 6 14 / .38);animation:veil-in .14s ease-out both;}
  .palette-position{position:absolute;inset:0;display:flex;justify-content:center;align-items:flex-start;padding:clamp(48px,15vh,180px) max(16px,env(safe-area-inset-right)) 24px max(16px,env(safe-area-inset-left));pointer-events:none;}
  .palette-panel{pointer-events:auto;display:flex;flex-direction:column;width:680px;max-width:100%;max-height:min(650px,75dvh);border:1px solid color-mix(in srgb,var(--foreground) 18%,transparent);border-radius:20px;background:var(--elevated);box-shadow:0 30px 100px #0006,0 4px 16px #0003,inset 0 1px 0 color-mix(in srgb,var(--foreground) 7%,transparent);animation:palette-in .15s cubic-bezier(.16,1,.3,1) both;}
  .leaving{pointer-events:none;}
  .leaving .palette-panel{animation:palette-out .09s ease-in both;}
  .leaving .palette-backdrop{animation:veil-out .09s ease-in both;}
  .palette-input-row{display:flex;align-items:center;gap:16px;padding:22px 24px 21px;}
  .palette-input-row :global(.search-icon){flex:none;color:var(--muted);}
  input{width:100%;min-width:0;outline:none;border:0;background:transparent;font-size:25px;line-height:34px;letter-spacing:-.035em;font-weight:400;color:var(--foreground);box-shadow:none;}
  input::placeholder{color:var(--muted);opacity:.8;}
  .escape-key{border:1px solid var(--separator);border-radius:6px;padding:2px 5px;font-size:10px;line-height:16px;color:var(--muted);}
  .search-progress{height:1px;background:var(--separator);position:relative;overflow:hidden;flex:none;}
  .search-progress.loading::after{content:'';position:absolute;inset:0;width:35%;background:var(--accent);animation:search-progress 1.1s ease-in-out infinite;}
  .palette-section-heading{display:flex;justify-content:space-between;align-items:center;padding:17px 23px 9px;font-size:10px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);flex:none;}
  .palette-results{min-height:90px;max-height:440px;overflow:auto;overscroll-behavior:contain;padding:0 10px 8px;scrollbar-width:thin;scrollbar-color:color-mix(in srgb,var(--foreground) 16%,transparent) transparent;}
  .palette-option{position:absolute;left:0;right:0;top:0;height:64px;display:flex;align-items:center;border:1px solid transparent;border-radius:11px;transition:background .075s ease;}
  .palette-option.selected{background:color-mix(in srgb,var(--foreground) 8%,transparent);border-color:color-mix(in srgb,var(--foreground) 5%,transparent);}
  .result-main{display:flex;align-items:center;gap:13px;flex:1;min-width:0;height:100%;padding:7px 12px;text-align:left;}
  .result-copy{display:flex;min-width:0;flex-direction:column;gap:2px;}
  .result-title{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px;line-height:18px;font-weight:500;letter-spacing:-.015em;}
  mark{color:var(--foreground);background:color-mix(in srgb,var(--accent) 20%,transparent);border-radius:3px;box-decoration-break:clone;}
  .result-subtitle{display:flex;align-items:center;gap:7px;font-size:11px;line-height:14px;color:var(--muted);white-space:nowrap;min-width:0;}
  .result-agent{display:inline-flex;align-items:center;gap:4px;min-width:0;max-width:150px;}
  .result-agent-name{overflow:hidden;text-overflow:ellipsis;}
  .result-updated{font-size:10px;line-height:12px;color:var(--muted);}
  .separator{opacity:.45;}
  .number-hint{font-size:10px;color:var(--muted);opacity:.45;letter-spacing:.03em;margin:0 3px 0 8px;white-space:nowrap;}
  .selected .number-hint{opacity:.85;}
  .redirect-button{display:flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:6px;margin:0 8px 0 3px;color:var(--muted);opacity:.45;}
  .selected .redirect-button,.redirect-button:focus-visible{opacity:1;}
  .redirect-button:hover{background:color-mix(in srgb,var(--foreground) 9%,transparent);color:var(--foreground);}
  .palette-footer{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;padding:13px 23px;border-top:1px solid var(--separator);border-radius:0 0 20px 20px;font-size:10px;color:var(--muted);background:color-mix(in srgb,var(--foreground) 2%,transparent);flex:none;}
  .history-clear{padding:4px 2px;line-height:16px;text-decoration:underline;text-underline-offset:2px;}
  .history-clear:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
  .palette-footer>div{display:flex;align-items:center;gap:4px;}
  .palette-footer span{margin:0 10px 0 3px;opacity:.8;}
  .palette-footer kbd{display:flex;align-items:center;justify-content:center;gap:2px;height:17px;min-width:17px;padding:0 3px;border-radius:4px;border:1px solid var(--separator);font-size:10px;}
  .palette-footer :global(.footer-brand){margin-left:auto;opacity:.45;}
  .palette-empty{display:flex;flex-direction:column;align-items:center;gap:12px;padding:35px 10px 45px;color:var(--muted);font-size:12px;}
  .palette-empty strong{font-size:14px;font-weight:500;color:var(--foreground);}
  .palette-notice,.load-more{text-align:center;padding:18px 12px;width:100%;font-size:12px;color:var(--muted);}
  .palette-notice button{color:var(--accent);margin-left:6px;}
  @keyframes palette-in{from{opacity:0;transform:translateY(-5px) scale(.985)}to{opacity:1;transform:none}}
  @keyframes palette-out{to{opacity:0;transform:translateY(-3px) scale(.99)}}
  @keyframes veil-in{from{opacity:0}to{opacity:1}}
  @keyframes veil-out{to{opacity:0}}
  @keyframes search-progress{from{transform:translateX(-110%)}to{transform:translateX(400%)}}
  @media(max-width:600px){.palette-position{padding-top:max(32px,env(safe-area-inset-top));}.palette-panel{border-radius:16px;max-height:calc(100dvh - 70px);}.palette-input-row{padding:18px;gap:12px}input{font-size:20px}.footer-redirect{display:none!important}.palette-footer{padding:12px 16px}.number-hint{display:none}}
  @media(prefers-reduced-motion:reduce){*,*::after{animation:none!important;transition:none!important}}
  @media(prefers-reduced-transparency:reduce),(prefers-contrast:more){.palette-layer{-webkit-backdrop-filter:none;backdrop-filter:none}.palette-backdrop{background:rgb(3 6 14 / .65)}}
</style>
