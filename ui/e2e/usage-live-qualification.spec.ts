/**
 * Real served application + authentication + usage SQL. Synthetic ledger only.
 * Launched by tests/e2e/test_usage_live_browser_acceptance.py; no API mocks.
 */
import { test, expect, type Page, type TestInfo } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { login } from './helpers';
import { usageGeometry } from './usage-geometry';

const output = process.env.COGNIS_USAGE_RUN_DIR;
test.skip(!output, 'Requires the isolated real-app Python bridge');
const seed = output ? JSON.parse(readFileSync(resolve(output, 'seed.json'), 'utf8')) : {};
const pin = process.env.COGNIS_USAGE_BASE_REVISION?.slice(0, 12);
const packets: { path: string; request: Record<string, unknown>; status: number; body: any }[] = [];
const network: { method: string; path: string }[] = [];
const errors: string[] = [];
const captures: unknown[] = [];
const lifecycle: unknown[] = [];

test.use({ viewport: { width: 1440, height: 1000 }, hasTouch: true });

function url(surface = 'overview', scope = 'personal', project = false) {
  const params = new URLSearchParams({
    usage_start: seed.start, usage_end: seed.end,
    usage_surface: surface, usage_scope: scope, usage_group: 'provider',
  });
  if (project) params.set('tab', 'usage');
  return `${project ? `/projects/${seed.project}` : '/usage'}?${params}`;
}

async function loaded(page: Page, heading: string) {
  if (heading === 'Observed attempt lifecycle') {
    const summary = page.getByText('Attempt lifecycle and accounting details', { exact: true });
    await expect(summary).toBeVisible();
    if (await summary.locator('..').getAttribute('open') === null) await summary.click();
  }
  await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
  await expect(page.getByText('Loading active usage view…', { exact: true })).toHaveCount(0);
  if (heading === 'Sessions & Requests') {
    const mode = new URL(page.url()).searchParams.get('usage_explorer') ?? 'sessions';
    const labels: Record<string, string> = {
      sessions: 'Returned session page · exact data and protected drills',
      requests: 'Returned request observations · exact data and protected trails',
      attempts: 'Returned physical attempts · exact data and selection',
    };
    const summary = page.getByText(labels[mode], { exact: true });
    await expect(summary).toBeVisible();
    if (await summary.locator('..').getAttribute('open') === null) await summary.click();
  }
  await page.evaluate(() => new Promise<void>((done) =>
    requestAnimationFrame(() => requestAnimationFrame(() => done()))));
}

async function settingsLoaded(page: Page, section: 'system' | 'account' = 'system') {
  await expect(page.getByText('Loading settings', { exact: true })).toHaveCount(0);
  if (section === 'system') {
    await expect(page.getByTestId('system-settings-editor')).toBeVisible();
    await expect(page.getByTestId('system-settings-editor').getByRole('spinbutton').first()).toBeVisible();
  } else {
    await expect(page.getByRole('main').getByRole('textbox', { name: 'Display name', exact: true })).toBeVisible();
    await expect(page.getByRole('main').getByRole('button', { name: 'Sign out', exact: true })).toBeVisible();
  }
  await page.evaluate(() => new Promise<void>((done) =>
    requestAnimationFrame(() => requestAnimationFrame(() => done()))));
}

async function capture(page: Page, name: string, heading: string, info: TestInfo, root = '.usage-workspace', scroll = true) {
  if (root === '[data-app-content]') await settingsLoaded(page);
  else await loaded(page, heading);
  if (scroll) await page.getByRole('heading', { name: heading, exact: true }).scrollIntoViewIfNeeded();
  await page.evaluate(() => new Promise<void>((done) =>
    requestAnimationFrame(() => requestAnimationFrame(() => done()))));
  const geometry = await page.evaluate(usageGeometry, root);
  const filename = `${pin}-${name}.png`;
  const path = resolve(output!, filename);
  await page.screenshot({ path, fullPage: true });
  captures.push({ filename, geometry, apiBacked: true, syntheticData: true, test: info.title });
  expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth);
  expect(geometry.hits).toEqual([]);
}

