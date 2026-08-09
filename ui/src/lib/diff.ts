export interface FileDiff {
  path: string;
  diff: string;
  additions?: number | null;
  deletions?: number | null;
  content_truncated?: boolean;
  truncated?: boolean;
  original_size?: number;
  omitted_count?: number;
}

export type DiffLineType = 'context' | 'add' | 'remove' | 'hunk' | 'file' | 'meta';

export interface ParsedDiffLine {
  type: DiffLineType;
  content: string;
  oldLine: number | null;
  newLine: number | null;
}

export interface ParsedFileDiff {
  path: string;
  language: string | null;
  languageLabel: string;
  iconLabel: string;
  additions: number;
  deletions: number;
  truncated: boolean;
  omittedCount: number;
  lines: ParsedDiffLine[];
}

const EXTENSIONS: Record<string, { language: string; label: string; icon: string }> = {
  py: { language: 'python', label: 'Python', icon: 'Py' },
  pyw: { language: 'python', label: 'Python', icon: 'Py' },
  js: { language: 'javascript', label: 'JavaScript', icon: 'JS' },
  jsx: { language: 'javascript', label: 'JavaScript', icon: 'JS' },
  mjs: { language: 'javascript', label: 'JavaScript', icon: 'JS' },
  cjs: { language: 'javascript', label: 'JavaScript', icon: 'JS' },
  ts: { language: 'typescript', label: 'TypeScript', icon: 'TS' },
  tsx: { language: 'typescript', label: 'TypeScript', icon: 'TS' },
  svelte: { language: 'xml', label: 'Svelte', icon: 'Sv' },
  json: { language: 'json', label: 'JSON', icon: '{}' },
  yaml: { language: 'yaml', label: 'YAML', icon: 'Yml' },
  yml: { language: 'yaml', label: 'YAML', icon: 'Yml' },
  md: { language: 'markdown', label: 'Markdown', icon: 'Md' },
  css: { language: 'css', label: 'CSS', icon: 'CSS' },
  scss: { language: 'scss', label: 'SCSS', icon: 'Sc' },
  html: { language: 'xml', label: 'HTML', icon: '<>' },
  xml: { language: 'xml', label: 'XML', icon: '<>' },
  sh: { language: 'bash', label: 'Shell', icon: 'Sh' },
  bash: { language: 'bash', label: 'Shell', icon: 'Sh' },
  zsh: { language: 'bash', label: 'Shell', icon: 'Sh' },
  toml: { language: 'ini', label: 'TOML', icon: 'Toml' },
  ini: { language: 'ini', label: 'INI', icon: 'Ini' },
  sql: { language: 'sql', label: 'SQL', icon: 'SQL' },
  go: { language: 'go', label: 'Go', icon: 'Go' },
  rs: { language: 'rust', label: 'Rust', icon: 'Rs' },
  java: { language: 'java', label: 'Java', icon: 'Ja' },
  kt: { language: 'kotlin', label: 'Kotlin', icon: 'Kt' },
  rb: { language: 'ruby', label: 'Ruby', icon: 'Rb' },
  php: { language: 'php', label: 'PHP', icon: 'PHP' },
  c: { language: 'c', label: 'C', icon: 'C' },
  h: { language: 'c', label: 'C', icon: 'C' },
  cpp: { language: 'cpp', label: 'C++', icon: 'C++' },
  hpp: { language: 'cpp', label: 'C++', icon: 'C++' },
};

function extensionFor(path: string): string {
  const name = path.split(/[\\/]/).pop() ?? path;
  const index = name.lastIndexOf('.');
  return index >= 0 ? name.slice(index + 1).toLowerCase() : '';
}

export function detectFileLanguage(path: string): { language: string | null; label: string; icon: string } {
  const extension = extensionFor(path);
  const detected = EXTENSIONS[extension];
  if (detected) return detected;
  return { language: null, label: extension ? extension.toUpperCase() : 'Text', icon: extension ? extension.slice(0, 3).toUpperCase() : 'Txt' };
}

function parseHunkStart(line: string): { oldLine: number; newLine: number } {
  const match = /^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/.exec(line);
  return {
    oldLine: match ? Number(match[1]) : 0,
    newLine: match ? Number(match[2]) : 0,
  };
}

export function parseFileDiff(input: FileDiff): ParsedFileDiff {
  const detected = detectFileLanguage(input.path);
  const parsedLines: ParsedDiffLine[] = [];
  let oldLine = 0;
  let newLine = 0;
  let additions = 0;
  let deletions = 0;

  for (const rawLine of input.diff.split('\n')) {
    if (rawLine.startsWith('@@')) {
      const hunk = parseHunkStart(rawLine);
      oldLine = hunk.oldLine;
      newLine = hunk.newLine;
      parsedLines.push({ type: 'hunk', content: rawLine, oldLine: null, newLine: null });
      continue;
    }
    if (rawLine.startsWith('---') || rawLine.startsWith('+++')) {
      parsedLines.push({ type: 'file', content: rawLine, oldLine: null, newLine: null });
      continue;
    }
    if (rawLine.startsWith('... (diff truncated') || rawLine.startsWith('\\ No newline')) {
      parsedLines.push({ type: 'meta', content: rawLine, oldLine: null, newLine: null });
      continue;
    }
    if (rawLine.startsWith('+')) {
      parsedLines.push({ type: 'add', content: rawLine.slice(1), oldLine: null, newLine });
      newLine += 1;
      additions += 1;
      continue;
    }
    if (rawLine.startsWith('-')) {
      parsedLines.push({ type: 'remove', content: rawLine.slice(1), oldLine, newLine: null });
      oldLine += 1;
      deletions += 1;
      continue;
    }
    const content = rawLine.startsWith(' ') ? rawLine.slice(1) : rawLine;
    parsedLines.push({ type: 'context', content, oldLine, newLine });
    oldLine += 1;
    newLine += 1;
  }

  return {
    path: input.path,
    language: detected.language,
    languageLabel: detected.label,
    iconLabel: detected.icon,
    additions: input.additions ?? additions,
    deletions: input.deletions ?? deletions,
    truncated: Boolean(input.truncated || input.content_truncated),
    omittedCount: typeof input.omitted_count === 'number' ? input.omitted_count : 0,
    lines: parsedLines,
  };
}

export function normalizeFileDiffs(value: unknown): FileDiff[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item): FileDiff | null => {
      if (!item || typeof item !== 'object') return null;
      const record = item as Record<string, unknown>;
      const path = typeof record.path === 'string' ? record.path : '';
      const diff = typeof record.diff === 'string' ? record.diff : '';
      const omittedCount = typeof record.omitted_count === 'number' ? record.omitted_count : undefined;
      if (!path && !omittedCount) return null;
      return {
        path,
        diff,
        ...(typeof record.additions === 'number' ? { additions: record.additions } : {}),
        ...(typeof record.deletions === 'number' ? { deletions: record.deletions } : {}),
        ...(record.content_truncated === true ? { content_truncated: true } : {}),
        truncated: record.truncated === true,
        original_size: typeof record.original_size === 'number' ? record.original_size : undefined,
        omitted_count: omittedCount,
      };
    })
    .filter((item): item is FileDiff => item !== null);
}
