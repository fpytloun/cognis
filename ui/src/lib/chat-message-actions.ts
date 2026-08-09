export const forkAssistantMessageContext = Symbol('fork-assistant-message');

export type ForkAssistantMessage = (
  sourceSessionId: string,
  sourceSeq: number
) => Promise<void>;
