let anonymousInstance = 0;
const usedNamespaces = new Set<string>();

function slug(value: string): string {
  return value
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '') || 'anonymous';
}

export function allocateRichInstanceNamespace(durableId = ''): string {
  const base = durableId
    ? `rich-deliverable-${slug(durableId)}`
    : `rich-deliverable-anonymous-${++anonymousInstance}`;
  let candidate = base;
  let suffix = 2;
  while (usedNamespaces.has(candidate)) candidate = `${base}-${suffix++}`;
  usedNamespaces.add(candidate);
  return candidate;
}
