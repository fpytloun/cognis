import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

export const bannedPhrases = /\b(?:does not|do not|will not|not guaranteed|best effort|advisory|for reference|remains enforced|capture pipeline|non-blocking capture|no payload content|not an invoice|not invoices|not a cash invoice|no broader request|idempotency|reconciliation|convergence)\b/i;
const excluded = /(?:\.(?:test|spec)\.[^/]+$|(?:^|\/)(?:__tests__|fixtures)(?:\/|$)|(?:^|\/)[^/]*fixture[^/]*\.[^/]+$)/i;

function inlineClasses(file, rows, findings) {
  if (!file.startsWith('ui/src/') || !/\.(svelte|html)$/.test(file) || excluded.test(file) || file.startsWith('ui/src/lib/components/ui/')) return;
  const source = rows.map((row) => row.text).join('\n');
  for (const match of source.matchAll(/\bclass\s*=\s*["']([^"']*)["']/g)) {
    const start = source.slice(0, match.index).split('\n').length - 1;
    const added = rows.slice(start, start + match[0].split('\n').length).find((row) => row.added);
    if (!added) continue;
    const value = match[1];
    if (/\brounded-(?:xl|2xl|3xl)\b/.test(value) && /\bborder\b/.test(value) && /\bbg-(?:surface|raised|elevated)\b/.test(value))
      findings.push({ file, line: added.line, rule: 'Use Card or Panel, not inline card classes' });
    if (/\brounded-full\b/.test(value) && /\bpx-[\d.]+\b/.test(value) && /\b(?:text-xs|border|bg-[\w-]+)\b/.test(value))
      findings.push({ file, line: added.line, rule: 'Use Badge, not inline pill classes' });
  }
}

/** Small ratchet over added production lines, not a debt snapshot. */
export function lintDiff(diff) {
  const findings = [];
  let file = '', line = 0, script = false, style = false;
  let rows = [];
  for (const entry of diff.split('\n')) {
    if (entry.startsWith('+++ b/')) {
      inlineClasses(file, rows, findings); rows = [];
      file = entry.slice(6); script = false; style = false; continue;
    }
    const hunk = /^@@ .* \+(\d+)(?:,\d+)? @@/.exec(entry);
    if (hunk) { line = Number(hunk[1]); continue; }
    if (!entry.startsWith('+') && !entry.startsWith(' ')) continue;
    const added = entry.startsWith('+');
    const text = entry.slice(1);
    rows.push({ text, added, line });
    const eligible = file.startsWith('ui/src/') && /\.(svelte|ts|js|css|html)$/.test(file) && !excluded.test(file);
    if (eligible && added) {
      const report = (rule) => findings.push({ file, line, rule });
      if (/\btext-\[\d+(?:\.\d+)?px\]/.test(text)) report('Use the type scale, not text-[Npx]');
      if (/(?:[:\s"'([])#[\da-f]{3}(?:[\da-f]{3})?(?:[\da-f]{2})?\b/i.test(text)) report('Use colour tokens, not hex colours');
      if (/\b(?:rgb|rgba|hsl|hsla)\(\s*[\d.]/i.test(text)) report('Use colour tokens, not literal RGB/HSL colours');
      if (!file.startsWith('ui/src/lib/components/ui/')) {
        if (/<button(?:\s|>|$)/.test(text)) report('Use Button from components/ui');
      }
      if (!style && !/^\s*(?:\/\/|\/\*|\*|<!--)/.test(text)) {
        const candidates = file.endsWith('.svelte') && !script ? [text] : [...text.matchAll(/(['"`])((?:\\.|(?!\1).)*)\1/g)].map((m) => m[2]);
        if (candidates.some((value) => bannedPhrases.test(value))) report('Replace defensive or implementation copy with a fact or action');
      }
    }
    if (/<script\b/.test(text)) script = true;
    if (/<\/script>/.test(text)) script = false;
    if (/<style\b/.test(text)) style = true;
    if (/<\/style>/.test(text)) style = false;
    line++;
  }
  inlineClasses(file, rows, findings);
  return findings;
}

export function changedDiff(root, base) {
  const mergeBase = execFileSync('git', ['merge-base', base, 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
  const diff = execFileSync('git', ['diff', '--no-ext-diff', '--no-renames', '--unified=100000', mergeBase, '--', 'ui/src'], { cwd: root, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
  const untracked = execFileSync('git', ['ls-files', '--others', '--exclude-standard', '-z', '--', 'ui/src'], { cwd: root, encoding: 'utf8' }).split('\0').filter(Boolean);
  return diff + untracked.map((file) => {
    const lines = readFileSync(path.join(root, file), 'utf8').split('\n');
    return `\n+++ b/${file}\n@@ -0,0 +1,${lines.length} @@\n${lines.map((line) => `+${line}`).join('\n')}\n`;
  }).join('');
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = fileURLToPath(new URL('../../', import.meta.url));
  try {
    const findings = lintDiff(changedDiff(root, process.env.DESIGN_LINT_BASE || 'origin/main'));
    for (const finding of findings) console.error(`${finding.file}:${finding.line}: ${finding.rule}`);
    if (findings.length) process.exitCode = 1;
    else console.log('Design/copy lint: added lines pass');
  } catch (error) {
    console.error(`Design/copy lint could not read the base: ${error.message}`);
    process.exitCode = 1;
  }
}
