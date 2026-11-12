/** Track the shell slot, including notice appearance, expansion, and removal. */
export function observeConnectionNotice(
  element: HTMLElement,
  reserve: (height: number) => void,
): () => void {
  const update = () => reserve(Math.max(0, Math.ceil(element.getBoundingClientRect().height)));
  update();
  if (typeof ResizeObserver === 'undefined') return () => reserve(0);
  const observer = new ResizeObserver(update);
  observer.observe(element);
  return () => {
    observer.disconnect();
    reserve(0);
  };
}
