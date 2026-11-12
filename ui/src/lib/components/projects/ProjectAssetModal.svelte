<script lang="ts">
  import { api } from '$lib/api/client';
  import { resolveHtmlPreviewUrl } from '$lib/attachments/html-preview-private';
  import type { SignedHtmlPreview } from '$lib/attachments/html-preview';
  import AttachmentPreviewModal from '$lib/components/AttachmentPreviewModal.svelte';
  import AssistantDeliverableBlock from '$lib/components/AssistantDeliverableBlock.svelte';
  import DashboardModalShell from '$lib/components/dashboard/DashboardModalShell.svelte';
  import { previewKind } from '$lib/attachments/preview';
  import AttachmentSaveButton from '$lib/components/AttachmentSaveButton.svelte';
  import DocumentFavoriteButton from '$lib/components/DocumentFavoriteButton.svelte';
  import type { AttachmentRef, ProjectActivity } from '$lib/types/api';

  let { deliverable = null, artifact = null, onClose } = $props<{
    deliverable?: ProjectActivity['deliverables'][number] | null;
    artifact?: AttachmentRef | null;
    onClose: () => void;
  }>();
  let url = $state<string | null>(null);
  let htmlSource = $state<SignedHtmlPreview | null>(null);
  let error = $state('');
  const kind = $derived(artifact ? previewKind(artifact) : null);
  // Task authors use the step-scoped API. The conversation API deliberately
  // rejects their workflow outputs; project readers use its shared-read path.
  const loadDeliverable = (id: string) => deliverable?.step_run_id && deliverable.is_readonly_for_caller === false
    ? api.deliverables.getForStepRun(deliverable.step_run_id, id)
    : api.deliverables.get(id);

  $effect(() => {
    const id = artifact?.artifact_id;
    const controller = new AbortController();
    let cancelled = false;
    url = null;
    htmlSource = null;
    error = '';
    if (id) {
      (kind === 'html' ? resolveHtmlPreviewUrl(id, controller.signal)
        : api.artifacts.signedUrl(id, 900, 'download')).then((result) => {
        if (!cancelled) {
          url = result.url;
          htmlSource = 'lease' in result ? result as SignedHtmlPreview : null;
        }
      }).catch((caught) => { if (!cancelled) error = String(caught); });
    }
    return () => { cancelled = true; controller.abort(); };
  });

  async function resolveDownloadUrl(): Promise<string | null> {
    if (!artifact) return null;
    return (await api.artifacts.signedUrl(artifact.artifact_id, 900, 'download')).url;
  }
</script>

{#if artifact && kind && url}
  <AttachmentPreviewModal attachment={artifact} {kind} mediaUrl={url} {htmlSource} {onClose} {resolveDownloadUrl} />
{:else}
  <DashboardModalShell title={deliverable?.title ?? artifact?.filename ?? 'Asset'} {onClose} testId="project-asset-modal">
    <div class="min-h-0 flex-1 overflow-auto p-4">
      {#if error}
        <p role="alert" class="text-danger">{error}</p>
      {:else if deliverable}
        <AssistantDeliverableBlock
          item={{ id: deliverable.deliverable_id, kind: 'assistant_deliverable', deliverableId: deliverable.deliverable_id, format: deliverable.format, title: deliverable.title }}
          {loadDeliverable}
          allowSharing={false}
        />
      {:else if artifact && url}
        {#if artifact.mime_type.startsWith('image/')}
          <img src={url} alt={artifact.filename} class="mx-auto max-h-[70vh] max-w-full object-contain" />
        {:else if artifact.mime_type.startsWith('audio/')}
          <audio src={url} controls class="w-full"></audio>
        {:else}
          <p class="text-muted">A preview is not available for this file.</p>
        {/if}
        <div class="flex items-center gap-2"><DocumentFavoriteButton kind="artifact" id={artifact.artifact_id} /><AttachmentSaveButton filename={artifact.filename} resolveUrl={resolveDownloadUrl} /></div>
      {:else}
        <p class="text-muted">Loading asset…</p>
      {/if}
    </div>
  </DashboardModalShell>
{/if}
