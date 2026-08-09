export interface RootOverviewRequestToken {
  generation: number;
  rootConversationId: string;
}

export class RootOverviewRequestEpoch {
  #generation = 0;

  begin(rootConversationId: string): RootOverviewRequestToken {
    return {
      generation: ++this.#generation,
      rootConversationId,
    };
  }

  isCurrent(token: RootOverviewRequestToken, currentRootConversationId: string | null): boolean {
    return token.generation === this.#generation
      && token.rootConversationId === currentRootConversationId;
  }
}

export async function promoteRootOverview<T>(
  epoch: RootOverviewRequestEpoch,
  token: RootOverviewRequestToken,
  currentRootConversationId: () => string | null,
  load: () => Promise<T>,
  promote: (value: T) => void,
): Promise<boolean> {
  const value = await load();
  if (!epoch.isCurrent(token, currentRootConversationId())) return false;
  promote(value);
  return true;
}
