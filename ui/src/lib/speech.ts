import type { TtsSynthesizeResponse } from '$lib/types/api';

export interface SpeechCapabilities {
  tts: boolean;
  stt: boolean;
  streaming_tts: boolean;
  streaming_stt: boolean;
  stt_sample_rate: number;
  detail: string | null;
}

export type SpeechStreamEvent =
  | { type: 'audio_start'; part_index: number; sample_rate: number }
  | { type: 'audio'; part_index: number; sample_rate: number; sample_offset: number; audio_base64: string }
  | { type: 'audio_done'; part_index: number }
  | { type: 'part'; part_index: number; streamed: boolean; part: { audio_url: string } }
  | { type: 'complete'; response: TtsSynthesizeResponse }
  | { type: 'error'; code: string };
