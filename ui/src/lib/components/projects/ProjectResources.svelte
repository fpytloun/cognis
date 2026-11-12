<script lang="ts">
  import Card from '$lib/components/ui/Card.svelte';
  import ProjectArtifactThumbnail from './ProjectArtifactThumbnail.svelte';
  import DocumentFavoriteIndicator from '$lib/components/DocumentFavoriteIndicator.svelte';
  import DocumentCommentCount from '$lib/components/DocumentCommentCount.svelte';
  import { stripMarkdown } from '$lib/markdown';
  import type { ProjectActivity } from '$lib/types/api';

  let { activity, onOpen, limit = 30 } = $props<{
    activity: ProjectActivity;
    onOpen: (kind: 'deliverable' | 'artifact', id: string) => void;
    limit?: number;
  }>();
</script>

<div class="grid min-w-0 grid-cols-1 items-start gap-4 md:grid-cols-2" data-testid="project-resources">
  <Card class="min-w-0 p-4">
    <h2 class="text-lg font-semibold text-strong">Deliverables</h2>
    <p class="mt-1 text-xs text-quiet">Reports and outputs from project work</p>
    <div class="mt-4 grid min-w-0 grid-cols-1 gap-3">
      {#each activity.deliverables.slice(0, limit) as item (item.deliverable_id)}
        <button type="button" class="group flex w-full min-w-0 flex-col gap-2 rounded-xl border border-separator bg-canvas/40 p-3 text-left hover:border-accent/40 focus-visible:outline focus-visible:outline-accent" onclick={() => onOpen('deliverable', item.deliverable_id)}>
          <span class="flex w-full min-w-0 items-start gap-2">
            <span class="line-clamp-2 min-w-0 flex-1 break-words text-sm font-medium text-foreground [overflow-wrap:anywhere]">{item.title || 'Untitled deliverable'}</span>
            <DocumentFavoriteIndicator kind="deliverable" id={item.deliverable_id} />
          </span>
          {#if item.content_preview}
            <span class="line-clamp-3 w-full break-words text-sm leading-relaxed text-muted [overflow-wrap:anywhere]">{stripMarkdown(item.content_preview)}</span>
          {:else}
            <span class="text-xs text-quiet">Open to read this deliverable</span>
          {/if}
          <span class="flex flex-wrap items-center gap-2 text-xs uppercase tracking-wide text-accent/80">{item.format}{(item.revision ?? 1) > 1 ? ` · rev ${item.revision}` : ''}<DocumentCommentCount id={item.deliverable_id} /></span>
        </button>
      {:else}
        <p class="py-3 text-sm text-muted">No deliverables yet.</p>
      {/each}
    </div>
  </Card>
  <Card class="min-w-0 p-4">
    <h2 class="text-lg font-semibold text-strong">Artifacts</h2>
    <p class="mt-1 text-xs text-quiet">Images, documents and other files</p>
    <div class="mt-4 grid min-w-0 grid-cols-1 gap-3">
      {#each activity.artifacts.slice(0, limit) as item (item.artifact_id)}
        <button type="button" class="flex w-full min-w-0 items-center gap-3 rounded-xl border border-separator bg-canvas/40 p-3 text-left hover:border-accent/40 focus-visible:outline focus-visible:outline-accent" onclick={() => onOpen('artifact', item.artifact_id)}>
          <ProjectArtifactThumbnail artifact={item} />
          <span class="flex min-w-0 flex-1 flex-col gap-2">
            <span class="flex min-w-0 items-start gap-2">
              <span class="line-clamp-2 min-w-0 flex-1 break-words text-sm font-medium text-foreground [overflow-wrap:anywhere]">{item.filename}</span>
              <DocumentFavoriteIndicator kind="artifact" id={item.artifact_id} />
            </span>
            <span class="truncate text-xs text-quiet">{item.mime_type}{(item.revision ?? 1) > 1 ? ` · rev ${item.revision}` : ''}</span>
            <span class="flex flex-wrap items-center gap-2 text-xs text-quiet">{Math.max(1, Math.round(item.size_bytes / 1024))} KB <DocumentCommentCount id={item.artifact_id} /></span>
          </span>
        </button>
      {:else}
        <p class="py-3 text-sm text-muted">No artifacts yet.</p>
      {/each}
    </div>
  </Card>
</div>
