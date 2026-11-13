<script lang="ts">
  import { onMount } from 'svelte';
  import ScopedChatV2Timeline from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
  import { ScopedFixtureController, fixtureScopes } from '$lib/chat-v2/scoped-fixture';
  import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
  import type { TimelineScope, ToolCallTimelineItem } from '$lib/chat-v2/types';
  import { documentCoordination } from '$lib/document-coordination';
  import { offlineCache } from '$lib/offline/cache';

  let ready = $state(false);
  onMount(() => {
    void (async () => {
      const coordination = documentCoordination();
      await coordination.initialize();
      await coordination.verified('tool-groups@cognis.test');
      await offlineCache.setOwner(coordination.storageName('offline-cache'));
      ready = true;
    })();
  });
  const controller = new ScopedFixtureController();
  const separate = new URLSearchParams(location.search).has('separate');
  const preferences = {
    ...DEFAULT_USER_PREFERENCES,
    chat: { ...DEFAULT_USER_PREFERENCES.chat, keep_assistant_messages_separate: separate },
  };
  // One tool per inference cycle, with file and non-file exploration adjacent.
  const names = ['read', 'grep', 'artifact_get_metadata', 'read', 'glob', 'bash', 'apply_patch', 'read'];
  const api = {
    ...controller.api,
    snapshot: async (scope: TimelineScope) => {
      const snapshot = await controller.api.snapshot(scope);
      const items: ToolCallTimelineItem[] = names.map((tool_name, index) => ({
        id: `${scope.key}:group-tool-${index}`, call_id: `group-call-${index}`,
        kind: 'tool_call', tool_name, sort_key: String(index).padStart(4, '0'),
        stable: true, status: 'complete', turn_id: 'group-turn', turn_cycle_index: index,
        source_refs: [{ store: 'fixture', session_id: 'group-session', seq: index + 1, event_type: 'tool_call' }],
        arguments: null, result_preview: 'Synthetic tool result', is_error: false,
        attachments: [], file_diffs: [], truncated: false, has_full_output: false,
      }));
      return { ...snapshot, timeline: { items, has_more_before: false, before_cursor: null } };
    },
  };
</script>

<main class="mx-auto max-w-5xl space-y-6 p-4 text-foreground">
  <header>
    <h1 class="text-xl font-semibold">Consecutive tool cycles</h1>
    <p class="text-sm text-secondary">Production timeline · synthetic step/chat snapshots · no backend or device evidence</p>
    <p class="text-sm text-secondary">Assistant messages {separate ? 'separate' : 'folded'} · five exploration calls, one command, one edit and its read</p>
  </header>
  {#if ready}
  {#each [fixtureScopes['task-step'], fixtureScopes.parent] as scope (scope.key)}
    <section>
      <h2 class="mb-2 font-semibold">{scope.kind === 'task_step' ? 'Execute step — Logs' : 'Conversation timeline'}</h2>
      <div class="h-72 overflow-hidden rounded-xl border border-separator" data-testid={scope.kind}>
        <ScopedChatV2Timeline {scope} {api} realtime={controller.realtime} {preferences}
          autoBackfill={false} compact />
      </div>
    </section>
  {/each}
  {/if}
</main>
