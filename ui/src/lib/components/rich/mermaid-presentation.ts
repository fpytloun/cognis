import { namespaceMermaidSvg } from './namespace-svg';

/** Resolve the live viewer tokens used by Mermaid's inline SVG styles. */
function resolveMermaidTheme(el: Element | undefined) {
  const fallbackDark = {
    background: '#020617',
    primaryColor: '#0f172a',
    primaryTextColor: '#e2e8f0',
    primaryBorderColor: '#38bdf8',
    lineColor: '#67e8f9',
    secondaryColor: '#082f49',
    secondaryTextColor: '#e0f2fe',
    secondaryBorderColor: '#0ea5e9',
    tertiaryColor: '#111827',
    tertiaryTextColor: '#e5e7eb',
    tertiaryBorderColor: '#334155',
    noteBkgColor: '#0f172a',
    noteTextColor: '#e2e8f0',
    noteBorderColor: '#38bdf8',
    clusterBkg: '#020617',
    clusterBorder: '#1e293b',
    edgeLabelBackground: '#020617',
  };
  if (typeof window === 'undefined' || !el) return { darkMode: true, themeVariables: fallbackDark };
  const style = getComputedStyle(el);
  const surface = style.getPropertyValue('--rich-surface').trim();
  const surfaceRaised = style.getPropertyValue('--rich-surface-raised').trim();
  const text = style.getPropertyValue('--rich-text').trim();
  const textSecondary = style.getPropertyValue('--rich-text-secondary').trim();
  const accent = style.getPropertyValue('--rich-accent').trim();
  const accentSoft = style.getPropertyValue('--rich-accent-soft').trim();
  const line = style.getPropertyValue('--rich-line').trim();
  if (!surface || !text || !accent) return { darkMode: true, themeVariables: fallbackDark };
  const isLight = el.closest('[data-viewer-theme]')?.getAttribute('data-viewer-theme') === 'light'
    || (!el.closest('[data-viewer-theme]') && document.documentElement.getAttribute('data-resolved-theme') === 'light');
  return {
    darkMode: !isLight,
    themeVariables: {
      background: surface,
      primaryColor: surfaceRaised,
      primaryTextColor: text,
      primaryBorderColor: accent,
      lineColor: accentSoft,
      secondaryColor: surfaceRaised,
      secondaryTextColor: textSecondary,
      secondaryBorderColor: accent,
      tertiaryColor: surface,
      tertiaryTextColor: textSecondary,
      tertiaryBorderColor: line,
      noteBkgColor: surfaceRaised,
      noteTextColor: textSecondary,
      noteBorderColor: accent,
      clusterBkg: surface,
      clusterBorder: line,
      edgeLabelBackground: surface,
    },
  };
}

export async function renderMermaidPresentation(
  nodes: readonly HTMLElement[],
  themeRoot: Element | undefined,
  instanceNamespace: string,
  isCurrent: (node: HTMLElement) => boolean,
  publish: (node: HTMLElement, presentation: HTMLDivElement) => void,
): Promise<void> {
  if (!nodes.some(isCurrent)) return;
  // Keep Mermaid's preload catalog behind this presentation boundary too.
  const mermaid = (await import('mermaid')).default;
  if (!nodes.some(isCurrent)) return;
  const resolvedTheme = resolveMermaidTheme(themeRoot);
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'base',
    darkMode: resolvedTheme.darkMode,
    themeVariables: {
      ...resolvedTheme.themeVariables,
      fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif',
    },
  });
  for (const [index, node] of nodes.entries()) {
    if (!isCurrent(node)) continue;
    // Decode persisted arrow brackets without interpreting authored HTML.
    const source = (node.textContent ?? '').replaceAll('&gt;', '>').replaceAll('&lt;', '<');
    try {
      const renderId = node.dataset.mermaidId || `${instanceNamespace}-mermaid-${index}`;
      const result = await mermaid.render(renderId, source);
      if (!isCurrent(node)) continue;
      const wrapper = document.createElement('div');
      wrapper.className = 'rich-mermaid';
      wrapper.dataset.mermaidSource = source;
      wrapper.dataset.mermaidId = renderId;
      wrapper.innerHTML = namespaceMermaidSvg(result.svg, renderId);
      publish(node, wrapper);
    } catch {
      // Keep escaped source fallback.
    }
  }
}
