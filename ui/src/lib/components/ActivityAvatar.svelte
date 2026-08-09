<script lang="ts">
  import AgentAvatar from '$lib/components/AgentAvatar.svelte';
  import { cn } from '$lib/utils';
  import type { ActivityAvatarState } from '$lib/conversation-activity';

  let {
    name,
    avatarUrl = null,
    active = false,
    turnInProgress = false,
    background = false,
    attention = false,
    unread = false,
    error = false,
    attentionLabel = 'New activity',
    class: className = 'h-8 w-8',
    state = null,
  } = $props<{
    name: string;
    avatarUrl?: string | null;
    active?: boolean;
    turnInProgress?: boolean;
    background?: boolean;
    attention?: boolean;
    unread?: boolean;
    error?: boolean;
    attentionLabel?: string;
    class?: string;
    state?: ActivityAvatarState | null;
  }>();

  const effective = $derived(state ?? {
    active: active || turnInProgress,
    background,
    attention,
    unread,
    error,
    tone: error ? 'rose' : attention ? 'amber' : 'default',
    label: attentionLabel,
  });
</script>

<span
  class={cn('relative grid shrink-0 place-items-center', className, 'rounded-full')}
  data-testid="activity-avatar"
  data-avatar-shape="circle"
  aria-label={effective.label}
>
  {#if effective.active || effective.background}
    <span
      class={`activity-orbit ${effective.background ? 'activity-orbit--background' : ''} ${effective.tone === 'rose' ? 'activity-orbit--rose' : effective.tone === 'amber' ? 'activity-orbit--amber' : ''}`}
      data-testid="activity-avatar-orbit"
      data-avatar-shape="circle"
      aria-hidden="true"
    ></span>
  {/if}
  <AgentAvatar {name} {avatarUrl} class="h-full w-full rounded-full" />
  {#if effective.attention || effective.unread || effective.error}
    <span
      class={`absolute right-0 top-0 h-2.5 w-2.5 rounded-full border-2 border-slate-950 ${effective.error ? 'bg-rose-400' : effective.attention ? 'bg-amber-400' : 'bg-sky-400'}`}
      title={effective.label}
      aria-hidden="true"
      data-testid={effective.error ? 'activity-avatar-error' : effective.attention ? 'activity-avatar-attention' : 'activity-avatar-unread'}
    ></span>
  {/if}
</span>

<style>
  .activity-orbit {
    position: absolute;
    inset: -0.2rem;
    border: 2px solid rgb(56 189 248 / 0.25);
    border-top-color: rgb(56 189 248);
    border-radius: 9999px;
    animation: activity-spin 0.9s linear infinite;
  }
  .activity-orbit--background {
    border-color: rgb(167 139 250 / 0.25);
    border-top-color: rgb(167 139 250);
  }
  .activity-orbit--rose {
    border-color: rgb(251 113 133 / 0.25);
    border-top-color: rgb(251 113 133);
  }
  .activity-orbit--amber {
    border-color: rgb(251 191 36 / 0.25);
    border-top-color: rgb(251 191 36);
  }
  @media (prefers-reduced-motion: reduce) {
    .activity-orbit { animation: none; }
  }
  @keyframes activity-spin {
    to { transform: rotate(360deg); }
  }
</style>
