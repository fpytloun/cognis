<script lang="ts">
  import { onMount } from 'svelte';
  import FullChat from '../../../src/routes/(app)/chat/[conversationId]/+page.svelte';
  import ScopedChat from '$lib/components/chat-v2/ScopedChatV2Timeline.svelte';
  import TimelineDiagnosticExportPanel from '$lib/components/chat/TimelineDiagnosticExportPanel.svelte';
  import { api } from '$lib/api/client';
  import { auth } from '$lib/stores/auth';
  import { DEFAULT_USER_PREFERENCES } from '$lib/user-preferences';
  import { documentCoordination } from '$lib/document-coordination';
  import { offlineCache } from '$lib/offline/cache';
  import { ChatV2ApiClient, chatV2Api } from '$lib/chat-v2/api';
  import { wsClient } from '$lib/ws/client';
  import { acquireTimeline, timelineResourceCounts, type TimelineController } from '$lib/chat-v2/timeline-controller.svelte';
  import { conversationTimelineScope, type ActivityOverviewResponse, type ChatSnapshot, type CompactionTimelineItem, type MessageTimelineItem, type ChatV2TimelineItem } from '$lib/chat-v2/types';
  import type { CognisWebSocketClientEvent } from '$lib/ws/types';
  import { page } from './state.svelte';

  const scope = conversationTimelineScope('synthetic-history');
  const conversation = {
    conversation_id: scope.conversation_id!, agent_id: 'synthetic-agent', status: 'active',
    title: 'Synthetic history', context: { type: 'web' }, has_message_history: true,
  };
  const agent = { agent_id: 'synthetic-agent', name: 'Synthetic agent', enabled: true };
  const auditPreloads = new URLSearchParams(location.search).has('audit-preloads');
  const scrollScenario = new URLSearchParams(location.search).get('scenario') ?? '';
  const carriedMode = new URLSearchParams(location.search).has('carried-response');
  const queuedAdmissionMode = new URLSearchParams(location.search).has('queued-admission');
  const failedAdmissionMode = queuedAdmissionMode || new URLSearchParams(location.search).has('failed-admission');
  const cachedManagedMode = new URLSearchParams(location.search).has('cached-managed');
  const forceRefreshMode = new URLSearchParams(location.search).has('force-refresh');
  const oldInputEchoMode = new URLSearchParams(location.search).has('old-input-echoes');
  const nextConversation = { ...conversation, conversation_id: 'synthetic-next', title: 'Next conversation' };
  const row = (index: number): MessageTimelineItem => ({
    id: `history:${index}`, kind: 'message', message_id: `history:${index}`, role: 'assistant',
    sort_key: String(10_000 + index).padStart(8, '0'), source_refs: [], stable: true,
    content: `Synthetic history ${index}`, attachments: [], partial: false, status: 'complete',
  });
  const scrollItems: ChatV2TimelineItem[] = Array.from({ length: 800 }, (_, index) => {
    const item = row(index);
    if (scrollScenario !== 'scroll-collapsed'
      && (scrollScenario !== 'scroll-groups' || index % 80 === 0 || index % 80 === 79)) {
      if (scrollScenario === 'scroll-groups') {
        item.role = index % 80 === 0 ? 'user' : 'assistant';
        if (item.role === 'assistant') item.content += '\n\n' + Array.from({ length: 12 }, (_, n) =>
          `Paragraph ${n}: stable long-message content for bidirectional history navigation.`).join('\n\n');
      }
      return item;
    }
    return {
      id: `tool:${index}`, kind: 'tool_call', sort_key: item.sort_key,
      source_refs: [], stable: true, status: 'complete',
      call_id: `call-${index}`, tool_name: scrollScenario === 'scroll-collapsed'
        ? 'read' : ['read', 'bash', 'apply_patch'][Math.floor(index / 8) % 3],
      turn_id: scrollScenario === 'scroll-collapsed' ? 'long-running-turn' : `turn-${Math.floor(index / 80)}`,
      turn_cycle_index: 0,
      assistant_phase_index: 0, arguments: null, is_error: false,
      attachments: [], file_diffs: [], truncated: false, has_full_output: false,
    };
  });
  const assistant = (turn: string, phase: number, seq: number, content: string): MessageTimelineItem => ({
    ...row(seq), id: `message:${turn}:phase:${phase}`, message_id: turn, turn_id: turn,
    assistant_phase_index: phase, turn_cycle_index: turn === 'closed-turn' ? 18 : 1,
    sort_key: `0000:${String(seq).padStart(15, '0')}:${String(phase).padStart(6, '0')}:02:000000000`,
    source_refs: [{ store: 'intaris', session_id: 'carried-session', seq, event_type: 'assistant_message' }],
    content,
  });
  const interrupted = {
    ...assistant('closed-turn', 47, 450, 'Retained interrupted response.'),
    stable: false, partial: true, status: 'running', turn_cycle_index: 19,
    sort_key: '9998:999999999999999:000047:02:000000000',
    source_refs: [{ store: 'runtime', session_id: 'carried-session', seq: 0, event_type: 'assistant_stream' }],
  } as MessageTimelineItem;
  const carriedSample = (advanced = false): ChatSnapshot => ({
    schema_version: 2, projection_version: 'synthetic-history', scope, conversation,
    cursor: advanced ? 'carried:new' : 'carried:old', server_time: '',
    timeline: { items: [
      assistant('closed-turn', 44, 447, 'Previous canonical response'),
      ...(advanced ? [assistant('current-turn', 2, 465, 'Newer canonical response')] : []),
    ], has_more_before: true, before_cursor: 'carried:before' },
    state: { state_version: advanced ? 2 : 1, active_turn: {}, pending: {}, active_session: {}, capabilities: [] },
    runtime: {
      runtime_epoch: 'carried-epoch', runtime_revision: advanced ? 2 : 1,
      generated_at: advanced ? '2026-01-02T00:01:00Z' : '2026-01-02T00:00:00Z',
      has_active_turn: true,
      active_turn: { turn_id: advanced ? 'current-turn' : 'closed-turn', session_id: 'carried-session', status: 'running' },
      volatile_items: advanced ? [{
        ...assistant('current-turn', 3, 466, 'Current live progress'),
        partial: true, stable: false, status: 'running',
        sort_key: '9998:999999999999999:000003:02:000000000',
        source_refs: [{ store: 'runtime', session_id: 'carried-session', seq: 0, event_type: 'assistant_stream' }],
      }] : [interrupted],
    },
    queue: { messages: [], queued_count: 0 },
  } as ChatSnapshot);
  const failedInput: MessageTimelineItem = {
    ...row(0), id: 'user:failed-input', role: 'user', message_id: 'client:failed-input',
    client_message_id: 'failed-input', client_txn_id: 'failed-transaction', turn_id: 'failed-turn',
    content: 'Retained failed input', stable: false,
    sort_key: '9996:999999999999999:000000:00:000000000',
  };
  const failedInputs = Array.from({ length: queuedAdmissionMode ? 4 : 1 }, (_, index) => index === 0
    ? { ...failedInput, turn_id: queuedAdmissionMode ? undefined : failedInput.turn_id } : {
      ...failedInput, id: `user:failed-input-${index}`, message_id: `client:failed-input-${index}`,
      client_message_id: `failed-input-${index}`, client_txn_id: `failed-transaction-${index}`,
      turn_id: queuedAdmissionMode ? undefined : `failed-turn-${index}`, content: `Retained queued input ${index}`,
    });
  const oldInputs: MessageTimelineItem[] = [
    ['confirmed-input', 'Previously confirmed input'],
    ['legacy-input', 'Older processed input'],
    ['unconfirmed-input', 'Unconfirmed recoverable input'],
  ].map(([id, content]) => ({
    ...failedInput, id: `user:${id}`, message_id: `client:${id}`, client_message_id: id,
    client_txn_id: undefined, turn_id: undefined, content,
  }));
  const failedSample = (): ChatSnapshot => ({
    ...carriedSample(true), cursor: 'failed:initial', runtime: null,
    timeline: { items: [
      {
        ...failedInput, id: 'user:later-input', client_message_id: 'later-input', client_txn_id: 'later-transaction',
        message_id: 'client:later-input', turn_id: 'later-turn', content: 'Later accepted input', stable: true,
        sort_key: '0001:000000000000510:000000:00:000000000',
        source_refs: [
          { store: 'intaris', session_id: 'previous-session', seq: 510, event_type: 'user_message' },
          { store: 'intaris', session_id: 'rotated-session', seq: 9, event_type: 'user_message' },
        ],
      },
      {
        ...assistant('later-turn', 4, 100, 'Newer canonical response'),
        sort_key: '0002:000000000000100:000004:02:000000000',
        source_refs: [{ store: 'intaris', session_id: 'rotated-session', seq: 100, event_type: 'assistant_message' }],
      },
    ], has_more_before: false, before_cursor: null },
  });
  const sample = (): ChatSnapshot => failedAdmissionMode ? failedSample() : carriedMode ? carriedSample() : ({
    schema_version: 2, projection_version: 'synthetic-history', scope, conversation,
    cursor: 'synthetic:initial', server_time: '',
    timeline: { items: scrollScenario ? scrollItems.slice(300) : Array.from({ length: 200 }, (_, i) => i === 151 ? {
      ...row(i), kind: 'notice', title: 'Synthetic child hook', level: 'info',
      hook: { run_id: 'synthetic-hook-run', hook_id: 'synthetic-hook',
        status: 'completed', child_session_id: 'synthetic-child-session', trigger: 'conversation_idle' },
    } : row(i)), has_more_before: true, before_cursor: 'older:1' },
    state: { state_version: 1, active_turn: {}, pending: {}, active_session: {}, capabilities: [] },
    runtime: null, queue: { messages: [], queued_count: 0 },
  } as ChatSnapshot);
  let ready = $state(false);
  let scopedVisible = $state(!new URLSearchParams(location.search).has('full-only'));
  let fullVisible = $state(!cachedManagedMode);
  let failure = $state('');
  let recoveryController = $state.raw<TimelineController | null>(null);

  onMount(() => {
    void (async () => {
      // Fictional coordination metadata, not a credential or auth session.
      // The real principal-scoped registry/cache and fences stay active.
      await documentCoordination().initialize();
      await documentCoordination().verified('history-fixture@cognis.test');
      await offlineCache.setOwner(documentCoordination().storageName('offline-cache'));
      const listeners = new Set<(event: CognisWebSocketClientEvent) => void>();
      wsClient.subscribe = callback => { listeners.add(callback); return () => { listeners.delete(callback); }; };
      wsClient.acquireChatV2 = () => {};
      wsClient.releaseChatV2 = () => {};
      wsClient.updateChatV2Cursor = () => {};
      wsClient.subscribeConversation = () => {};
      wsClient.unsubscribeConversation = () => {};
      const state = {
        snapshots: 0, nextSnapshots: 0, historyRequests: 0, appends: 0, unexpectedRequests: [] as string[],
        backfillInFlight: 0, maxBackfillInFlight: 0, holdScrollingHistory: false,
        snapshotAborts: 0,
        releaseSnapshot: null as (() => void) | null,
        syncRequests: 0,
        admissionRequests: 0,
        resolveFailedAdmission: null as (() => void) | null,
        releaseSync: null as (() => void) | null,
        preloadNext: () => {
          for (const listener of listeners) listener({
            type: 'conversation_updated', conversation_id: nextConversation.conversation_id, has_active_turn: true,
          } as CognisWebSocketClientEvent);
        },
        releaseHistory: null as (() => void) | null,
        counts: () => timelineResourceCounts(),
        owner: () => offlineCache.currentOwner,
        cachedRecoveryIds: async () => {
          const cached = await offlineCache.get<{ state: { localItems: ChatV2TimelineItem[] } }>('timeline', scope.key);
          return cached?.state.localItems.map(item => item.id) ?? [];
        },
        closeScoped: () => { scopedVisible = false; },
        closeFull: () => { fullVisible = false; },
        reopenFull: () => { fullVisible = true; },
        changeConversation: () => {
          page.params.conversationId = 'synthetic-next';
          page.url = new URL('http://localhost/chat/synthetic-next');
        },
        returnConversation: () => {
          page.params.conversationId = scope.conversation_id!;
          page.url = new URL(`http://localhost/chat/${scope.conversation_id}`);
        },
        append: () => {
          const lease = acquireTimeline(scope);
          const cursor = lease.controller.store.snapshot.cursor!;
          state.appends++;
          const event = {
            type: 'chat_v2_frame', schema_version: 2, projection_version: 'synthetic-history', scope,
            conversation_id: scope.conversation_id, cursor_before: cursor,
            cursor_after: `synthetic:live:${state.appends}`,
             ops: [{ op: 'upsert_item', item: row((scrollScenario ? 800 : 200) + state.appends) },
               { op: 'upsert_item', item: { ...row(scrollScenario ? 799 : 150), content: `Synthetic live update ${state.appends}` } }],
            runtime: null, server_time: '',
          } as CognisWebSocketClientEvent;
          for (const listener of listeners) listener(event);
          lease.release();
        },
        advanceCarried: () => {
          const lease = acquireTimeline(scope);
          const next = carriedSample(true);
          for (const listener of listeners) listener({
            type: 'chat_v2_frame', schema_version: 2, projection_version: next.projection_version, scope,
            conversation_id: scope.conversation_id, cursor_before: lease.controller.store.snapshot.cursor,
            cursor_after: next.cursor, ops: next.timeline.items.map(item => ({ op: 'upsert_item', item })),
            runtime: next.runtime, server_time: '',
          } as CognisWebSocketClientEvent);
          lease.release();
        },
        confirmCarried: () => {
          const lease = acquireTimeline(scope);
          const next = carriedSample(true);
          for (const listener of listeners) listener({
            type: 'chat_v2_frame', schema_version: 2, projection_version: next.projection_version, scope,
            conversation_id: scope.conversation_id, cursor_before: lease.controller.store.snapshot.cursor,
            cursor_after: 'carried:confirmed',
            ops: [{ op: 'upsert_item', item: {
              ...assistant('closed-turn', 47, 450, interrupted.content), partial: true, turn_cycle_index: 19,
            } }],
            runtime: { ...next.runtime, runtime_revision: 3 }, server_time: '',
          } as CognisWebSocketClientEvent);
          lease.release();
        },
        older: () => {
          const lease = acquireTimeline(scope);
          const before = lease.controller.store.snapshot.beforeCursor!;
          return lease.controller.older(before).finally(lease.release);
        },
      };
      Object.assign(window, { historyFixture: state });
      const admissionReleases = new Map<string, () => void>();
      window.fetch = async input => {
        if (String(input) === '/api/readyz') return Response.json({ status: 'ready' });
        if (String(input) === '/api/v1/feedback/conversations/synthetic-history/staging') {
          return Response.json({ conversation_id: scope.conversation_id, version: 0, items: [], receipts: [] });
        }
        if (auditPreloads && String(input) === '/api/auth/me') {
          return Response.json({ email: 'history-fixture@cognis.test', role: 'user' });
        }
        if (oldInputEchoMode && String(input).includes('/message-admissions/')) {
          const id = new URL(String(input), location.href).pathname.split('/').at(-1);
          state.admissionRequests++;
          if (id === 'unconfirmed-input') return Response.json(
            { detail: 'Message admission not found' }, { status: 404 },
          );
          if (id !== 'legacy-input') throw new Error(`Unexpected admission identity: ${id}`);
          await new Promise<void>(resolve => {
            admissionReleases.set(id, resolve);
            state.resolveFailedAdmission = () => admissionReleases.forEach(release => release());
          });
          admissionReleases.delete(id);
          return Response.json({
            conversation_id: scope.conversation_id, client_txn_id: 'legacy-transaction',
            client_message_id: id, turn_id: 'legacy-turn', status: 'cancelled',
            started: true, input_persisted: true, admission_order: 22120, before_client_message_id: null,
          });
        }
        const admitted = failedAdmissionMode ? failedInputs.find(item =>
          new URL(String(input), location.href).pathname
            === `/api/v1/chat/v2/conversations/synthetic-history/messages/${item.client_txn_id}/admission`) : null;
        if (admitted) {
          state.admissionRequests++;
          await new Promise<void>(resolve => {
            admissionReleases.set(admitted.client_txn_id!, resolve);
            state.resolveFailedAdmission = () => admissionReleases.forEach(release => release());
          });
          admissionReleases.delete(admitted.client_txn_id!);
          const index = failedInputs.indexOf(admitted);
          return Response.json({
            conversation_id: scope.conversation_id, client_txn_id: admitted.client_txn_id,
            client_message_id: admitted.client_message_id, turn_id: index ? `failed-turn-${index}` : 'failed-turn',
            status: index % 2 ? 'cancelled' : 'failed',
            started: false, admission_order: 22124 + index, before_client_message_id: 'later-input',
          });
        }
        state.unexpectedRequests.push(String(input));
        throw new Error(`Unexpected nonfixture request: ${input}`);
      };
      Object.assign(api.conversations, {
        detail: async id => ({ ...conversation, conversation_id: id }), sessions: async () => [],
        sidebar: async () => ({
          conversations: { items: auditPreloads ? [conversation, nextConversation] : [conversation], cursor: null, has_more: false },
          agents: [agent], agent_direct_chats: [], context_types: ['web'], sidebar_revision: '1', is_delta: false,
          sync_timestamp: '2026-10-19T16:00:00Z',
          background_work: { items: [], active_count: 0, truncated: false, generated_at: '2026-10-19T16:00:00Z' },
        }),
        contextTypes: async () => ['web'], waiting: async () => [],
        getQueue: async () => ({ messages: [], queued_count: 0 }),
        markRead: async () => {}, rememberOpened: async () => {},
      });
      Object.assign(api.agents, { list: async () => [agent] });
      Object.assign(api.sessions, { detail: async () => ({
        session_id: 'synthetic-child-session', conversation_id: scope.conversation_id,
        agent_id: agent.agent_id, status: 'completed', kind: 'child',
      }) });
      Object.assign(api.projects, { list: async () => [] });
      Object.assign(api.notifications, { list: async () => [] });
      Object.assign(api.search, { health: async () => ({ enabled: false }) });
      Object.assign(api.userPreferences, { get: async () => DEFAULT_USER_PREFERENCES });
      chatV2Api.clientPerformance = async () => {};
      chatV2Api.snapshot = async (asked, options: { signal?: AbortSignal } = {}) => {
        if (typeof asked !== 'string' && asked.conversation_id === 'synthetic-next') {
          state.nextSnapshots++;
          return {
            ...sample(), scope: asked, conversation: { ...conversation, conversation_id: 'synthetic-next' },
            timeline: { items: [{ ...row(1), id: 'next:1', message_id: 'next:1', content: 'New conversation only' }],
              has_more_before: false, before_cursor: null },
          } as ChatSnapshot;
        }
        if (typeof asked !== 'string' && asked.key !== scope.key) return {
          ...sample(), scope: asked,
          timeline: { items: [], has_more_before: false, before_cursor: null },
        };
        state.snapshots++;
        if (new URLSearchParams(location.search).has('hold-snapshot') && state.snapshots === 1) {
          await new Promise<void>((resolve, reject) => {
            const aborted = () => {
              state.snapshotAborts++;
              reject(options.signal?.reason ?? new DOMException('The operation was aborted.', 'AbortError'));
            };
            options.signal?.addEventListener('abort', aborted, { once: true });
            state.releaseSnapshot = () => {
              options.signal?.removeEventListener('abort', aborted);
              resolve();
            };
            if (options.signal?.aborted) aborted();
          });
        }
        if (forceRefreshMode && new URLSearchParams(location.search).has('refresh-error')) {
          throw new Error('Synthetic snapshot failure');
        }
        return sample();
      };
      chatV2Api.sync = async (asked, cursor) => {
        state.syncRequests++;
        if (new URLSearchParams(location.search).has('hold-sync') && state.syncRequests === 1) {
          await new Promise<void>(resolve => { state.releaseSync = resolve; });
        }
        return {
        schema_version: 2, projection_version: 'synthetic-history',
        scope: typeof asked === 'string' ? conversationTimelineScope(asked) : asked,
        conversation_id: typeof asked === 'string' ? asked : asked.conversation_id!,
        cursor_before: cursor, cursor_after: cursor, ops: [], has_more: false, runtime: null,
        reset_required: false, server_time: '',
        };
      };
      chatV2Api.timeline = async (_scope, options) => {
        state.historyRequests++;
        const page = Number(options?.before?.split(':')[1]);
        if (scrollScenario) {
          state.backfillInFlight++;
          state.maxBackfillInFlight = Math.max(state.maxBackfillInFlight, state.backfillInFlight);
          if (state.holdScrollingHistory) {
            await new Promise<void>(resolve => { state.releaseHistory = resolve; });
            state.releaseHistory = null;
          }
          else await new Promise(resolve => setTimeout(resolve, 150));
          state.backfillInFlight--;
          return {
            schema_version: 2, projection_version: 'synthetic-history', scope,
            conversation_id: scope.conversation_id!,
            items: scrollItems.slice(Math.max(0, 300 - page * 100), 400 - page * 100),
            has_more_before: page < 3, before_cursor: page < 3 ? `older:${page + 1}` : null, server_time: '',
          };
        }
        if (page !== state.historyRequests || page > 2) throw new Error('Synthetic history cursor did not advance');
        await new Promise<void>(resolve => { state.releaseHistory = resolve; });
        state.releaseHistory = null;
        return {
          schema_version: 2, projection_version: 'synthetic-history', scope, conversation_id: scope.conversation_id!,
          items: Array.from({ length: 60 }, (_, i) => row(i - page * 60)),
          has_more_before: page < 2, before_cursor: page < 2 ? 'older:2' : null, server_time: '',
        };
      };
      if (auditPreloads) await auth.bootstrap();
      if (failedAdmissionMode) {
        // The default singleton captured fetch before this fixture installed its
        // local transport. Keep the real typed request/auth/response path.
        const receipts = new ChatV2ApiClient({ fetch: (input, init) => window.fetch(input, init) });
        chatV2Api.messageAdmission = receipts.messageAdmission.bind(receipts);
        chatV2Api.messageAdmissionByClient = receipts.messageAdmissionByClient.bind(receipts);
        const lease = acquireTimeline(scope);
        const snapshot = failedSample();
        const cached = oldInputEchoMode
          ? await offlineCache.get<{ state: typeof lease.controller.store.snapshot }>('timeline', scope.key) : null;
        lease.controller.store.restoreState(cached?.state ?? {
          ...lease.controller.store.snapshot, scope, scopeKey: scope.key,
          conversationId: scope.conversation_id, projectionVersion: snapshot.projection_version,
          cursor: snapshot.cursor, syncStatus: 'ready', timelineItems: oldInputEchoMode ? [
            { ...oldInputs[0], stable: true, source_refs: [{
              store: 'intaris', session_id: 'previous-root-session', seq: 100, event_type: 'user_message',
            }], sort_key: '0000:000000000000100:000000:00:000000000' },
            ...snapshot.timeline.items,
          ] : snapshot.timeline.items,
          state: snapshot.state, localItems: [...failedInputs, ...(oldInputEchoMode ? oldInputs : []), ...(forceRefreshMode ? [{
            ...row(1), id: 'message:cached-only', stable: false, turn_id: 'cached-only-turn',
            sort_key: '9996:999999999999999:000001:02:000000000', content: 'Cached-only activity',
          }] : [])],
          admissionPlacements: Object.fromEntries(failedInputs.map(item => [item.client_message_id!, {
            placement: queuedAdmissionMode ? 'queue' : 'timeline', status: 'complete',
            clientTxnId: item.client_txn_id, queueId: queuedAdmissionMode ? `queue:${item.client_message_id}` : null,
          }])),
        });
        lease.controller.persist();
        if (forceRefreshMode) recoveryController = lease.controller;
        lease.release();
      }
      if (cachedManagedMode) {
        const compaction = (id: string, session: string, stable: boolean): CompactionTimelineItem => ({
          id: `compaction:${id}`, kind: 'compaction', status: 'compacted',
          session_id: session, previous_session_id: `${session}-previous`,
          stable, sort_key: stable ? '9995:own' : `9997:${id}`,
          source_refs: stable ? [{ store: 'intaris', session_id: session, seq: 201, event_type: 'lifecycle' }] : [],
          method: 'llm', turns_compacted: 3, summary_preview: 'Synthetic compaction',
        });
        chatV2Api.activityOverview = async () => ({
          scope,
          workstreams: [{
            key: 'delegate:fixture-child', parent_key: scope.key, kind: 'delegate',
            conversation_id: scope.conversation_id, session_id: 'child-one',
            backing_session_ids: ['child-one', 'child-two'],
          }],
        } as ActivityOverviewResponse);
        const lease = acquireTimeline(scope);
        const snapshot = sample();
        lease.controller.store.restoreState({
          ...lease.controller.store.snapshot, scope, scopeKey: scope.key,
          conversationId: scope.conversation_id, projectionVersion: snapshot.projection_version,
          cursor: snapshot.cursor, syncStatus: 'ready', state: snapshot.state,
          timelineItems: [...snapshot.timeline.items, compaction('cached-own', 'root-session', true)],
          localItems: [compaction('leaked-one', 'child-one', false), compaction('leaked-two', 'child-two', false)],
        });
        lease.controller.persist();
        lease.release();
      }
      ready = true;
    })().catch(error => { failure = String(error); });
  });
</script>

{#if failure}<p role="alert">{failure}</p>{/if}
{#if ready}
  <div class="flex h-dvh min-h-0 flex-col bg-slate-950 text-slate-200">
    <section class="relative flex min-h-0 flex-1 flex-col" data-testid="full-surface">
      {#if fullVisible}<FullChat />{/if}
    </section>
    <section class="relative flex min-h-0 shrink-0 flex-col border-t {forceRefreshMode ? 'max-h-80 overflow-y-auto bg-surface p-4 text-foreground' : 'h-56'}" data-testid="scoped-surface">
      {#if forceRefreshMode && recoveryController}
        <TimelineDiagnosticExportPanel clientState={recoveryController.store.snapshot} visibleItems={recoveryController.store.visibleItems} />
      {:else if scopedVisible}<ScopedChat {scope} autoBackfill={false} compact hasEditableComposer />{/if}
    </section>
  </div>
{/if}
