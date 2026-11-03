<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import RichDeliverable from '$lib/components/rich/RichDeliverable.svelte';
  import { setViewerTheme } from '$lib/components/rich/viewer-theme';
  import { defaultRichScenarioId, getRichScenario, requireRichScenario, richGalleryScenarios } from '$lib/rich-scenarios/registry';
  import { projectAppearance, resolveTheme, type AppearancePreference } from '$lib/theme/appearance';
  import { DEFAULT_THEME_FAMILY, type ThemeFamily } from '$lib/theme/catalog';

  const widths = ['360', '390', '768', '1024', '1230', '1280', '1440'];
  const themes = ['light', 'dark', 'system'];
  const surfaces = ['embedded', 'standalone'];
  let scenario = $derived(getRichScenario(page.url.searchParams.get('scenario') ?? '') ?? requireRichScenario(defaultRichScenarioId));
  let width = $derived(widths.includes(page.url.searchParams.get('width') ?? '') ? page.url.searchParams.get('width')! : '1280');
  let theme = $derived(themes.includes(page.url.searchParams.get('theme') ?? '') ? page.url.searchParams.get('theme')! : 'system');
  let surface = $derived((surfaces.includes(page.url.searchParams.get('surface') ?? '') ? page.url.searchParams.get('surface') : 'embedded') as 'embedded' | 'standalone');
  let capture = $derived(page.url.searchParams.get('capture') === 'viewer');
  let mounted = $state(false);
  onMount(() => { mounted = true; });

  $effect(() => {
    if (!mounted) return;
    const queryTheme = page.url.searchParams.get('theme');
    if (queryTheme === 'light' || queryTheme === 'dark' || queryTheme === 'system') setViewerTheme(queryTheme);
    const root = document.documentElement;
    const family = (root.dataset.themeFamily ?? DEFAULT_THEME_FAMILY) as ThemeFamily;
    const previous = root.dataset.resolvedTheme === 'dark' ? 'dark' : 'light';
    projectAppearance(family, resolveTheme(theme as AppearancePreference, matchMedia('(prefers-color-scheme: dark)').matches));
    return () => {
      projectAppearance(family, previous);
    };
  });

  function select(key: string, value: string) {
    const url = new URL(page.url);
    url.searchParams.set(key, value);
    void goto(`${url.pathname}${url.search}`, { keepFocus: true, noScroll: true });
  }
</script>

<svelte:head><title>Rich Deliverable Gallery · Cognis</title></svelte:head>

<main class="fixture-page" class:capture data-testid="rich-deliverable-fixture-page">
  {#if !capture}
    <header class="gallery-toolbar" data-testid="rich-gallery-toolbar">
      <strong>Rich gallery</strong>
      <label class="scenario-picker">Scenario
        <select aria-label="Scenario" value={scenario.id} onchange={event => select('scenario', event.currentTarget.value)}>
          {#each richGalleryScenarios as item}<option value={item.id} data-scenario-id={item.id}>{item.title}</option>{/each}
        </select>
      </label>
      <label>Theme<select aria-label="Preview theme" value={theme} onchange={event => select('theme', event.currentTarget.value)}>
        {#each themes as value}<option value={value}>{value}</option>{/each}
      </select></label>
      <label>Width<select aria-label="Preview width" value={width} onchange={event => select('width', event.currentTarget.value)}>
        {#each widths as value}<option value={value}>{value}px</option>{/each}
      </select></label>
      <label>Surface<select aria-label="Preview surface" value={surface} onchange={event => select('surface', event.currentTarget.value)}>
        {#each surfaces as value}<option value={value}>{value}</option>{/each}
      </select></label>
    </header>
  {/if}
  <section class="fixture-shell" data-testid="rich-deliverable-fixture" data-scenario={scenario.id} data-theme={theme} data-width={width} data-surface={surface} style={`--fixture-review-width:${width}px`}>
    {#key scenario.id}
      <RichDeliverable title={scenario.title} content={scenario.content} payload={scenario.payload} instanceId={`fixture-${scenario.id}`}
        internalUrl={page.url.searchParams.has('sharing') ? page.url.pathname + page.url.search : ''}
        shareLinkCallback={page.url.searchParams.has('sharing') ? async () => page.url.href : null}
        standaloneUrl={`/rich-deliverable-fixture?scenario=${encodeURIComponent(scenario.id)}&theme=${theme}&width=${width}&surface=standalone&capture=viewer`} {surface} />
    {/key}
  </section>
</main>

<style>
  .fixture-page { width: 100%; min-width: 0; height: 100%; overflow-y: auto; background: #0e141e; color: #bcc5d3; }
  .fixture-shell { width: min(100%, var(--fixture-review-width, 1280px)); min-width: 0; margin: auto; }
  .capture .fixture-shell { width: 100%; }
  .gallery-toolbar { display: flex; align-items: center; gap: 12px; min-height: 56px; box-sizing: border-box; padding: 6px 16px; border-bottom: 1px solid #64748b44; }
  strong { font-size: 13px; white-space: nowrap; }
  label { display: flex; align-items: center; gap: 6px; min-width: 0; font-size: 11px; }
  .scenario-picker { flex: 1; }
  select { min-width: 0; max-width: 100%; height: 36px; border: 1px solid #64748b66; border-radius: 5px; background: #1a2434; color: inherit; padding: 0 8px; font-size: 12px; }
  .scenario-picker select { width: 100%; }
  select:focus-visible { outline: 2px solid #159fef; outline-offset: 2px; }
  :global(:root[data-resolved-theme="light"]) .fixture-page { background: #f6f8fb; color: #4c5c72; }
  :global(:root[data-resolved-theme="light"]) select { background: white; }
  @media (max-width: 700px) { .gallery-toolbar { flex-wrap: wrap; gap: 6px; padding: 6px 10px; } strong { display: none; } .scenario-picker { flex-basis: 100%; } select { height: 44px; } }
</style>
