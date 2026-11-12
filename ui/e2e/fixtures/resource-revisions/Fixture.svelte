<script lang="ts">
  import { onMount } from 'svelte';
  import { projectAppearance } from '../../../src/lib/theme/appearance';
  import { rememberStartupAppearance } from '../../../src/lib/theme/startup-appearance';
  import '../../../src/app.css';
  import ResourceReader from '../../../src/lib/components/library/ResourceReader.svelte';
  import DocumentReader from '../../../src/lib/components/knowledge/DocumentReader.svelte';
  import AssistantDeliverableBlock from '../../../src/lib/components/AssistantDeliverableBlock.svelte';
  import TimelineItemRenderer from '../../../src/lib/components/timeline/TimelineItemRenderer.svelte';
  import DeliverableReferenceActions from '../../../src/lib/components/DeliverableReferenceActions.svelte';
  import type { Agent } from '../../../src/lib/types/api';
  import AttachmentPreviewModal from '../../../src/lib/components/AttachmentPreviewModal.svelte';
  import ImageLightbox from '../../../src/lib/components/ImageLightbox.svelte';
  import type { AssistantDeliverableTimelineItem } from '../../../src/lib/chat-v2/types';
  import { artifactId, deliverableId, imageId, libraryItem } from './data';
  const params = new URLSearchParams(location.search);
  const surface = params.get('surface') ?? 'library-deliverable';
  const theme = params.get('theme') ?? 'dark';
  document.documentElement.dataset.themeFamily = params.get('family') ?? 'paper';
  document.documentElement.dataset.resolvedTheme = theme;
  document.documentElement.classList.toggle('dark', theme === 'dark');
  onMount(() => {
    const family = params.get('family') === 'classic' ? 'classic' : 'paper';
    const mode = theme === 'light' ? 'light' : 'dark';
    projectAppearance(family, mode);
    rememberStartupAppearance(family, mode);
  });
  let closed = $state(false);
  const item = {
    id: 'revision-card', kind: 'assistant_deliverable', sort_key: '1', source_refs: [],
    stable: true, status: 'complete', deliverable_id: deliverableId, format: 'markdown',
    title: 'Release readiness report',
  } as AssistantDeliverableTimelineItem;
</script>

<main class="min-h-screen bg-canvas p-3 text-foreground">
  {#if closed}
    <p>Viewer closed</p>
  {:else if surface === 'knowledge'}
    <div class="h-screen">
      <DocumentReader
        doc={{ doc_id: 'readiness', knowledgebase_id: 'readiness', artifact_id: null,
          display_name: 'Readiness.md', source_path: null, mime_type: 'text/markdown',
          size_bytes: null, status: 'indexed', chunk_count: 0, metadata: {}, last_job_id: null,
          last_error: null, attached_at: null, indexed_at: null }}
        loading={false} error={null} downloadUrl={null} onDownload={null} knowledgebaseId="readiness"
        content={{ extractedText: false, text: '# Release overview\n\nReadiness evidence.\n\n## Verification\n\nBrowser evidence.\n\n### Browser checks with a longer label that wraps naturally\n\nChecked navigation.\n\n# Rollout\n\nRelease plan.' }} />
    </div>
  {:else if surface.startsWith('library-')}
    <ResourceReader item={libraryItem(surface === 'library-deliverable' ? 'deliverable' : 'artifact')} />
  {:else if surface === 'chat-deliverable'}
    <AssistantDeliverableBlock {item} fullscreenOnly onClose={() => { closed = true; }} />
  {:else if surface === 'tool-deliverable'}
    <DeliverableReferenceActions {item} />
  {:else if surface === 'chat-inline'}
    <TimelineItemRenderer
      item={{ id: item.id, kind: 'assistant_deliverable', deliverableId, format: 'markdown', createdAt: params.get('createdAt') ?? '2026-11-03T10:00:00Z' }}
      agent={{ name: 'laforge', display_name: 'LaForge', avatar_url: null } as Agent} />
  {:else if surface === 'chat-artifact'}
    <AttachmentPreviewModal attachment={{ artifact_id: artifactId, kind: 'file', filename: 'Readiness report.txt', mime_type: 'text/plain', size_bytes: 120 }}
      kind="text" onClose={() => { closed = true; }} resolveDownloadUrl={async () => '/report.txt'} />
  {:else if surface === 'chat-image'}
    <ImageLightbox src="/image.svg" images={[{ src: '/image.svg', filename: 'Readiness diagram.svg', artifact_id: imageId }]}
      onClose={() => { closed = true; }} />
  {/if}
</main>
