<script lang="ts">
  import { api } from '$lib/api/client';
  import type { AppContactView, AppContactAttachment } from '$lib/app-contacts';
  import type { Agent } from '$lib/types/api';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import ChatV2Composer from '$lib/components/chat-v2/ChatV2Composer.svelte';
  import SessionLogHeader from '$lib/components/chat-v2/SessionLogHeader.svelte';
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import Menu from 'lucide-svelte/icons/menu';
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import { renderMarkdown } from '$lib/markdown';
  import { confirmAction } from '$lib/stores/confirm';
  import { tick } from 'svelte';

  let { contactId, standalone = false, agent = null, onOpenSidebar, onInitialLoaded }: {
    contactId: string; standalone?: boolean; agent?: Agent | null; onOpenSidebar?: () => void;
    onInitialLoaded?: () => void;
  } = $props();

  let contact = $state<AppContactView | null>(null);
  let error = $state('');
  let loadError = $state('');
  let draft = $state('');
  let busy = $state(false);
  let editingTitle = $state(false);
  let actionsOpen = $state(false);
  let titleDraft = $state('');
  let viewport = $state<HTMLDivElement | null>(null);
  let readSequence = 0;
  let lastAttempt: { content: string; id: string } | null = null;

  $effect(() => {
    const id = contactId;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let initialLoaded = false;
    contact = null;
    draft = '';
    lastAttempt = null;
    error = '';
    loadError = '';
    editingTitle = false;
    actionsOpen = false;
    busy = false;
    async function refresh(): Promise<void> {
      const sequence = ++readSequence;
      try {
        const next = await api.appContacts.get(id, { signal: abort.signal });
        if (!abort.signal.aborted && sequence === readSequence) {
          const following = !viewport || viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop < 72;
          const changed = next.messages.at(-1)?.id !== contact?.messages.at(-1)?.id;
          contact = next;
          loadError = '';
          if (!initialLoaded) {
            initialLoaded = true;
            onInitialLoaded?.();
          }
          await tick();
          if (!abort.signal.aborted && following && changed && viewport) viewport.scrollTop = viewport.scrollHeight;
        }
      } catch (cause) {
        if (!abort.signal.aborted && sequence === readSequence) loadError = cause instanceof Error ? cause.message : 'Could not load this conversation. Try refreshing.';
      } finally {
        if (!abort.signal.aborted) timer = setTimeout(() => void refresh(), 5000);
      }
    }
    void refresh();
    return () => { abort.abort(); clearTimeout(timer); };
  });

  async function send(): Promise<void> {
    if (busy || !contact?.can_reply || !draft.trim() || draft.length > 32000) return;
    const id = contactId;
    const content = draft;
    if (lastAttempt?.content !== content) lastAttempt = { content, id: crypto.randomUUID() };
    busy = true;
    readSequence++;
    error = '';
    try {
      await api.appContacts.reply(id, content, lastAttempt.id);
      if (id !== contactId) return;
      draft = '';
      lastAttempt = null;
      const sequence = ++readSequence;
      const next = await api.appContacts.get(id);
      if (id === contactId && sequence === readSequence) {
        contact = next;
        await tick();
        if (viewport) viewport.scrollTop = viewport.scrollHeight;
      }
    } catch (cause) {
      if (id === contactId) error = cause instanceof Error ? cause.message : 'Reply was not confirmed. Retry to check the same message safely.';
    } finally {
      if (id === contactId) busy = false;
    }
  }

  async function end(block: boolean): Promise<void> {
    if (busy) return;
    actionsOpen = false;
    const id = contactId;
    if (!await confirmAction({
      title: block ? 'Block sender?' : 'End conversation?',
      message: block
        ? 'Stop this exchange and future contact from this sender across their agents. Your visible history is kept.'
        : 'Stop replies and agent activity for this exchange. Your visible history is kept.',
      confirmLabel: block ? 'Block sender' : 'End conversation',
    }) || id !== contactId) return;
    busy = true;
    error = '';
    try {
      await api.appContacts.end(id, block);
      const sequence = ++readSequence;
      const next = await api.appContacts.get(id);
      if (id === contactId && sequence === readSequence) contact = next;
    } catch (cause) {
      if (id === contactId) error = cause instanceof Error ? cause.message : 'Could not end this contact. Try again.';
    } finally {
      if (id === contactId) busy = false;
    }
  }

  async function rename(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    const conversationId = contact?.conversation_id;
    const title = titleDraft.trim();
    if (!conversationId || !title || busy) return;
    const id = contactId;
    busy = true;
    error = '';
    readSequence++;
    try {
      await api.conversations.update(conversationId, { title });
      if (id === contactId && contact) {
        readSequence++;
        contact = { ...contact, title };
        editingTitle = false;
      }
    } catch (cause) {
      if (id === contactId) error = cause instanceof Error ? cause.message : 'Could not rename this conversation.';
    } finally {
      if (id === contactId) busy = false;
    }
  }

  async function download(attachment: AppContactAttachment): Promise<void> {
    try {
      const { url } = await api.appContacts.attachment(attachment.href);
      window.location.assign(url);
    } catch (cause) {
      error = cause instanceof Error ? cause.message : 'This attachment is no longer available.';
    }
  }
</script>

