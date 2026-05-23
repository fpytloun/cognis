const ESC_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

const BARE_URL_PATTERN = /https?:\/\/[^\s<>'")\]]+/g;
const ESC = '\x1b';
const BEL = '\x07';

const BASIC_COLORS = [
  '#111827',
  '#ef4444',
  '#22c55e',
  '#eab308',
  '#3b82f6',
  '#d946ef',
  '#06b6d4',
  '#e5e7eb',
];

const BRIGHT_COLORS = [
  '#6b7280',
  '#f87171',
  '#4ade80',
  '#facc15',
  '#60a5fa',
  '#e879f9',
  '#22d3ee',
  '#f9fafb',
];

type TerminalStyle = {
  fg?: string;
  bg?: string;
  bold: boolean;
  faint: boolean;
  italic: boolean;
  underline: boolean;
  inverse: boolean;
};

type TerminalCell = {
  char: string;
  style: TerminalStyle;
};

type TerminalLine = {
  cells: TerminalCell[];
  clearedByControl: boolean;
  hadCarriageReturn: boolean;
};

type CsiSequence = {
  type: 'csi';
  params: string;
  intermediates: string;
  command: string;
  length: number;
};

type EscSequence =
  | CsiSequence
  | {
      type: 'osc';
      length: number;
    }
  | {
      type: 'esc';
      length: number;
      command?: string;
    };

function defaultStyle(): TerminalStyle {
  return {
    bold: false,
    faint: false,
    italic: false,
    underline: false,
    inverse: false,
  };
}

function cloneStyle(style: TerminalStyle): TerminalStyle {
  return { ...style };
}

function sameStyle(a: TerminalStyle, b: TerminalStyle): boolean {
  return (
    a.fg === b.fg &&
    a.bg === b.bg &&
    a.bold === b.bold &&
    a.faint === b.faint &&
    a.italic === b.italic &&
    a.underline === b.underline &&
    a.inverse === b.inverse
  );
}

function escapeHtml(input: string): string {
  return input.replace(/[&<>"']/g, (ch) => ESC_MAP[ch] ?? ch);
}

function trimBareUrl(rawUrl: string): { url: string; suffix: string } {
  let url = rawUrl;
  let suffix = '';
  while (url.length > 0 && /[.,;:!?]/.test(url.at(-1) ?? '')) {
    suffix = `${url.at(-1) ?? ''}${suffix}`;
    url = url.slice(0, -1);
  }
  return { url, suffix };
}

function linkifyEscapedText(text: string): string {
  let cursor = 0;
  let html = '';
  BARE_URL_PATTERN.lastIndex = 0;
  for (const match of text.matchAll(BARE_URL_PATTERN)) {
    const rawUrl = match[0];
    const index = match.index ?? 0;
    const { url, suffix } = trimBareUrl(rawUrl);
    if (!url) continue;
    html += escapeHtml(text.slice(cursor, index));
    const safeUrl = escapeHtml(url);
    html += `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer" style="color: inherit; text-decoration: underline; text-decoration-style: dotted; text-underline-offset: 2px;">${safeUrl}</a>${escapeHtml(suffix)}`;
    cursor = index + rawUrl.length;
  }
  html += escapeHtml(text.slice(cursor));
  return html;
}

function xterm256Color(index: number): string | undefined {
  if (!Number.isInteger(index) || index < 0 || index > 255) return undefined;
  if (index < 8) return BASIC_COLORS[index];
  if (index < 16) return BRIGHT_COLORS[index - 8];
  if (index < 232) {
    const value = index - 16;
    const r = Math.floor(value / 36);
    const g = Math.floor((value % 36) / 6);
    const b = value % 6;
    const channel = (component: number) => (component === 0 ? 0 : 55 + component * 40);
    return `rgb(${channel(r)}, ${channel(g)}, ${channel(b)})`;
  }
  const gray = 8 + (index - 232) * 10;
  return `rgb(${gray}, ${gray}, ${gray})`;
}

function rgbColor(r: number, g: number, b: number): string | undefined {
  if (![r, g, b].every((value) => Number.isInteger(value) && value >= 0 && value <= 255)) return undefined;
  return `rgb(${r}, ${g}, ${b})`;
}

function applyExtendedColor(codes: number[], index: number, target: 'fg' | 'bg', style: TerminalStyle): number {
  const mode = codes[index + 1];
  if (mode === 5) {
    const color = xterm256Color(codes[index + 2]);
    if (color) style[target] = color;
    return index + 2;
  }
  if (mode === 2) {
    const color = rgbColor(codes[index + 2], codes[index + 3], codes[index + 4]);
    if (color) style[target] = color;
    return index + 4;
  }
  return index;
}

function applySgrCodes(params: string, style: TerminalStyle): TerminalStyle {
  const next = { ...style };
  const codes = params.length === 0 ? [0] : params.split(';').map((part) => (part === '' ? 0 : Number(part)));

  for (let index = 0; index < codes.length; index += 1) {
    const code = codes[index];
    if (!Number.isFinite(code)) continue;
    if (code === 0) Object.assign(next, defaultStyle());
    else if (code === 1) next.bold = true;
    else if (code === 2) next.faint = true;
    else if (code === 3) next.italic = true;
    else if (code === 4) next.underline = true;
    else if (code === 7) next.inverse = true;
    else if (code === 22) {
      next.bold = false;
      next.faint = false;
    } else if (code === 23) next.italic = false;
    else if (code === 24) next.underline = false;
    else if (code === 27) next.inverse = false;
    else if (code === 39) delete next.fg;
    else if (code === 49) delete next.bg;
    else if (code >= 30 && code <= 37) next.fg = BASIC_COLORS[code - 30];
    else if (code >= 40 && code <= 47) next.bg = BASIC_COLORS[code - 40];
    else if (code >= 90 && code <= 97) next.fg = BRIGHT_COLORS[code - 90];
    else if (code >= 100 && code <= 107) next.bg = BRIGHT_COLORS[code - 100];
    else if (code === 38) index = applyExtendedColor(codes, index, 'fg', next);
    else if (code === 48) index = applyExtendedColor(codes, index, 'bg', next);
  }

  return next;
}

function parseParams(params: string, fallback = 1): number[] {
  if (params.length === 0) return [fallback];
  return params.split(';').map((part) => (part === '' ? fallback : Number(part)));
}

function firstParam(params: string, fallback = 1): number {
  const value = parseParams(params, fallback)[0];
  return Number.isFinite(value) ? value : fallback;
}

function createLine(): TerminalLine {
  return { cells: [], clearedByControl: false, hadCarriageReturn: false };
}

class VirtualTerminal {
  private lines: TerminalLine[] = [createLine()];
  private row = 0;
  private col = 0;
  private style = defaultStyle();

  write(text: string): void {
    let index = 0;
    while (index < text.length) {
      const sequence = text[index] === ESC ? readEscSequence(text, index) : null;
      if (sequence) {
        this.applySequence(sequence);
        index += sequence.length;
        continue;
      }

      const char = text[index];
      index += 1;
      this.writeControlOrChar(char);
    }
  }

  renderHtml(): string {
    return this.lines
      .filter((line) => !line.clearedByControl || line.cells.some((cell) => cell.char.trim().length > 0))
      .map((line) => this.renderLine(line))
      .join('\n');
  }

  private currentLine(): TerminalLine {
    while (this.lines.length <= this.row) this.lines.push(createLine());
    return this.lines[this.row];
  }

  private writeControlOrChar(char: string): void {
    if (char === '\n') {
      this.row += 1;
      this.col = 0;
      this.currentLine();
      return;
    }
    if (char === '\r') {
      this.currentLine().hadCarriageReturn = true;
      this.col = 0;
      return;
    }
    if (char === '\b') {
      this.col = Math.max(0, this.col - 1);
      return;
    }
    if (char === '\t') {
      const spaces = 8 - (this.col % 8);
      for (let count = 0; count < spaces; count += 1) this.putChar(' ');
      return;
    }
    if (char === BEL || char < ' ') return;
    this.putChar(char);
  }

  private putChar(char: string): void {
    const line = this.currentLine();
    while (line.cells.length < this.col) line.cells.push({ char: ' ', style: defaultStyle() });
    line.cells[this.col] = { char, style: cloneStyle(this.style) };
    this.col += 1;
  }

  private applySequence(sequence: EscSequence): void {
    if (sequence.type === 'osc') return;
    if (sequence.type === 'esc') {
      if (sequence.command === 'c') this.reset();
      return;
    }
    const csiSequence = sequence;
    if (csiSequence.intermediates !== '') return;
    this.applyCsi(csiSequence);
  }

  private applyCsi(sequence: CsiSequence): void {
    const amount = Math.max(1, firstParam(sequence.params, 1));
    if (sequence.command === 'm') {
      this.style = applySgrCodes(sequence.params, this.style);
    } else if (sequence.command === 'K') {
      this.clearLine(firstParam(sequence.params, 0));
    } else if (sequence.command === 'J') {
      this.clearDisplay(firstParam(sequence.params, 0));
    } else if (sequence.command === 'G') {
      this.col = Math.max(0, amount - 1);
    } else if (sequence.command === 'A') {
      this.row = Math.max(0, this.row - amount);
    } else if (sequence.command === 'B') {
      this.row += amount;
      this.currentLine();
    } else if (sequence.command === 'C') {
      this.col += amount;
    } else if (sequence.command === 'D') {
      this.col = Math.max(0, this.col - amount);
    } else if (sequence.command === 'H' || sequence.command === 'f') {
      const params = parseParams(sequence.params, 1);
      this.row = Math.max(0, (Number.isFinite(params[0]) ? params[0] : 1) - 1);
      this.col = Math.max(0, (Number.isFinite(params[1]) ? params[1] : 1) - 1);
      this.currentLine();
    }
  }

  private clearLine(mode: number): void {
    const line = this.currentLine();
    line.clearedByControl = true;
    if (mode === 0) line.cells.length = Math.min(line.cells.length, this.col);
    else if (mode === 1) {
      for (let index = 0; index <= this.col && index < line.cells.length; index += 1) delete line.cells[index];
    } else if (mode === 2) line.cells = [];
  }

  private clearDisplay(mode: number): void {
    if (mode === 2) {
      this.lines = [createLine()];
      this.row = 0;
      this.col = 0;
    } else if (mode === 0) {
      this.clearLine(0);
      this.lines.splice(this.row + 1);
    } else if (mode === 1) {
      this.clearLine(1);
      this.lines.splice(0, this.row);
      this.row = 0;
    }
  }

  private reset(): void {
    this.lines = [createLine()];
    this.row = 0;
    this.col = 0;
    this.style = defaultStyle();
  }

  private renderLine(line: TerminalLine): string {
    const cells = line.cells.slice();
    while (cells.length > 0 && (cells.at(-1)?.char ?? ' ') === ' ') cells.pop();
    let html = '';
    let text = '';
    let style: TerminalStyle | null = null;
    const flush = () => {
      if (style) html += renderTextSegment(text, style);
      text = '';
      style = null;
    };

    for (let index = 0; index < cells.length; index += 1) {
      const cell = cells[index] ?? { char: ' ', style: defaultStyle() };
      if (!style) style = cell.style;
      if (!sameStyle(style, cell.style)) {
        flush();
        style = cell.style;
      }
      text += cell.char;
    }
    flush();
    return html;
  }
}

function readEscSequence(text: string, index: number): EscSequence | null {
  const next = text[index + 1];
  if (!next) return null;
  if (next === '[') return readCsiSequence(text, index);
  if (next === ']') return readOscSequence(text, index);
  return { type: 'esc', command: next, length: 2 };
}

function readCsiSequence(text: string, index: number): CsiSequence | null {
  let cursor = index + 2;
  let params = '';
  while (cursor < text.length && text.charCodeAt(cursor) >= 0x30 && text.charCodeAt(cursor) <= 0x3f) {
    params += text[cursor];
    cursor += 1;
  }

  let intermediates = '';
  while (cursor < text.length && text.charCodeAt(cursor) >= 0x20 && text.charCodeAt(cursor) <= 0x2f) {
    intermediates += text[cursor];
    cursor += 1;
  }

  if (cursor >= text.length) return null;
  const command = text[cursor];
  if (text.charCodeAt(cursor) < 0x40 || text.charCodeAt(cursor) > 0x7e) return null;
  return { type: 'csi', params, intermediates, command, length: cursor - index + 1 };
}

function readOscSequence(text: string, index: number): EscSequence | null {
  let cursor = index + 2;
  while (cursor < text.length) {
    if (text[cursor] === BEL) return { type: 'osc', length: cursor - index + 1 };
    if (text[cursor] === ESC && text[cursor + 1] === '\\') return { type: 'osc', length: cursor - index + 2 };
    cursor += 1;
  }
  return null;
}

function cssForStyle(style: TerminalStyle): string {
  const declarations: string[] = [];
  const fg = style.inverse ? style.bg : style.fg;
  const bg = style.inverse ? style.fg : style.bg;
  if (fg) declarations.push(`color: ${fg}`);
  if (bg) declarations.push(`background-color: ${bg}`);
  if (style.bold) declarations.push('font-weight: 700');
  if (style.faint) declarations.push('opacity: 0.7');
  if (style.italic) declarations.push('font-style: italic');
  if (style.underline) declarations.push('text-decoration: underline');
  return declarations.join('; ');
}

function renderTextSegment(text: string, style: TerminalStyle): string {
  if (!text) return '';
  const html = linkifyEscapedText(text);
  const css = cssForStyle(style);
  return css ? `<span style="${escapeHtml(css)}">${html}</span>` : html;
}

export function renderTerminalOutput(text: string): string {
  const terminal = new VirtualTerminal();
  terminal.write(text);
  return terminal.renderHtml();
}
