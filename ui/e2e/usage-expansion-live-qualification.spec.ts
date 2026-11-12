import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, test, type Locator, type Page, type Request, type Response } from '@playwright/test';
import { login } from './helpers';
import { usageGeometry } from './usage-geometry';
import { usageBounds } from './usage-bounds';

// This file is admitted only by the bridge after all original five cases pass.
// Contracts reconciled against the approved final dashboard/sidebar composition.
// No fixture mode, response substitution or additional UI hooks.
const enabled = process.env.COGNIS_USAGE_EXPANSION === '1';
const directory = process.env.COGNIS_USAGE_RUN_DIR;
const runtime = enabled && directory ? JSON.parse(readFileSync(join(directory, 'runtime.json'), 'utf8')) : null;
const manifest = enabled && directory ? JSON.parse(readFileSync(join(directory, 'expansion-seed.json'), 'utf8')) : null;
const preflight = enabled && directory ? JSON.parse(readFileSync(join(directory, 'expansion-api-preflight.json'), 'utf8')) : null;
const output = directory;

type Dispatch = {
  id: number; path: string; method: string; bodyHash: string;
  view?: string; selector?: { kind: string; id: string }; inclusion?: string;
  status?: number; failed?: string; errorCode?: string; projects?: string[]; mode?: string;
};
function observe(page: Page) {
  const events: Dispatch[] = [];
  const identities = new Map<Request, Dispatch>();
  const responses: Promise<void>[] = [];
  const request = (item: Request) => {
    const path = new URL(item.url()).pathname;
    if (!path.startsWith('/api/v1/usage/')) return;
    const bytes = item.postData() ?? '';
    const body = bytes ? JSON.parse(bytes) : {};
    const event: Dispatch = {
      id: events.length + 1, path, method: item.method(),
      bodyHash: createHash('sha256').update(bytes).digest('hex'),
      view: body.view, selector: body.items?.[0], inclusion: body.inclusion,
      projects: body.filters?.project_ids, mode: body.scope?.mode,
    };
    identities.set(item, event);
    events.push(event); // Dispatch is evidence even if aborted or never answered.
  };
  const response = (item: Response) => {
    const event = identities.get(item.request());
    if (!event) return;
    event.status = item.status();
    if (item.status() >= 400) responses.push(item.json().then((body) => {
      event.errorCode = body.error?.code;
    }).catch(() => { event.errorCode = 'unstructured_response'; }));
  };
  const failed = (item: Request) => {
    const event = identities.get(item);
    if (event) event.failed = item.failure()?.errorText ?? 'request failed';
  };
  page.on('request', request);
  page.on('response', response);
  page.on('requestfailed', failed);
  return {
    events,
    drain: () => Promise.all(responses),
    close() {
      page.off('request', request); page.off('response', response); page.off('requestfailed', failed);
    },
  };
}

