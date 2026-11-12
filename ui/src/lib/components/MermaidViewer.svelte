<script lang="ts">
  import { onMount, tick } from 'svelte';
  import Minimize2 from 'lucide-svelte/icons/minimize-2';
  import Scan from 'lucide-svelte/icons/scan';
  import Plus from 'lucide-svelte/icons/plus';
  import Minus from 'lucide-svelte/icons/minus';
  import Button from '$lib/components/ui/Button.svelte';
  import BlockingDialog from '$lib/components/ui/BlockingDialog.svelte';
  import { CODE_COPY_FEEDBACK_MS, CODE_COPY_ICON, CODE_COPIED_ICON, CODE_EXPAND_ICON } from '$lib/chat-code-controls';
  import { renderMermaidPresentation } from './rich/mermaid-presentation';

  let { source }: { source: string } = $props();
  const id = $props.id();
  let root: HTMLDivElement;
  let stage = $state<HTMLDivElement>();
  let svg = $state('');
  let failed = $state(false);
  let rendering = $state(true);
  let fullOpen = $state(false);
  let copied = $state(false);
  let copyError = $state('');
  let scale = $state(1);
  let x = $state(0);
  let y = $state(0);
  let width = $state(1);
  let height = $state(1);
  let dragging = $state(false);
  let fitScale = 1;
  let fitted = true;
  let copyTimer: ReturnType<typeof setTimeout>;
  type Point = { x: number; y: number };
  const pointers = new Map<number, Point>();
  const midpoint = (a: Point, b: Point): Point => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
  const distance = (a: Point, b: Point) => Math.hypot(a.x - b.x, a.y - b.y);

  function fit() {
    if (!stage || !svg) return;
    fitScale = Math.min((stage.clientWidth - 32) / width, (stage.clientHeight - 32) / height);
    if (fitScale <= 0) return;
    scale = fitScale;
    x = (stage.clientWidth - width * scale) / 2;
    y = (stage.clientHeight - height * scale) / 2;
    fitted = true;
  }

  function zoom(factor: number, point?: Point) {
    if (!stage) return;
    const rect = stage.getBoundingClientRect();
    const px = point ? point.x - rect.left : stage.clientWidth / 2;
    const py = point ? point.y - rect.top : stage.clientHeight / 2;
    const next = Math.min(fitScale * 32, Math.max(fitScale / 4, scale * factor));
    x = px - (px - x) * next / scale;
    y = py - (py - y) * next / scale;
    scale = next;
    fitted = false;
  }

  function down(event: PointerEvent) {
    if (event.button !== 0) return;
    stage?.focus({ preventScroll: true });
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    dragging = true;
  }
  function move(event: PointerEvent) {
    const previous = pointers.get(event.pointerId);
    if (!previous) return;
    const before = [...pointers.values()];
    const point = { x: event.clientX, y: event.clientY };
    pointers.set(event.pointerId, point);
    const after = [...pointers.values()];
    if (before.length === 2) {
      const a = midpoint(before[0], before[1]), b = midpoint(after[0], after[1]);
      zoom(distance(after[0], after[1]) / Math.max(1, distance(before[0], before[1])), a);
      x += b.x - a.x;
      y += b.y - a.y;
    } else if (before.length === 1) {
      x += point.x - previous.x;
      y += point.y - previous.y;
    }
    fitted = false;
  }
  function up(event: PointerEvent) {
    pointers.delete(event.pointerId);
    dragging = pointers.size > 0;
    if (stage?.hasPointerCapture(event.pointerId)) stage.releasePointerCapture(event.pointerId);
  }
  function keyboard(event: KeyboardEvent) {
    if (event.key === '+' || event.key === '=') zoom(1.25);
    else if (event.key === '-') zoom(0.8);
    else if (event.key === '0' || event.key === 'Home') fit();
    else if (event.key.startsWith('Arrow')) {
      x += event.key === 'ArrowLeft' ? 40 : event.key === 'ArrowRight' ? -40 : 0;
      y += event.key === 'ArrowUp' ? 40 : event.key === 'ArrowDown' ? -40 : 0;
      fitted = false;
    } else return;
    event.preventDefault();
    event.stopPropagation();
  }
  function gestures(node: HTMLDivElement) {
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? node.clientHeight : 1);
      zoom(Math.exp(-Math.max(-300, Math.min(300, delta)) * 0.002), { x: event.clientX, y: event.clientY });
    };
    node.addEventListener('wheel', wheel, { passive: false });
    const observer = new ResizeObserver(() => { if (fitted) fit(); });
    observer.observe(node);
    void tick().then(fit);
    return { destroy() {
      observer.disconnect();
      node.removeEventListener('wheel', wheel);
      pointers.clear();
      dragging = false;
    } };
  }
  async function fullscreen() {
    fullOpen = !fullOpen;
    fitted = true;
    await tick();
    fit();
    if (!fullOpen) root.querySelector<HTMLButtonElement>('[aria-label="Open diagram fullscreen"]')?.focus({ preventScroll: true });
  }
  async function copy() {
    try {
      await navigator.clipboard.writeText(source);
      copied = true;
      copyError = '';
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => copied = false, CODE_COPY_FEEDBACK_MS);
    } catch { copyError = 'Could not copy diagram. Check clipboard permissions.'; }
  }

  onMount(() => {
    let generation = 0, disposed = false;
    async function render() {
      const current = ++generation;
      rendering = true;
      const fallback = document.createElement('pre');
      fallback.textContent = source;
      failed = false;
      try {
        await renderMermaidPresentation([fallback], root, `chat-diagram-${id.replaceAll(':', '')}`,
          () => !disposed && current === generation,
          (_node, presentation) => {
            const element = presentation.querySelector('svg');
            if (!element) return;
            const box = element.getAttribute('viewBox')?.split(/[\s,]+/).map(Number);
            width = box?.[2] || 1;
            height = box?.[3] || 1;
            svg = presentation.innerHTML;
          });
        if (disposed || current !== generation) return;
        failed = !svg;
        await tick();
        if (fitted) fit();
      } catch {
        if (!disposed && current === generation) failed = true;
      } finally {
        if (!disposed && current === generation) rendering = false;
      }
    }
    void render();
    const themes = new MutationObserver(() => { void render(); });
    themes.observe(document.documentElement, { attributes: true, attributeFilter: ['data-resolved-theme', 'data-theme-family'] });
    return () => { disposed = true; generation++; themes.disconnect(); clearTimeout(copyTimer); };
  });
