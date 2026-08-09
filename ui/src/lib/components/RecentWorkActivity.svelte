<script lang="ts">
  import ToolCallBlock from './ToolCallBlock.svelte';
  import FileDiffViewer from './FileDiffViewer.svelte';
  import MessageAttachments from './MessageAttachments.svelte';
  import AssistantDeliverableBlock from './AssistantDeliverableBlock.svelte';
  import { commandToToolCall, mutationToToolCall } from '$lib/work/workEventAdapter';
  import type { ActivityOverviewResponse, AssistantDeliverableTimelineItem, FileDiffRef, TimelineScope, WorkCategory, WorkDeliverable, WorkMutationEvent } from '$lib/chat-v2/types';
  import type { FileDiff } from '$lib/diff';

  let { overview, scope, limit = 10, onSeeAll }: {
    overview: ActivityOverviewResponse;
    scope: TimelineScope;
    limit?: number;
    onSeeAll?: (category: WorkCategory) => void;
  } = $props();

  const recentWork = $derived(overview.recent_work ?? null);
  const MAX_FILE_EDITS_PER_EVENT = 20;
  function deliverableItem(item: WorkDeliverable): AssistantDeliverableTimelineItem {
    return {
      id: `deliverable:${item.deliverable_id}`, kind: 'assistant_deliverable',
      deliverable_id: item.deliverable_id, format: item.format, title: item.title,
      content: item.content, render_metadata: item.render_metadata,
      export_metadata: item.export_metadata, sort_key: item.sort_key ?? '',
      source_refs: [], stable: true,
    };
  }
  function displayPath(edit: Pick<FileDiffRef, 'path' | 'relative_path'>): string {
    return edit.relative_path || edit.path;
  }
  function mergeFileEdit(edits: FileDiffRef[], incoming: FileDiffRef): void {
    const path = displayPath(incoming);
    if (!path) return;
    const existingIndex = edits.findIndex((candidate) => {
      if (incoming.path_id && candidate.path_id) return candidate.path_id === incoming.path_id;
      return displayPath(candidate) === path || candidate.path === incoming.path;
    });
    if (existingIndex < 0) {
      edits.push(incoming);
      return;
    }
    const existing = edits[existingIndex]!;
    edits[existingIndex] = {
      ...incoming,
      ...existing,
      path_id: existing.path_id ?? incoming.path_id,
      relative_path: existing.relative_path ?? incoming.relative_path,
      root_label: existing.root_label ?? incoming.root_label,
      root_name: existing.root_name ?? incoming.root_name,
      root_id: existing.root_id ?? incoming.root_id,
      additions: existing.additions ?? incoming.additions,
      deletions: existing.deletions ?? incoming.deletions,
      diff: existing.diff || incoming.diff,
    };
  }
  function fileEdits(event: WorkMutationEvent): FileDiff[] {
    const edits: FileDiffRef[] = [];
    for (const diff of Array.isArray(event.file_diffs) ? event.file_diffs : []) {
      mergeFileEdit(edits, diff);
    }
    for (const stat of Array.isArray(event.file_stats) ? event.file_stats : []) {
      mergeFileEdit(edits, {
        path: stat.relative_path || stat.path,
        path_id: stat.path_id,
        relative_path: stat.relative_path,
        root_label: stat.root_label,
        root_name: stat.root_name,
        root_id: stat.root_id,
        additions: stat.additions,
        deletions: stat.deletions,
        diff: '',
        content_truncated: true,
      });
    }
    for (const path of Array.isArray(event.paths) ? event.paths : []) {
      mergeFileEdit(edits, { path, diff: '', content_truncated: true });
    }
    const visible = edits.slice(0, MAX_FILE_EDITS_PER_EVENT).map((edit) => ({
      path: displayPath(edit),
      diff: edit.diff,
      additions: edit.additions,
      deletions: edit.deletions,
      content_truncated: edit.content_truncated,
      truncated: edit.truncated,
    }));
    const total = Math.max(event.total_file_count ?? 0, edits.length);
    const omitted = Math.max(event.omitted_file_count ?? 0, total - visible.length);
    return omitted > 0
      ? [...visible, { path: '', diff: '', omitted_count: omitted, truncated: true }]
      : visible;
  }
</script>

<div class="space-y-5" data-testid="recent-work-activity">
  {#if recentWork?.commands.length}
    <section><h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Commands</h3>
      <div class="space-y-1">{#each recentWork.commands.slice(0, limit) as command (command.id)}<ToolCallBlock item={commandToToolCall(command)} {scope} density="compact" summaryMode="command" />{/each}</div>
      <button type="button" class="mt-2 text-xs text-sky-300" onclick={() => onSeeAll?.('commands')}>See all commands</button>
    </section>
  {/if}
  {#if recentWork?.files.length}
    <section><h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Files</h3>
      <div class="space-y-2">{#each recentWork.files.slice(0, limit) as event (event.id)}{@const diffs = fileEdits(event)}{#if diffs.length}<FileDiffViewer {diffs} collapsedByDefault onExpand={() => onSeeAll?.('files')} />{/if}{/each}</div>
      <button type="button" class="mt-2 text-xs text-sky-300" onclick={() => onSeeAll?.('files')}>See all files</button>
    </section>
  {/if}
  {#if recentWork?.mutations.length}
    <section><h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Mutations</h3>
      <div class="space-y-2">{#each recentWork.mutations.slice(0, limit) as event (event.id)}<ToolCallBlock item={mutationToToolCall(event)} {scope} density="compact" />{/each}</div>
      <button type="button" class="mt-2 text-xs text-sky-300" onclick={() => onSeeAll?.('mutations')}>See all mutations</button>
    </section>
  {/if}
  {#if recentWork?.artifacts.length}
    <section><h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Artifacts</h3>
      <MessageAttachments attachments={recentWork.artifacts.slice(0, limit).map((item) => ({ artifact_id: item.artifact_id, kind: item.mime_type?.startsWith('image/') ? 'image' : 'file', mime_type: item.mime_type ?? 'application/octet-stream', filename: item.filename, size_bytes: item.size_bytes ?? 0 }))} />
      <button type="button" class="mt-2 text-xs text-sky-300" onclick={() => onSeeAll?.('artifacts')}>See all artifacts</button>
    </section>
  {/if}
  {#if recentWork?.deliverables.length}
    <section><h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Deliverables</h3>
      <div class="space-y-3">{#each recentWork.deliverables.slice(0, limit) as item (item.deliverable_id)}<AssistantDeliverableBlock item={deliverableItem(item)} collapsedByDefault />{/each}</div>
      <button type="button" class="mt-2 text-xs text-sky-300" onclick={() => onSeeAll?.('deliverables')}>See all deliverables</button>
    </section>
  {/if}
</div>
