<script lang="ts">
  import Mic from 'lucide-svelte/icons/mic';
  import Pause from 'lucide-svelte/icons/pause';
  import Play from 'lucide-svelte/icons/play';
  import X from 'lucide-svelte/icons/x';

  import type { AttachmentRef } from '$lib/types/api';

  /**
   * Composer attachment preview strip.
   *
   * Renders image attachments as small thumbnails (familiar messaging-app
   * affordance), audio recordings as inline mini-players, and everything
   * else as a filename chip. Every item has an overlay remove button so
   * the user can drop an attachment before sending. Reused by the inline
   * and expanded composer to keep previews consistent.
   */

  interface Props {
    attachments: AttachmentRef[];
    onremove: (artifactId: string) => void;
    disabled?: boolean;
    class?: string;
  }

  let { attachments, onremove, disabled = false, class: className = '' }: Props = $props();

  let playingId = $state<string | null>(null);
  const audioElements = new Map<string, HTMLAudioElement>();

  function isImage(attachment: AttachmentRef): boolean {
    return typeof attachment.mime_type === 'string' && attachment.mime_type.startsWith('image/');
  }

  function isAudio(attachment: AttachmentRef): boolean {
    if (attachment.voice_recording) return true;
    return typeof attachment.mime_type === 'string' && attachment.mime_type.startsWith('audio/');
  }

  function audioUrl(attachment: AttachmentRef): string | null {
    return attachment.blob_url ?? attachment.url ?? null;
  }

  function formatDuration(seconds: number | null | undefined): string {
    if (!seconds || !Number.isFinite(seconds)) return '0:00';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds) % 60;
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  function togglePlay(attachment: AttachmentRef): void {
    const id = attachment.artifact_id;
    let audio = audioElements.get(id);
    if (!audio) {
      const url = audioUrl(attachment);
      if (!url) return;
      audio = new Audio(url);
      audio.onended = () => {
        if (playingId === id) playingId = null;
      };
      audioElements.set(id, audio);
    }
    if (playingId === id) {
      audio.pause();
      playingId = null;
      return;
    }
    audioElements.forEach((other, key) => {
      if (key !== id) other.pause();
    });
    void audio.play().then(() => {
      playingId = id;
    });
  }
</script>

{#if attachments.length > 0}
  <div class={`flex flex-wrap gap-2 ${className}`}>
    {#each attachments as attachment (attachment.artifact_id)}
      {#if isImage(attachment) && attachment.url}
        <div class="group relative h-20 w-20 overflow-hidden rounded-xl border border-slate-700 bg-slate-900">
          <img
            alt={attachment.filename}
            class="h-full w-full object-cover"
            loading="lazy"
            src={attachment.url}
          />
          <button
            aria-label={`Remove ${attachment.filename}`}
            class="absolute right-1 top-1 inline-flex h-6 w-6 items-center justify-center rounded-full bg-slate-950/85 text-slate-100 shadow-md transition hover:bg-rose-500 disabled:cursor-not-allowed disabled:opacity-50"
            {disabled}
            onclick={() => onremove(attachment.artifact_id)}
            type="button"
          >
            <X class="h-3.5 w-3.5" />
          </button>
        </div>
      {:else if isAudio(attachment)}
        <div class="flex items-center gap-2 rounded-full border border-slate-700 bg-slate-950/70 px-3 py-1.5 text-xs text-slate-200">
          <button
            aria-label={playingId === attachment.artifact_id ? 'Pause recording' : 'Play recording'}
            class="inline-flex h-7 w-7 items-center justify-center rounded-full bg-slate-800 text-slate-100 transition hover:bg-slate-700 disabled:opacity-50"
            type="button"
            onclick={() => togglePlay(attachment)}
            disabled={!audioUrl(attachment)}
          >
            {#if playingId === attachment.artifact_id}
              <Pause class="h-3.5 w-3.5" />
            {:else}
              <Play class="h-3.5 w-3.5" />
            {/if}
          </button>
          <Mic class="h-3.5 w-3.5 text-rose-300" />
          <span class="font-medium tabular-nums">{formatDuration(attachment.duration_seconds)}</span>
          {#if attachment.voice_recording}
            <span class="text-[10px] uppercase tracking-wider text-slate-400">voice</span>
          {/if}
          <button
            aria-label={`Remove ${attachment.filename}`}
            class="text-slate-400 transition hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
            {disabled}
            onclick={() => onremove(attachment.artifact_id)}
            type="button"
          >
            <X class="h-3.5 w-3.5" />
          </button>
        </div>
      {:else}
        <div class="flex items-center gap-2 rounded-full border border-slate-700 bg-slate-950/70 px-3 py-2 text-xs text-slate-200">
          <span class="max-w-[220px] truncate">{attachment.filename}</span>
          <button
            aria-label={`Remove ${attachment.filename}`}
            class="text-slate-400 transition hover:text-white disabled:cursor-not-allowed disabled:opacity-50"
            {disabled}
            onclick={() => onremove(attachment.artifact_id)}
            type="button"
          >
            <X class="h-3.5 w-3.5" />
          </button>
        </div>
      {/if}
    {/each}
  </div>
{/if}
