<script lang="ts">
  import RefreshCw from 'lucide-svelte/icons/refresh-cw';

  import { api, asApiError } from '$lib/api/client';
  import Button from '$lib/components/ui/Button.svelte';
  import Input from '$lib/components/ui/Input.svelte';
  import { statusToneClass } from '$lib/knowledge/format';
  import { confirmAction } from '$lib/stores/confirm';
  import { addToast } from '$lib/stores/toasts';
  import type { KnowledgebaseDiagnostics, KnowledgebaseModel } from '$lib/types/api';

  let {
    kb,
    diagnostics,
    canEdit = true,
    canManageLifecycle = true,
    onUpdated,
    onDeleted
  }: {
    kb: KnowledgebaseModel;
    diagnostics: KnowledgebaseDiagnostics | null;
    canEdit?: boolean;
    canManageLifecycle?: boolean;
    onUpdated: (kb: KnowledgebaseModel) => void;
    onDeleted: () => void;
  } = $props();

  let name = $state('');
  let description = $state('');
  let metadataSchemaText = $state('{}');
  let chunkSettingsText = $state('{}');
  let savingProfile = $state(false);
  let savingSchema = $state(false);
  let savingChunking = $state(false);
  let schemaError = $state('');
  let chunkingError = $state('');
  let reindexing = $state(false);

  $effect(() => {
    name = kb.name;
    description = kb.description ?? '';
    metadataSchemaText = JSON.stringify(kb.metadata_schema ?? {}, null, 2);
    chunkSettingsText = JSON.stringify(kb.settings ?? {}, null, 2);
  });

  const staleCount = $derived(diagnostics?.artifact_counts?.stale ?? 0);

  async function saveProfile(): Promise<void> {
    savingProfile = true;
    try {
      const updated = await api.knowledgebases.update(kb.knowledgebase_id, {
        name: name.trim(),
        description: description.trim() || null
      });
      onUpdated(updated);
      addToast('Saved', 'success');
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    } finally {
      savingProfile = false;
    }
  }

  async function saveSchema(): Promise<void> {
    schemaError = '';
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(metadataSchemaText);
    } catch {
      schemaError = 'Invalid JSON';
      return;
    }
    savingSchema = true;
    try {
      const updated = await api.knowledgebases.update(kb.knowledgebase_id, { metadata_schema: parsed });
      onUpdated(updated);
      addToast('Metadata schema saved. Search filters will pick it up automatically.', 'success');
    } catch (err) {
      schemaError = asApiError(err).message;
    } finally {
      savingSchema = false;
    }
  }

  async function saveChunking(): Promise<void> {
    chunkingError = '';
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(chunkSettingsText);
    } catch {
      chunkingError = 'Invalid JSON';
      return;
    }
    savingChunking = true;
    try {
      const updated = await api.knowledgebases.update(kb.knowledgebase_id, { settings: parsed });
      onUpdated(updated);
      addToast('Chunk settings saved. Existing documents may need reindexing to apply changes.', 'success');
    } catch (err) {
      chunkingError = asApiError(err).message;
    } finally {
      savingChunking = false;
    }
  }

  async function reindexAll(): Promise<void> {
    reindexing = true;
    try {
      await api.knowledgebases.reindexAll(kb.knowledgebase_id);
      addToast('Reindexing all documents', 'info');
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    } finally {
      reindexing = false;
    }
  }

  async function toggleArchive(): Promise<void> {
    const archiving = kb.status !== 'archived';
    if (archiving) {
      const confirmed = await confirmAction({
        title: 'Archive knowledgebase',
        message: `"${kb.name}" will be hidden from active use but not deleted.`,
        confirmLabel: 'Archive',
        variant: 'primary'
      });
      if (!confirmed) return;
    }
    try {
      const updated = await api.knowledgebases.update(kb.knowledgebase_id, {
        status: archiving ? 'archived' : 'active'
      });
      onUpdated(updated);
      addToast(archiving ? 'Archived' : 'Reactivated', 'success');
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    }
  }

  async function deleteKb(): Promise<void> {
    const confirmed = await confirmAction({
      title: 'Delete knowledgebase',
      message: `This permanently deletes "${kb.name}" and its indexed documents. This cannot be undone.`,
      confirmLabel: 'Delete',
      variant: 'danger'
    });
    if (!confirmed) return;
    try {
      await api.knowledgebases.remove(kb.knowledgebase_id);
      addToast('Deleted', 'success');
      onDeleted();
    } catch (err) {
      addToast(asApiError(err).message, 'error');
    }
  }
</script>

