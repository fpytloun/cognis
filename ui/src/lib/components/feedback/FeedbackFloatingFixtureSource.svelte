<script lang="ts">
  import { getFeedbackSurface } from '$lib/feedback/context';
  import { documentCoordination } from '$lib/document-coordination';
  import TextAnnotations from './annotations/TextAnnotations.svelte';
  import FileDiffViewer from '../FileDiffViewer.svelte';
  import PdfAttachmentViewer from '../PdfAttachmentViewer.svelte';
  import HtmlPreview from '../HtmlPreview.svelte';
  let { kind, text, diff }: { kind: string; text: string; diff: string } = $props();
  const surface = getFeedbackSurface();
  const binding = $derived(surface?.binding ?? null);
  let syntax = $state(false);
</script>
<div class="mb-4 flex min-h-11 gap-3">
  <button type="button" class="min-h-11 border px-3" onpointerdown={event => event.preventDefault()}
    onclick={() => surface?.commenting?.(true)}>Activate selection intent</button>
  {#if kind === 'text'}<button type="button" class="min-h-11 border px-3" onclick={() => syntax = !syntax}>Rerender syntax nodes</button>{/if}
  <span class="text-xs" role="status">{binding ? 'Binding ready' : 'Passive source'}</span>
</div>
{#if kind === 'diff'}
  <FileDiffViewer diffs={[{ path: 'report.txt', diff }]} annotationBindings={[binding]} />
{:else if kind === 'pdf'}
  <div class="h-[calc(100dvh-220px)]"><PdfAttachmentViewer url="/floating-fixture.pdf" /></div>
{:else if kind === 'html'}
  <HtmlPreview source={{ text: `<p style="font:18px/32px sans-serif;margin:32px">${text}</p>`, guard: documentCoordination().capture() }} />
{:else}
  <TextAnnotations {text}>
    <article data-testid="floating-native-source" class="whitespace-pre-wrap p-8 text-lg leading-8">
      {#if syntax}<span>{text.slice(0, 9)}</span><code style="color:rgb(32,120,210)">{text.slice(9, 15)}</code><span>{text.slice(15)}</span>
      {:else}{text}{/if}
    </article>
  </TextAnnotations>
{/if}