async function frames(page: Page) {
  await page.evaluate(() => new Promise<void>((resolve) =>
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

async function settled(root: Locator) {
  await expect(root).toBeVisible();
  await expect(root.getByText(/^(Loading|Saving)(?: active)?(?: usage)?(?: view)?[.…]*$/)).toHaveCount(0);
  await expect(root.getByRole('alert')).toHaveCount(0);
}

async function capture(page: Page, name: string, root: Locator) {
  expect(output).toBeTruthy();
  await settled(root);
  await frames(page);
  const bounds = await root.evaluate(usageBounds);
  const chat = await root.getAttribute('aria-label') === 'Usage accounting';
  // The dashboard checker also treats the global composer as an obstacle.
  // Chat's modal legitimately covers an inert background composer: qualify
  // its own visible controls, bounds and hit testing instead, not that checker.
  const floating = chat ? null : await page.evaluate(usageGeometry, '.usage');
  const revision = process.env.COGNIS_USAGE_BASE_REVISION!;
  expect(revision).toMatch(/^[a-f0-9]{40}$/);
  // Persist diagnostics before assertions so a geometry failure keeps its cause.
  writeFileSync(join(output!, `${name}.geometry.json`), JSON.stringify({
    revision, bounds, floating,
  }, null, 2));
  await page.screenshot({ path: join(output!, `${revision}-${name}.png`), fullPage: true });
  expect(bounds.documentOverflow, 'No page-wide horizontal clipping').toBe(false);
  expect(bounds.overflow, 'Painted headings, prose and legends must not clip').toEqual([]);
  for (const panel of bounds.panels) {
    expect(panel.width).toBeGreaterThan(0);
    expect(panel.x).toBeGreaterThanOrEqual(-1);
    expect(panel.x + panel.width).toBeLessThanOrEqual(bounds.viewport.width + 1);
  }
  // Native radio glyphs use their 44px label targets, checked in chat cases.
  for (const target of bounds.targets.filter((item) => item.tag !== 'INPUT')) {
    expect(target.height, target.name).toBeGreaterThanOrEqual(bounds.viewport.width < 600 ? 43 : 23);
    if (target.hit !== null) expect(target.hit, `Target is obstructed: ${target.name}`).toBe(true);
  }
  if (floating) {
    expect(floating.covered, JSON.stringify(floating.hitDetails)).toBe(0);
    expect(floating.noticeHeaderOverlap, 'Mobile header must not cover the connection notice').toBeNull();
  }
  if (chat) {
    expect(bounds.root.x).toBeGreaterThanOrEqual(-1);
    expect(bounds.root.x + bounds.root.width).toBeLessThanOrEqual(bounds.viewport.width + 1);
    const last = root.getByRole('button', { name: 'Refresh usage', exact: true });
    await last.scrollIntoViewIfNeeded();
    const lastBounds = await last.boundingBox();
    expect(lastBounds).not.toBeNull();
    await page.mouse.move(
      lastBounds!.x + lastBounds!.width / 2,
      lastBounds!.y + lastBounds!.height / 2,
    );
    await page.mouse.wheel(0, 1000);
    await frames(page);
    await expect(last).toBeInViewport({ ratio: 1 });
    const bottom = await last.evaluate((element) => {
      let owner = element.parentElement;
      while (owner && !(owner.scrollHeight > owner.clientHeight
        && /auto|scroll/.test(getComputedStyle(owner).overflowY))) owner = owner.parentElement;
      const rect = element.getBoundingClientRect();
      const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
      return { hit: !!hit && element.contains(hit),
        remaining: owner ? owner.scrollHeight - owner.clientHeight - owner.scrollTop : 0 };
    });
    writeFileSync(join(output!, `${name}-bottom.geometry.json`), JSON.stringify({ revision, bottom }, null, 2));
    await page.screenshot({ path: join(output!, `${revision}-${name}-bottom.png`), fullPage: true });
    expect(bottom.hit).toBe(true);
    expect(bottom.remaining).toBeLessThanOrEqual(8);
  }
}

function url(surface = 'overview') {
  return `/usage?${new URLSearchParams({
    usage_scope: 'admin', usage_project: manifest.project,
    usage_start: manifest.start, usage_end: manifest.end, usage_surface: surface,
  })}`;
}

function sameMetrics(actual: any, expected: any) {
  for (const field of ['finalized_attempts', 'partially_priced_attempts', 'unpriced_attempts',
    'unallocatable_cost_attempts', 'tokens', 'token_known_attempts', 'costs']) {
    expect(actual[field], field).toEqual(expected[field]);
  }
}

async function composition(page: Page, currency: string) {
  await page.getByRole('combobox', { name: 'Overview cost band' }).selectOption(`${currency}:api_estimate`);
  const body = preflight.filtered.body;
  for (const [dimension, title] of [['subject', 'User'], ['agent', 'Agent'], ['provider', 'Provider'], ['model', 'Model']]) {
    const panel = page.getByRole('region', { name: `${title} cost attribution`, exact: true });
    await expect(panel).toBeVisible();
    await expect(panel.getByRole('img', { name: new RegExp(`^${title}:`) })).toBeVisible();
    expect(await panel.locator('svg circle[stroke-dasharray]').count()).toBeGreaterThan(0);
    await expect(panel.getByRole('list', { name: `${title} ranked known subtotals` })).toBeVisible();
    await expect(panel).toContainText('partially priced');
    await expect(panel).toContainText('missing prices are not zero-valued slices');
    const disclosure = panel.locator('summary');
    await disclosure.focus();
    await page.keyboard.press('Enter');
    await expect(disclosure.locator('..')).toHaveAttribute('open', '');
    const table = panel.getByRole('table');
    await expect(table.locator('caption')).toContainText(currency);
    const marginal = body.cost_breakdowns.dimensions.find((item: any) => item.dimension === dimension);
    const amounts = marginal.groups.map((item: any) => ({
      label: item.label,
      amount: item.metrics.costs.find((cost: any) => cost.currency === currency
        && cost.basis === 'api_estimate')?.known_amount ?? 'Unknown',
    }));
    const actual = await table.locator('tbody tr').evaluateAll((rows) =>
      rows.map((row) => ({ label: row.querySelector('th')!.textContent!.trim(),
        amount: row.querySelectorAll('td')[0].textContent!.trim() })));
    const byLabel = (left: { label: string }, right: { label: string }) => left.label.localeCompare(right.label);
    expect(actual.sort(byLabel)).toEqual(amounts.sort(byLabel));
    await expect(table.locator('tfoot')).toContainText(manifest.filtered.amounts[currency]);
    // Close through keyboard before geometry/capture, not DOM attribute writes.
    await disclosure.focus();
    await page.keyboard.press('Enter');
  }
}

async function summaryAction(page: Page, expectedName: string, action: () => Promise<unknown>) {
  const expected = manifest.summaries[expectedName];
  const responsePromise = page.waitForResponse((response) => {
    if (new URL(response.url()).pathname !== '/api/v1/usage/session-summaries') return false;
    const body = response.request().postDataJSON();
    return body.items?.[0]?.kind === expected.selector.kind
      && body.items?.[0]?.id === expected.selector.id && body.inclusion === expected.inclusion;
  });
  await action();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  const actual = await response.json();
  expect(actual.items).toHaveLength(1);
  const item = actual.items[0];
  expect(item.available).toBe(true);
  expect(item.selector).toEqual(expected.selector);
  expect(item.inclusion).toBe(expected.inclusion);
  expect(item.physical_sessions).toBe(expected.sessions);
  sameMetrics(item.metrics, preflight[expectedName].body.items[0].metrics);
  const panel = page.getByRole('region', { name: 'Usage accounting', exact: true });
  await expect(panel.getByRole('button', { name: 'Refresh usage', exact: true })).toBeVisible();
  await settled(panel);
  await expect(panel).toContainText(`Unpriced attempts: ${expected.unpriced}`);
  const meta = actual.meta;
  const coverage = meta.capture?.state === 'unknown' ? 'unknown'
    : meta.complete && !meta.accounting_degraded && meta.capture?.state !== 'gaps'
      && (!meta.projection || meta.projection.state === 'ready') ? 'complete' : 'partial';
  await expect(panel).toContainText(`Coverage: ${coverage}. Unpriced attempts: ${expected.unpriced}.`);
  await expect(panel).toContainText(`Observed through: ${meta.observed_through ?? 'Unknown'}.`);
  await expect(panel).toContainText(`Represented accounting sessions: ${expected.sessions}.`);
  await expect(panel).toContainText(`Partially priced attempts: ${expected.partial}.`);
  if (expected.partial) await expect(panel).toContainText('Pricing is partial or unknown.');
  const tokens = panel.getByRole('heading', { name: 'Observed tokens', exact: true }).locator('..');
  await expect(tokens.locator('dd').nth(0)).toHaveText(String(expected.input));
  await expect(tokens.locator('dd').nth(1)).toHaveText(String(expected.output));
  expect(actual.items[0].metrics.finalized_attempts).toBe(expected.attempts);
  expect(actual.items[0].metrics.observed_attempts).toBeNull();
  const attempts = panel.getByRole('heading', { name: 'Attempts', exact: true }).locator('..');
  // Finalized counts are known, but the default source does not request pending
  // captured identities. Its observed Total remains Unknown, never an inferred sum.
  await expect(attempts.locator('dd').nth(0)).toHaveText('Unknown');
  await expect(attempts.locator('dd').nth(1)).toHaveText(String(expected.attempts));
  await expect(attempts.locator('dd').nth(2)).toHaveText('0');
  for (const [currency, amount] of Object.entries(expected.amounts)) {
    await expect(panel).toContainText(`${amount} ${currency}`);
  }
  return panel;
}

test.describe('Usage expansion · actual API and persisted lineage', () => {
  test.skip(!enabled || !runtime || !manifest || !preflight,
    'Only the canonical bridge may admit this second phase after the original five cases');
  test.describe.configure({ mode: 'default', retries: 0, timeout: 60_000 });

  for (const device of [
    { name: 'desktop', viewport: { width: 1440, height: 1000 }, mobile: false },
    { name: 'mobile', viewport: { width: 390, height: 844 }, mobile: true },
  ]) {
    test(`${device.name}: four simultaneous panels, exact bands, six chart-led views`, async ({ browser }) => {
      const context = await browser.newContext({ viewport: device.viewport, isMobile: device.mobile,
        hasTouch: device.mobile, baseURL: process.env.COGNIS_E2E_URL });
      const page = await context.newPage();
      const journal = observe(page);
      try {
        await login(page, { email: runtime.actor, password: runtime.fixture_password });
        const responsePromise = page.waitForResponse((response) => {
          if (new URL(response.url()).pathname !== '/api/v1/usage/query') return false;
          const body = response.request().postDataJSON();
          return body.view === 'cost_breakdowns'
            && body.filters?.project_ids?.includes(manifest.project);
        });
        await page.goto(url());
        const response = await responsePromise;
        expect(response.status()).toBe(200);
        const body = await response.json();
        sameMetrics(body.total, preflight.filtered.body.total);
        expect(body.cost_breakdowns).toEqual(preflight.filtered.body.cost_breakdowns);
        await composition(page, 'USD');
        await page.getByRole('heading', { name: 'Usage', exact: true, level: 1 }).scrollIntoViewIfNeeded();
        await capture(page, `${device.name}-overview-usd`, page.getByRole('main'));
        await expect(page.locator('[aria-label="Simultaneous cost attribution"]')).toBeVisible();
        for (const title of ['User', 'Agent', 'Provider', 'Model']) {
          const panel = page.getByRole('region', { name: `${title} cost attribution`, exact: true });
          await panel.scrollIntoViewIfNeeded();
          await capture(page, `${device.name}-usd-${title.toLowerCase()}`, panel);
        }
        await composition(page, 'EUR');
        await page.getByRole('heading', { name: 'Usage', exact: true, level: 1 }).scrollIntoViewIfNeeded();
        await capture(page, `${device.name}-overview-eur`, page.getByRole('main'));
        // A real keyboard cohort action must retain the authorized population.
        const legendButton = page.getByRole('region', { name: 'User cost attribution', exact: true })
          .getByRole('list').getByRole('button').first();
        await legendButton.focus();
        await page.keyboard.press('Enter');
        await expect(page.getByRole('button', { name: 'Back to source view', exact: true })).toBeVisible();
        await page.getByRole('button', { name: 'Back to source view', exact: true }).click();
        await expect(page.getByRole('heading', { name: 'Known priced subtotal over time' })).toBeVisible();
        for (const [surface, tab, ready] of [
          ['costs', 'Costs & Tokens', 'Estimated cost over time'],
          ['performance', 'Performance', 'Provider comparison'],
          ['limits', 'Limits', 'Stored provider limits'],
          ['companions', 'Companion Services', null],
          ['explorer', 'Sessions & Requests', 'Sessions & Requests'],
        ]) {
          if (device.mobile) await page.getByRole('combobox', { name: 'Usage view', exact: true }).selectOption(surface!);
          else await page.getByRole('navigation', { name: 'Usage views', exact: true })
            .getByRole('button', { name: tab!, exact: true }).click();
          await expect(page).toHaveURL(new RegExp(`usage_surface=${surface}`));
          if (ready) await expect(page.getByRole('heading', { name: ready, exact: true })).toBeVisible();
          if (surface === 'costs') {
            const trend = page.getByRole('region', { name: 'Token consumption over time', exact: true });
            await expect(trend.getByRole('group', { name: 'Token consumption over time, tokens', exact: true })).toBeVisible();
            expect(await trend.getByRole('button', { name: /^Chart point / }).count()).toBeGreaterThan(0);
            await expect(page.getByRole('region', { name: 'Known input and output composition' }))
              .toBeVisible();
            await expect(page.getByRole('main')).toContainText('They are not added again');
          } else if (surface === 'performance') {
            await expect(page.getByRole('main')).toContainText('Missing client receipt remains unknown');
          } else if (surface === 'limits') {
            await expect(page.getByRole('main')).toContainText('This view never refreshes a provider account');
            await expect(page.getByRole('main')).toContainText('explicitly load its stored history');
            const path = `/api/v1/usage/providers/${preflight.quota.body.provider_id}/history`;
            expect(journal.events.filter((event) => event.path === path)).toHaveLength(0);
            await expect(page.getByRole('button', { name: 'Load stored provider quota history', exact: true })).toBeDisabled();
            const personal = new URL(url('limits'), runtime.url);
            personal.searchParams.set('usage_scope', 'personal');
            personal.searchParams.set('usage_provider', preflight.quota.body.provider_id);
            await page.goto(personal.href);
            const load = page.getByRole('button', { name: 'Load stored provider quota history', exact: true });
            await expect(load).toBeEnabled();
            expect(journal.events.filter((event) => event.path === path)).toHaveLength(0);
            const pending = page.waitForResponse((response) => new URL(response.url()).pathname === path);
            await load.click();
            const response = await pending;
            expect(response.status()).toBe(200);
            const history = await response.json();
            expect(history.samples).toEqual(preflight.quota.body.samples);
            const observations = page.getByRole('region', { name: 'Stored provider observations', exact: true });
            await expect(observations).toContainText('3 stored observations on this returned page');
            await expect(observations).toContainText(`Most recent returned observation in this cohort: ${history.samples[0].observed_at}`);
            const utilization = page.getByRole('region', { name: 'Observed utilization history', exact: true });
            await expect(utilization).toContainText('No supported measured points. Unknown observations are not plotted as zero.');
            await expect(utilization.locator('svg')).toHaveCount(0);
            const maximum = page.getByRole('region', { name: 'Historical bucket maximum utilization', exact: true });
            expect(await maximum.getByRole('button', { name: /^Chart point / }).count()).toBe(2);
            const disclosure = page.getByText('Stored observations · original reported values', { exact: true });
            await disclosure.focus();
            await page.keyboard.press('Enter');
            const table = disclosure.locator('..').getByRole('table');
            await expect(table.locator('tbody tr')).toHaveCount(3);
            const rows = await table.locator('tbody tr').evaluateAll((items) => items.map((row) =>
              row.querySelectorAll('td')[2].childNodes[0].textContent!.trim()));
            expect(rows).toEqual(history.samples.map((sample: any) => `Unknown / ${sample.remaining ?? 'Unknown'}`));
            const beforeInspect = journal.events.length;
            await table.getByRole('button', { name: 'Inspect stored observation', exact: true }).nth(2).click();
            const original = page.getByRole('region', { name: 'Stored quota observation', exact: true });
            const oldest = history.samples[2];
            await expect(original.getByText('Historical bucket maximum', { exact: true })
              .locator('xpath=following-sibling::dd[1]'))
              .toHaveText(`${oldest.maximum_utilization}% (not latest utilization)`);
            await expect(original.getByText('Reported utilization / remaining / limit', { exact: true })
              .locator('xpath=following-sibling::dd[1]'))
              .toHaveText(`Unknown / ${oldest.remaining} / ${oldest.limit} (percent / native units / native units)`);
            await frames(page);
            expect(journal.events).toHaveLength(beforeInspect);
            expect(journal.events.filter((event) => event.path === path)).toHaveLength(1);
            await capture(page, `${device.name}-${surface}`, page.getByRole('main'));
            await page.goto(url('limits')); // Restore explicit admin/project population for the remaining views.
            await expect(page.getByRole('heading', { name: 'Stored provider limits', exact: true })).toBeVisible();
            continue;
          } else if (surface === 'companions') {
            await expect(page.getByRole('combobox', { name: 'Authenticated service', exact: true })).toBeVisible();
            await expect(page.getByRole('main')).toContainText('No supported service/operation comparisons were returned');
            await expect(page.getByRole('main')).toContainText('unfiltered Cognis traffic is not substituted');
          } else {
            const disclosure = page.getByText('Returned session page · exact data and protected drills', { exact: true });
            await disclosure.click();
            await expect(page.getByRole('table').last()).toContainText(manifest.execution);
          }
          await capture(page, `${device.name}-${surface}`, page.getByRole('main'));
        }
        await journal.drain();
        const refusals = journal.events.filter((event) => event.status && event.status >= 400);
        expect(refusals).toHaveLength(3); // Costs, performance and service cost rankings.
        for (const refusal of refusals) {
          expect(refusal.path).toBe('/api/v1/usage/query');
          expect(refusal.view).toBe('ranking');
          expect(refusal.status).toBe(422);
          expect(refusal.errorCode).toBe('mixed_cost_ranking');
        }
        for (const request of journal.events.filter((event) => event.path === '/api/v1/usage/query')) {
          expect(request.mode).toBe('admin');
          expect(request.projects).toEqual([manifest.project]);
        }
      } finally {
        writeFileSync(join(output!, `${device.name}-dashboard-dispatch.json`), JSON.stringify(journal.events, null, 2));
        journal.close();
        await context.close();
      }
    });

    test(`${device.name}: chat direct/inclusive, hidden silence and stable execution anchor`, async ({ browser }) => {
      const context = await browser.newContext({ viewport: device.viewport, isMobile: device.mobile,
        hasTouch: device.mobile, baseURL: process.env.COGNIS_E2E_URL });
      const page = await context.newPage();
      const journal = observe(page);
      try {
        await login(page, { email: runtime.actor, password: runtime.fixture_password });
        await page.goto(`/chat/${manifest.conversation}`);
        const toggle = page.getByRole('button', { name: 'Toggle session details', exact: true });
        await expect(toggle).toBeVisible();
        await expect(toggle).toHaveAttribute('aria-expanded', 'false');
        await frames(page);
        expect(journal.events).toEqual([]);
        await toggle.click();
        await expect(page.getByRole('tab', { name: 'Overview', exact: true })).toBeVisible();
        await frames(page);
        expect(journal.events).toEqual([]);
        let panel = await summaryAction(page, 'conversation_direct',
          () => page.getByRole('tab', { name: 'Usage', exact: true }).click());
        await expect(panel.getByRole('radio', { name: 'Conversation only', exact: true })).toBeChecked();
        await expect(panel).toContainText('Whole retained conversation history');
        await expect(panel).toContainText('Known priced subtotal');
        await capture(page, `${device.name}-conversation-direct`, panel);
        panel = await summaryAction(page, 'conversation_inclusive',
          () => panel.getByRole('radio', { name: 'Including children', exact: true }).check());
        await capture(page, `${device.name}-conversation-inclusive`, panel);
        for (const label of await panel.locator('fieldset label').all()) {
          const box = await label.boundingBox();
          expect(box?.height).toBeGreaterThanOrEqual(43);
        }
        const hiddenCount = journal.events.length;
        await page.getByRole('tab', { name: 'Overview', exact: true }).click();
        await expect(page.getByRole('tab', { name: 'Overview', exact: true })).toHaveAttribute('aria-selected', 'true');
        await frames(page);
        expect(journal.events).toHaveLength(hiddenCount);
        await toggle.click(); // Drawer closed, not merely a non-selected Usage tab.
        await expect(toggle).toHaveAttribute('aria-expanded', 'false');
        await page.reload();
        await expect(toggle).toBeVisible();
        await expect(toggle).toHaveAttribute('aria-expanded', 'false');
        await frames(page);
        expect(journal.events).toHaveLength(hiddenCount);
        await toggle.click();
        // Real persisted activity tree selection; no URL/private-state focus injection.
        const parent = page.getByTestId(`activity-node-${manifest.execution_parent_key}`);
        await expect(parent).toBeVisible();
        const expand = parent.getByRole('button', { name: /^Expand / });
        if (await expand.count()) await expand.click();
        await page.getByRole('button', { name: `View session ${manifest.execution_title}`, exact: true }).click();
        await expect(page.getByRole('region', { name: 'Selected session details', exact: true }))
          .toContainText(manifest.execution_title);
        const usage = page.getByRole('tab', { name: 'Usage', exact: true });
        panel = await summaryAction(page, 'execution_direct', () => usage.click());
        await expect(panel.getByRole('radio', { name: 'Session only', exact: true })).toBeChecked();
        await expect(panel).toContainText('Selected execution and its validated continuation lane only');
        await capture(page, `${device.name}-execution-direct`, panel);
        panel = await summaryAction(page, 'execution_inclusive',
          () => panel.getByRole('radio', { name: 'Including children', exact: true }).check());
        await capture(page, `${device.name}-execution-inclusive`, panel);
        const summaries = journal.events.filter((event) => event.selector?.kind === 'execution');
        expect(summaries.length).toBeGreaterThanOrEqual(2);
        expect(summaries.every((event) => event.selector!.id === manifest.execution)).toBe(true);
        const inclusions = summaries.map((event) => event.inclusion);
        expect(inclusions.filter((inclusion, index) => index === 0 || inclusion !== inclusions[index - 1]))
          .toEqual(['direct', 'inclusive']);
        expect(journal.events.filter((event) => event.path !== '/api/v1/usage/session-summaries')).toEqual([]);
      } finally {
        writeFileSync(join(output!, `${device.name}-chat-dispatch.json`), JSON.stringify(journal.events, null, 2));
        journal.close();
        await context.close();
      }
    });
  }
});