function latestDetail() {
  return packets.filter((packet) => packet.path.endsWith('/attempts/search')).at(-1)!;
}
function sameContext(actual: Record<string, unknown>, expected: Record<string, unknown>) {
  // Compare opaque references without printing them in assertion diagnostics.
  expect(actual.selection_ref === expected.selection_ref).toBe(true);
  expect(actual.scope).toEqual(expected.scope);
  expect(actual.start).toBe(expected.start);
  expect(actual.end).toBe(expected.end);
  expect(actual.filters).toEqual(expected.filters);
}

async function nativeEntry(page: Page, stage: string) {
  // Read the installed Kit's native entry metadata; never write or copy state.
  const evidence = await page.evaluate(async () => {
    const hash = async (value: string) => Array.from(new Uint8Array(
      await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)),
    )).map((byte) => byte.toString(16).padStart(2, '0')).join('');
    const state = window.history.state;
    const usage = state?.['sveltekit:states']?.usage;
    const selection = usage?.context?.selectionRef;
    const authority = usage?.authority;
    return {
      path: location.pathname,
      urlHash: await hash(location.href),
      documentTimeOrigin: performance.timeOrigin,
      historyLength: history.length,
      historyIndex: state?.['sveltekit:history'] ?? null,
      navigationIndex: state?.['sveltekit:navigation'] ?? null,
      usagePresent: Boolean(usage),
      selectionPresent: typeof selection === 'string' && selection.length > 0,
      selectionHash: typeof selection === 'string' ? await hash(selection) : null,
      authorityHash: typeof authority === 'string' ? await hash(authority) : null,
      usageHash: usage ? await hash(JSON.stringify(usage)) : null,
    };
  });
  lifecycle.push({ nativeEntry: stage, ...evidence });
  return evidence;
}

async function delayedReload(page: Page, label: string) {
  const expected = latestDetail().request;
  const before = network.filter((request) => request.path.startsWith('/api/v1/usage/')).length;
  const detailCount = packets.filter((packet) => packet.path.endsWith('/attempts/search')).length;
  let arrived!: () => void;
  let release!: () => void;
  const requestArrived = new Promise<void>((done) => arrived = done);
  const gate = new Promise<void>((done) => release = done);
  const handler = async (route: import('@playwright/test').Route) => {
    arrived();
    await gate;
    await route.continue();
  };
  await page.evaluate(() => sessionStorage.setItem('usage-checkpoint-delay', '1'));
  await page.route('**/api/auth/me', handler);
  try {
    const reload = page.reload({ waitUntil: 'domcontentloaded' });
    await requestArrived;
    await expect(page.getByText('Loading workspace', { exact: true })).toBeVisible();
    expect(network.filter((request) => request.path.startsWith('/api/v1/usage/')).length).toBe(before);
    release();
    await reload;
    await expect(page.getByText('Preparing workspace', { exact: true })).toBeVisible();
    await page.waitForFunction(() => (window as any).__usageReadiness?.blocked === true);
    expect(network.filter((request) => request.path.startsWith('/api/v1/usage/')).length).toBe(before);
    await page.evaluate(() => (window as any).__usageReadiness.release());
    await loaded(page, 'Sessions & Requests');
    await expect.poll(() => packets.filter((packet) => packet.path.endsWith('/attempts/search')).length).toBeGreaterThan(detailCount);
    sameContext(latestDetail().request, expected);
    lifecycle.push({ label, authGateObserved: true, offlineGateObserved: true, earlyUsageRequests: 0, exactContext: true });
  } finally {
    release();
    await page.unroute('**/api/auth/me', handler);
  }
}

async function fence(page: Page) {
  await page.route('**/*', async (route) => {
    const host = new URL(route.request().url()).hostname;
    if (['127.0.0.1', 'localhost', '[::1]'].includes(host)) await route.continue();
    else await route.abort('blockedbyclient');
  });
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('request', (request) => {
    network.push({ method: request.method(), path: new URL(request.url()).pathname });
  });
  page.on('response', async (response) => {
    const path = new URL(response.url()).pathname;
    if (!path.startsWith('/api/v1/usage/')) return;
    try {
      packets.push({
        path, request: response.request().postDataJSON() ?? {},
        status: response.status(), body: await response.json(),
      });
    } catch { /* Aborted navigation does not supply a JSON response. */ }
  });
}

