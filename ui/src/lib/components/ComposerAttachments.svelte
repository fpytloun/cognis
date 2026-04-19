<script lang="ts">
  import X from 'lucide-svelte/icons/x';

  import type { AttachmentRef } from '$lib/types/api';

  /**
   * Composer attachment preview strip.
   *
   * Renders image attachments as small thumbnails (familiar messaging-app
   * affordance) and other files as a filename chip. Every item has an
   * overlay remove button so the user can drop an attachment before
   * sending. Reused by the inline and expanded composer to keep previews
   * consistent.
   */

  interface Props {
    attachments: AttachmentRef[];
    onremove: (artifactId: string) => void;
    disabled?: boolean;
    class?: string;
  }

  let { attachments, onremove, disabled = false, class: className = '' }: Props = $props();

  function isImage(attachment: AttachmentRef): boolean {
    return typeof attachment.mime_type === 'string' && attachment.mime_type.startsWith('image/');
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
