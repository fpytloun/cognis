import type { Conversation, MessageMetadata } from '$lib/types/api';

export interface ManagedChannelIdentity {
  channelType: string;
  channelLabel: string;
  participant: string;
}

export function managedChannelIdentity(
  conversation: Pick<Conversation, 'context' | 'managed_agent'> | null | undefined,
): ManagedChannelIdentity | null {
  const metadata = conversation?.context?.platform_data;
  if (conversation?.managed_agent?.kind !== 'channel' &&
      metadata?.managed_conversation_kind !== 'channel') return null;
  const channelType = typeof metadata?.channel_type === 'string' ? metadata.channel_type : '';
  const participant = typeof metadata?.participant === 'string' ? metadata.participant : '';
  const label = channelType.replace(/[-_]/g, ' ');
  return {
    channelType,
    channelLabel: label ? label[0].toUpperCase() + label.slice(1) : 'Channel',
    participant: participant.trim() || 'Remote participant',
  };
}

export function remoteMessageSender(metadata?: MessageMetadata | null): string | null {
  if (!metadata?.untrusted || metadata.channel === 'agent_work') return null;
  return metadata.sender?.trim() || null;
}

export function isPrivateInstruction(metadata?: MessageMetadata | null): boolean {
  return metadata?.private_controller_instruction === true ||
    (metadata?.channel === 'agent_work' && !metadata.untrusted);
}