test.beforeEach(async ({ page }) => {
  packets.length = 0;
  network.length = 0;
  errors.length = 0;
  captures.length = 0;
  lifecycle.length = 0;
  await fence(page);
  await login(page);
  expect((await page.request.get('/api/auth/me')).ok()).toBe(true);
  await page.addInitScript(() => {
    if (sessionStorage.getItem('usage-checkpoint-delay') !== '1') return;
    sessionStorage.removeItem('usage-checkpoint-delay');
    const native = IDBFactory.prototype.databases;
    let release!: () => void;
    const gate = new Promise<void>((done) => release = done);
    const readiness = { blocked: false, release };
    (window as any).__usageReadiness = readiness;
    IDBFactory.prototype.databases = async function () {
      readiness.blocked = true;
      await gate;
      return native.call(this);
    };
  });
});

test.afterEach(async ({ browser }, info) => {
  const require = createRequire(resolve('package.json'));
  const evidence = JSON.stringify({
    checkpoint: process.env.COGNIS_USAGE_BASE_REVISION,
    browser: browser.version(), packets, network, errors, captures, lifecycle,
    apiBacked: true, syntheticData: true, authOverride: false,
    actualExpiryProven: false,
    expiryLimitation: 'The unmodified real server uses a 3600-second TTL; no clock or token codec override.',
    finalCaptureQualification: false, physicalDevice: false,
    runtime: {
      cwd: process.cwd(), node: process.version,
      vite: require.resolve('vite'), svelte: require.resolve('svelte/compiler'),
      playwright: require.resolve('@playwright/test'),
    },
  }, null, 2);
  writeFileSync(resolve(output!, `browser-evidence-${createHash('sha256').update(info.title).digest('hex').slice(0, 12)}.json`), evidence);
  writeFileSync(resolve(output!, 'browser-evidence.json'), evidence);
});

test('global and personal grouped accounting, Cognis session and physical attempt', async ({ page }, info) => {
  await page.goto(url('overview', 'admin'));
  await loaded(page, 'Observed attempt lifecycle');
  await expect(page.getByRole('button', { name: /^Finalized contributions/ })).toContainText('12');
  await capture(page, 'global-overview', 'Observed attempt lifecycle', info);
  await page.getByRole('combobox', { name: 'Usage scope' }).selectOption('personal');
  await loaded(page, 'Observed attempt lifecycle');
  await expect(page.getByRole('button', { name: /^Finalized contributions/ })).toContainText('8');
  await expect(page.getByRole('button', { name: /^Known priced subtotal/ })).toContainText('2 unpriced attempts');
  await capture(page, 'personal-honest-unpriced', 'Observed attempt lifecycle', info);
  await page.getByRole('button', { name: 'Costs & Tokens', exact: true }).click();
  await loaded(page, 'Token consumption over time');
  await page.getByRole('combobox', { name: 'Group usage by' }).selectOption('provider');
  await expect(page.getByText('Cost ranking requires fully priced, compatible contributions.', { exact: false })).toBeVisible();
  const refusal = packets.find((packet) => packet.status === 422 && packet.body.error?.code === 'incomplete_cost_ranking');
  expect(refusal).toBeTruthy();
  await capture(page, 'personal-provider-groups', 'Token consumption over time', info);
  const point = page.getByRole('button', {
    name: new RegExp(`^Chart point ${seed.provider} · Known input tokens:`),
  }).first();
  await point.focus();
  await point.press('Enter');
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: 'Physical trail', exact: true }).first()).toBeVisible();
  const drill = packets.filter((packet) => packet.path.endsWith('/requests')).at(-1)!;
  expect((drill.request.filters as any).provider_ids).toEqual([seed.provider]);
  expect(Date.parse(drill.request.end as string) - Date.parse(drill.request.start as string)).toBe(86400000);
  await page.getByRole('button', { name: 'Back to source view', exact: true }).click();
  await loaded(page, 'Token consumption over time');
  await page.getByRole('button', { name: 'Requests', exact: true }).click();
  await page.getByRole('button', { name: 'Sessions', exact: true }).click();
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: seed.session, exact: true })).toBeVisible();
  await page.getByRole('button', { name: seed.session, exact: true }).click();
  await loaded(page, 'Sessions & Requests');
  await page.getByRole('button', { name: 'Physical trail', exact: true }).first().click();
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: /^attempt_usage_browser_/ }).first()).toBeVisible();
  await page.getByRole('button', { name: /^attempt_usage_browser_/ }).first().click();
  await capture(page, 'cognis-attempt-desktop', 'Attempt details', info);
  expect(errors).toEqual([]);
});

