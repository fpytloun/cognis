<script lang="ts">
  import Plus from 'lucide-svelte/icons/plus';
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';

  import { api, asApiError } from '$lib/api/client';
  import AddDocumentsWizard from '$lib/components/knowledge/AddDocumentsWizard.svelte';
  import DocumentsTable from '$lib/components/knowledge/DocumentsTable.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { statusToneClass, jobStatusTone, formatRelativeOrDate } from '$lib/knowledge/format';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import type { KnowledgebaseCapabilities, KnowledgebaseDocumentModel, KnowledgebaseIndexJobModel, KnowledgebaseModel } from '$lib/types/api';

  let {
    kb,
    documents,
    jobs,
    capabilities,
    canMutate = true,
    onRefresh
  }: {
    kb: KnowledgebaseModel;
    documents: KnowledgebaseDocumentModel[];
    jobs: KnowledgebaseIndexJobModel[];
    capabilities: KnowledgebaseCapabilities;
    canMutate?: boolean;
    onRefresh: () => Promise<void>;
  } = $props();

  let wizardOpen = $state(false);
  let busyIds = $state(new Set<string>());
  let retryingJobIds = $state(new Set<string>());
  let artifactId = $state('');
  let artifactSourcePath = $state('');
  let attachingArtifact = $state(false);

  const existingPaths = $derived(
    new Set(documents.map((doc) => doc.source_path).filter((path): path is string => Boolean(path)))
  );
  const failedJobs = $derived(jobs.filter((job) => job.status === 'failed'));
  const activeJobs = $derived(jobs.filter((job) => job.status === 'queued' || job.status === 'running'));

  function setBusy(id: string, busy: boolean): void {
    const next = new Set(busyIds);
    if (busy) next.add(id);
    else next.delete(id);
    busyIds = next;
  }

  async function reindex(doc: KnowledgebaseDocumentModel): Promise<void> {
    if (!doc.artifact_id) return;
    setBusy(doc.doc_id, true);
    try {
      await api.knowledgebases.reindexArtifact(kb.knowledgebase_id, doc.artifact_id);
      addToast(`Reindexing "${doc.display_name}"`, 'info');
      await onRefresh();
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    } finally {
      setBusy(doc.doc_id, false);
    }
  }

  async function detach(doc: KnowledgebaseDocumentModel): Promise<void> {
    if (!doc.artifact_id) return;
    const confirmed = await confirmAction({
      title: 'Remove document',
      message: `"${doc.display_name}" will be removed from this knowledgebase and its index.`,
      confirmLabel: 'Remove',
      variant: 'danger'
    });
    if (!confirmed) return;
    setBusy(doc.doc_id, true);
    try {
      await api.knowledgebases.detachArtifact(kb.knowledgebase_id, doc.artifact_id);
      addToast(`"${doc.display_name}" removed`, 'success');
      await onRefresh();
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    } finally {
      setBusy(doc.doc_id, false);
    }
  }

  async function retryJob(job: KnowledgebaseIndexJobModel): Promise<void> {
    const next = new Set(retryingJobIds);
    next.add(job.job_id);
    retryingJobIds = next;
    try {
      await api.knowledgebases.retryJob(kb.knowledgebase_id, job.job_id);
      addToast('Job queued for retry', 'success');
      await onRefresh();
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    } finally {
      const cleared = new Set(retryingJobIds);
      cleared.delete(job.job_id);
      retryingJobIds = cleared;
    }
  }

  async function cancelJob(job: KnowledgebaseIndexJobModel): Promise<void> {
    const next = new Set(retryingJobIds);
    next.add(job.job_id);
    retryingJobIds = next;
    try {
      await api.knowledgebases.cancelJob(kb.knowledgebase_id, job.job_id);
      addToast('Job cancelled', 'success');
      await onRefresh();
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    } finally {
      const cleared = new Set(retryingJobIds);
      cleared.delete(job.job_id);
      retryingJobIds = cleared;
    }
  }

  async function attachExistingArtifact(): Promise<void> {
    if (!artifactId.trim()) return;
    attachingArtifact = true;
    try {
      await api.knowledgebases.attachArtifact(kb.knowledgebase_id, {
        artifact_id: artifactId.trim(),
        source_path: artifactSourcePath.trim() || null
      });
      artifactId = '';
      artifactSourcePath = '';
      addToast('Existing artifact attached', 'success');
      await onRefresh();
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    } finally {
      attachingArtifact = false;
    }
  }
</script>