<svelte:head><title>{contact?.title ?? 'Conversation'} · Cognis</title></svelte:head>
<div class="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden" data-testid="app-contact-chat">
  <SessionLogHeader title={contact?.title ?? 'Conversation'} testId="app-contact-header">
    {#snippet leading()}
      {#if onOpenSidebar}<button type="button" class="touch-target-compact inline-flex items-center justify-center rounded-lg text-muted hover:bg-surface-hover sm:hidden" aria-label="Open conversations" onclick={onOpenSidebar}><Menu class="h-5 w-5" /></button>{/if}
      {#if standalone}<a href="/notifications" class="text-sm text-accent hover:underline">Back to inbox</a>{/if}
    {/snippet}
    {#snippet metadata()}
      {#if contact}
        <div class="flex min-w-0 items-center gap-1.5">
          <AgentAvatar name={contact.agent_name} avatarUrl={agent?.avatar_url} class="h-4 w-4 shrink-0" />
          <span class="truncate">{contact.agent_name} · On behalf of {contact.sender_email}</span>
        </div>
      {/if}
    {/snippet}
    {#snippet actions()}
      {#if contact}
        <details class="relative" bind:open={actionsOpen}>
          <summary class="touch-target-compact inline-flex cursor-pointer list-none items-center justify-center rounded-lg border border-control-border text-muted hover:bg-surface-hover" aria-label="Conversation actions"><ChevronDown class="h-4 w-4" /></summary>
          <div class="absolute right-0 z-30 mt-2 w-56 rounded-xl border border-control-border bg-surface p-2 shadow-card">
            {#if contact.conversation_id}<button type="button" disabled={busy} class="min-h-11 w-full rounded-lg px-3 text-left text-sm hover:bg-surface-hover" onclick={() => { titleDraft = contact?.title ?? ''; actionsOpen = false; editingTitle = true; }}>Rename conversation</button>{/if}
            {#if contact.can_reply}<button type="button" disabled={busy} class="min-h-11 w-full rounded-lg px-3 text-left text-sm hover:bg-surface-hover" onclick={() => void end(false)}>End conversation</button>{/if}
            {#if !contact.blocked}<button type="button" disabled={busy} class="min-h-11 w-full rounded-lg px-3 text-left text-sm text-danger hover:bg-surface-hover" onclick={() => void end(true)}>Block sender</button>{/if}
          </div>
        </details>
      {/if}
    {/snippet}
  </SessionLogHeader>
  {#if editingTitle}
    <form class="flex shrink-0 flex-wrap items-center gap-2 border-b border-separator p-3" onsubmit={rename}>
      <label class="sr-only" for="app-contact-title">Conversation title</label>
      <input id="app-contact-title" aria-label="Conversation title" bind:value={titleDraft} maxlength="200" disabled={busy} class="min-h-11 min-w-0 flex-1 rounded-lg border border-control-border bg-surface px-3" />
      <button type="submit" disabled={busy || !titleDraft.trim()} class="min-h-11 rounded-lg px-3 text-accent">Save</button>
      <button type="button" onclick={() => { editingTitle = false; }} class="min-h-11 rounded-lg px-3 text-muted">Cancel</button>
    </form>
  {/if}
  {#if error || loadError}<p class="shrink-0 px-4 py-2 text-sm text-danger" role="alert">{error || loadError}</p>{/if}
  <div bind:this={viewport} class="min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 py-4 sm:px-4" data-testid="app-contact-transcript">
  {#if contact}
    <ol class="mx-auto w-full max-w-4xl space-y-5" aria-label="Conversation messages">
      {#each contact.messages as message (message.id)}
        <li>
          <ChatMessage {agent} item={{
            id: message.id, kind: 'message', role: message.direction === 'outgoing' ? 'user' : 'assistant',
            content: message.content, html: renderMarkdown(message.content), seq: null,
            timestamp: message.occurred_at, runtime: { agent_name: contact.agent_name },
          }} />
          {#each message.attachments as attachment (attachment.artifact_id)}
            <button type="button" class="mt-2 min-h-11 break-all text-left text-sm text-accent hover:underline" onclick={() => void download(attachment)}>Download {attachment.filename}</button>
          {/each}
        </li>
      {/each}
    </ol>
  {:else if !loadError}
    <p class="text-sm text-muted" role="status">Loading conversation…</p>
  {/if}
  </div>
  {#if contact}
    <footer class="shrink-0 border-t border-separator px-3 pt-3 sm:px-4" style="padding-bottom: max(0.75rem, var(--app-bottom-control-inset, 0px));">
      <div class="mx-auto w-full max-w-4xl">
        {#if contact.can_reply}
          <ChatV2Composer bind:value={draft} {busy} maxLength={32000} placeholder={`Message ${contact.agent_name}`} ariaLabel="Message" textareaTestId="app-contact-message" showMic={false} allowQueue={false} allowSlashCommands={false} onSend={send} />
          {#if draft.length > 32000}<p role="alert" class="mt-1 text-xs text-danger">Messages must be at most 32,000 characters.</p>{/if}
        {:else}
          <p class="py-2 text-sm text-muted" role="status">{contact.expires_at ? `This exchange is ${contact.blocked ? 'blocked' : 'not accepting replies'}.` : 'One-way message. This message has no return route.'}</p>
        {/if}
        <p class="mt-2 text-center text-xs text-muted">Sender’s workspace · Only messages sent here are shared.</p>
      </div>
    </footer>
  {/if}
</div>