test('proxy retry trail, exact filters, back/reload and missing/malformed protected selection', async ({ page, browser }, info) => {
  await page.goto(url('explorer'));
  await loaded(page, 'Sessions & Requests');
  await page.getByRole('combobox', { name: 'Session source' }).selectOption('proxy');
  await page.getByRole('textbox', { name: 'Session search' }).fill(seed.proxy);
  await page.getByRole('textbox', { name: 'Session search' }).press('Tab');
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: seed.proxy, exact: true })).toBeVisible();
  expect(page.url().includes(seed.proxy)).toBe(false);
  await page.getByRole('button', { name: seed.proxy, exact: true }).click();
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByText('Unknown — not inferred from attempt outcome').first()).toBeVisible();
  await capture(page, 'proxy-retained-requests', 'Sessions & Requests', info);
  const retry = page.getByRole('row').filter({ hasText: 'request_usage_browser_retry' }).first();
  await retry.getByRole('button', { name: 'Physical trail', exact: true }).click();
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: 'attempt_usage_browser_5', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'attempt_usage_browser_6', exact: true })).toBeVisible();
  const protectedUrl = page.url();
  expect(protectedUrl.includes('v2.')).toBe(false);
  expect(new URL(protectedUrl).searchParams.has('usage_sessionId')).toBe(false);
  await delayedReload(page, 'global protected retry trail');
  await expect(page.getByRole('button', { name: 'attempt_usage_browser_5', exact: true })).toBeVisible();
  await page.getByRole('listbox', { name: 'Explorer error categories' }).selectOption('rate_limit');
  await page.getByRole('textbox', { name: 'Explorer logical request IDs' }).fill('request_usage_browser_retry');
  await page.getByRole('textbox', { name: 'Explorer logical request IDs' }).press('Tab');
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: 'attempt_usage_browser_5', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'attempt_usage_browser_6', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'attempt_usage_browser_5', exact: true }).click();
  await capture(page, 'proxy-filtered-error-attempt', 'Attempt details', info);
  await delayedReload(page, 'global exact error/request filters');
  await expect(page.getByRole('button', { name: 'attempt_usage_browser_5', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'attempt_usage_browser_6', exact: true })).toHaveCount(0);
  const detail = packets.filter((packet) => packet.path.endsWith('/attempts/search')).at(-1)!;
  expect((detail.request.filters as any).error_categories).toEqual(['rate_limit']);
  expect((detail.request.filters as any).logical_request_ids).toEqual(['request_usage_browser_retry']);
  await page.goBack();
  await loaded(page, 'Sessions & Requests');
  await page.goForward();
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: 'attempt_usage_browser_5', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'attempt_usage_browser_6', exact: true })).toHaveCount(0);
  sameContext(latestDetail().request, detail.request);
  const original = detail.request.selection_ref as string;
  const malformed = original.slice(0, 10) + (original[10] === 'A' ? 'B' : 'A') + original.slice(11);
  const rejected = await page.request.post('/api/v1/usage/attempts/search', {
    data: { ...detail.request, selection_ref: malformed },
  });
  expect(rejected.status()).toBe(422);
  writeFileSync(resolve(output!, 'malformed-selection.json'), JSON.stringify({
    status: rejected.status(), body: await rejected.json(), actualExpiry: false,
  }, null, 2));
  await page.getByRole('button', { name: 'Back to source view', exact: true }).click();
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: 'Physical trail', exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Back to source view', exact: true }).click();
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: seed.proxy, exact: true })).toBeVisible();
  const fresh = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const freshPage = await fresh.newPage();
  try {
    await fence(freshPage);
    await login(freshPage);
    const requests: any[] = [];
    freshPage.on('request', (request) => {
      const path = new URL(request.url()).pathname;
      if (path.startsWith('/api/v1/usage/') && !path.endsWith('/catalog')) requests.push(request.postDataJSON());
    });
    await freshPage.goto(protectedUrl);
    await capture(freshPage, 'missing-protected-history', 'Usage unavailable', info);
    expect(requests).toEqual([]);
  } finally { await fresh.close(); }
  expect(errors).toEqual([]);
});

