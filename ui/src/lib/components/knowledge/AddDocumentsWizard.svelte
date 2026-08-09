<script lang="ts">
  import { onDestroy } from 'svelte';
  import CircleAlert from 'lucide-svelte/icons/circle-alert';
  import CircleCheck from 'lucide-svelte/icons/circle-check';
  import FolderUp from 'lucide-svelte/icons/folder-up';
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';
  import Upload from 'lucide-svelte/icons/upload';

  import Button from '$lib/components/ui/Button.svelte';
  import Sheet from '$lib/components/ui/Sheet.svelte';
  import { formatBytes } from '$lib/knowledge/format';
  import {
    applyUploadOutcomes,
    chunkIngestionPlan,
    chunkRetryableOutcomes,
    includedItems,
    initialOutcomeStates,
    markBatchFailed,
    markBatchCancelled,
    markBatchUploading,
    planIngestion,
    totalPlanSize,
    type IngestionOutcomeState,
    type IngestionPlanItem,
    type IngestionSourceFile
  } from '$lib/knowledge/ingestion';
  import { uploadIngestionBatch } from '$lib/knowledge/upload';
  import { toErrorMessage } from '$lib/utils';
  import type { KnowledgebaseDocumentConflictPolicy } from '$lib/types/api';

  let {
    open,
    knowledgebaseId,
    existingPaths,
    supportedExtensions,
    maxFileSizeBytes,
    maxBatchFiles,
    maxBatchUploadBytes,
    onClose,
    onComplete
  }: {
    open: boolean;
    knowledgebaseId: string;
    existingPaths: ReadonlySet<string>;
    supportedExtensions: ReadonlySet<string>;
    maxFileSizeBytes: number;
    maxBatchFiles: number;
    maxBatchUploadBytes: number;
    onClose: () => void;
    onComplete: () => void;
  } = $props();

  type Step = 'pick' | 'review' | 'upload';

  let step = $state<Step>('pick');
  let sources = $state<IngestionSourceFile[]>([]);
  let conflictPolicy = $state<KnowledgebaseDocumentConflictPolicy>('skip');
  let plan = $derived<IngestionPlanItem[]>(
    planIngestion(sources, existingPaths, conflictPolicy, { supportedExtensions, maxFileSizeBytes })
  );
  let outcomes = $state<IngestionOutcomeState[]>([]);
  let uploading = $state(false);
  let dragActive = $state(false);
  let filesInput = $state<HTMLInputElement | null>(null);
  let folderInput = $state<HTMLInputElement | null>(null);
  let uploadController: AbortController | null = null;

  function resetState(): void {
    step = 'pick';
    sources = [];
    outcomes = [];
    uploading = false;
    uploadController?.abort();
    uploadController = null;
    dragActive = false;
  }

  $effect(() => {
    if (open) resetState();
  });

  function addFiles(files: File[]): void {
    const withPaths = files.map((file) => ({
      file,
      path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
    }));
    sources = [...sources, ...withPaths];
  }

  function handleFilePick(event: Event): void {
    const target = event.currentTarget as HTMLInputElement;
    addFiles(Array.from(target.files ?? []));
    target.value = '';
  }

  function handleDrop(event: DragEvent): void {
    event.preventDefault();
    dragActive = false;
    const files = Array.from(event.dataTransfer?.files ?? []);
    if (files.length > 0) addFiles(files);
  }

  function removeSource(index: number): void {
    sources = sources.filter((_, i) => i !== index);
  }

  function goToReview(): void {
    if (sources.length === 0) return;
    step = 'review';
  }

  async function uploadBatches(
    batches: IngestionPlanItem[][],
    items: IngestionPlanItem[],
    controller: AbortController
  ): Promise<void> {
    for (const batch of batches) {
      if (controller.signal.aborted) break;
      outcomes = markBatchUploading(outcomes, batch);
      try {
        const results = await uploadIngestionBatch(knowledgebaseId, batch, conflictPolicy, controller.signal);
        outcomes = applyUploadOutcomes(outcomes, batch, results);
      } catch (err) {
        if (controller.signal.aborted || (err as { name?: string }).name === 'AbortError') {
          outcomes = markBatchCancelled(outcomes, batch);
          break;
        }
        outcomes = markBatchFailed(outcomes, batch, toErrorMessage(err, 'Upload failed'));
      }
    }

    if (controller.signal.aborted) {
      outcomes = markBatchCancelled(outcomes, items);
    }
  }

  async function startUpload(): Promise<void> {
    step = 'upload';
    uploading = true;
    const controller = new AbortController();
    uploadController = controller;
    outcomes = initialOutcomeStates(plan);
    const items = includedItems(plan);
    const batches = chunkIngestionPlan(items, maxBatchUploadBytes, maxBatchFiles);

    await uploadBatches(batches, items, controller);
    uploading = false;
    uploadController = null;
    onComplete();
  }

  async function retryFailed(): Promise<void> {
    const batches = chunkRetryableOutcomes(outcomes, maxBatchUploadBytes, maxBatchFiles);
    if (batches.length === 0) return;
    const failed = batches.flat();
    uploading = true;
    const controller = new AbortController();
    uploadController = controller;

    await uploadBatches(batches, failed, controller);
    uploading = false;
    uploadController = null;
    onComplete();
  }

  const includedCount = $derived(includedItems(plan).length);
  const skippedCount = $derived(plan.length - includedCount);
  const totalSize = $derived(totalPlanSize(plan));
  const failedCount = $derived(outcomes.filter((o) => o.status === 'failed').length);
  const doneCount = $derived(outcomes.filter((o) => o.status !== 'pending' && o.status !== 'uploading').length);

  onDestroy(() => uploadController?.abort());

  function cancelUpload(): void {
    uploadController?.abort();
  }
