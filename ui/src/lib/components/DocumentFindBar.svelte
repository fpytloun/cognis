<script lang="ts">
  import { createDocumentFind, documentMatches, type DocumentFindAdapter } from '$lib/document-find';
  import FindBar from './FindBar.svelte';

  let { content, adapter = null, onclose }: {
    content: HTMLElement | undefined; adapter?: DocumentFindAdapter | null; onclose(): void;
  } = $props();
  let input = $state<HTMLInputElement | null>(null);
  let query = $state(''), total = $state(0), index = $state(-1);
  let busy = $state(false), error = $state('');
  let local: DocumentFindAdapter | null = null;
  let generation = 0;
  const current = () => adapter ?? (content ? local ??= createDocumentFind(content, documentMatches) : null);
  export function getInput() { return input ?? null; }
  function close() {
    onclose();
  }
  async function search() {
    const version = ++generation;
    total = 0; index = -1; error = ''; busy = true;
    try {
      const target = current(), count = await target?.search(query) ?? 0;
      if (version !== generation) return;
      total = count; index = count ? 0 : -1;
      if (count) await target?.select(0);
    } catch { if (version === generation) error = 'Could not search this document. Try again.'; }
    finally { if (version === generation) busy = false; }
  }
  async function move(delta: number) {
    if (!total || busy) return;
    index = (index + delta + total) % total;
    const version = ++generation;
    busy = true;
    try { await current()?.select(index); }
    catch { if (version === generation) error = 'Could not show this match. Try again.'; }
    finally { if (version === generation) busy = false; }
  }
  $effect(() => {
    const target = adapter;
    query = ''; total = 0; index = -1; busy = false; error = '';
    return () => { generation++; local?.clear(); local = null; target?.clear(); };
  });
</script>

<FindBar bind:query bind:inputElement={input} label="Find in document"
  {error} count={busy ? 'Searching…' : query ? total ? `${index + 1} of ${total}` : 'No matches' : ''}
  navigationDisabled={!total || busy} onInput={() => void search()} onSubmit={() => void move(1)}
  onPrevious={() => void move(-1)} onNext={() => void move(1)} onClose={close} />
