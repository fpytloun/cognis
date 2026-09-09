let nextMountId = 0;
let activeSubscriptions = 0;

export function resetConversationWorkspaceFixture(): void {
  nextMountId = 0;
  activeSubscriptions = 0;
}

export function conversationWorkspaceSubscriptionCount(): number {
  return activeSubscriptions;
}

export function mountConversationWorkspaceFixture(): {
  mountId: number;
  destroy(): void;
} {
  nextMountId += 1;
  activeSubscriptions += 1;
  let active = true;
  return {
    mountId: nextMountId,
    destroy(): void {
      if (!active) return;
      active = false;
      activeSubscriptions -= 1;
    },
  };
}
