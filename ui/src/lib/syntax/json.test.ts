import { describe, expect, it } from 'vitest';
import { reindentJson } from './json';
import { looseQuoteRange } from '$lib/feedback/annotations/text';

describe('reindentJson', () => {
  it('indents structure and leaves every token byte-identical', () => {
    const source = '{"a":1.0,"b":[1e3, "x\\u00e1\\"y"],"c":{},"d":[],"e":{"f":"g, h: {i}"}}';
    const pretty = reindentJson(source);
    expect(pretty).toBe([
      '{', '  "a": 1.0,', '  "b": [', '    1e3,', '    "x\\u00e1\\"y"', '  ],', '  "c": {},', '  "d": [],',
      '  "e": {', '    "f": "g, h: {i}"', '  }', '}',
    ].join('\n'));
    expect(pretty.replace(/\s+/g, '')).toBe(source.replace(/\s+/g, ''));
  });

  it('lets a selection in the indented view map back to the exact source range', () => {
    const source = '{"lease":{"owner":"LaForge","task":"conv_1"},"owner":"LaForge"}';
    const selected = '"owner": "LaForge",\n    "task"';
    const position = looseQuoteRange(source, selected, '{\n  "lease": {\n    ', true);
    expect(position).not.toBeNull();
    expect(source.slice(position!.start, position!.end)).toBe('"owner":"LaForge","task"');
  });
});
