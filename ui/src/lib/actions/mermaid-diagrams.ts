import { mount, tick, unmount } from 'svelte';

/** Enhance completed Markdown fences without importing Mermaid for ordinary messages. */
export function mermaidDiagrams(root: HTMLElement, options: { html: string; streaming?: boolean }) {
  let generation = 0;
  let disposed = false;
  const viewers = new Map<HTMLElement, ReturnType<typeof mount>>();

  async function sync(next: typeof options) {
    const current = ++generation;
    await tick();
    for (const [host, viewer] of viewers) {
      if (!root.contains(host)) {
        void unmount(viewer);
        viewers.delete(host);
      }
    }
    if (disposed || next.streaming) return;
    const blocks = Array.from(root.querySelectorAll<HTMLElement>('pre:has(> code.language-mermaid)'));
    if (!blocks.length) return;
    const { default: MermaidViewer } = await import('$lib/components/MermaidViewer.svelte');
    if (disposed || current !== generation) return;
    for (const block of blocks) {
      if (!root.contains(block)) continue;
      const source = block.querySelector('code')?.textContent ?? '';
      const target = block.parentElement?.classList.contains('chat-code-wrap') ? block.parentElement : block;
      const host = document.createElement('div');
      host.className = 'not-prose';
      target.replaceWith(host);
      viewers.set(host, mount(MermaidViewer, { target: host, props: { source } }));
    }
  }

  void sync(options);
  return {
    update(next: typeof options) { void sync(next); },
    destroy() {
      disposed = true;
      generation++;
      for (const viewer of viewers.values()) void unmount(viewer);
      viewers.clear();
    },
  };
}
