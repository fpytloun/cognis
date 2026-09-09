<script lang="ts">
  import { onDestroy } from 'svelte';

  import { api, asApiError } from '$lib/api/client';
  import CredentialRequestForm from '$lib/components/CredentialRequestForm.svelte';
  import EscalationPrompt from '$lib/components/EscalationPrompt.svelte';
  import AttentionPanel from '$lib/components/task-cockpit/AttentionPanel.svelte';
  import { wsClient, wsState } from '$lib/ws/client';
  import type {
    Escalation,
    Notification,
    QuestionSetAnswer,
    QuestionSetQuestion,
  } from '$lib/types/api';

  let {
    conversationId,
    presentation = 'regular',
  } = $props<{
    conversationId: string;
    presentation?: 'regular' | 'floating';
  }>();
  const floating = $derived(presentation === 'floating');
  const passiveClass = $derived(floating
    ? 'pointer-events-auto rounded-2xl border border-slate-700 bg-slate-950 px-4 py-3 text-xs text-slate-400 shadow-2xl'
    : 'shrink-0 border-t border-slate-800 px-4 py-3 text-xs text-slate-400');

  let notifications = $state<Notification[]>([]);
  let loading = $state(true);
  let error = $state('');
  let busyId = $state<string | null>(null);
  let secondsNow = $state(Date.now());
  let generation = 0;
  let refreshTimer: ReturnType<typeof setTimeout> | null = null;
  let connectionStateInitialized = false;
  let wasConnected = false;

  const pending = $derived(notifications.filter((item) => item.status === 'pending'));
  const escalationNotification = $derived(pending.find((item) => item.notification_type === 'escalation') ?? null);
  const credentialRequest = $derived(pending.find((item) => item.notification_type === 'credential_request') ?? null);
  const inputRequest = $derived(pending.find((item) => ['gate', 'step_question', 'auth_challenge'].includes(item.notification_type)) ?? null);
  const isOAuthAuthorization = $derived(
    inputRequest?.notification_type === 'auth_challenge'
    && inputRequest.payload.kind === 'oauth_authorization'
  );
  const oauthAuthorization = $derived.by(() => {
    const item = inputRequest;
    if (
      !isOAuthAuthorization
      || item?.notification_type !== 'auth_challenge'
      || item.payload.kind !== 'oauth_authorization'
    ) return null;
    const metadata = item.payload.metadata;
    if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return null;
    const values = metadata as Record<string, unknown>;
    const rawUrl = values.verification_uri_complete ?? values.verification_uri ?? values.authorization_url;
    if (typeof rawUrl !== 'string') return null;
    let url: URL;
    try {
      url = new URL(rawUrl);
    } catch {
      return null;
    }
    if (!['http:', 'https:'].includes(url.protocol)) return null;
    return {
      url: url.toString(),
      userCode: typeof values.user_code === 'string' ? values.user_code : null,
      callbackMode: typeof values.callback_mode === 'string' ? values.callback_mode : null,
      executorName: typeof values.oauth_executor_name === 'string'
        ? values.oauth_executor_name
        : typeof values.oauth_executor_id === 'string' ? values.oauth_executor_id : null,
    };
  });

  const escalation = $derived.by((): Escalation | null => {
    const item = escalationNotification;
    if (!item) return null;
    return {
      call_id: item.notification_id,
      session_id: item.session_id,
      tool_name: typeof item.payload.tool_name === 'string' ? item.payload.tool_name : null,
      arguments_display: item.payload.arguments_display && typeof item.payload.arguments_display === 'object' && !Array.isArray(item.payload.arguments_display)
        ? item.payload.arguments_display as Record<string, unknown>
        : null,
      decision: 'escalate',
      resolved: false,
      reasoning: typeof item.payload.reasoning === 'string' ? item.payload.reasoning : null,
      risk: typeof item.payload.risk === 'string' ? item.payload.risk : null,
      timeout_seconds: typeof item.payload.timeout_seconds === 'number' ? item.payload.timeout_seconds : 300,
      received_at: item.created_at ? Date.parse(item.created_at) : secondsNow,
    };
  });

  const inputPause = $derived.by(() => {
    const item = inputRequest;
    if (!item) return null;
    const questions = Array.isArray(item.payload.questions)
      ? item.payload.questions as QuestionSetQuestion[]
      : [{
          id: typeof item.payload.question_id === 'string' ? item.payload.question_id : 'response',
          question: typeof item.payload.message === 'string'
            ? item.payload.message
            : typeof item.payload.question === 'string'
              ? item.payload.question
              : 'The assistant needs your response.',
          header: null,
          options: Array.isArray(item.payload.options) ? item.payload.options : [],
          multiple: false,
          allow_custom: true,
          required: true,
        } satisfies QuestionSetQuestion];
    return {
      pause_type: item.notification_type === 'gate' ? 'gate' : 'question',
      step_name: typeof item.payload.step_name === 'string' ? item.payload.step_name : null,
      question: typeof item.payload.question === 'string'
        ? item.payload.question
        : typeof item.payload.message === 'string' ? item.payload.message : null,
      questions,
      options: Array.isArray(item.payload.options) ? item.payload.options : null,
    };
  });

  async function refresh(): Promise<void> {
    const currentId = conversationId;
    const requestGeneration = ++generation;
    try {
      const next = await api.notifications.list(currentId);
      if (requestGeneration !== generation || currentId !== conversationId) return;
      notifications = next;
      error = '';
    } catch (caught) {
      if (requestGeneration !== generation || currentId !== conversationId) return;
      error = asApiError(caught).message;
    } finally {
      if (requestGeneration === generation && currentId === conversationId) loading = false;
    }
  }

  function scheduleRefresh(): void {
    if (refreshTimer !== null) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      void refresh();
    }, 50);
  }

  function eventMatchesConversation(event: {
    conversation_id?: unknown;
    managed_origin_conversation_id?: unknown;
    payload?: unknown;
  }): boolean {
    if (event.conversation_id === conversationId) return true;
    if (event.managed_origin_conversation_id === conversationId) return true;
    if (!event.payload || typeof event.payload !== 'object' || Array.isArray(event.payload)) {
      return false;
    }
    return (event.payload as Record<string, unknown>).managed_origin_conversation_id === conversationId;
  }

  async function resolve(notification: Notification, payload: Parameters<typeof api.notifications.resolve>[1]): Promise<boolean> {
    if (busyId) return false;
    busyId = notification.notification_id;
    error = '';
    try {
      await api.notifications.resolve(notification.notification_id, payload);
      notifications = notifications.filter((item) => item.notification_id !== notification.notification_id);
      await refresh();
      return true;
    } catch (caught) {
      error = asApiError(caught).message;
      return false;
    } finally {
      if (busyId === notification.notification_id) busyId = null;
    }
  }

  async function answerQuestions(answers: QuestionSetAnswer[]): Promise<boolean> {
    if (!inputRequest) return false;
    if (inputRequest.notification_type === 'auth_challenge') {
      const response = answers[0]?.custom_answer
        ?? answers[0]?.selected_option_ids?.[0]
        ?? '';
      return resolve(inputRequest, { decision: 'continue', response });
    }
    return resolve(inputRequest, {
      decision: 'continue',
      response_payload: { answers },
    });
  }

  $effect(() => {
    const currentId = conversationId;
    loading = true;
    notifications = [];
    error = '';
    void refresh();
    return () => {
      if (currentId === conversationId) generation += 1;
    };
  });

  const unsubscribe = wsClient.subscribe((event) => {
    const candidate = event as typeof event & {
      conversation_id?: string;
      managed_origin_conversation_id?: string;
      payload?: unknown;
      reason?: string;
    };
    const interactionEvents = new Set([
      'escalation',
      'escalation_resolved',
      'workflow_gate',
      'workflow_gate_resolved',
      'workflow_step_question',
      'workflow_step_question_resolved',
      'auth_challenge',
      'auth_challenge_resolved',
      'credential_request',
      'credential_request_resolved',
    ]);
    if (
      interactionEvents.has(candidate.type)
      && eventMatchesConversation(candidate)
    ) {
      scheduleRefresh();
    }
    if (
      candidate.type === 'scope_invalidated'
      && candidate.reason === 'notification_state_changed'
      && eventMatchesConversation(candidate)
    ) {
      scheduleRefresh();
    }
  });
  const unsubscribeConnection = wsState.subscribe((state) => {
    const connected = state.status === 'connected';
    if (connectionStateInitialized && connected && !wasConnected) scheduleRefresh();
    wasConnected = connected;
    connectionStateInitialized = true;
  });
  const clock = setInterval(() => { secondsNow = Date.now(); }, 1000);

  onDestroy(() => {
    unsubscribe();
    unsubscribeConnection();
    clearInterval(clock);
    if (refreshTimer !== null) clearTimeout(refreshTimer);
    generation += 1;
  });
