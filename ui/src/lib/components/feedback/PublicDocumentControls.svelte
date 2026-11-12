<script lang="ts">
  import { onDestroy } from 'svelte';
  import DeliverableAudio from '../DeliverableAudio.svelte';
  import DownloadMenu from '../rich/DownloadMenu.svelte';
  import type { PublicFeedbackClient, PublicFeedbackContent } from '$lib/feedback/public-api';

  let { client, contentRef, content, guard }: {
    client: PublicFeedbackClient; contentRef: string; content: PublicFeedbackContent;
    guard: { assertCurrent: () => void };
  } = $props();
  const audioClient = $derived(client.audio());
  const formats = $derived(contentRef.startsWith('dlv_') ? ['pdf', 'html', 'markdown'] : ['original']);
  let error = $state('');
  let busy = false;
  let disposed = false;
  const urls = new Map<string, ReturnType<typeof setTimeout>>();
  onDestroy(() => {
    disposed = true;
    for (const [url, timer] of urls) { clearTimeout(timer); URL.revokeObjectURL(url); }
  });
  async function download(format: string) {
    if (busy) return;
    busy = true; error = '';
    try {
      guard.assertCurrent();
      const blob = await client.download(format);
      if (disposed) return;
      guard.assertCurrent();
      const url = URL.createObjectURL(blob);
      urls.set(url, setTimeout(() => { URL.revokeObjectURL(url); urls.delete(url); }, 60_000));
      const anchor = document.createElement('a');
      anchor.href = url;
      const extension = format === 'markdown' ? 'md' : format;
      anchor.download = format === 'original' ? content.filename : `${content.filename}.${extension}`;
      document.body.append(anchor); anchor.click(); anchor.remove();
    } catch (caught) {
      if (!disposed) error = caught instanceof Error ? caught.message : 'Download failed.';
    } finally { busy = false; }
  }
</script>
<DeliverableAudio sourceRef={contentRef} client={audioClient} />
<DownloadMenu {formats} onDownload={download} />
{#if error}<span role="alert" class="text-xs text-danger">{error}</span>{/if}
