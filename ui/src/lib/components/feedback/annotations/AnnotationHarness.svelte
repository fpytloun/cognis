<script lang="ts">
  import FeedbackSourceProvider from '../FeedbackSourceProvider.svelte';
  import type { FeedbackSurfaceBinding } from '$lib/feedback/types';
  import ScopedTextSelection from './ScopedTextSelection.svelte';
  import VisualAnnotations from './VisualAnnotations.svelte';
  import RichAnnotations from './RichAnnotations.svelte';
  import ImageLightbox from '../../ImageLightbox.svelte';
  import ObservedCapture from './ObservedCapture.svelte';
  import PdfAttachmentViewer from '../../PdfAttachmentViewer.svelte';
  import { annotateRichPayload } from '$lib/feedback/annotations/rich';
  const rich = (annotateRichPayload({ blocks: [{ type: 'markdown', id: 'selected-block', content: 'Selected rich text' }] }) as { blocks: Record<string, unknown>[] }).blocks[0];
  let { binding, kind = 'scope' }: { binding: FeedbackSurfaceBinding; kind?: 'scope' | 'visual' | 'rich' | 'lightbox' | 'capture' | 'pdf' } = $props();
</script>
<FeedbackSourceProvider {binding} onOpen={() => {}}>
  {#if kind === 'scope'}
    <ScopedTextSelection {binding} scope={{ kind: 'document' }} locator={range => ({ kind: 'text', range })} />
  {:else if kind === 'rich'}
    <RichAnnotations block={rich} index={0} semantic={true}><p data-testid="rich-rendered-text">Selected rich text</p></RichAnnotations>
  {:else if kind === 'lightbox'}
    <ImageLightbox src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" onClose={() => {}} />
  {:else if kind === 'capture'}
    <ObservedCapture {binding} viewport={{ width_css_px: 800, height_css_px: 600, scroll_x_css_px: 20, scroll_y_css_px: 30, device_pixel_ratio: 2 }} />
  {:else if kind === 'pdf'}
    <PdfAttachmentViewer url="/fixture.pdf" />
  {:else}
    <div style="width:400px;height:300px;position:relative">
      <VisualAnnotations {binding} makeLocator={shapes => ({ kind: 'image', intrinsic_width: 800, intrinsic_height: 600, orientation: 1, shapes })} />
    </div>
  {/if}
</FeedbackSourceProvider>