test('project-visible mobile protected drill and current membership revocation', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(url('costs', 'project', true));
  await loaded(page, 'Token consumption over time');
  await expect(page.getByText('Cost ranking requires fully priced, compatible contributions.', { exact: false })).toBeVisible();
  await capture(page, 'project-provider-groups-mobile', 'Token consumption over time', info);
  await page.getByRole('combobox', { name: 'Usage view' }).selectOption('explorer');
  await loaded(page, 'Sessions & Requests');
  await page.getByRole('combobox', { name: 'Session source' }).selectOption('proxy');
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: /^Protected proxy session/ }).first()).toBeVisible();
  await page.getByRole('button', { name: /^Protected proxy session/ }).first().tap();
  await loaded(page, 'Sessions & Requests');
  await page.getByRole('button', { name: 'Physical trail', exact: true }).first().tap();
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: 'Protected attempt · details', exact: true }).first()).toBeVisible();
  await delayedReload(page, 'project protected physical trail');
  await page.getByRole('button', { name: 'Protected attempt · details', exact: true }).first().tap();
  await capture(page, 'project-protected-attempt-mobile', 'Attempt details', info);
  await expect(page.getByRole('textbox', { name: 'Explorer client ID' })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'Explorer key ID' })).toHaveCount(0);
  await expect(page.getByRole('link', { name: /transcript/i })).toHaveCount(0);
  await delayedReload(page, 'project protected detail');
  await expect(page.getByRole('button', { name: 'Protected attempt · details', exact: true }).first()).toBeVisible();
  const detail = packets.filter((packet) => packet.path.endsWith('/attempts/search')).at(-1)!;
  await page.goBack();
  await loaded(page, 'Sessions & Requests');
  await page.goForward();
  await loaded(page, 'Sessions & Requests');
  sameContext(latestDetail().request, detail.request);
  expect(detail.body.items.every((row: any) => row.attempt_id === null)).toBe(true);
  execFileSync(process.env.COGNIS_USAGE_PYTHON!, ['-m', 'tests.e2e.usage_checkpoint.seed', 'revoke'], {
    cwd: resolve('..'), env: process.env, stdio: 'pipe',
  });
  const revoked = await page.request.post('/api/v1/usage/attempts/search', { data: detail.request });
  expect(revoked.status()).toBe(403);
  writeFileSync(resolve(output!, 'current-authority-rejected.json'), JSON.stringify({
    status: revoked.status(), body: await revoked.json(), previouslyValidRef: true,
  }, null, 2));
  await page.getByRole('button', { name: 'Refresh active view', exact: true }).tap();
  await capture(page, 'project-revoked-cleared-mobile', 'Usage unavailable', info);
  await expect(page.getByRole('heading', { name: 'Attempt details', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Protected attempt · details', exact: true })).toHaveCount(0);
  expect(network.some((request) => /\/(turns|messages)$/.test(request.path) && request.method === 'POST')).toBe(false);
  expect(errors).toEqual([]);
});

test('non-usage non-chat mobile shell bounds its scrolling paint between measured chrome', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/settings?tab=system');
  await settingsLoaded(page);
  const bounds = await page.evaluate(() => {
    const content = document.querySelector('[data-app-content]')!.getBoundingClientRect();
    const header = document.querySelector('#main-content > header')!.getBoundingClientRect();
    const nav = document.querySelector('nav[aria-label="Primary"]')!.getBoundingClientRect();
    return { top: content.top, bottom: content.bottom, headerBottom: header.bottom, navTop: nav.top };
  });
  expect(bounds.top).toBeGreaterThanOrEqual(bounds.headerBottom - 1);
  expect(bounds.bottom).toBeLessThanOrEqual(bounds.navTop + 1);
  await capture(page, 'settings-shell-mobile', 'System settings', info, '[data-app-content]');
  await settingsLoaded(page);
  await page.locator('[data-app-content]').evaluate((element) => element.scrollTo(0, element.scrollHeight));
  await capture(page, 'settings-shell-bottom-mobile', 'System settings', info, '[data-app-content]', false);
  const scroll = await page.locator('[data-app-content]').evaluate((element) => ({
    top: element.scrollTop, height: element.clientHeight, total: element.scrollHeight,
  }));
  expect(scroll.top).toBeGreaterThan(0);
  expect(scroll.top + scroll.height).toBeGreaterThanOrEqual(scroll.total - 1);
  lifecycle.push({ nonUsageShell: bounds, settingsSettled: true, bottomScroll: scroll });
});

