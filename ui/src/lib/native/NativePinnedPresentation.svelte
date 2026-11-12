<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { auth } from '$lib/stores/auth';
  import { nativeVisibility, nativeComposerFocus, postNative } from './host';
  import { createPinnedPanel } from '$lib/components/chat/pinned-panel.svelte';
  import PinnedConversationPanel from '$lib/components/chat/PinnedConversationPanel.svelte';

  const owner = $auth.user?.email;
  const current = () => $auth.status === 'authenticated'
    && $auth.user?.email === owner && !$page.url.searchParams.has('direct');
  const dismiss = () => { if (current()) postNative('dismiss', {}); };
  const panel = createPinnedPanel({
    visible: () => current() && $nativeVisibility.visible && $nativeVisibility.focused,
    projectId: () => null,
    onClose: dismiss,
  });
  let opened = $state('');
  $effect(() => {
    if (current()) postNative('panel-state', { conversationId: panel.selectedId, agentId: panel.group?.id ?? null });
  });
  $effect(() => {
    const agentId = $page.url.searchParams.get('agent');
    const conversationId = $page.url.searchParams.get('conversation');
    const group = panel.groups.find((item) => item.id === agentId);
    const key = `${agentId}:${conversationId}`;
    if (!current() || !group || key === opened) return;
    opened = key;
    const entry = group.entries.find((item) =>
      (item.window.resolvedConversationId ?? item.window.entityId) === conversationId);
    if (entry) void panel.select(entry); else void panel.openAgent(group.id);
  });
  onMount(() => {
    document.addEventListener('visibilitychange', panel.observe);
    return () => document.removeEventListener('visibilitychange', panel.observe);
  });
</script>

<PinnedConversationPanel {panel} onMinimize={panel.close}
  composerFocusRequest={current() && $nativeVisibility.visible && $nativeVisibility.focused
    && $nativeComposerFocus?.agentId === panel.group?.id ? $nativeComposerFocus?.request : null}
  onOpenFull={(path) => postNative('open-window', { conversationId: new URL(path, location.origin).pathname.split('/').pop() })} />