</script>

{#snippet viewer()}
  <div class="mermaid-viewer primitive-card rounded-3xl border not-prose" class:fullscreen={fullOpen} data-mermaid-viewer aria-busy={rendering}>
    <div class="chat-code-actions" role="group" aria-label="Diagram controls" data-copy-exclude>
      <Button variant="ghost" size={null} class="copy-icon-button" aria-label={fullOpen ? 'Exit diagram fullscreen' : 'Open diagram fullscreen'} title={fullOpen ? 'Exit fullscreen' : 'Fullscreen'} onclick={fullscreen}>
        {#if fullOpen}<Minimize2 />{:else}{@html CODE_EXPAND_ICON}{/if}
      </Button>
      <Button variant="ghost" size={null} class="copy-icon-button" aria-label="Fit diagram to box" title="Fit to box (0)" onclick={fit} disabled={!svg}><Scan /></Button>
      <Button variant="ghost" size={null} class="copy-icon-button" aria-label="Zoom diagram in" title="Zoom in (+)" onclick={() => zoom(1.25)} disabled={!svg}><Plus /></Button>
      <Button variant="ghost" size={null} class="copy-icon-button" aria-label="Zoom diagram out" title="Zoom out (-)" onclick={() => zoom(0.8)} disabled={!svg}><Minus /></Button>
      <Button variant="ghost" size={null} class="copy-icon-button" aria-label={copied ? 'Diagram copied' : 'Copy diagram source'} title="Copy Mermaid source" onclick={copy}>
        {@html copied ? CODE_COPIED_ICON : CODE_COPY_ICON}
      </Button>
    </div>
    {#if svg}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex, a11y_no_noninteractive_element_interactions (A custom diagram canvas with keyboard pan and zoom.) -->
      <div bind:this={stage} use:gestures class="diagram-stage" class:dragging style:aspect-ratio={`${width} / ${height}`} tabindex="0" role="application" aria-roledescription="interactive diagram" aria-label="Mermaid diagram. Drag to pan, scroll or pinch to zoom. Use arrow keys to pan, plus or minus to zoom, zero to fit."
        onpointerdown={down} onpointermove={move} onpointerup={up} onpointercancel={up} onlostpointercapture={up} onkeydown={keyboard}>
        <div class="diagram-transform" style:width={`${width}px`} style:height={`${height}px`} style:transform={`translate(${x}px, ${y}px) scale(${scale})`}>
          {@html svg}
        </div>
      </div>
      <span class="diagram-hint" aria-hidden="true">Drag to pan · Scroll or pinch to zoom</span>
    {:else if failed}
      <p role="status" class="px-4 text-sm text-muted">Could not render diagram. Mermaid source:</p>
      <pre class="diagram-source"><code>{source}</code></pre>
    {:else}
      <p role="status" class="p-4 text-sm text-muted">Rendering diagram…</p>
    {/if}
    {#if copyError}<p role="alert" class="px-4 text-sm text-danger">{copyError}</p>{/if}
  </div>
{/snippet}

<div bind:this={root} class="diagram-theme">
  {#if !fullOpen}{@render viewer()}{:else}<div class="diagram-placeholder">Diagram open fullscreen</div>{/if}
  {#if fullOpen}
    <BlockingDialog label="Mermaid diagram fullscreen" onClose={fullscreen} panelClass="!max-w-none h-full" bodyClass="!p-0 !overflow-hidden">
      {@render viewer()}
    </BlockingDialog>
  {/if}
</div>

<style>
  .diagram-theme, .mermaid-viewer {
    --rich-surface: rgb(var(--color-surface));
    --rich-surface-raised: rgb(var(--color-raised));
    --rich-text: rgb(var(--color-foreground));
    --rich-text-secondary: rgb(var(--color-secondary));
    --rich-accent: rgb(var(--color-accent));
    --rich-accent-soft: rgb(var(--color-accent));
    --rich-line: rgb(var(--color-separator));
  }
  .mermaid-viewer {
    position: relative;
    margin-block: 1rem;
    padding-top: 3.5rem;
    overflow: hidden;
    background: rgb(var(--color-surface));
    color: rgb(var(--color-foreground));
  }
  .mermaid-viewer.fullscreen { height: 100%; margin: 0; border: 0; }
  .diagram-stage { position: relative; width: 100%; min-width: 0; min-height: 14rem; max-height: 32rem; overflow: hidden; touch-action: none; cursor: grab; user-select: none; }
  .fullscreen .diagram-stage { height: calc(100% - 2rem); max-height: none; aspect-ratio: auto !important; }
  .diagram-stage:focus-visible { outline: none; box-shadow: none; }
  .mermaid-viewer:has(.diagram-stage:focus-visible) { box-shadow: inset 0 0 0 2px rgb(var(--color-accent) / 0.6); }
  .diagram-stage.dragging { cursor: grabbing; }
  .diagram-transform { position: absolute; left: 0; top: 0; transform-origin: 0 0; pointer-events: none; }
  .diagram-transform :global(svg) { display: block; width: 100% !important; height: 100% !important; max-width: none !important; }
  .diagram-hint { display: block; height: 2rem; padding-inline: 1rem; color: rgb(var(--color-muted)); font-size: 0.75rem; line-height: 2rem; }
  .diagram-source { max-height: 24rem; overflow: auto; padding: 1rem; white-space: pre; }
  .diagram-placeholder { height: 24rem; display: grid; place-items: center; color: rgb(var(--color-muted)); }
</style>
