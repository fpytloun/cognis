/** Browser-side geometry shared by qualification and its focused DOM regression. */
export function usageBounds(element: Element) {
  const visible = (node: Element) => node.checkVisibility();
  const rect = (node: Element) => {
    const box = node.getBoundingClientRect();
    return { x: box.x, y: box.y, width: box.width, height: box.height };
  };
  const paintedCenter = (node: Element, x: number, y: number) => {
    if (x < 0 || x >= innerWidth || y < 0 || y >= innerHeight) return false;
    // checkVisibility does not account for ancestor scrollport clipping.
    for (let parent = node.parentElement; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      const clips = (overflow: string) => /^(auto|scroll|hidden|clip)$/.test(overflow);
      const clipsX = clips(style.overflowX || style.overflow);
      const clipsY = clips(style.overflowY || style.overflow);
      if (!clipsX && !clipsY) continue;
      const box = parent.getBoundingClientRect();
      const scaleX = parent.offsetWidth ? box.width / parent.offsetWidth : 1;
      const scaleY = parent.offsetHeight ? box.height / parent.offsetHeight : 1;
      const left = box.left + parent.clientLeft * scaleX;
      const top = box.top + parent.clientTop * scaleY;
      if (clipsX && (x < left || x >= left + parent.clientWidth * scaleX)) return false;
      if (clipsY && (y < top || y >= top + parent.clientHeight * scaleY)) return false;
    }
    return true;
  };
  const unpaintedScreenReaderOnly = (node: Element) => {
    if (!node.classList.contains('sr-only')) return false;
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return box.width === 1 && box.height === 1
      && style.position === 'absolute' && style.overflow === 'hidden'
      && style.clip.replace(/\s|px/g, '') === 'rect(0,0,0,0)';
  };
  const panels = Array.from(element.querySelectorAll('section[aria-label]')).filter(visible).map(rect);
  const targets = Array.from(element.querySelectorAll('button, summary, select, input[type=radio]'))
    .filter(visible).map((node) => {
      const box = rect(node);
      const x = box.x + box.width / 2, y = box.y + box.height / 2;
       const hit = box.width > 0 && box.height > 0 && paintedCenter(node, x, y)
         ? document.elementFromPoint(x, y) : null;
       // The workspace's own sticky navigation clips scrolling content, not
       // its own controls. Other overlays remain obstruction failures.
       const navigation = hit?.closest('[data-usage-sticky-navigation]');
       const behindNavigation = navigation && element.contains(navigation)
         && !navigation.contains(node) && getComputedStyle(navigation).position === 'sticky';
       return { name: node.getAttribute('aria-label') ?? node.textContent?.trim(),
         ...box, tag: node.tagName, hit: hit && !behindNavigation ? node.contains(hit) : null };
    });
  const overflow = Array.from(element.querySelectorAll('h2,h3,p,legend,ol'))
    .filter(visible).filter((node) => !unpaintedScreenReaderOnly(node))
    .filter((node) => node.scrollWidth > node.clientWidth + 1)
    .map((node) => node.tagName + ':' + node.textContent?.slice(0, 100));
  return { root: rect(element), viewport: { width: innerWidth, height: innerHeight },
    panels, targets, overflow, documentOverflow: document.documentElement.scrollWidth > innerWidth + 1 };
}
