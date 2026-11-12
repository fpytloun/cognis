import { describe, expect, it } from 'vitest';
import { mapRenderedMarkdown, renderMarkdown } from './markdown';

describe('canonical Markdown provenance', () => {
  it.each([
    'Plain 😀 Unicode č text',
    'A **bold** and [link](https://host.test) with `inline` code.',
    '- first **bold**\n- second [label](https://host.test)',
    'A &amp; B \\* literal &lt;tag&gt;',
    '# Heading\r\n\r\nSame phrase. **Same phrase.**',
    '| A | B |\n| -- |\n| first | second |',
    '````markdown\n# Heading\n```js\ncode\n```\n````',
  ])('preserves the default rendered text and raw UTF-16 units: %s', async source => {
    const map = await mapRenderedMarkdown(source);
    const root = document.createElement('div'); root.innerHTML = renderMarkdown(source);
    expect(map.text).toBe(root.textContent);
    expect(map.units).toHaveLength(map.text.length);
    expect(map.source).toBe(source.replace(/\r\n?/g, '\n'));
    for (let i = 0; i < map.units.length; i++) {
      const unit = map.units[i];
      if (!unit) continue;
      expect(unit.start).toBeGreaterThanOrEqual(0);
      expect(unit.end).toBeLessThanOrEqual(map.source.length);
    }
  });
  it('distinguishes repeated phrases by parser ownership, not quote search', async () => {
    const source = 'Same **Same** and [Same](https://host.test/Same).';
    const map = await mapRenderedMarkdown(source);
    const at = map.text.indexOf('Same', 5);
    expect(map.units[at]?.start).toBe(7);
    const link = map.text.lastIndexOf('Same');
    expect(map.units[link]?.start).toBe(source.indexOf('[Same') + 1);
  });
  it('maps entities, escapes and code boundaries without assigning generated paragraph whitespace', async () => {
    const source = 'A &amp; \\* `x` 😀';
    const map = await mapRenderedMarkdown(source);
    const amp = map.units[map.text.indexOf('&')]!;
    expect(source.slice(amp.start, amp.end)).toBe('&amp;');
    expect(map.units[map.text.indexOf('*')]?.start).toBe(source.indexOf('*'));
    expect(map.units[map.text.indexOf('x')]?.start).toBe(source.indexOf('x'));
    expect(map.units.at(-1)).toBeNull();
  });
  it('maps table cell text to its own source line so a cell selection can be commented', async () => {
    const source = '| Item | State |\n| -- | -- |\n| #457 analytics and projection alerting | open, not merged |\n| **bold** cell | done |';
    const map = await mapRenderedMarkdown(source);
    const at = map.text.indexOf('projection alerting');
    const first = map.units[at]!, last = map.units[at + 'projection alerting'.length - 1]!;
    expect(source.slice(first.start, last.end)).toBe('projection alerting');
    const bold = map.units[map.text.indexOf('bold')]!;
    expect(source.slice(bold.start, bold.end)).toBe('b');
    // The header cell is mapped to the header line, not to a body row with the same word.
    expect(map.units[map.text.indexOf('State')]?.start).toBe(source.indexOf('State'));
  });
  it('never anchors a table cell inside a neighbouring cell', async () => {
    const source = '| A | B |\n| -- | -- |\n| x\\| open | open |';
    const map = await mapRenderedMarkdown(source);
    // The rendered second cell is the last "open"; it must point at its own cell or nowhere.
    const second = map.units[map.text.lastIndexOf('open')];
    if (second) expect(second.start).toBe(source.lastIndexOf('open'));
  });
  it('cannot accept authored source markers as provenance', async () => {
    const source = '<span data-cognis-map-forged="0">authored</span>';
    const map = await mapRenderedMarkdown(source);
    expect(map.html).not.toContain('<span data-cognis-map-forged');
    const template = document.createElement('template'); template.innerHTML = map.html;
    expect(template.content.querySelector('[data-cognis-map-forged]')).toBeNull();
    const at = map.text.indexOf('authored'), unit = map.units[at]!;
    expect(source.slice(unit.start, unit.end)).toBe('a');
  });
});
