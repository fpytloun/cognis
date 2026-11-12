import type { UsageGroup } from './model';

/** Requests made by Cognis itself and its companion services, without an agent. */
export const SYSTEM_AGENT_LABEL = 'Cognis system';

const SERVICE: Record<string, string> = { intaris: 'Intaris', mnemory: 'Mnemory', cognis: 'Cognis' };

/** Readable names for the request purposes of system work. Unknown purposes keep their key. */
const PURPOSE: Record<string, string> = {
  systemone: 'Guardrail checks',
  l3: 'Guardrail reviews',
  judge: 'Guardrail judgements',
  alignment: 'Intention alignment',
  intention: 'Session intentions',
  summary_window: 'Session summaries',
  compaction: 'Context compaction',
  embeddings: 'Embeddings',
  embedding: 'Embeddings',
  chat: 'Memory extraction',
  provider_test: 'Provider checks',
  auxiliary_generation: 'Titles and recaps',
  attachment_analysis: 'Attachment analysis',
  audio_narrative: 'Audio narration',
  image_generation: 'Image generation',
  image_edit: 'Image editing',
  speech_to_text: 'Speech to text',
  text_to_speech: 'Text to speech',
  video: 'Video generation',
  video_extend: 'Video extension',
  source_context: 'Source context',
  policy_compile: 'Policy compilation'
};

export function serviceLabel(id: string | null): string {
  return id ? SERVICE[id] ?? id : 'Cognis';
}

export function purposeLabel(id: string | null): string {
  return id ? PURPOSE[id] ?? id.replaceAll('_', ' ') : 'Other';
}

export interface SystemFunction { key: string; label: string; services: string; group: UsageGroup }

/** One row per purpose, named for people, with the services that made those requests. */
export function systemFunctions(purposes: UsageGroup[], byService: Map<string, string[]>): SystemFunction[] {
  return purposes.map((group) => ({
    key: group.id ?? '',
    label: purposeLabel(group.id),
    services: (byService.get(group.id ?? '') ?? []).map(serviceLabel).join(', '),
    group
  }));
}
