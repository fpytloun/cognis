<script lang="ts">
  import ToolCallBlock from './ToolCallBlock.svelte';
  import RecentFileEditRow from './RecentFileEditRow.svelte';
  import MessageAttachments from './MessageAttachments.svelte';
  import AssistantDeliverableBlock from './AssistantDeliverableBlock.svelte';
  import { commandToToolCall, mutationToToolCall } from '$lib/work/workEventAdapter';
  import type { ActivityOverviewResponse, AssistantDeliverableTimelineItem, FileDiffRef, TimelineScope, WorkCategory, WorkCommandEvent, WorkDeliverable, WorkMutationEvent } from '$lib/chat-v2/types';
  import type { WorkInitialFocus } from '$lib/work/workFocus';

  let { overview, scope, limit = 10, onSeeAll, onOpenWork }: {
    overview: ActivityOverviewResponse;
    scope: TimelineScope;
    limit?: number;
    onSeeAll?: (category: WorkCategory) => void;
    onOpenWork?: (scope: TimelineScope, focus: WorkInitialFocus) => void;
  } = $props();

  const recentWork = $derived(overview.recent_work ?? null);
  function eventFiles(event: WorkMutationEvent): FileDiffRef[] {
    const result: FileDiffRef[] = [];
    const insert = (incoming: FileDiffRef): void => {
      const index = result.findIndex((item) =>
        (incoming.path_generation_id && item.path_generation_id === incoming.path_generation_id)
        || (incoming.path_id && item.path_id === incoming.path_id)
        || (item.relative_path ?? item.path) === (incoming.relative_path ?? incoming.path)
      );
      if (index < 0) result.push(incoming);
      else result[index] = { ...incoming, ...result[index], diff: result[index]!.diff || incoming.diff };
    };
    for (const diff of event.file_diffs ?? []) insert(diff);
    for (const stat of event.file_stats ?? []) insert({ ...stat, diff: '', preview_omitted: true });
    for (const path of event.paths ?? []) insert({ path, diff: '', preview_omitted: true });
    return result;
  }
  function fileEventTimestamp(event: WorkMutationEvent): string | null {
    return event.created_at
      ?? event.updated_at
      ?? overview.recent.files?.find((item) => item.id === event.id)?.occurred_at
      ?? null;
  }
  const recentFiles = $derived(
    (recentWork?.files ?? []).flatMap((event) =>
      eventFiles(event).map((file, fileOrdinal) => ({
        event,
        file: {
          ...file,
          occurred_at: file.occurred_at ?? fileEventTimestamp(event),
        },
        key: `${event.id}:${file.path_generation_id ?? file.path_id ?? file.path}:${fileOrdinal}`,
      }))
    ),
  );
  const COMMAND_LABEL_MODE_KEY = 'cognis:work-command-label-mode';
  let commandLabelMode = $state<'command' | 'description'>('command');

  $effect(() => {
    if (typeof window === 'undefined') return;
    const stored = window.localStorage.getItem(COMMAND_LABEL_MODE_KEY);
    if (stored === 'command' || stored === 'description') commandLabelMode = stored;
  });

  function setCommandLabelMode(mode: 'command' | 'description'): void {
    commandLabelMode = mode;
    window.localStorage.setItem(COMMAND_LABEL_MODE_KEY, mode);
  }

  function commandItem(command: WorkCommandEvent) {
    const fallbackTimestamp = overview.recent.commands?.find(
      (item) => item.id === command.id,
    )?.occurred_at ?? null;
    return commandToToolCall(command, fallbackTimestamp);
  }

  function deliverableItem(item: WorkDeliverable): AssistantDeliverableTimelineItem {
    return {
      id: `deliverable:${item.deliverable_id}`, kind: 'assistant_deliverable',
      deliverable_id: item.deliverable_id, format: item.format, title: item.title,
      content: item.content, render_metadata: item.render_metadata,
      export_metadata: item.export_metadata, sort_key: item.sort_key ?? '',
      source_refs: [], stable: true,
    };
  }
</script>

<div class="space-y-5" data-testid="recent-work-activity">
  {#if recentWork?.commands.length}
    <section>
      <div class="mb-2 flex items-center justify-between gap-2">
        <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-400">Commands</h3>
        <div class="inline-flex rounded-md border border-white/10 bg-slate-950/40 p-px" role="group" aria-label="Command label">
          {#each ['command', 'description'] as mode}
            <button
              type="button"
              class={`rounded px-1.5 py-0.5 text-[10px] leading-4 transition-colors ${commandLabelMode === mode ? 'bg-slate-700/80 text-white' : 'text-slate-500 hover:text-slate-300'}`}
              aria-pressed={commandLabelMode === mode}
              onclick={() => setCommandLabelMode(mode as 'command' | 'description')}
            >
              {mode === 'command' ? 'Command' : 'Description'}
            </button>
          {/each}
        </div>
      </div>
      <div class="space-y-1">{#each recentWork.commands.slice(0, limit) as command (command.id)}<ToolCallBlock item={commandItem(command)} {scope} density="compact" summaryMode="command" compactLabelMode={commandLabelMode} />{/each}</div>
      <button type="button" class="mt-2 text-xs text-sky-300" onclick={() => onSeeAll?.('commands')}>See all commands</button>
    </section>
  {/if}
  {#if recentFiles.length}
    <section><h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Files</h3>
      <div class="space-y-1">{#each recentFiles.slice(0, limit) as row (row.key)}<RecentFileEditRow event={row.event} file={row.file} {scope} {onOpenWork} />{/each}</div>
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
