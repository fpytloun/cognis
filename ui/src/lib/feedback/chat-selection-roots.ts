const roots = new WeakMap<Node, number>();
export function registerChatSelectionRoot(root: Node) {
  roots.set(root, (roots.get(root) ?? 0) + 1);
  return () => { const count = (roots.get(root) ?? 1) - 1; if (count) roots.set(root, count); else roots.delete(root); };
}
export function ownsChatSelection(root: Node, node: Node | null): boolean {
  for (; node; node = node.parentNode) if (roots.has(node)) return node === root;
  return false;
}