<div class="flex flex-col gap-6">
  <div class="flex flex-wrap items-center justify-between gap-3">
    <h2 class="text-base font-semibold text-white">Documents</h2>
    {#if canMutate}
      <Button onclick={() => (wizardOpen = true)} data-testid="knowledge-add-documents-button">
        <Plus class="mr-1.5 h-4 w-4" /> Add documents
      </Button>
    {/if}
  </div>

  {#if canMutate}
    <details class="rounded-2xl border border-slate-800/80 bg-slate-900/40 px-4 py-3">
      <summary class="cursor-pointer text-sm font-medium text-slate-200">Attach an existing artifact</summary>
      <form
        class="mt-3 grid gap-3 sm:grid-cols-[1fr_1fr_auto]"
        onsubmit={(event) => { event.preventDefault(); void attachExistingArtifact(); }}
      >
        <label class="flex flex-col gap-1 text-xs text-slate-400">
          Artifact ID
          <Input bind:value={artifactId} placeholder="art_…" required />
        </label>
        <label class="flex flex-col gap-1 text-xs text-slate-400">
          Source path (optional)
          <Input bind:value={artifactSourcePath} placeholder="folder/document.md" />
        </label>
        <Button type="submit" class="self-end" disabled={attachingArtifact || !artifactId.trim()}>
          {attachingArtifact ? 'Attaching…' : 'Attach'}
        </Button>
      </form>
    </details>
  {/if}

  {#if documents.length === 0}
    <div class="flex flex-col items-center gap-3 rounded-3xl border border-dashed border-slate-800/80 px-6 py-14 text-center text-sm text-slate-400">
      <p>No documents yet.</p>
      {#if canMutate}
        <Button onclick={() => (wizardOpen = true)}>
          <Plus class="mr-1.5 h-4 w-4" /> Add your first documents
        </Button>
      {/if}
    </div>
  {:else}
    <DocumentsTable {documents} {busyIds} {canMutate} onReindex={reindex} onDetach={detach} />
  {/if}

  {#if failedJobs.length > 0}
    <div class="flex flex-col gap-2">
      <h3 class="text-sm font-semibold text-white">Failed jobs</h3>
      <ul class="flex flex-col gap-2">
        {#each failedJobs as job (job.job_id)}
          <li class="flex items-center justify-between gap-3 rounded-xl border border-slate-800/80 bg-slate-900/60 px-4 py-3 text-sm">
            <div class="min-w-0">
              <span class={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium capitalize ${statusToneClass(jobStatusTone(job.status))}`}>
                {job.job_type.replace('_', ' ')}
              </span>
              <p class="mt-1 truncate text-slate-400">{job.error ?? 'Unknown error'}</p>
              <p class="text-xs text-slate-500">{formatRelativeOrDate(job.completed_at ?? job.started_at)}</p>
            </div>
            {#if canMutate}
              <Button size="sm" variant="secondary" disabled={retryingJobIds.has(job.job_id)} onclick={() => retryJob(job)}>
                <RefreshCw class="mr-1.5 h-3.5 w-3.5" /> Retry
              </Button>
            {/if}
          </li>
        {/each}
      </ul>
    </div>
  {/if}

  {#if activeJobs.length > 0}
    <div class="flex flex-col gap-2">
      <h3 class="text-sm font-semibold text-white">Active jobs</h3>
      <ul class="flex flex-col gap-2" aria-live="polite">
        {#each activeJobs as job (job.job_id)}
          <li class="flex items-center justify-between gap-3 rounded-xl border border-slate-800/80 bg-slate-900/60 px-4 py-3 text-sm">
            <span class="text-slate-300">{job.job_type.replaceAll('_', ' ')} · {job.status}</span>
            {#if canMutate}
              <Button size="sm" variant="ghost" disabled={retryingJobIds.has(job.job_id)} onclick={() => cancelJob(job)}>
                Cancel
              </Button>
            {/if}
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</div>

{#if canMutate}
  <AddDocumentsWizard
    open={wizardOpen}
    knowledgebaseId={kb.knowledgebase_id}
    {existingPaths}
    supportedExtensions={new Set(capabilities.supported_extensions.map((extension) => extension.replace(/^\./, '').toLowerCase()))}
    maxFileSizeBytes={capabilities.limits.max_upload_bytes}
    maxBatchFiles={capabilities.limits.max_batch_files}
    maxBatchUploadBytes={capabilities.limits.max_batch_upload_bytes}
    onClose={() => (wizardOpen = false)}
    onComplete={onRefresh}
  />
{/if}
