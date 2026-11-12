/** Run inside the browser: clipping text to its painted scrollport is not an overlap exemption. */
export function usageGeometry(rootSelector: string) {
  type Box = { left: number; top: number; right: number; bottom: number };
  const viewport: Box = { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
  const intersection = (a: Box, b: Box): Box => ({
    left: Math.max(a.left, b.left), top: Math.max(a.top, b.top),
    right: Math.min(a.right, b.right), bottom: Math.min(a.bottom, b.bottom),
  });
  const nonempty = (box: Box) => box.right > box.left && box.bottom > box.top;
  const overlaps = (a: Box, b: Box) => a.left < b.right - 1 && a.right > b.left + 1
    && a.top < b.bottom - 1 && a.bottom > b.top + 1;
  const rectangle = (box: Box): Box => ({ left: box.left, top: box.top, right: box.right, bottom: box.bottom });
  const identity = (element: Element) => {
    const path: string[] = [];
    for (let node: Element | null = element; node && path.length < 10; node = node.parentElement) {
      path.unshift(`${node.tagName.toLowerCase()}[${node.parentElement ? Array.from(node.parentElement.children).indexOf(node) + 1 : 1}]`);
    }
    return { tag: element.tagName, role: element.getAttribute('role'), path: path.join('/') };
  };
  const diagnostics = (element: Element) => {
    const style = getComputedStyle(element);
    return { ...identity(element), rect: rectangle(element.getBoundingClientRect()),
      position: style.position, visibility: style.visibility, display: style.display, opacity: style.opacity,
      overflowX: style.overflowX, overflowY: style.overflowY, clip: style.clip, clipPath: style.clipPath, transform: style.transform };
  };
  type Clip = ReturnType<typeof diagnostics> & { applied: boolean; result: Box };
  const visible = (node: Element) => {
    const style = getComputedStyle(node);
    return style.visibility === 'visible' && style.display !== 'none' && Number(style.opacity) !== 0;
  };
  const excludedDisclosures: unknown[] = [];
  const unpaintedDisclosure = (element: Element): boolean => {
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      if (!(parent instanceof HTMLDetailsElement) || parent.open) continue;
      const summary = Array.from(parent.children).find((child) => child.tagName === 'SUMMARY');
      if (summary?.contains(element)) continue;
      // Native characterization proves nonempty boxes can survive while the
      // closed disclosure suppresses both paint and hit testing.
      excludedDisclosures.push({ candidate: diagnostics(element), disclosure: diagnostics(parent),
        reason: 'non-summary descendant of closed details' });
      return true;
    }
    return false;
  };
  const painted = (rect: Box, element: Element): { box: Box; chain: Clip[] } => {
    let box = intersection(rect, viewport);
    const chain: Clip[] = [];
    let fixed = getComputedStyle(element).position === 'fixed';
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      if (!visible(parent)) {
        box = { left: 0, right: 0, top: 0, bottom: 0 };
        chain.push({ ...diagnostics(parent), applied: true, result: box });
        return { box, chain };
      }
      const style = getComputedStyle(parent);
      const clipX = /^(auto|scroll|hidden|clip)$/.test(style.overflowX);
      const clipY = /^(auto|scroll|hidden|clip)$/.test(style.overflowY);
      if (!fixed && (clipX || clipY)) {
        const bound = parent.getBoundingClientRect();
        const left = bound.left + parent.clientLeft;
        const top = bound.top + parent.clientTop;
        box = intersection(box, {
          left: clipX ? left : box.left, right: clipX ? left + parent.clientWidth : box.right,
          top: clipY ? top : box.top, bottom: clipY ? top + parent.clientHeight : box.bottom,
        });
      }
      chain.push({ ...diagnostics(parent), applied: !fixed && (clipX || clipY), result: rectangle(box) });
      // Fixed descendants escape ordinary ancestor overflow. Retaining extra
      // boxes for transformed containing blocks is conservative, never an exemption.
      fixed ||= style.position === 'fixed';
    }
    return { box, chain };
  };
  type Obstacle = { box: Box; raw: Box; element: Element; chain: Clip[]; line: number };
  const raw: Obstacle[] = [];
  const ink: Obstacle[] = [];
  let clipped = 0;
  const root = document.querySelector(rootSelector)!;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const element = walker.currentNode.parentElement!;
    if (!walker.currentNode.textContent?.trim() || !visible(element) || unpaintedDisclosure(element)) continue;
    const range = document.createRange();
    range.selectNodeContents(walker.currentNode);
    for (const rect of Array.from(range.getClientRects())) {
      if (!nonempty(rect)) continue;
      const { box, chain } = painted(rect, element);
      const obstacle = { raw: rectangle(rect), element, chain, line: raw.length };
      raw.push({ ...obstacle, box: rectangle(rect) });
      if (box.left !== rect.left || box.right !== rect.right || box.top !== rect.top || box.bottom !== rect.bottom) clipped++;
      if (nonempty(box)) ink.push({ ...obstacle, box });
    }
  }
  for (const element of document.querySelectorAll('textarea,[contenteditable="true"]')) {
    if (!visible(element) || unpaintedDisclosure(element)) continue;
    const rect = element.getBoundingClientRect();
    if (!nonempty(rect)) continue;
    const { box, chain } = painted(rect, element);
    const obstacle = { raw: rectangle(rect), element, chain, line: raw.length };
    raw.push({ ...obstacle, box: rectangle(rect) });
    if (nonempty(box)) ink.push({ ...obstacle, box });
  }
  const floating = Array.from(document.querySelectorAll('body *')).filter((element) => {
    const position = getComputedStyle(element).position;
    return visible(element) && !element.contains(root) && (position === 'fixed'
      || position === 'absolute' && element.matches('nav,header,aside,[role="dialog"],[role="tooltip"]'))
      && !unpaintedDisclosure(element);
  });
  const hits: string[] = [];
  const rawHits: string[] = [];
  const hitDetails: unknown[] = [];
  for (const element of floating) {
    const box = intersection(element.getBoundingClientRect(), viewport);
    if (!nonempty(box)) continue;
    for (const obstacle of raw) if (!element.contains(obstacle.element) && overlaps(box, obstacle.box)) rawHits.push(element.tagName);
    for (const obstacle of ink) if (!element.contains(obstacle.element) && overlaps(box, obstacle.box)) {
      hits.push(element.tagName);
      hitDetails.push({ floating: diagnostics(element), obstacle: diagnostics(obstacle.element),
        floatingRect: rectangle(box), line: obstacle.line, lineRect: obstacle.raw, paintedLineRect: obstacle.box,
        clippingChain: obstacle.chain });
    }
  }
  const notice = document.querySelector('[data-testid="connection-notice-slot"]');
  const header = document.querySelector('.app-keyboard-stable-header');
  const noticeHeaderOverlap = notice && header && visible(notice) && visible(header)
    ? intersection(intersection(notice.getBoundingClientRect(), header.getBoundingClientRect()), viewport)
    : { left: 0, top: 0, right: 0, bottom: 0 };
  return { rawLineBoxes: raw.length, paintedLineBoxes: ink.length, clipped, rawHits, hits, hitDetails, excludedDisclosures,
    noticeHeaderOverlap: nonempty(noticeHeaderOverlap) ? rectangle(noticeHeaderOverlap) : null,
    covered: hits.length, documentWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth };
}
