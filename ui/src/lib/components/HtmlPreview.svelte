<script lang="ts">
  import DocumentFind from './DocumentFind.svelte';
  import type { DocumentFindAdapter } from '$lib/document-find';
  import { untrack } from 'svelte';
  import { htmlPreviewGuard, mountHtmlPreview, type HtmlPreviewSource } from '$lib/attachments/html-preview';
  import { getFeedbackSurface } from '$lib/feedback/context';
  import { textDigest } from '$lib/feedback/annotations/digest';
  import type { HtmlObservation } from '$lib/feedback/annotations/html-bridge';
  import ObservedCapture from './feedback/annotations/ObservedCapture.svelte';
  import { surfaceIdentity } from '$lib/feedback/annotations/surface-identity';
  import type { FeedbackLocator } from '$lib/feedback/types';
  import type { FeedbackTargetView, FeedbackTargetGeometry, FeedbackVisibleArea } from '$lib/feedback/types';
  import { exposeGeometry } from '$lib/feedback/annotations/expose-target';
  import { targetView } from '$lib/feedback/annotations/native-selection';
  import type { FeedbackViewport } from '$lib/feedback/types';
  const surface = getFeedbackSurface();
  const binding = $derived(surface?.binding);
  let observation = $state<HtmlObservation | null>(null);
  let observedViewport = $state<FeedbackViewport | null>(null);
  let host = $state<HTMLDivElement>();
  let capture = $state<{ targetGeometry: (anchor: FeedbackLocator) => FeedbackTargetGeometry | null }>();
  let selectionError = $state('');
  let locateHtml = $state<((anchor: Extract<FeedbackLocator, { kind: 'html' }>, visible?: FeedbackVisibleArea) => Promise<boolean>) | null>(null);
  let syncMode = $state<(() => void) | undefined>();
  let renderEpoch = $state(0);
  let live = $state(false);
  let generation = 0;
  const observedTargets = new Map<string, HtmlObservation>();
  const authority = $derived(JSON.stringify([surfaceIdentity(binding), binding?.source.subject_id,
    binding?.source.content_digest, binding?.source.availability, binding?.completeness,
    binding?.source.capabilities, binding?.source.permitted_locator_kinds, binding?.supported_locator_kinds, binding?.mode, binding?.tool]));
  const lifetime = $derived(JSON.stringify([authority, renderEpoch, live]));
  const permitted = $derived(!!binding?.source.capabilities.read && binding.source.capabilities.comment
    && binding.source.availability !== 'unavailable' && binding.completeness !== 'unavailable'
    && binding.supported_locator_kinds.includes('html') && binding.source.permitted_locator_kinds.includes('html'));
  function guardCurrent() {
    const guard = htmlPreviewGuard(source);
    if (guard.signal.aborted) return false;
    try { guard.assertCurrent(); return true; } catch { return false; }
  }
  function interaction(value: boolean) {
    // Revocation still drops local state, but cannot patch an expired owner.
    if (guardCurrent()) binding?.onInteractionChange(value);
  }
  const captureBinding = $derived(binding ? { ...binding, onInteractionChange: interaction } : null);
  function clearObservation() {
    generation++;
    const interacting = observation !== null;
    observation = null; selectionError = ''; observedTargets.clear();
    if (guardCurrent()) binding?.onTargetChange?.(null);
    if (interacting) interaction(false);
  }
  // Snapshot bindings are replaceable views, not observation lifetimes.
  $effect(() => { lifetime; untrack(clearObservation); });
  $effect(() => { lifetime; const sync = syncMode; untrack(() => sync?.()); });
  const instance = crypto.randomUUID();
  $effect(() => {
    if (!binding) return;
    const current = binding;
    return current.registerAdapter(instance, {
      clearSelection: () => { clearObservation(); interaction(false); },
       targetGeometry: anchor => anchor.kind === 'html' ? capture?.targetGeometry(anchor) ?? observedGeometry(observedTargets.get(JSON.stringify(anchor))
        ?? [...observedTargets.values()].find(value => value.selector === anchor.selector_path && (anchor.range ? value.quote === anchor.range.exact : value.selection_kind === 'element'))) : null,
       exposeTarget: async (anchor, visible) => {
         if (anchor.kind !== 'html') return;
         const geometry = () => capture?.targetGeometry(anchor) ?? observedGeometry(observedTargets.get(JSON.stringify(anchor))
           ?? [...observedTargets.values()].find(value => value.selector === anchor.selector_path && (anchor.range ? value.quote === anchor.range.exact : value.selection_kind === 'element')));
         exposeGeometry(host, geometry, visible);
         const rect = geometry()?.rects[0], frame = host?.querySelector('iframe');
         if (!rect || !frame || !anchor.selector_path || rect.top >= visible.top && rect.top + rect.height <= visible.top + visible.height) return;
         const box = frame.getBoundingClientRect(), view = observedViewport;
         if (!view || !box.width || !box.height) return;
         const top = Math.max(0, (visible.top - box.top) / box.height * view.height_css_px);
         const bottom = Math.min(view.height_css_px, (visible.top + visible.height - box.top) / box.height * view.height_css_px);
         await locateHtml?.(anchor, { left: 0, top, width: view.width_css_px, height: Math.max(0, bottom - top) });
       },
      locate: async anchor => {
        const token = lifetime;
        if (anchor.kind !== 'html') return { status: 'not_located' };
        if (anchor.capture_id) return { status: 'unavailable', reason: 'Open the saved observed capture; live HTML cannot reproduce its pixels.' };
        const located = await locateHtml?.(anchor);
        return { status: token === lifetime && located ? 'exact' : 'not_located' };
      },
    });
  });
  function observedGeometry(value: HtmlObservation | undefined): FeedbackTargetGeometry | null {
    const frame = host?.querySelector('iframe');
    if (!value || !frame) return null;
    const box = frame.getBoundingClientRect(), viewport = observedViewport ?? value.viewport;
    const scaleX = box.width / viewport.width_css_px, scaleY = box.height / viewport.height_css_px;
    return { space: 'client', rects: value.rects.map(rect => ({
      left: box.left + (rect.left + value.viewport.scroll_x_css_px - viewport.scroll_x_css_px) * scaleX,
      top: box.top + (rect.top + value.viewport.scroll_y_css_px - viewport.scroll_y_css_px) * scaleY,
      width: rect.width * scaleX, height: rect.height * scaleY,
    })) };
  }
  async function comment() {
    const selected = observation, token = lifetime, operation = generation, guard = htmlPreviewGuard(source);
    if (!binding || !selected || !live || !permitted || binding.mode !== 'comment') return;
    const current = () => token === lifetime && operation === generation && observation === selected
      && live && permitted && binding?.mode === 'comment' && !guard.signal.aborted;
    try {
      guard.signal.throwIfAborted(); guard.assertCurrent();
      const digest = await textDigest(JSON.stringify(selected));
      if (!current()) return;
      const quoteDigest = selected.quote ? await textDigest(selected.quote) : null;
      if (!current()) return;
      guard.assertCurrent();
      function target(value: HtmlObservation): FeedbackTargetView {
        const locator: FeedbackLocator = { kind: 'html', element_key: value.element_key, selector_path: value.selector,
          range: quoteDigest && value.selection_kind === 'text' ? { representation: 'feedback-text-v1', text_digest: quoteDigest, start: value.start, end: value.end, exact: value.quote, prefix: '', suffix: '' } : null,
          viewport: value.viewport, render_state_digest: digest, shapes: [], capture_id: null };
        observedTargets.set(JSON.stringify(locator), value);
        if (observedTargets.size > 64) observedTargets.delete(observedTargets.keys().next().value!);
        const result = targetView(binding!, instance, { locator }, value.quote || 'Observed element', () => observedGeometry(value));
        result.validate = async () => { guard.signal.throwIfAborted(); await guard.validate(); guard.assertCurrent(); };
        result.scopes = value.scopes.map(scope => ({ label: scope.label, target: () => target({ ...value, selector: scope.selector, element_key: null, rects: scope.rects }) }));
        return result;
      }
      binding?.onTargetChange?.(target(selected));
    } catch (cause) {
      if (current()) selectionError = cause instanceof Error ? cause.message : String(cause);
    }
  }

  let { source }: { source: HtmlPreviewSource } = $props();
  let error = $state<string | null>(null);
  let findAdapter = $state<DocumentFindAdapter | null>(null);
  let findControls = $state<DocumentFind>();

  function preview(node: HTMLElement, value: HtmlPreviewSource) {
    let dispose = () => {};
    let mount = 0;
    function update(next: HtmlPreviewSource) {
      // Actions track their argument; observation state must not become an
      // additional dependency of the action's update render effect.
      untrack(() => {
        dispose();
        const epoch = ++mount;
        renderEpoch++; live = false; clearObservation();
        node.replaceChildren();
        observedViewport = null;
        dispose = mountHtmlPreview(node, next, htmlPreviewGuard(next), (message) => { if (epoch === mount) error = message; }, {
          enabled: () => live && permitted && binding?.mode === 'comment' && guardCurrent(),
          tool: () => binding?.tool ?? 'text',
          observe: value => { if (epoch === mount && live && permitted && binding?.mode === 'comment' && guardCurrent()) {
            observation = value; generation++;
            if (value.selection_kind === 'none') { binding?.onTargetChange?.(null); return; }
            interaction(true); void comment();
          } },
          ready: (value, sync) => { if (epoch === mount) {
            locateHtml = value; syncMode = sync; live = value !== null;
            if (!value) { clearObservation(); observedViewport = null; renderEpoch++; }
          } },
          viewport: value => { if (epoch === mount) observedViewport = value; },
        }, {
          ready: value => { if (epoch === mount) findAdapter = value; },
          open: () => findControls?.requestOpen(),
        });
      });
    }
    update(value);
    return { update, destroy: () => { dispose(); mount++; live = false; clearObservation(); } };
  }
</script>

{#if error}<p role="alert" class="p-5 text-sm text-red-300">{error}</p>{/if}
{#if error === null}<p role="status" class="p-4 text-sm text-slate-400">Loading HTML preview…</p>{/if}
{#if selectionError}<p role="alert">{selectionError}</p>{/if}
<DocumentFind bind:this={findControls} adapter={findAdapter} identity={source} fill>
  <div bind:this={host} use:preview={source} class="h-full min-h-96 min-w-0"></div>
</DocumentFind>
{#if captureBinding && live && permitted && (captureBinding.mode === 'area' || captureBinding.mode === 'draw')}
  {#key lifetime}
   <ObservedCapture bind:this={capture} adapterId={instance} binding={captureBinding} viewport={observedViewport ?? observation?.viewport ?? { width_css_px: host?.clientWidth || 1024, height_css_px: host?.clientHeight || 768, scroll_x_css_px: 0, scroll_y_css_px: 0, device_pixel_ratio: window.devicePixelRatio || 1 }} />
  {/key}
{/if}