</script>

{#if loading}
  <div class={passiveClass} data-presentation={presentation} data-testid="pending-interactions-loading">
    Loading pending requests…
  </div>
{:else if error && pending.length === 0}
  <div
    class={floating
      ? 'pointer-events-auto rounded-2xl border border-rose-700/70 bg-slate-950 px-4 py-3 text-xs text-rose-200 shadow-2xl'
      : 'shrink-0 border-t border-rose-900/60 bg-rose-950/30 px-4 py-3 text-xs text-rose-200'}
    data-presentation={presentation}
    role="alert"
  >
    {error}
    <button class="ml-2 underline" type="button" onclick={() => void refresh()}>Retry</button>
  </div>
{:else if pending.length > 0}
  <section
    class={floating
      ? 'pointer-events-auto max-h-full space-y-3 overflow-y-auto overscroll-contain rounded-2xl border border-slate-700 bg-slate-950 p-3 shadow-2xl'
      : 'max-h-[45%] shrink-0 space-y-3 overflow-y-auto border-t border-slate-800 bg-slate-950/90 p-3'}
    aria-label="Pending conversation requests"
    data-presentation={presentation}
    data-testid="pending-conversation-interactions"
  >
    {#if error}<p class="text-xs text-rose-200" role="alert">{error}</p>{/if}
    {#if escalation && escalationNotification}
      <EscalationPrompt
        item={escalation}
        secondsRemaining={Math.max(0, Math.ceil(((escalation.received_at ?? secondsNow) + (escalation.timeout_seconds ?? 300) * 1000 - secondsNow) / 1000))}
        pending={busyId === escalationNotification.notification_id}
        queuedCount={Math.max(0, pending.filter((item) => item.notification_type === 'escalation').length - 1)}
        onApprove={(note) => resolve(escalationNotification, { decision: 'approve', note })}
        onDeny={(note) => resolve(escalationNotification, { decision: 'deny', note })}
      />
    {/if}
    {#if credentialRequest}
      {#key credentialRequest.notification_id}
        <CredentialRequestForm notification={credentialRequest} compact onResolved={refresh} />
      {/key}
    {/if}
    {#if inputPause && inputRequest}
      {#key inputRequest.notification_id}
        {#if isOAuthAuthorization}
          <article class="rounded-2xl border border-amber-400/30 bg-amber-400/10 px-4 py-3 text-sm text-amber-50">
            <p class="text-xs font-semibold uppercase tracking-[0.2em] text-amber-200">Authorization required</p>
            <p class="mt-2">{typeof inputRequest.payload.message === 'string' ? inputRequest.payload.message : 'Complete authorization in the provider window.'}</p>
            <div class="mt-3 flex flex-wrap gap-2">
              {#if oauthAuthorization?.callbackMode === 'executor_loopback'}
                <span class="w-full text-xs text-amber-100">Open this URL on executor {oauthAuthorization.executorName ?? 'selected executor'}:</span>
                <code class="w-full break-all rounded-xl bg-slate-950/50 px-3 py-2">{oauthAuthorization.url}</code>
              {:else if oauthAuthorization}
                <a class="rounded-xl bg-amber-300 px-3 py-2 font-medium text-slate-950" href={oauthAuthorization.url} target="_blank" rel="noreferrer">Open authorization</a>
              {/if}
              {#if oauthAuthorization?.userCode}
                <span class="rounded-xl border border-amber-300/40 px-3 py-2">Code: <code>{oauthAuthorization.userCode}</code></span>
              {/if}
              <button class="rounded-xl border border-amber-300/40 px-3 py-2" type="button" disabled={busyId === inputRequest.notification_id} onclick={() => void resolve(inputRequest, { decision: 'cancel' })}>Cancel</button>
            </div>
          </article>
        {:else}
          <AttentionPanel
            pause={inputPause}
            compact
            busy={busyId === inputRequest.notification_id}
            onGate={(action, instruction) => resolve(inputRequest, { decision: action, feedback: instruction })}
            onQuestion={answerQuestions}
          />
        {/if}
      {/key}
    {/if}
  </section>
{/if}
