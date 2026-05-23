import { describe, expect, it } from 'vitest';

import { highlightToolOutput, inferLanguageFromPath, isReadToolName, pathFromToolArguments } from './tool-output';

describe('tool output syntax helpers', () => {
  it('infers languages from common read paths', () => {
    expect(inferLanguageFromPath('src/main.py')).toBe('python');
    expect(inferLanguageFromPath('ui/src/lib/markdown.ts')).toBe('typescript');
    expect(inferLanguageFromPath('package.json')).toBe('json');
    expect(inferLanguageFromPath('Dockerfile')).toBe('bash');
  });

  it('returns null for unknown extensions', () => {
    expect(inferLanguageFromPath('notes.unknown-extension')).toBeNull();
  });

  it('extracts paths from supported tool argument keys', () => {
    expect(pathFromToolArguments({ file_path: 'a.py' })).toBe('a.py');
    expect(pathFromToolArguments({ filePath: 'b.ts' })).toBe('b.ts');
    expect(pathFromToolArguments({ path: 'c.json' })).toBe('c.json');
  });

  it('escapes unknown-language output safely', () => {
    const html = highlightToolOutput('<script>alert("x")</script>', null);

    expect(html).toContain('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;');
    expect(html).not.toContain('<script>');
  });

  it('highlights known-language output without emitting unsafe html', () => {
    const html = highlightToolOutput('const value = "<script>";', 'typescript');

    expect(html).toContain('hljs-');
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<script>');
  });

  it('matches only read tool names and known read namespaces', () => {
    expect(isReadToolName('read')).toBe(true);
    expect(isReadToolName('filesystem.read')).toBe(true);
    expect(isReadToolName('read_file')).toBe(true);
    expect(isReadToolName('filesystem/read_file')).toBe(true);
    expect(isReadToolName('mcp_files__read_file')).toBe(true);
    expect(isReadToolName('builtin:read')).toBe(true);
    expect(isReadToolName('thread')).toBe(false);
    expect(isReadToolName('web_fetch')).toBe(false);
    expect(isReadToolName('spreadsheet_import')).toBe(false);
  });
});