</script>

<Sheet {open} {onClose} side="right" label="Add documents" dismissible={!uploading} class="w-[min(30rem,100vw)]">
  {#snippet header()}
    <h2 class="text-lg font-semibold text-white">Add documents</h2>
    <p class="mt-1 text-sm text-slate-400">Upload files or an entire folder. Only unsupported or oversized files are skipped.</p>
  {/snippet}

  {#snippet children()}
    {#if step === 'pick'}
      <div class="flex flex-col gap-4">
        <!-- svelte-ignore a11y_no_static_element_interactions -->
        <div
          class={`flex flex-col items-center gap-3 rounded-2xl border-2 border-dashed px-6 py-10 text-center transition ${dragActive ? 'border-sky-500 bg-sky-500/5' : 'border-slate-700'}`}
          ondragover={(event) => { event.preventDefault(); dragActive = true; }}
          ondragleave={() => (dragActive = false)}
          ondrop={handleDrop}
        >
          <Upload class="h-8 w-8 text-slate-500" />
          <p class="text-sm text-slate-400">Drag and drop files here, or</p>
          <div class="flex flex-wrap justify-center gap-2">
            <Button size="sm" variant="secondary" onclick={() => filesInput?.click()}>Choose files</Button>
            <Button size="sm" variant="secondary" onclick={() => folderInput?.click()}>
              <FolderUp class="mr-1.5 h-3.5 w-3.5" /> Choose folder
            </Button>
          </div>
          <input bind:this={filesInput} type="file" multiple class="hidden" onchange={handleFilePick} data-testid="knowledge-upload-files-input" />
          <input
            bind:this={folderInput}
            type="file"
            multiple
            webkitdirectory
            class="hidden"
            onchange={handleFilePick}
            data-testid="knowledge-upload-folder-input"
          />
        </div>

        {#if sources.length > 0}
          <div class="max-h-64 overflow-y-auto rounded-xl border border-slate-800/80">
            <ul class="divide-y divide-slate-800/80 text-sm">
              {#each sources as source, index (source.path + index)}
                <li class="flex items-center justify-between gap-3 px-3 py-2">
                  <span class="truncate text-slate-200" title={source.path}>{source.path}</span>
                  <span class="flex shrink-0 items-center gap-2 text-xs text-slate-500">
                    {formatBytes(source.file.size)}
                    <button type="button" class="text-slate-500 hover:text-rose-300" onclick={() => removeSource(index)} aria-label={`Remove ${source.path}`}>
                      ✕
                    </button>
                  </span>
                </li>
              {/each}
            </ul>
          </div>
        {/if}

        <div class="flex justify-end gap-3">
          <Button variant="secondary" onclick={onClose}>Cancel</Button>
          <Button disabled={sources.length === 0} onclick={goToReview} data-testid="knowledge-upload-review-button">
            Review ({sources.length})
          </Button>
        </div>
      </div>
    {:else if step === 'review'}
      <div class="flex flex-col gap-4">
        <fieldset class="flex flex-col gap-2">
          <legend class="text-sm font-medium text-slate-300">If a document already exists at the same path</legend>
          <div class="flex gap-2 text-sm">
            {#each [['skip', 'Skip'], ['replace', 'Replace'], ['keep_both', 'Keep both']] as [value, label] (value)}
              <label class={`flex-1 cursor-pointer rounded-xl border px-3 py-2 text-center ${conflictPolicy === value ? 'border-sky-500 bg-sky-500/10 text-sky-200' : 'border-slate-700 text-slate-300'}`}>
                <input type="radio" class="sr-only" name="conflict-policy" value={value} checked={conflictPolicy === value} onchange={() => (conflictPolicy = value as KnowledgebaseDocumentConflictPolicy)} />
                {label}
              </label>
            {/each}
          </div>
        </fieldset>

        <p class="text-sm text-slate-400">
          {includedCount} of {plan.length} file{plan.length === 1 ? '' : 's'} will be uploaded ({formatBytes(totalSize)}).
          {#if skippedCount > 0}{skippedCount} will be skipped.{/if}
        </p>

        <div class="max-h-72 overflow-y-auto rounded-xl border border-slate-800/80">
          <ul class="divide-y divide-slate-800/80 text-sm">
            {#each plan as item (item.id)}
              <li class="flex items-center justify-between gap-3 px-3 py-2">
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-slate-200" title={item.path}>{item.path}</span>
                  {#if item.skipReason}
                    <span class="block text-xs text-amber-300">{item.skipReason}</span>
                  {:else if item.conflict}
                    <span class="block text-xs text-slate-500">
                      {item.action === 'replace'
                        ? 'Will replace existing document'
                        : item.action === 'keep_both'
                          ? 'The server will assign a unique source path'
                          : ''}
                    </span>
                  {/if}
                </span>
                <span class="shrink-0 text-xs text-slate-500">{formatBytes(item.size)}</span>
              </li>
            {/each}
          </ul>
        </div>

        <div class="flex justify-end gap-3">
          <Button variant="secondary" onclick={() => (step = 'pick')}>Back</Button>
          <Button disabled={includedCount === 0} onclick={startUpload} data-testid="knowledge-upload-start-button">
            Upload {includedCount} file{includedCount === 1 ? '' : 's'}
          </Button>
        </div>
      </div>
    {:else}
      <div class="flex flex-col gap-4">
        <p class="text-sm text-slate-400" aria-live="polite">
          {#if uploading}Uploading… {doneCount} of {includedCount} done.{:else}Upload finished. {failedCount} failed.{/if}
        </p>
        <div class="h-2 overflow-hidden rounded-full bg-slate-800">
          <div class="h-full bg-sky-500 transition-all" style={`width: ${includedCount ? (doneCount / includedCount) * 100 : 0}%`}></div>
        </div>

        <div class="max-h-72 overflow-y-auto rounded-xl border border-slate-800/80">
          <ul class="divide-y divide-slate-800/80 text-sm">
            {#each outcomes as outcome (outcome.id)}
              <li class="flex items-center justify-between gap-3 px-3 py-2">
                <span class="min-w-0 flex-1 truncate text-slate-200" title={outcome.resolvedPath}>{outcome.resolvedPath}</span>
                <span class="flex shrink-0 items-center gap-1.5 text-xs">
                  {#if outcome.status === 'failed'}
                    <CircleAlert class="h-3.5 w-3.5 text-rose-400" /> <span class="text-rose-300">{outcome.error ?? 'Failed'}</span>
                   {:else if outcome.status === 'skipped' || outcome.status === 'cancelled'}
                     <span class="text-slate-500">{outcome.status === 'cancelled' ? 'Cancelled' : 'Skipped'}</span>
                  {:else if outcome.status === 'uploading' || outcome.status === 'pending'}
                    <span class="text-slate-500">{outcome.status === 'uploading' ? 'Uploading…' : 'Queued'}</span>
                  {:else}
                    <CircleCheck class="h-3.5 w-3.5 text-emerald-400" /> <span class="text-emerald-300 capitalize">{outcome.status}</span>
                  {/if}
                </span>
              </li>
            {/each}
          </ul>
        </div>

        <div class="flex justify-end gap-3">
          {#if uploading}
            <Button variant="secondary" onclick={cancelUpload} data-testid="knowledge-upload-cancel-button">
              Cancel upload
            </Button>
          {/if}
          {#if failedCount > 0 && !uploading}
            <Button variant="secondary" onclick={retryFailed} data-testid="knowledge-upload-retry-button">
              <RefreshCw class="mr-1.5 h-3.5 w-3.5" /> Retry failed
            </Button>
          {/if}
          <Button disabled={uploading} onclick={onClose}>Done</Button>
        </div>
      </div>
    {/if}
  {/snippet}
</Sheet>
