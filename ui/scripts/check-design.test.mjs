import { test } from 'node:test';
import assert from 'node:assert/strict';
import { changedDiff, lintDiff } from './check-design.mjs';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const diff = (body, file = 'ui/src/routes/page.svelte') => `+++ b/${file}\n@@ -1,2 +1,3 @@\n${body}\n`;
test('flags added violations but ignores existing debt', () => {
  assert.equal(lintDiff(diff(' <button class="text-[10px]">Existing</button>\n+<p>New fact</p>')).length, 0);
  assert.equal(lintDiff(diff('+<button class="text-[10px]">New</button>')).length, 2);
});
test('flags colours, reliable cards and pills, and defensive copy', () => {
  assert.equal(lintDiff(diff('+<div class="rounded-xl border bg-surface">Card</div>')).length, 1);
  assert.equal(lintDiff(diff('+<span class="rounded-full px-2 text-xs">Pill</span>')).length, 1);
  assert.equal(lintDiff(diff('+<p style="color: #abcdef">Not guaranteed</p>')).length, 2);
  assert.equal(lintDiff(diff('+const label = "Best effort";', 'ui/src/lib/copy.ts')).length, 1);
});
test('allows primitive buttons, comments, ordinary prose and test fixtures', () => {
  assert.equal(lintDiff(diff('+<button>Action</button>', 'ui/src/lib/components/ui/Button.svelte')).length, 0);
  assert.equal(lintDiff(diff('+// Do not log payloads', 'ui/src/lib/copy.ts')).length, 0);
  assert.equal(lintDiff(diff('+<p>May 2026 · 42 requests</p>')).length, 0);
  assert.equal(lintDiff(diff('+<button class="text-[10px]">Test</button>', 'ui/src/lib/View.test.ts')).length, 0);
});
test('flags multiline markup and RGB literals but allows colour tokens', () => {
  assert.equal(lintDiff(diff('+<button\n+ class="rounded-xl border\n+ bg-surface">Action</button>')).length, 2);
  assert.equal(lintDiff(diff('+<span class="\n+ rounded-full px-2\n+ text-xs">State</span>')).length, 1);
  assert.equal(lintDiff(diff('+<p style="color:rgb(12 34 56)">Fact</p>')).length, 1);
  assert.equal(lintDiff(diff('+<p style="color:rgb(var(--color-secondary))">Fact</p>')).length, 0);
});
test('real git diff covers committed, staged, unstaged and untracked additions, not old debt', () => {
  const root = mkdtempSync(path.join(tmpdir(), 'cognis-design-lint-'));
  const git = (...args) => execFileSync('git', args, { cwd: root, stdio: 'pipe', encoding: 'utf8' }).trim();
  try {
    git('init');
    mkdirSync(path.join(root, 'ui/src'), { recursive: true });
    writeFileSync(path.join(root, 'ui/src/page.svelte'), '<button>Existing debt</button>\n');
    git('add', '.');
    git('-c', 'user.name=Design lint test', '-c', 'user.email=design-lint@test.local', 'commit', '-m', 'Baseline');
    const base = git('rev-parse', 'HEAD');
    assert.deepEqual(lintDiff(changedDiff(root, base)), []);
    writeFileSync(path.join(root, 'ui/src/page.svelte'), '<button>Existing debt</button>\n<p class="text-[10px]">Added</p>\n');
    git('add', '.');
    git('-c', 'user.name=Design lint test', '-c', 'user.email=design-lint@test.local', 'commit', '-m', 'Added violation');
    writeFileSync(path.join(root, 'ui/src/staged.svelte'), '<button>Staged</button>\n');
    git('add', '.');
    writeFileSync(path.join(root, 'ui/src/page.svelte'), '<button>Existing debt</button>\n<p class="text-[10px]">Added</p>\n<button>Unstaged</button>\n');
    writeFileSync(path.join(root, 'ui/src/new.svelte'), '<button>Untracked</button>\n');
    const findings = lintDiff(changedDiff(root, base));
    assert.equal(findings.length, 4);
    assert.equal(findings.some((finding) => finding.file.endsWith('page.svelte') && finding.line === 1), false);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
test('CI needs fetched ancestry and the explicit event base to lint committed additions', () => {
  const root = mkdtempSync(path.join(tmpdir(), 'cognis-design-ci-'));
  const source = path.join(root, 'source'), clone = path.join(root, 'checkout');
  const git = (cwd, ...args) => execFileSync('git', args, { cwd, stdio: 'pipe', encoding: 'utf8' }).trim();
  try {
    git(root, 'init', '-b', 'main', source);
    mkdirSync(path.join(source, 'ui/src'), { recursive: true });
    writeFileSync(path.join(source, 'ui/src/page.svelte'), '<p>Baseline</p>\n');
    git(source, 'add', '.');
    git(source, '-c', 'user.name=Design lint test', '-c', 'user.email=design-lint@test.local', 'commit', '-m', 'Baseline');
    const base = git(source, 'rev-parse', 'HEAD');
    writeFileSync(path.join(source, 'ui/src/page.svelte'), '<p>Baseline</p>\n<button>Added violation</button>\n');
    git(source, 'add', '.');
    git(source, '-c', 'user.name=Design lint test', '-c', 'user.email=design-lint@test.local', 'commit', '-m', 'Added violation');
    git(root, 'clone', '--depth=1', pathToFileURL(source).href, clone);
    assert.throws(() => changedDiff(clone, base));
    git(clone, 'fetch', '--unshallow', 'origin');
    assert.equal(lintDiff(changedDiff(clone, base)).length, 1);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