<div class="flex flex-col gap-8">
  {#if !canEdit}
    <p class="rounded-xl border border-amber-800/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-200">
      Archived knowledgebases are read-only. Reactivate this knowledgebase to change its profile, schema, chunking, access, or documents.
    </p>
  {/if}
  <section class="flex flex-col gap-3">
    <h2 class="text-base font-semibold text-white">Profile</h2>
    <label class="flex flex-col gap-1.5 text-sm text-slate-300">
      Name
      <Input bind:value={name} maxlength={200} disabled={!canEdit} data-testid="knowledge-settings-name" />
    </label>
    <label class="flex flex-col gap-1.5 text-sm text-slate-300">
      Description
      <textarea
        class="min-h-[80px] rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2.5 text-sm text-slate-100"
        bind:value={description}
        disabled={!canEdit}
      ></textarea>
    </label>
    <Button class="self-start" disabled={!canEdit || savingProfile || !name.trim()} onclick={saveProfile}>
      {savingProfile ? 'Saving…' : 'Save profile'}
    </Button>
  </section>

  <section class="flex flex-col gap-3">
    <h2 class="text-base font-semibold text-white">Metadata schema</h2>
    <p class="text-sm text-slate-400">
      Defines fields available to ingestion and retrieval. Only fields with <code>filterable: true</code> appear in Search/Ask.
      Example: <code>&#123;"fields": &#123;"category": &#123;"type": "keyword", "filterable": true&#125;&#125;&#125;</code>
    </p>
    <textarea
      class="min-h-[140px] rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2.5 font-mono text-xs text-slate-100"
      bind:value={metadataSchemaText}
      disabled={!canEdit}
      data-testid="knowledge-settings-schema"
    ></textarea>
    {#if schemaError}<p class="text-sm text-rose-300">{schemaError}</p>{/if}
    <Button class="self-start" variant="secondary" disabled={!canEdit || savingSchema} onclick={saveSchema}>
      {savingSchema ? 'Saving…' : 'Save schema'}
    </Button>
  </section>

  <section class="flex flex-col gap-3">
    <h2 class="text-base font-semibold text-white">Chunk settings</h2>
    <p class="text-sm text-slate-400">Backend-defined chunking and embedding options for this knowledgebase.</p>
    <textarea
      class="min-h-[100px] rounded-xl border border-slate-700 bg-slate-950/80 px-3 py-2.5 font-mono text-xs text-slate-100"
      bind:value={chunkSettingsText}
      disabled={!canEdit}
    ></textarea>
    {#if chunkingError}<p class="text-sm text-rose-300">{chunkingError}</p>{/if}
    {#if staleCount > 0}
      <p class="rounded-xl border border-amber-800/60 bg-amber-950/40 px-3 py-2 text-sm text-amber-200">
        {staleCount} document{staleCount === 1 ? '' : 's'} need reindexing to reflect current chunk settings.
      </p>
    {/if}
    <div class="flex gap-3">
      <Button variant="secondary" disabled={!canEdit || savingChunking} onclick={saveChunking}>
        {savingChunking ? 'Saving…' : 'Save chunk settings'}
      </Button>
      <Button variant="ghost" disabled={!canEdit || reindexing} onclick={reindexAll}>
        <RefreshCw class="mr-1.5 h-3.5 w-3.5" /> Reindex all documents
      </Button>
    </div>
  </section>

  {#if diagnostics}
    <section class="flex flex-col gap-3">
      <h2 class="text-base font-semibold text-white">Diagnostics</h2>
      <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {#each Object.entries(diagnostics.artifact_counts) as [status, count] (status)}
          <div class="rounded-xl border border-slate-800/80 bg-slate-900/60 px-3 py-2 text-center">
            <p class="text-lg font-semibold text-white">{count}</p>
            <p class={`mt-0.5 inline-block rounded-full border px-2 py-0.5 text-xs capitalize ${statusToneClass('neutral')}`}>{status}</p>
          </div>
        {/each}
      </div>
      <p class="text-sm text-slate-400">
        {diagnostics.chunk_count} chunk{diagnostics.chunk_count === 1 ? '' : 's'} indexed ·
        backend {diagnostics.enabled ? 'enabled' : 'disabled'}
      </p>
    </section>
  {/if}

  {#if canManageLifecycle}
  <section class="flex flex-col gap-3 border-t border-slate-800/80 pt-6">
    <h2 class="text-base font-semibold text-rose-300">Danger zone</h2>
    <div class="flex flex-wrap gap-3">
      <Button variant="secondary" onclick={toggleArchive}>
        {kb.status === 'archived' ? 'Reactivate knowledgebase' : 'Archive knowledgebase'}
      </Button>
      <Button variant="danger" onclick={deleteKb} data-testid="knowledge-settings-delete">
        Delete knowledgebase
      </Button>
    </div>
  </section>
  {/if}
</div>
