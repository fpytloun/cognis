const IDREF_ATTRIBUTES = ['aria-labelledby', 'aria-describedby'] as const;
const REFERENCE_ATTRIBUTES = [
  'href',
  'xlink:href',
  'fill',
  'stroke',
  'marker-start',
  'marker-mid',
  'marker-end',
  'clip-path',
  'mask',
  'filter',
  'cursor',
  'style',
] as const;

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function rewriteReference(value: string, ids: ReadonlyMap<string, string>): string {
  let rewritten = value;
  for (const [oldId, newId] of ids) {
    const escaped = escapeRegex(oldId);
    rewritten = rewritten
      .replace(new RegExp(`url\\((['"]?)#${escaped}\\1\\)`, 'g'), `url(#${newId})`)
      .replace(new RegExp(`^#${escaped}$`), `#${newId}`)
      .replace(new RegExp(`#${escaped}(?![\\w:.-])`, 'g'), `#${newId}`);
  }
  return rewritten;
}

/**
 * Namespace Mermaid-generated SVG IDs before the SVG enters the live document.
 * Mermaid can emit identical marker definitions more than once. Equivalent
 * duplicates are collapsed because one reference cannot select them separately.
 */
export function namespaceMermaidSvg(svg: string, namespace: string): string {
  const document = new DOMParser().parseFromString(svg, 'image/svg+xml');
  if (document.querySelector('parsererror')) throw new Error('Invalid Mermaid SVG');

  const idElements = Array.from(document.querySelectorAll('[id]'));
  const canonical = new Map<string, Element>();
  const renamed = new Map<string, string>();
  for (const element of idElements) {
    const oldId = element.getAttribute('id') ?? '';
    const existing = canonical.get(oldId);
    if (existing) {
      const existingShape = existing.cloneNode(true) as Element;
      const duplicateShape = element.cloneNode(true) as Element;
      existingShape.removeAttribute('id');
      duplicateShape.removeAttribute('id');
      if (existingShape.outerHTML !== duplicateShape.outerHTML) {
        throw new Error(`Ambiguous duplicate Mermaid SVG ID: ${oldId}`);
      }
      element.remove();
      continue;
    }
    canonical.set(oldId, element);
    renamed.set(oldId, oldId === namespace ? namespace : `${namespace}-${oldId}`);
  }

  for (const [oldId, element] of canonical) {
    element.setAttribute('id', renamed.get(oldId) ?? oldId);
  }
  for (const element of Array.from(document.querySelectorAll('*'))) {
    for (const attribute of REFERENCE_ATTRIBUTES) {
      const value = element.getAttribute(attribute);
      if (value !== null) element.setAttribute(attribute, rewriteReference(value, renamed));
    }
    for (const attribute of IDREF_ATTRIBUTES) {
      const value = element.getAttribute(attribute);
      if (value !== null) {
        element.setAttribute(
          attribute,
          value.split(/\s+/).map((id) => renamed.get(id) ?? id).join(' '),
        );
      }
    }
  }
  for (const style of Array.from(document.querySelectorAll('style'))) {
    style.textContent = rewriteReference(style.textContent ?? '', renamed);
  }

  const outputIds = Array.from(document.querySelectorAll('[id]')).map(
    (element) => element.getAttribute('id') ?? '',
  );
  if (new Set(outputIds).size !== outputIds.length) {
    throw new Error('Mermaid SVG IDs are not unique after namespacing');
  }
  return new XMLSerializer().serializeToString(document.documentElement);
}
