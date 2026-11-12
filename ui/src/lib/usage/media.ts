import type { MediaQuantity } from './wire';

const OPERATIONS: Record<string, string> = {
  image_generate: 'Image generation',
  image_edit: 'Image editing',
  transcribe: 'Speech to text',
  synthesize: 'Text to speech',
  video_generate: 'Video generation',
  video_extend: 'Video extension'
};

export function operationLabel(operation: string | null): string {
  return operation ? OPERATIONS[operation] ?? operation : 'Unknown';
}

/** Media duration is stored in milliseconds, never provider-call wall time. */
export function mediaQuantity(quantity: MediaQuantity): string {
  const number = new Intl.NumberFormat('en', { maximumFractionDigits: 2 });
  let value = quantity.value;
  let unit: string = quantity.unit ?? 'tokens';
  if (unit === 'milliseconds') {
    const video = quantity.name.includes('video');
    value /= video ? 1000 : 60000;
    unit = video ? 'video seconds' : 'audio minutes';
  } else if (unit === 'images' && value === 1) {
    unit = 'image';
  } else if (unit === 'tokens') {
    unit = quantity.name.replaceAll('_', ' ');
  }
  const evidence = quantity.status === 'reported' ? '' : ` (${quantity.status})`;
  return `${number.format(value)} ${unit}${evidence}`;
}
