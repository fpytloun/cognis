import { describe, expect, it } from 'vitest';

import { classifyDocument, languageForHighlight } from './render';

describe('classifyDocument', () => {
  it('classifies by extension when mime type is generic', () => {
    expect(classifyDocument('text/plain', 'notes.md')).toBe('markdown');
    expect(classifyDocument('application/octet-stream', 'data.json')).toBe('binary');
    expect(classifyDocument('text/plain', 'config.yaml')).toBe('yaml');
    expect(classifyDocument('text/plain', 'feed.xml')).toBe('xml');
    expect(classifyDocument('text/plain', 'script.py')).toBe('code');
    expect(classifyDocument('text/plain', 'log.txt')).toBe('plain');
  });

  it('classifies PDF and Word documents as binary regardless of extension mismatch', () => {
    expect(classifyDocument('application/pdf', 'report')).toBe('binary');
    expect(classifyDocument('application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'x')).toBe('binary');
  });

  it('falls back to binary for unknown types', () => {
    expect(classifyDocument('application/x-custom', 'thing.xyz')).toBe('binary');
  });
});

describe('languageForHighlight', () => {
  it('maps structured kinds to their highlight.js language', () => {
    expect(languageForHighlight('json', 'a.json')).toBe('json');
    expect(languageForHighlight('yaml', 'a.yaml')).toBe('yaml');
    expect(languageForHighlight('xml', 'a.xml')).toBe('xml');
  });

  it('uses the file extension as the language for code and plaintext otherwise', () => {
    expect(languageForHighlight('code', 'script.py')).toBe('py');
    expect(languageForHighlight('plain', 'notes.txt')).toBe('plaintext');
  });
});
