import { describe, expect, it } from 'vitest';
import { namespaceMermaidSvg } from './namespace-svg';

describe('namespaceMermaidSvg', () => {
  it('namespaces IDs and preserves every supported internal reference', () => {
    const svg = `<svg id="root" aria-labelledby="title desc" xmlns:xlink="http://www.w3.org/1999/xlink">
      <title id="title">Diagram</title><desc id="desc">Flow</desc>
      <defs>
        <marker id="arrow"><path id="tip"/></marker>
        <clipPath id="clip"><path/></clipPath>
        <filter id="shadow"><feGaussianBlur/></filter>
      </defs>
      <style>.edge{marker-end:url(#arrow)} #tip{fill:red}</style>
      <path class="edge" marker-end="url(#arrow)" clip-path="url('#clip')" filter="url(#shadow)"/>
      <use href="#tip" xlink:href="#tip"/>
    </svg>`;
    const output = namespaceMermaidSvg(svg, 'report-mermaid-0');
    const document = new DOMParser().parseFromString(output, 'image/svg+xml');
    const ids = Array.from(document.querySelectorAll('[id]')).map((node) => node.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids.every((id) => id.startsWith('report-mermaid-0-'))).toBe(true);
    for (const id of ids) expect(document.getElementById(id)).not.toBeNull();
    const references = Array.from(
      output.matchAll(/(?:url\(['"]?#|(?:href|xlink:href)="|aria-labelledby=")([\w:.-]+)/g),
      (match) => match[1],
    );
    for (const reference of references) {
      expect(document.getElementById(reference), reference).not.toBeNull();
    }
    expect(output).toContain('url(#report-mermaid-0-arrow)');
    expect(output).toContain('href="#report-mermaid-0-tip"');
  });

  it('collapses equivalent duplicate definitions and rejects ambiguous duplicates', () => {
    const equivalent = '<svg><defs><marker id="arrow"><path/></marker><marker id="arrow"><path/></marker></defs><path marker-end="url(#arrow)"/></svg>';
    const output = namespaceMermaidSvg(equivalent, 'diagram');
    expect((output.match(/id="diagram-arrow"/g) ?? [])).toHaveLength(1);
    expect(output).toContain('marker-end="url(#diagram-arrow)"');

    expect(() => namespaceMermaidSvg(
      '<svg><path id="same" d="M0 0"/><path id="same" d="M1 1"/></svg>',
      'diagram',
    )).toThrow('Ambiguous duplicate Mermaid SVG ID');
  });

  it('keeps identical diagrams isolated by their render namespace', () => {
    const svg = '<svg id="root"><marker id="arrow"><path/></marker><path marker-end="url(#arrow)"/></svg>';
    const first = namespaceMermaidSvg(svg, 'first');
    const second = namespaceMermaidSvg(svg, 'second');
    expect(first).not.toContain('second-');
    expect(second).not.toContain('first-');
  });
});