test('another real actor cannot restore a prior actor protected entry', async ({ page }, info) => {
  await page.goto(url('explorer'));
  await loaded(page, 'Sessions & Requests');
  await page.getByRole('combobox', { name: 'Session source' }).selectOption('proxy');
  await page.getByRole('textbox', { name: 'Session search' }).fill(seed.proxy);
  await page.getByRole('textbox', { name: 'Session search' }).press('Tab');
  await loaded(page, 'Sessions & Requests');
  await page.getByRole('button', { name: seed.proxy, exact: true }).click();
  await loaded(page, 'Sessions & Requests');
  await page.getByRole('button', { name: 'Physical trail', exact: true }).first().click();
  await loaded(page, 'Sessions & Requests');
  await expect(page.getByRole('button', { name: /^attempt_usage_browser_/ }).first()).toBeVisible();
  const original = latestDetail().request;
  const originalBody = JSON.stringify(original);
  const requestPreflight = JSON.parse(execFileSync(process.env.COGNIS_USAGE_PYTHON!, [
    '-m', 'tests.e2e.usage_checkpoint.requests',
  ], { cwd: resolve('..'), env: process.env, input: JSON.stringify(original), encoding: 'utf8' }));
  expect(requestPreflight).toEqual({ scope: 'personal', canonical_request_valid: true });
  const protectedUrl = page.url();
  expect(protectedUrl.includes(String(original.selection_ref))).toBe(false);
  expect(protectedUrl.includes(seed.proxy)).toBe(false);
  const protectedEntry = await nativeEntry(page, 'original-protected-entry');
  expect(typeof protectedEntry.historyIndex).toBe('number');
  expect(protectedEntry.usagePresent && protectedEntry.selectionPresent).toBe(true);
  expect(protectedEntry.selectionHash === createHash('sha256').update(String(original.selection_ref)).digest('hex')).toBe(true);
  expect(protectedEntry.authorityHash === createHash('sha256')
    .update(`${seed.sign_in_preflight.find((actor: any) => actor.role === 'admin').email}:admin`).digest('hex')).toBe(true);
  await page.goto('/settings?tab=account');
  await settingsLoaded(page, 'account');
  await nativeEntry(page, 'account-before-logout');
  await page.getByRole('main').getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  await nativeEntry(page, 'ordinary-login-form');
  await login(page, { email: seed.member, password: process.env.COGNIS_E2E_ADMIN_PASSWORD! });
  const identity = await page.request.get('/api/auth/me');
  expect(identity.ok()).toBe(true);
  const member = await identity.json();
  expect(member.email).toBe(seed.member);
  expect(member.role).toBe('user');
  await nativeEntry(page, 'verified-member-dashboard');
  const count = packets.length;
  const dispatched = network.length;
  let backSteps = 0;
  let incomingEntry: Awaited<ReturnType<typeof nativeEntry>> | undefined;
  // Normal logout/login can add or replace intermediate entries. Traverse the
  // real browser history, never synthesize the prior protected entry's state.
  for (; backSteps < 4 && page.url() !== protectedUrl; backSteps++) {
    await page.goBack();
    const committed = await nativeEntry(page, `native-back-${backSteps + 1}-committed`);
    if (page.url() === protectedUrl) incomingEntry = committed;
    // An authenticated login entry redirects on mount. Wait for the actual app
    // shell before the next Back, rather than racing that supported redirect.
    if (page.url() !== protectedUrl) {
      await expect(page).not.toHaveURL((url) => url.pathname === '/login');
      await expect(page.getByRole('main')).toBeVisible();
      await nativeEntry(page, `native-back-${backSteps + 1}-settled`);
    }
  }
  await expect(page).toHaveURL(protectedUrl);
  expect(incomingEntry).toBeDefined();
  expect(incomingEntry!.historyIndex).toBe(protectedEntry.historyIndex);
  expect(incomingEntry!.navigationIndex).toBe(protectedEntry.navigationIndex);
  expect(incomingEntry!.urlHash).toBe(protectedEntry.urlHash);
  expect(incomingEntry!.usagePresent && incomingEntry!.selectionPresent).toBe(true);
  expect(incomingEntry!.usageHash).toBe(protectedEntry.usageHash);
  expect(incomingEntry!.selectionHash).toBe(protectedEntry.selectionHash);
  expect(incomingEntry!.authorityHash).toBe(protectedEntry.authorityHash);
  expect(incomingEntry!.authorityHash === createHash('sha256').update(`${member.email}:${member.role}`).digest('hex')).toBe(false);
  await capture(page, 'other-actor-restoration-blocked', 'Usage unavailable', info);
  await expect(page.getByRole('heading', { name: 'Attempt details', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^attempt_usage_browser_/ })).toHaveCount(0);
  const visible = await page.getByRole('main').innerText();
  expect(visible.includes(seed.proxy) || visible.includes(String(original.selection_ref))).toBe(false);
  const restoredEntry = await nativeEntry(page, 'revisited-original-protected-entry');
  expect(restoredEntry.historyIndex).toBe(protectedEntry.historyIndex);
  expect(restoredEntry.navigationIndex).toBe(protectedEntry.navigationIndex);
  expect(restoredEntry.urlHash).toBe(protectedEntry.urlHash);
  // CSR enter replaces native state. This cannot identify snapshot restoration
  // versus state erasure, so it does not prove the runtime authority-guard branch.
  expect(network.slice(dispatched).some((request) =>
    request.path.startsWith('/api/v1/usage/') && !request.path.endsWith('/catalog'))).toBe(false);
  expect(packets.slice(count).some((packet) => !packet.path.endsWith('/catalog'))).toBe(false);
  const settledIdentity = await page.request.get('/api/auth/me');
  expect(settledIdentity.status()).toBe(200);
  const settledMember = await settledIdentity.json();
  expect(settledMember.email).toBe(member.email);
  expect(settledMember.role).toBe(member.role);
  expect(JSON.stringify(original) === originalBody).toBe(true);
  const denied = await page.request.post('/api/v1/usage/attempts/search', { data: original });
  lifecycle.push({ originalReferenceStatus: denied.status(), originalRequestUnchanged: JSON.stringify(original) === originalBody });
  expect(denied.status()).toBe(422);
  const refusal = await denied.json();
  lifecycle.push({ originalReferenceError: refusal.error.code });
  expect(refusal.error.code).toBe('invalid_usage_cursor');
  // Explicit test control, not a renderer fallback or an automatic broader read.
  const control = { scope: { mode: 'personal' }, start: original.start, end: original.end,
    view: 'overview', metric: 'attempts' };
  const authorized = await page.request.post('/api/v1/usage/query', { data: control });
  lifecycle.push({ explicitPersonalControlStatus: authorized.status(), request: control });
  expect(authorized.status()).toBe(200);
  const personal = await authorized.json();
  expect(personal.total.finalized_attempts).toBe(4);
  expect(JSON.stringify(original) === originalBody).toBe(true);
  expect(network.slice(dispatched).some((request) =>
    request.path.startsWith('/api/v1/usage/') && !request.path.endsWith('/catalog'))).toBe(false);
  expect(packets.slice(count).some((packet) => !packet.path.endsWith('/catalog'))).toBe(false);
  lifecycle.push({ differentActor: true, freshIdentityVerified: true, nativeBackSteps: backSteps,
    incomingPrivateEntryMatched: true, protectedViewUnavailable: true, priorAttemptDataVisible: false,
    settledNativePrivateStatePresent: restoredEntry.usagePresent && restoredEntry.selectionPresent,
    runtimeAuthorityGuardProven: false, restorationBranch: 'snapshot-restoration-vs-native-state-erasure-not-distinguished',
    broaderUsageDispatches: 0, canonicalRequestValidated: true, originalRequestUnchanged: true,
    originalReferenceRejected: denied.status(), originalReferenceError: refusal.error.code,
    explicitPersonalControlStatus: authorized.status(), personalFinalizedAttempts: personal.total.finalized_attempts,
    actualExpiryProven: false });
  expect(errors).toEqual([]);
});
