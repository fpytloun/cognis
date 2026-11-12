export interface DocumentFindAdapter {
  search(query: string): number | Promise<number>;
  select(index: number): void | Promise<void>;
  clear(): void;
}

/** Literal, case-insensitive matches retain offsets in the original UTF-16 text. */
export function documentMatches(text: string, query: string): { start: number; end: number }[] {
  if (!query) return [];
  const expression = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu');
  return [...text.matchAll(expression)].map(match => ({ start: match.index, end: match.index + match[0].length }));
}

/** Non-mutating ranges keep source annotations and syntax highlighting intact. */
export function createDocumentFind(root: HTMLElement, matchText: typeof documentMatches, joinTextRuns = false): DocumentFindAdapter {
  let ranges: Range[] = [];
  const name = `cognis-find-${crypto.randomUUID()}`;
  const currentName = `${name}-current`;
  const style = document.createElement('style');
  style.textContent = `::highlight(${name}) { background: rgb(var(--color-warning, 234 179 8) / .3); color: inherit; }
    ::highlight(${currentName}) { background: rgb(var(--color-accent, 59 130 246) / .3); color: inherit; }`;
  root.append(style);
  function clear() {
    CSS.highlights.delete(name); CSS.highlights.delete(currentName); ranges = [];
  }
  return {
    search(query) {
      clear();
      if (!query) return 0;
      if (!style.isConnected) root.append(style);
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      const groups: { block: Element; nodes: Text[]; text: string }[] = [];
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const parent = node.parentElement;
        if (!parent || parent.closest('script,style,button,input,textarea,select,[hidden],[inert],[aria-hidden="true"],[data-find-ignore],[contenteditable="true"],[contenteditable="plaintext-only"]')) continue;
        let hidden = false, block: Element = root;
        for (let element: Element | null = parent; element && root.contains(element); element = element.parentElement) {
          const computed = getComputedStyle(element);
          if (computed.display === 'none' || computed.visibility === 'hidden' || computed.visibility === 'collapse') { hidden = true; break; }
          if (!joinTextRuns && block === root && !['inline', 'contents'].includes(computed.display)) block = element;
        }
        if (hidden) continue;
        let group = groups.at(-1);
        if (!group || group.block !== block) {
          group = { block, nodes: [], text: '' }; groups.push(group);
        }
        group.nodes.push(node as Text); group.text += node.textContent ?? '';
      }
      for (const group of groups) {
        for (const match of matchText(group.text, query)) {
          const range = document.createRange();
          let offset = 0, started = false;
          for (const node of group.nodes) {
            const end = offset + node.length;
            if (!started && match.start < end) { range.setStart(node, match.start - offset); started = true; }
            if (started && match.end <= end) { range.setEnd(node, match.end - offset); break; }
            offset = end;
          }
          ranges.push(range);
        }
      }
      const highlight = new Highlight();
      ranges.forEach(range => highlight.add(range));
      CSS.highlights.set(name, highlight);
      return ranges.length;
    },
    select(index) {
      const range = ranges[index];
      CSS.highlights.delete(currentName);
      if (!range) return;
      CSS.highlights.set(currentName, new Highlight(range));
      const element = range.startContainer.parentElement;
      for (let parent = element; parent && root.contains(parent); parent = parent.parentElement) {
        if (parent instanceof HTMLDetailsElement) parent.open = true;
      }
      for (let parent = element; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        const scrolls = parent === document.scrollingElement || /(auto|scroll|overlay)/.test(`${style.overflowY} ${style.overflowX}`);
        if (!scrolls) continue;
        const rect = range.getBoundingClientRect(), box = parent.getBoundingClientRect();
        const height = parent === document.scrollingElement ? innerHeight : parent.clientHeight;
        const width = parent === document.scrollingElement ? innerWidth : parent.clientWidth;
        const top = parent === document.scrollingElement ? 0 : box.top;
        const left = parent === document.scrollingElement ? 0 : box.left;
        parent.scrollBy({
          top: parent.scrollHeight > height ? rect.top - top - (height - rect.height) / 2 : 0,
          left: parent.scrollWidth > width && (rect.left < left || rect.right > left + width) ? rect.left - left - (width - rect.width) / 2 : 0,
          behavior: 'instant',
        });
      }
    },
    clear() { clear(); style.remove(); },
  };
}
