// Synthetic protocol/renderer acceptance, never real API or deployed-app evidence.
import { chromium, expect } from '@playwright/test';
import { mkdir, writeFile, readdir, readFile } from 'node:fs/promises';
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
const uiRoot = fileURLToPath(new URL('../../', import.meta.url));
const repoRoot = fileURLToPath(new URL('../../../', import.meta.url));
process.chdir(uiRoot);
const revision = execFileSync('git', ['rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
const hash = createHash('sha256');
for (const folder of ['src/lib/usage', 'e2e/usage']) {
  for (const file of (await readdir(`${uiRoot}${folder}`, { withFileTypes: true })).filter((entry) => entry.isFile()).sort((a, b) => a.name.localeCompare(b.name))) {
    hash.update(`${folder}/${file.name}`).update(await readFile(`${uiRoot}${folder}/${file.name}`));
  }
}
hash.update(await readFile(`${uiRoot}vite.usage.config.ts`));
const sourceHash = hash.digest('hex');
const dirty = execFileSync('git', ['status', '--porcelain', '--', 'ui/'], { encoding: 'utf8', cwd: repoRoot }).trim().length > 0;
if (dirty) throw new Error('Commit the reviewed task-owned UI checkpoint before capturing revision-labelled evidence.');
const output = process.env.USAGE_CAPTURE_DIR;
if (!output) throw new Error('Set USAGE_CAPTURE_DIR to a task-owned directory.');
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1200 }, hasTouch: true });
const page = await context.newPage();
const network = [], errors = [], results = [], packets = [], protocolCalls = [];
page.on('request', (request) => network.push({ method: request.method(), url: request.url() }));
page.on('pageerror', (error) => errors.push(error.message));
async function loaded(heading) {
  await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
  await expect(page.getByText('Loading active usage view…', { exact: true })).toHaveCount(0);
}
async function capture(name, heading) {
  await loaded(heading);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const geometry = await page.evaluate(() => ({
    viewport: innerWidth, scrollWidth: document.documentElement.scrollWidth,
    floating: [...document.querySelectorAll('body *')].filter((el) => ['fixed', 'sticky'].includes(getComputedStyle(el).position)).length
  }));
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.viewport);
  expect(geometry.floating).toBe(0);
  const path = `${output}/${revision.slice(0, 12)}-${sourceHash.slice(0, 12)}-${name}.png`;
  await page.screenshot({ path, fullPage: true });
  results.push({ path, geometry, fixtureBacked: true, apiBacked: false, revision, sourceHash, dirty });
}
async function collect() {
  packets.push(...await page.evaluate(() => window.__usageFixturePackets));
  protocolCalls.push(...await page.evaluate(() => window.__usageFixtureCalls));
}
try {
  await page.goto('http://127.0.0.1:5197/');
  await expect(page.getByText(/FIXTURE-BACKED/).first()).toBeVisible();
  await loaded('Observed attempt lifecycle');
  await page.getByRole('button', { name: 'Costs & Tokens', exact: true }).click();
  await page.getByRole('combobox', { name: 'Group usage by' }).selectOption('provider');
  await capture('grouped-costs-desktop', 'Token consumption over time');
  const point = page.getByRole('button', { name: /^Chart point Fixture Alpha · Known input tokens: 2026-10-29/ });
  await point.focus();
  await point.press('Enter');
  await loaded('Sessions & Requests');
  expect(new URL(page.url()).searchParams.get('usage_preciseStart')).toBe('2026-10-29T00:00:00.000Z');
  expect(new URL(page.url()).searchParams.get('usage_preciseEnd')).toBe('2026-10-30T00:00:00.000Z');
  await capture('exact-group-request-population', 'Sessions & Requests');
  await page.getByRole('button', { name: 'Next request page', exact: true }).click();
  await expect(page.getByText('usage-fixture-logical-2', { exact: true }).first()).toBeVisible();
  expect(page.url()).not.toContain('usage-fixture-detail-next');
  await page.goBack();
  await expect(page.getByText('usage-fixture-logical-1', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Physical attempts', exact: true }).click();
  await expect(page.getByRole('button', { name: 'usage-fixture-attempt-1', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Next attempt page', exact: true }).click();
  await expect(page.getByRole('button', { name: 'usage-fixture-attempt-3', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Back to source view', exact: true }).click();
  await loaded('Token consumption over time');
  await page.getByRole('button', { name: 'Sessions & Requests', exact: true }).click();
  await page.getByRole('button', { name: 'Sessions', exact: true }).click();
  await expect(page.getByRole('button', { name: 'usage-fixture-cognis-session', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Next session page', exact: true }).click();
  await page.getByRole('button', { name: 'First page', exact: true }).click();
  await page.getByRole('combobox', { name: 'Session source' }).selectOption('proxy');
  await page.getByRole('textbox', { name: 'Session search' }).fill('usage_fixture_unverified_label');
  await page.getByRole('textbox', { name: 'Session search' }).press('Tab');
  await expect(page.getByRole('button', { name: 'usage_fixture_unverified_label', exact: true })).toBeVisible();
  expect(page.url()).not.toContain('usage_fixture_unverified_label');
  await capture('proxy-retained-session-source', 'Sessions & Requests');
  await page.getByRole('button', { name: 'usage_fixture_unverified_label', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Physical trail', exact: true }).first()).toBeVisible();
  expect(page.url()).not.toContain('v2.');
  expect(new URL(page.url()).searchParams.has('usage_sessionId')).toBe(false);
  await page.getByRole('button', { name: 'Physical trail', exact: true }).first().click();
  await expect(page.getByRole('button', { name: 'usage-fixture-attempt-3', exact: true })).toBeVisible();
  await page.getByRole('listbox', { name: 'Explorer error categories' }).selectOption('rate_limit');
  await page.getByRole('textbox', { name: 'Explorer logical request IDs' }).fill('usage-fixture-logical-2');
  await page.getByRole('textbox', { name: 'Explorer logical request IDs' }).press('Tab');
  await expect(page.getByRole('button', { name: 'usage-fixture-attempt-3', exact: true })).toBeVisible();
  const finalBody = await page.evaluate(() => window.__usageFixturePackets.filter((packet) => packet.schema === 'UsageAttemptQuery').at(-1).value);
  expect(finalBody.filters).toMatchObject({ error_categories: ['rate_limit'], logical_request_ids: ['usage-fixture-logical-2'] });
  expect(finalBody.filters.session_ids).toBeUndefined();
  await page.getByRole('button', { name: 'usage-fixture-attempt-3', exact: true }).click();
  await capture('proxy-filtered-physical-detail', 'Attempt details');
  await expect(page.getByRole('link', { name: /transcript/i })).toHaveCount(0);
  await page.getByRole('combobox', { name: 'Fixture state' }).selectOption('expired');
  await capture('expired-selection-no-broaden', 'Usage unavailable');
  await expect(page.getByRole('button', { name: 'usage-fixture-attempt-3', exact: true })).toHaveCount(0);
  await collect();

  await page.goto('http://127.0.0.1:5197/');
  await loaded('Observed attempt lifecycle');
  await page.getByRole('combobox', { name: 'Fixture scope' }).selectOption('project');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('combobox', { name: 'Usage view' }).selectOption('costs');
  await capture('project-grouped-phone', 'Token consumption over time');
  await page.getByRole('combobox', { name: 'Usage view' }).selectOption('explorer');
  await page.getByRole('button', { name: 'Requests', exact: true }).tap();
  await page.getByRole('button', { name: 'Physical trail', exact: true }).first().tap();
  await expect(page.getByRole('button', { name: 'Protected attempt · details', exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Protected attempt · details', exact: true }).first().tap();
  await capture('project-protected-phone', 'Attempt details');
  await expect(page.getByRole('textbox', { name: 'Explorer client ID' })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'Explorer key ID' })).toHaveCount(0);
  await expect(page.getByText('Authorized client identifiers')).toHaveCount(0);
  await page.getByRole('combobox', { name: 'Fixture state' }).selectOption('denied');
  await capture('scope-denial-cleared-phone', 'Usage unavailable');
  await expect(page.getByRole('heading', { name: 'Attempt details', exact: true })).toHaveCount(0);
  await collect();
  const validation = spawnSync('python3', [`${uiRoot}e2e/usage/validate-fixtures.py`], { input: JSON.stringify(packets), encoding: 'utf8' });
  expect(validation.status, validation.stderr).toBe(0);
  expect(protocolCalls.every((call) => call.method === 'POST' && !call.path.includes('?'))).toBe(true);
  expect(errors).toEqual([]);
  expect(network.filter((request) => request.method !== 'GET')).toEqual([]);
  expect(network.filter((request) => new URL(request.url).pathname.startsWith('/api'))).toEqual([]);
  await writeFile(`${output}/protocol-packets.json`, JSON.stringify(packets));
  await writeFile(`${output}/provenance.json`, JSON.stringify({ revision, sourceHash, dirty, browser: browser.version(), results, errors,
    fixtureBacked: true, apiBacked: false, validatedPackets: packets.length, validation: validation.stdout.trim(), protocolCalls, networkApiRequests: 0 }, null, 2));
  console.log(JSON.stringify(results, null, 2));
} finally { await context.close(); await browser.close(); }
