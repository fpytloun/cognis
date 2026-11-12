import { expect, test, type Page, type Browser } from '../../../ui/node_modules/@playwright/test';
import { createHash } from 'node:crypto';
import { execFileSync, spawn } from 'node:child_process';
import { copyFile, mkdir, readFile, stat, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { parseEnv } from 'node:util';
import { sendMessage } from '../../../ui/e2e/helpers';

type Family = 'classic' | 'porcelain' | 'paper';
type Route = 'setup' | 'chat' | 'work' | 'tasks' | 'project' | 'rich' | 'html' | 'usage' | 'comments' | 'feedback';
type Seed = {
  schema_version: number;
  base_url: string;
  source_revision: string;
  fixture_version: number;
  feature_coverage: { feedback_agent_reply: boolean };
  provenance: unknown;
  project_id: string;
  routes: Record<Route, string>;
  resources: Record<string, string> & { rich_title: string; work_file: string; comment_text: string };
  usage: { start: string; end: string; expected_requests: string };
  users: { owner_email: string; reviewer_email: string };
  runtime: { ports: { MOCK_LLM_PORT: number } };
  workspace: {
    conversations: Record<string, { conversation_id: string; session_id: string; agent_id: string; project_id: string }>;
    tasks: Record<string, { task_id: string; agent_id: string; project_id: string }>;
    projects: Record<string, string>;
  };
};
type Checkpoint = { name: string; route: string; action: string; expected: string; file: string; elapsed_ms: number };
const repo = resolve(__dirname, '../../..');
const families: Family[] = ['classic', 'porcelain', 'paper'];
// Documentation size: readable at the GitHub README column width when captured at scale 2.
const viewport = { width: 1280, height: 800 };
const DOCS_SCALE = 2;
const WORK_FILE = 'cart.py';
let captureStart = 0;

/** The seeded storefront coding conversation: patch, command and review reply. */
function carts(seed: Seed) {
  return seed.workspace.conversations['saved-carts'].conversation_id;
}
function chatUrl(seed: Seed, conversationId: string) {
  return new URL(`/chat/${conversationId}`, seed.base_url).href;
}
/** Usage range that ends yesterday: today's live local mock turns report no token usage. */
function usageRange(seed: Seed) {
  const end = new Date(`${seed.usage.end}T12:00:00Z`);
  end.setUTCDate(end.getUTCDate() - 1);
  return { start: seed.usage.start, end: end.toISOString().slice(0, 10) };
}
async function widenInspector(page: Page) {
  const resizer = page.getByRole('separator', { name: 'Resize conversation inspector', exact: true });
  await resizer.focus();
  await resizer.press('End');
  await expect.poll(async () => Number(await resizer.getAttribute('aria-valuenow'))).toBeGreaterThanOrEqual(400);
}

async function inputs() {
  if (!process.env.SHOWCASE_MANIFEST || !process.env.SHOWCASE_OUTPUT) {
    throw new Error('SHOWCASE_MANIFEST and SHOWCASE_OUTPUT are required');
  }
  const bytes = await readFile(process.env.SHOWCASE_MANIFEST);
  const seed: Seed = JSON.parse(bytes.toString());
  if (seed.schema_version !== 1 || !seed.source_revision || !seed.fixture_version ||
      !seed.project_id || !seed.provenance || !seed.users?.owner_email ||
      !seed.users?.reviewer_email || !seed.usage?.start || !seed.usage?.end ||
      !seed.workspace?.conversations?.['saved-carts'] || !seed.workspace?.tasks?.['readiness-brief']) {
    throw new Error('Incomplete showcase manifest');
  }
  const base = new URL(seed.base_url);
  if (!['http:', 'https:'].includes(base.protocol) ||
      !['localhost', '127.0.0.1', '[::1]'].includes(base.hostname) ||
      base.username || base.password) throw new Error('Showcase base_url must be loopback');
  for (const key of ['setup', 'chat', 'work', 'tasks', 'project', 'rich', 'html', 'usage', 'comments', 'feedback'] as Route[]) {
    if (typeof seed.routes?.[key] !== 'string') throw new Error(`Missing route ${key}`);
    const route = new URL(seed.routes[key], base);
    if (route.origin !== base.origin || route.username || route.password) throw new Error(`Route ${key} is not local`);
    seed.routes[key] = route.href;
  }
  for (const key of ['rich_title', 'work_file', 'comment_text'] as const) {
    if (!seed.resources?.[key]) throw new Error(`Missing resource ${key}`);
  }
  let privateEnv: Record<string, string | undefined> = {};
  if (process.env.SHOWCASE_PRIVATE_ENV) {
    const privatePath = process.env.SHOWCASE_PRIVATE_ENV;
    const mode = (await stat(privatePath)).mode & 0o777;
    if (mode !== 0o600) throw new Error('SHOWCASE_PRIVATE_ENV must have mode 0600');
    privateEnv = parseEnv(await readFile(privatePath, 'utf8'));
  }
  const ownerPassword = process.env.SHOWCASE_OWNER_PASSWORD ?? privateEnv.SHOWCASE_OWNER_PASSWORD;
  const reviewerPassword = process.env.SHOWCASE_REVIEWER_PASSWORD ?? privateEnv.SHOWCASE_REVIEWER_PASSWORD;
  if (!ownerPassword || !reviewerPassword) throw new Error('Both showcase passwords are required');
  const output = resolve(process.env.SHOWCASE_OUTPUT);
  await mkdir(output, { recursive: true, mode: 0o700 });
  return { seed, output, ownerPassword, reviewerPassword,
    manifestHash: createHash('sha256').update(bytes).digest('hex') };
}

async function signIn(page: Page, seed: Seed, email: string, password: string) {
  await page.goto(new URL('/', seed.base_url).href);
  await page.getByLabel('Email', { exact: true }).fill(email);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByTestId('control-center')).toBeVisible();
  const response = await page.request.get(new URL('/api/auth/me', seed.base_url).href);
  expect(response.ok()).toBeTruthy();
  expect((await response.json()).email).toBe(email);
}

async function theme(page: Page, seed: Seed, family: Family, mode: 'light' | 'dark' = 'light') {
  await page.goto(new URL('/settings?tab=display', seed.base_url).href);
  const main = page.getByRole('main');
  const familySelect = main.getByRole('combobox', { name: /^Family\b/ });
  const modeSelect = main.getByRole('combobox', { name: /^Appearance\b/ });
  await expect(familySelect).toBeVisible();
  await familySelect.selectOption(family);
  await modeSelect.selectOption(mode);
  await expect.poll(async () => {
    const response = await page.request.get(new URL('/api/v1/user-preferences', seed.base_url).href);
    if (!response.ok()) return null;
    const preferences = await response.json();
    return [preferences.display?.theme_family, preferences.display?.theme];
  }).toEqual([family, mode]);
  await verifyTheme(page, family, mode);
}
async function verifyTheme(page: Page, family: Family, mode: 'light' | 'dark' = 'light') {
  await expect(page.locator('html')).toHaveAttribute('data-theme-family', family);
  await expect(page.locator('html')).toHaveAttribute('data-resolved-theme', mode);
}
async function inspector(page: Page, tab: 'Overview' | 'Work' | 'Usage') {
  const control = page.getByTestId('chat-header-info');
  await expect(control).toBeVisible();
  if (await control.getAttribute('aria-expanded') !== 'true') await control.click();
  await page.getByRole('tab', { name: tab, exact: true }).click();
}
async function usage(page: Page, seed: Seed) {
  const range = usageRange(seed);
  const url = new URL(seed.routes.usage);
  url.searchParams.set('usage_start', range.start);
  url.searchParams.set('usage_end', range.end);
  await page.goto(url.href);
  const summary = page.getByRole('region', { name: 'Usage summary' });
  await expect(summary.getByRole('button', { name: /^Requests: [\d,]+/ })).toBeVisible();
  await expect(summary.getByText('All priced', { exact: true })).toBeVisible();
  const trend = page.getByRole('region', { name: 'Usage over time' });
  await expect(trend.getByRole('heading', { name: 'Estimated cost per day' })).toBeVisible();
  await expect(trend.getByRole('button')).toHaveCount(29);
  await expect(page.getByRole('region', { name: 'Top spend by work', exact: true }).getByText('Checkout · saved carts', { exact: true })).toBeVisible();
  await expect(page.getByRole('status', { name: 'Loading top spend', exact: true })).toHaveCount(0);
}
async function work(page: Page, seed: Seed, expanded = false) {
  await page.goto(chatUrl(seed, carts(seed)));
  await inspector(page, 'Work');
  if (expanded) await page.getByRole('button', { name: 'Expand inspector', exact: true }).click();
  const file = page.getByTestId('work-files-tree').getByText(WORK_FILE, { exact: true });
  await expect(file).toBeVisible();
  await file.click();
  const pane = page.getByTestId('work-diff-pane');
  await expect(pane).toBeVisible();
  await expect(pane).toContainText('def load(self, session)');
  await expect(pane.getByText('Loading file history...', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Conversation work', exact: true })
    .getByTestId('activity-lifecycle-catching_up')).toHaveCount(0);
}
async function rich(page: Page, seed: Seed, family: Family) {
  await page.goto(seed.routes.rich);
  const viewer = page.getByTestId('rich-deliverable').first();
  await expect(viewer).toBeVisible();
  await expect(page.getByText(seed.resources.rich_title, { exact: false }).first()).toBeVisible();
  await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
  await expect(viewer).toHaveAttribute('data-theme-family', family);
}
async function comments(page: Page, seed: Seed) {
  await page.goto(seed.routes.comments);
  await page.getByRole('button', { name: 'Comment options', exact: true }).click();
  await page.getByRole('menuitem', { name: /^Review comments/ }).click();
  const panel = page.getByRole('region', { name: 'Feedback panel' });
  await expect(panel).toBeVisible();
  await expect(panel.getByText(seed.resources.comment_text, { exact: false }).first()).toBeVisible();
}
async function shot(page: Page, dir: string, name: string, action: string, expected: string, checkpoints: Checkpoint[]) {
  await page.mouse.move(0, 0);
  await page.evaluate(() => new Promise<void>(done =>
    requestAnimationFrame(() => requestAnimationFrame(() => done()))));
  await expect(page.locator('[data-testid="activity-lifecycle-catching_up"]:visible')).toHaveCount(0);
  // Agent avatars load lazily; wait so portraits are not captured as initials or blank circles.
  await page.waitForTimeout(600);
  await page.waitForFunction(() => Array.from(document.images)
    .filter(image => image.getBoundingClientRect().width > 0)
    .every(image => image.complete && image.naturalWidth > 0), undefined, { timeout: 10_000 }).catch(() => undefined);
  const file = `${name}.png`;
  await page.screenshot({ path: resolve(dir, file), animations: 'disabled' });
  const url = new URL(page.url());
  checkpoints.push({ name, route: url.pathname, action, expected, file,
    elapsed_ms: Math.round(performance.now() - captureStart) });
  // Hold the settled view so the product recording can be read.
  if (process.env.SHOWCASE_SCREENSHOTS_ONLY !== '1') await page.waitForTimeout(3000);
}
async function evidence(seed: Seed, manifestHash: string, browser: Browser, checkpoints: Checkpoint[], stage: string) {
  const revision = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repo, encoding: 'utf8' }).trim();
  const diff = spawn('git', ['diff', '--binary', 'HEAD'], { cwd: repo, stdio: ['ignore', 'pipe', 'inherit'] });
  const completion = new Promise<number | null>((done, fail) => {
    diff.once('error', fail);
    diff.once('close', done);
  });
  const captureHash = createHash('sha256');
  for await (const chunk of diff.stdout) captureHash.update(chunk);
  if (await completion !== 0) throw new Error('Cannot fingerprint showcase source diff');
  const spec = await readFile(resolve(__dirname, 'capture.spec.ts'));
  const config = await readFile(resolve(__dirname, 'playwright.config.ts'));
  return { schema_version: 1, stage, data_label: 'Illustrative deterministic fixture; not real inference',
    source_revision: seed.source_revision, checked_out_revision: revision, fixture_version: seed.fixture_version,
    provenance: seed.provenance, project_id: seed.project_id, manifest_sha256: manifestHash,
    capture_sha256: captureHash.update(spec).update(config).digest('hex'),
    browser: browser.version(), node: process.version, viewport, checkpoints };
}

test('theme comparison', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'comparison');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  for (const family of families) {
    await theme(page, seed, family);
    await test.step(`${family}-usage`, async () => {
      await usage(page, seed);
      await verifyTheme(page, family);
      await shot(page, dir, `${family}-usage`, 'Open Usage dashboard', 'Seeded request summary and 29-day cost trend visible', checkpoints);
    });
    await rich(page, seed, family);
    await verifyTheme(page, family);
    await shot(page, dir, `${family}-rich`, 'Open native Rich result', 'Rich result in selected palette', checkpoints);
    const chart = page.locator('[data-rich-block-type="chart"]').first();
    await chart.scrollIntoViewIfNeeded();
    await expect(chart.getByRole('img', { name: 'Chart area', exact: true })).toBeVisible();
    await test.step(`${family}-rich-chart`, async () =>
      shot(page, dir, `${family}-rich-chart`, 'Inspect native Rich chart', 'Native chart and surrounding result visible', checkpoints));
    await work(page, seed);
    await verifyTheme(page, family);
    await shot(page, dir, `${family}-work`, 'Open changed file', 'Changed-file diff visible', checkpoints);
    await comments(page, seed);
    await verifyTheme(page, family);
    await shot(page, dir, `${family}-comments`, 'Open source comments', 'Seeded comment visible', checkpoints);
    await theme(page, seed, family, 'dark');
    await test.step(`${family}-dark-usage`, async () => {
      await usage(page, seed);
      await verifyTheme(page, family, 'dark');
      await shot(page, dir, `${family}-dark-usage`, 'Open Usage dashboard in dark mode', 'Same 29-day Usage in dark mode', checkpoints);
    });
  }
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify(await evidence(seed, manifestHash, browser, checkpoints, 'comparison'), null, 2), { mode: 0o600 });
});

test('feature capture', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  expect(seed.feature_coverage.feedback_agent_reply).toBe(true);
  const selected = process.env.SHOWCASE_THEME;
  if (!families.includes(selected as Family)) throw new Error('Choose SHOWCASE_THEME after reviewing the comparison');
  const family = selected as Family;
  const comparison = JSON.parse(await readFile(resolve(output, 'comparison/manifest.json'), 'utf8'));
  expect(comparison.manifest_sha256).toBe(manifestHash);
  expect(comparison.checkpoints).toHaveLength(18);
  const dir = resolve(output, 'feature');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, family);
  const state = await page.context().storageState();
  const context = await browser.newContext({ storageState: state, viewport, deviceScaleFactor: DOCS_SCALE,
    colorScheme: 'light', reducedMotion: 'reduce', serviceWorkers: 'block',
    recordVideo: process.env.SHOWCASE_SCREENSHOTS_ONLY === '1' ? undefined : { dir, size: viewport } });
  const product = await context.newPage();
  const video = product.video();
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  try {
    await product.goto(seed.routes.setup);
    await expect(product.getByRole('list', { name: 'Setup progress', exact: true })).toBeVisible();
    await expect(product.getByText('Preparing setup', { exact: true })).not.toBeVisible();
    await shot(product, dir, '01-setup', 'Open setup guide', 'Actual setup state, separate from fixture', checkpoints);
    await product.goto(chatUrl(seed, carts(seed)));
    await expect(product.locator('[data-kind="message"][data-role="assistant"]').first()).toBeVisible();
    await inspector(product, 'Overview');
    await expect(product.getByTestId('inspector-overview')).toBeVisible();
    await shot(product, dir, '02-chat-activity', 'Open conversation activity', 'Seeded conversation and activity visible', checkpoints);
    await product.goto(seed.routes.tasks);
    await expect(product.getByRole('heading', { name: 'Prepare the Autumn launch', exact: true })).toBeVisible();
    await shot(product, dir, '03-tasks', 'Open tasks', 'Task workspace visible', checkpoints);
    await work(product, seed, true);
    const files = product.getByTestId('work-files-tree-pane');
    await expect(files).toContainText('2 files');
    await shot(product, dir, '04-files', 'Expand Work inspector', 'Readable changed-file navigation, statistics and diff', checkpoints);
    await product.getByTestId('work-files-tree').getByText('test_cart.py', { exact: true }).click();
    await expect(product.getByTestId('work-diff-pane')).toContainText('test_guest_cart_merges_on_sign_in');
    await test.step('04-tests-diff', async () =>
      shot(product, dir, '04-tests-diff', 'Navigate to the changed test file', 'New cart tests visible in the seeded diff', checkpoints));
    await product.getByTestId('work-files-tree').getByText(WORK_FILE, { exact: true }).click();
    await product.getByTestId('work-diff-pane').getByRole('button').filter({ hasText: 'Expand diff' }).first().click();
    const expandedDiff = product.getByRole('dialog', { name: /^Expanded diff for / });
    await expect(expandedDiff).toContainText('saved.merge');
    await test.step('04-diff', async () =>
      shot(product, dir, '04-diff', 'Expand code diff', 'Readable full-screen seeded code change', checkpoints));
    await expandedDiff.getByRole('button', { name: 'Minimize diff', exact: true }).click();
    await product.goto(new URL('/library', seed.base_url).href);
    await product.getByRole('button', { name: 'New artifact', exact: true }).click();
    const creation = product.getByRole('dialog', { name: 'New artifact', exact: true });
    await creation.getByRole('textbox', { name: 'Artifact name' }).fill('lumen-launch-notes.md');
    await creation.getByRole('combobox', { name: 'Artifact format' }).selectOption('text/markdown');
    await creation.getByRole('textbox', { name: 'New artifact content' })
      .fill('# Lumen launch notes\n\n- Early access opens Friday at 9:00.\n- Partner newsletter goes out Thursday.\n- Courier capacity for the first weekend still needs confirmation.');
    await shot(product, dir, '05-create-draft', 'Write a new artifact', 'Human-authored draft', checkpoints);
    const createdResponse = product.waitForResponse(response =>
      response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/v1/library/artifacts');
    await creation.getByRole('button', { name: 'Create artifact', exact: true }).click();
    const created = await createdResponse;
    expect(created.ok()).toBeTruthy();
    const artifact = await created.json();
    expect(typeof artifact.id).toBe('string');
    expect(artifact.id.length).toBeGreaterThan(0);
    await expect(product.getByTestId('library-resource-modal')).toBeVisible();
    await expect(product.getByRole('heading', { name: 'Lumen launch notes', exact: true })).toBeVisible();
    await shot(product, dir, '05-create-saved', 'Create artifact through Library', 'New human-authored artifact saved', checkpoints);
    await rich(product, seed, family);
    await expect(product.locator('[data-rich-block-type="chart"]').first()).toBeVisible();
    await expect(product.locator('[data-rich-block-type="metric"]').first()).toBeVisible();
    await expect(product.locator('figure, [data-rich-block-type="figure"]').first()).toBeVisible();
    await test.step('06-rich', async () =>
      shot(product, dir, '06-rich', 'Open native Rich result', 'Seeded native chart, stat and figure', checkpoints));
    const chart = product.locator('[data-rich-block-type="chart"]').first();
    const chartArea = chart.getByRole('img', { name: 'Chart area', exact: true });
    await expect(chartArea).toBeVisible();
    await chartArea.hover();
    await expect(chart.getByTestId('rich-chart-tooltip')).toBeVisible();
    await product.waitForTimeout(1500);
    await chart.getByText('Chart data table', { exact: true }).click();
    await expect(chart.getByRole('table')).toContainText('7200');
    await chart.getByRole('table').scrollIntoViewIfNeeded();
    await expect(chart.getByRole('table')).toBeInViewport();
    await shot(product, dir, '07-rich-chart', 'Inspect categorical chart and open its data table', 'Chart data table shows seeded reach', checkpoints);
    await product.goto(seed.routes.html);
    const iframe = product.locator('iframe[sandbox]').first();
    await expect(iframe).toBeVisible();
    await expect(iframe).toHaveAttribute('sandbox', /allow-scripts/);
    const frame = product.frameLocator('iframe[sandbox]').first();
    const metric = frame.locator('#reach');
    await expect(metric).toHaveText('7,200');
    await frame.getByLabel('Audience', { exact: true }).selectOption('18000');
    await expect(metric).toHaveText('10,800');
    await test.step('08-html', async () =>
      shot(product, dir, '08-html', 'Change sandboxed HTML control', 'Metric changed', checkpoints));
    await product.goto(seed.routes.project);
    await expect(product.getByTestId('project-role')).toHaveText('Owner');
    await product.getByTestId('project-tab-sharing').click();
    await expect(product.getByTestId('project-members')).toBeVisible();
    await test.step('09-project', async () =>
      shot(product, dir, '09-project', 'Open project sharing', 'Project team visible', checkpoints));
    await rich(product, seed, family);
    await product.getByRole('button', { name: 'Share link', exact: true }).click();
    await product.getByRole('tab', { name: 'Share with people', exact: true }).click();
    const people = product.getByRole('tabpanel', { name: 'Share with people' });
    await people.getByLabel('Cognis user email').fill(seed.users.reviewer_email);
    await people.getByLabel('New recipient permission').selectOption('commenter');
    await people.getByRole('button', { name: 'Share with person' }).click();
    await expect(people.getByLabel(`Permission for ${seed.users.reviewer_email}`)).toHaveValue('commenter');
    await test.step('10-sharing', async () =>
      shot(product, dir, '10-sharing', 'Share with named local reviewer', 'Comment permission saved', checkpoints));
    await product.getByRole('button', { name: 'Close share link' }).click();
    await comments(product, seed);
    await test.step('11-comments', async () =>
      shot(product, dir, '11-comments', 'Open source comments', 'Seeded feedback visible', checkpoints));
    await product.getByTitle('Show in source').first().click();
    await product.getByRole('button', { name: 'Close comments', exact: true }).click();
    await test.step('11-source', async () =>
      shot(product, dir, '11-source', 'Locate anchored comment and dismiss its popover', 'Located source unobstructed', checkpoints));
    await product.goto(seed.routes.feedback);
    if (await product.getByTestId('chat-header-info').getAttribute('aria-expanded') === 'true') {
      await product.getByTestId('chat-header-info').click();
    }
    const replies = product.getByRole('region', { name: 'Comment replies' });
    await expect(replies).toContainText(seed.resources.feedback_reply_text);
    await replies.scrollIntoViewIfNeeded();
    await expect(replies).toBeInViewport();
    await shot(product, dir, '11-agent-reply', 'Open actual local mock feedback reply', 'Real admitted mock execution, not a fabricated reply event', checkpoints);
    await product.goto(seed.routes.rich);
    await product.getByTestId('library-revision-toggle').click();
    await expect(product.getByRole('listbox', { name: 'Revisions' }).getByRole('option')).toHaveCount(3);
    await test.step('11-revisions', async () =>
      shot(product, dir, '11-revisions', 'Open document revision history', 'Two illustrative starting revisions and the actual mock-produced successor', checkpoints));
    await usage(product, seed);
    const point = product.getByRole('region', { name: 'Usage over time' }).getByRole('button').first();
    await point.hover();
    await product.waitForTimeout(1500);
    await shot(product, dir, '12-usage-chart', 'Hover daily cost trend', 'Usage chart point visible', checkpoints);
    const provider = product.getByRole('region', { name: 'Cost share by provider' }).getByRole('button').first();
    await provider.click();
    await expect(product.getByRole('combobox', { name: 'Filter provider' })).not.toHaveValue('');
    await shot(product, dir, '13-usage-provider', 'Filter the Usage overview by provider', 'Provider filter applied to illustrative accounting', checkpoints);
  } catch (error) {
    await product.screenshot({ path: resolve(output, 'failed-feature.png') });
    throw error;
  } finally {
    await context.close();
  }
  if (video) await copyFile(await video.path(), resolve(dir, 'owner.webm'));
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'feature')),
    theme: family, video: video ? 'owner.webm' : null,
    omissions: ['Usage session/request/attempt detail', 'Conversation Usage inspector',
      'Reviewer and anonymous permission checks'],
  }, null, 2), { mode: 0o600 });
});

test('desktop documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'desktop-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const desktopViewport = viewport;
  await page.setViewportSize(desktopViewport);
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  await test.step('Desktop conversation Work diff', async () => {
    await work(page, seed);
    await widenInspector(page);
    await shot(page, dir, 'desktop-work-diff', 'Open Work beside the conversation',
      'Conversation and seeded source diff visible', checkpoints);
  });
  await test.step('Rich hero block preview', async () => {
    await page.goto(new URL('/rich-deliverable-block-fixture?preview=hero', seed.base_url).href);
    const fixture = page.getByTestId('rich-deliverable-block-fixture');
    await expect(fixture).toHaveAttribute('data-preview', 'hero');
    const viewer = fixture.getByTestId('rich-deliverable');
    await expect(viewer).toHaveAttribute('data-theme-family', 'porcelain');
    await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
    const block = fixture.locator('[data-rich-block-type="hero"]').first();
    await expect(block).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const file = 'hero.png';
    await block.screenshot({ path: resolve(dir, file), animations: 'disabled' });
    checkpoints.push({ name: 'rich-hero', route: new URL(page.url()).pathname,
      action: 'Open the canonical Rich hero preview', expected: 'Porcelain Light hero block visible',
      file, elapsed_ms: Math.round(performance.now() - captureStart) });
  });
  await test.step('Rich table block preview', async () => {
    await page.goto(new URL('/rich-deliverable-block-fixture?preview=table', seed.base_url).href);
    const fixture = page.getByTestId('rich-deliverable-block-fixture');
    await expect(fixture).toHaveAttribute('data-preview', 'table');
    const viewer = fixture.getByTestId('rich-deliverable');
    await expect(viewer).toHaveAttribute('data-theme-family', 'porcelain');
    await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
    const block = fixture.locator('[data-rich-block-type="table"]').first();
    await expect(block.getByRole('table')).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const file = 'table.png';
    await block.screenshot({ path: resolve(dir, file), animations: 'disabled' });
    checkpoints.push({ name: 'rich-table', route: new URL(page.url()).pathname,
      action: 'Open the canonical Rich table preview', expected: 'Porcelain Light table block visible',
      file, elapsed_ms: Math.round(performance.now() - captureStart) });
  });
  await test.step('Rich bar-chart block preview', async () => {
    await page.goto(new URL('/rich-deliverable-block-fixture?preview=chart-bar', seed.base_url).href);
    const fixture = page.getByTestId('rich-deliverable-block-fixture');
    await expect(fixture).toHaveAttribute('data-preview', 'chart-bar');
    const viewer = fixture.getByTestId('rich-deliverable');
    await expect(viewer).toHaveAttribute('data-theme-family', 'porcelain');
    await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
    await expect(fixture.locator('[data-rich-block-type="chart"] .rich-chart-canvas.chart-ready')).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const file = 'chart-bar.png';
    await fixture.screenshot({ path: resolve(dir, file), animations: 'disabled' });
    checkpoints.push({ name: 'rich-chart-bar', route: new URL(page.url()).pathname,
      action: 'Open the canonical Rich bar-chart preview', expected: 'Porcelain Light chart visible',
      file, elapsed_ms: Math.round(performance.now() - captureStart) });
  });
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'desktop-docs')),
    viewport: desktopViewport, theme: 'porcelain', mode: 'light',
  }, null, 2), { mode: 0o600 });
});

test('tools documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'tools-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const desktopViewport = viewport;
  await page.setViewportSize(desktopViewport);
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  await test.step('Skills registry', async () => {
    await page.goto(new URL('/tools?tab=skills', seed.base_url).href);
    await expect(page.getByRole('heading', { name: 'Tools & Skills', exact: true })).toBeVisible();
    await expect(page.getByPlaceholder('Search skills...')).toBeVisible();
    await expect(page.getByText('Cognis Coding', { exact: true })).toBeVisible();
    await shot(page, dir, 'tools-skills', 'Open the skills registry',
      'Populated built-in skill registry visible', checkpoints);
  });
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'tools-docs')),
    viewport: desktopViewport, theme: 'porcelain', mode: 'light',
  }, null, 2), { mode: 0o600 });
});

test('executor documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'executor-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const desktopViewport = viewport;
  await page.setViewportSize(desktopViewport);
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  await test.step('Executor browser automation', async () => {
    await page.goto(new URL('/settings?tab=executors', seed.base_url).href);
    await page.getByTestId('executor-card-local-compose-executor').click();
    const dialog = page.getByRole('dialog', { name: /^Executor:/ });
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: 'Runtime', exact: true }).click();
    await dialog.locator('summary').filter({ hasText: 'Browser Automation' }).click();
    await shot(page, dir, 'executor-browser', 'Open executor browser settings',
      'Local fixture executor browser configuration visible', checkpoints);
  });
  await test.step('Executor tools and MCP', async () => {
    const dialog = page.getByRole('dialog', { name: /^Executor:/ });
    await dialog.getByRole('button', { name: 'Tools & MCP', exact: true }).click();
    await expect(dialog.getByRole('heading', { name: 'Runtime capabilities', exact: true })).toBeVisible();
    await shot(page, dir, 'executor-tools', 'Open executor tools',
      'Local fixture executor runtime capabilities visible', checkpoints);
  });
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'executor-docs')),
    viewport: desktopViewport, theme: 'porcelain', mode: 'light',
  }, null, 2), { mode: 0o600 });
});

test('task documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'task-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  // The seed completed this task through the general-task workflow with scripted local mock output.
  const title = 'Launch readiness brief';
  const taskId = seed.workspace.tasks['readiness-brief'].task_id;
  const detailResponse = await page.request.get(new URL(`/api/v1/tasks/${taskId}`, seed.base_url).href);
  if (!detailResponse.ok()) throw new Error(`Cannot inspect task: ${detailResponse.status()}`);
  const detail = await detailResponse.json();
  if (detail.status !== 'completed' || !detail.step_runs.some((step: { deliverable_id: string | null }) => step.deliverable_id)) {
    throw new Error(`Existing showcase task requires reconciliation: ${detail.status}`);
  }
  const mobileViewport = { width: 390, height: 848 };
  const context = await browser.newContext({
    storageState: await page.context().storageState(), viewport: mobileViewport,
    deviceScaleFactor: 62 / 39, isMobile: true, hasTouch: true, colorScheme: 'light',
    reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  try {
    const mobile = await context.newPage();
    await test.step('Mobile completed task', async () => {
      await mobile.goto(new URL(`/tasks/${taskId}`, seed.base_url).href);
      await verifyTheme(mobile, 'porcelain');
      await expect(mobile.getByText(title, { exact: false }).first()).toBeVisible();
      await expect(mobile.locator('span').filter({ hasText: /^Completed(?: via direct delivery| silently)?$/ }).first()).toBeVisible();
      await shot(mobile, dir, 'mobile-task-brief', 'Open the completed illustrative task',
        'Native completed task with synthetic result visible', checkpoints);
    });
    await test.step('Mobile completed workflow', async () => {
      const workflow = mobile.getByText('Phase workflow', { exact: true });
      await workflow.scrollIntoViewIfNeeded();
      await expect(workflow).toBeVisible();
      await expect(mobile.getByTestId('pending-interactions-loading')).toHaveCount(0);
      await shot(mobile, dir, 'mobile-completed-task', 'Open the completed task workflow',
        'Native completed workflow with synthetic result visible', checkpoints);
    });
  } finally {
    await context.close();
  }
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'task-docs')),
    viewport: mobileViewport, deviceScaleFactor: 62 / 39, theme: 'porcelain', mode: 'light',
    provenance_note: 'Completed through the native workflow with scripted mock output; mobile browser emulation.',
    task_id: taskId,
  }, null, 2), { mode: 0o600 });
});

test('command documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'command-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const conversationId = carts(seed);
  const refresh = await page.request.post(new URL('/api/v1/work/refresh', seed.base_url).href, {
    data: { scope: { kind: 'conversation', key: `conversation:${conversationId}`, conversation_id: conversationId } },
  });
  if (!refresh.ok()) throw new Error(`Cannot refresh showcase Work: ${refresh.status()}`);
  const work = new URL(`/api/v1/chat/v2/conversations/${conversationId}/work`, seed.base_url);
  work.searchParams.set('category', 'commands');
  let callId = '';
  await expect.poll(async () => {
    const response = await page.request.get(work.href);
    if (!response.ok()) throw new Error(`Cannot inspect showcase Work: ${response.status()}`);
    const items = (await response.json()).commands;
    const command = items.find((item: { command: string }) => item.command === 'git diff --stat');
    callId = command?.call_id ?? '';
    return Boolean(callId);
  }, { timeout: 60_000, intervals: [500, 1000, 2000] }).toBe(true);
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  const desktopViewport = viewport;
  await page.setViewportSize(desktopViewport);
  await test.step('Desktop command output', async () => {
    await page.goto(chatUrl(seed, conversationId));
    await inspector(page, 'Work');
    await widenInspector(page);
    await page.getByTestId('work-tab-commands').click();
    const command = page.getByTestId(`work-command-${callId}`);
    await expect(command).toBeVisible();
    await command.getByRole('button').first().click();
    await expect(command.locator('pre').filter({ hasText: '2 files changed' }).first()).toBeVisible();
    await shot(page, dir, 'desktop-work-commands', 'Open the completed command',
      'Seeded command history and output', checkpoints);
  });
  const mobileViewport = { width: 390, height: 848 };
  const context = await browser.newContext({
    storageState: await page.context().storageState(), viewport: mobileViewport,
    deviceScaleFactor: 62 / 39, isMobile: true, hasTouch: true, colorScheme: 'light',
    reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  try {
    const mobile = await context.newPage();
    await test.step('Mobile command output', async () => {
      await mobile.goto(chatUrl(seed, conversationId));
      await inspector(mobile, 'Work');
      await mobile.getByTestId('work-tab-commands').click();
      const command = mobile.getByTestId(`work-command-${callId}`);
      await expect(command).toBeVisible();
      await command.getByRole('button').first().click();
      await expect(command.locator('pre').filter({ hasText: '2 files changed' }).first()).toBeVisible();
      await shot(mobile, dir, 'mobile-command-output', 'Open the completed command on mobile',
        'Seeded command history and output', checkpoints);
    });
  } finally {
    await context.close();
  }
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'command-docs')),
    desktopViewport, mobileViewport, mobileDeviceScaleFactor: 62 / 39, theme: 'porcelain', mode: 'light',
    provenance_note: 'Seeded recorded tool-call and result events; the command was not executed.',
    conversation_id: conversationId, call_id: callId,
  }, null, 2), { mode: 0o600 });
});

test('overview documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'overview-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const conversationId = carts(seed);
  const snapshotUrl = new URL(`/api/v1/chat/v2/conversations/${conversationId}/snapshot`, seed.base_url);
  const initialResponse = await page.request.get(snapshotUrl.href);
  if (!initialResponse.ok()) throw new Error(`Cannot inspect context snapshot: ${initialResponse.status()}`);
  const initial = await initialResponse.json();
  if (initial.timeline.items.some((item: { content?: string }) =>
    /scenario:|Hello world! This is a streaming response\./.test(item.content ?? ''))) {
    throw new Error('Published coding history contains diagnostic traffic');
  }
  if (initial.runtime?.context_usage &&
      (initial.runtime.context_usage.model !== 'mock-model' ||
       initial.runtime.context_usage.provider_id !== 'local-openai-compatible')) {
    throw new Error('Existing context was not produced by the isolated mock model');
  }
  if (!initial.runtime?.context_usage) {
    // One real local mock turn gives the conversation a native context estimate.
    const marker = 'Is the saved-carts change ready for review?';
    if (initial.timeline.items.some((item: { role?: string; content?: string }) =>
      item.role === 'user' && item.content === marker)) {
      throw new Error('Existing context scenario requires reconciliation');
    }
    const agentResponse = await page.request.get(new URL(`/api/v1/agents/${seed.resources.agent_id}`, seed.base_url).href);
    if (!agentResponse.ok()) throw new Error(`Cannot inspect showcase agent: ${agentResponse.status()}`);
    const agent = await agentResponse.json();
    const providerResponse = await page.request.get(new URL(`/api/v1/llm-providers/${agent.llm_config?.provider_id}`, seed.base_url).href);
    if (!providerResponse.ok()) throw new Error(`Cannot inspect showcase provider: ${providerResponse.status()}`);
    const provider = await providerResponse.json();
    if (provider.config?.api_base !== 'http://mock-llm:8090/v1' || agent.llm_config?.model !== 'mock-model') {
      throw new Error('Context projection requires the isolated mock-only model');
    }
    const catalog = await fetch(`http://127.0.0.1:${seed.runtime.ports.MOCK_LLM_PORT}/__mock/scenarios`);
    if (!catalog.ok) throw new Error(`Cannot inspect mock scenarios: ${catalog.status}`);
    const scenarios = (await catalog.json()).scenarios;
    if (scenarios.some((scenario: { active: boolean }) => scenario.active)) {
      throw new Error('Another mock scenario is active');
    }
    const registered = await fetch(`http://127.0.0.1:${seed.runtime.ports.MOCK_LLM_PORT}/__mock/scenario`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        id: 'showcase-review-ready-v1', trigger: marker,
        description: 'Seeded review reply from the local mock model.',
        turns: [{ role: 'assistant', steps: [{ type: 'text', delay_ms: 100, chunks: [
          'Yes. The change touches two files and adds a test for each path. ',
          'Guest carts merge on sign-in, so nobody loses items they added before logging in.',
        ] }] }],
      }),
    });
    if (!registered.ok) throw new Error(`Cannot register review scenario: ${registered.status}`);
    await page.goto(chatUrl(seed, conversationId));
    await sendMessage(page, marker);
    await expect.poll(async () => {
      const response = await page.request.get(snapshotUrl.href);
      if (!response.ok()) throw new Error(`Cannot inspect context snapshot: ${response.status()}`);
      const snapshot = await response.json();
      return snapshot.runtime?.context_usage?.model === 'mock-model' &&
        snapshot.runtime?.context_usage?.provider_id === 'local-openai-compatible' &&
        !snapshot.state?.active_turn?.has_active_turn;
    }, { timeout: 60_000, intervals: [500, 1000, 2000] }).toBe(true);
  }
  const desktopViewport = viewport;
  await page.setViewportSize(desktopViewport);
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  await test.step('Desktop activity overview', async () => {
    await page.goto(chatUrl(seed, conversationId));
    await inspector(page, 'Overview');
    await widenInspector(page);
    const overview = page.getByTestId('inspector-overview');
    await expect(overview).toBeVisible();
    await expect(overview.getByText('Commands', { exact: true }).first()).toBeVisible();
    await expect(overview.getByText(/[\d,]+ \/ [\d,]+ \(\d+%\)/)).toBeVisible();
    await expect(page.locator('.animate-spin:visible')).toHaveCount(0);
    await shot(page, dir, 'desktop-workspace', 'Open the conversation activity overview',
      'Work statistics and recent activity visible', checkpoints);
  });
  const mobileViewport = { width: 390, height: 848 };
  const context = await browser.newContext({
    storageState: await page.context().storageState(), viewport: mobileViewport,
    deviceScaleFactor: 62 / 39, isMobile: true, hasTouch: true, colorScheme: 'light',
    reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  try {
    const mobile = await context.newPage();
    await test.step('Mobile activity overview', async () => {
      await mobile.goto(chatUrl(seed, conversationId));
      await inspector(mobile, 'Overview');
      const overview = mobile.getByTestId('inspector-overview');
      await expect(overview).toBeVisible();
      await expect(overview.getByText('Commands', { exact: true }).first()).toBeVisible();
      await expect(overview.getByText(/[\d,]+ \/ [\d,]+ \(\d+%\)/)).toBeVisible();
      await expect(mobile.locator('.animate-spin:visible')).toHaveCount(0);
      await shot(mobile, dir, 'mobile-activity-overview', 'Open the mobile activity overview',
        'Work statistics and recent activity visible', checkpoints);
    });
    await test.step('Mobile recent activity', async () => {
      const recent = mobile.getByTestId('recent-work-activity');
      await recent.scrollIntoViewIfNeeded();
      await expect(recent.getByText('git diff --stat', { exact: false }).first()).toBeVisible();
      await shot(mobile, dir, 'mobile-activity-recent', 'Scroll to mobile recent activity',
        'Seeded command history and changed files visible', checkpoints);
    });
  } finally {
    await context.close();
  }
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'overview-docs')),
    desktopViewport, mobileViewport, mobileDeviceScaleFactor: 62 / 39, theme: 'porcelain', mode: 'light',
    provenance_note: 'Seeded Work events and an actual local mock-model context estimate; mobile browser emulation.',
  }, null, 2), { mode: 0o600 });
});

test('windows documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  if (!seed.resources.feedback_revised_deliverable_id) throw new Error('Missing revised Rich document ID');
  const dir = resolve(output, 'windows-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const desktopViewport = viewport;
  await page.setViewportSize(desktopViewport);
  const preference = page.getByRole('checkbox', { name: /Dashboard workspace windows/ });
  const originallyEnabled = await preference.isChecked();
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  try {
    if (!originallyEnabled) {
      await preference.check();
      await expect.poll(async () => {
        const response = await page.request.get(new URL('/api/v1/user-preferences', seed.base_url).href);
        return response.ok() && (await response.json()).display?.dashboard_workspace_windows === true;
      }).toBe(true);
    }
    await test.step('Desktop conversation window', async () => {
      await page.goto(new URL('/', seed.base_url).href);
      await page.getByTestId(`dashboard-conversation-row-${carts(seed)}`).first()
        .getByText('Checkout · saved carts', { exact: true }).click();
      const conversationWindow = page.getByTestId(`workspace-window-conversation:${carts(seed)}`);
      await expect(conversationWindow).toBeVisible();
      await expect(conversationWindow.getByText(/scenario:|Hello world! This is a streaming response\./)).toHaveCount(0);
      await expect(conversationWindow.getByText('Carts now follow the account', { exact: false })).toBeVisible();
      await expect(conversationWindow.getByText(/Loading (timeline|inspector)/)).toHaveCount(0);
      await shot(page, dir, 'desktop-window-activity', 'Open a conversation in the dashboard workspace',
        'Coding conversation in a managed workspace window', checkpoints);
    });
    await test.step('Desktop Rich document windows', async () => {
      await page.keyboard.press('Control+k');
      const palette = page.getByRole('dialog', { name: 'Command palette', exact: true });
      const search = palette.getByRole('combobox', { name: 'Search Cognis' });
      await search.fill(seed.resources.rich_title);
      await palette.getByRole('option').filter({ hasText: seed.resources.rich_title }).first().click();
      const documentWindow = page.getByTestId(`workspace-window-deliverable:${seed.resources.feedback_revised_deliverable_id}`);
      await expect(documentWindow).toBeVisible();
      const viewer = documentWindow.getByTestId('rich-deliverable').first();
      await expect(viewer).toHaveAttribute('data-theme-family', 'porcelain');
      await expect(viewer).toHaveAttribute('data-viewer-theme', 'light');
      await expect(page.getByTestId(`workspace-window-conversation:${carts(seed)}`)).toBeVisible();
      await shot(page, dir, 'desktop-window-deliverable', 'Open the Rich document in the workspace',
        'Conversation and native Rich document in managed windows', checkpoints);
    });
  } finally {
    if (!originallyEnabled) {
      await page.goto(new URL('/settings?tab=display', seed.base_url).href);
      await page.getByRole('checkbox', { name: /Dashboard workspace windows/ }).uncheck();
      await expect.poll(async () => {
        const response = await page.request.get(new URL('/api/v1/user-preferences', seed.base_url).href);
        return response.ok() && (await response.json()).display?.dashboard_workspace_windows === false;
      }).toBe(true);
    }
  }
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'windows-docs')),
    viewport: desktopViewport, theme: 'porcelain', mode: 'light',
  }, null, 2), { mode: 0o600 });
});

test('research documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'research-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const desktopViewport = viewport;
  await page.setViewportSize(desktopViewport);
  const research = seed.workspace.conversations['assistant-research'].conversation_id;
  const snapshot = await page.request.get(new URL(`/api/v1/chat/v2/conversations/${research}/snapshot`, seed.base_url).href);
  if (!snapshot.ok()) throw new Error(`Cannot inspect seeded research history: ${snapshot.status()}`);
  const history = (await snapshot.json()).timeline.items;
  if (history.some((item: { content?: string }) =>
    /scenario:|Hello world! This is a streaming response\./.test(item.content ?? ''))) {
    throw new Error('Published research history contains diagnostic traffic');
  }
  const tools = history
    .filter((item: { kind: string }) => item.kind === 'tool_call')
    .map((item: { tool_name: string }) => item.tool_name);
  if (!tools.includes('web_search') || !tools.includes('web_fetch')) {
    throw new Error('Seeded web-search and web-fetch history is missing');
  }
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  await test.step('Desktop research chat', async () => {
    await page.goto(chatUrl(seed, research));
    await expect(page.getByText('Brief: AI shopping assistants in home lighting', { exact: false }).first()).toBeVisible();
    await shot(page, dir, 'chat-desktop', 'Open the research conversation',
      'Seeded web-search and web-fetch history visible; no web requests made', checkpoints);
  });
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'research-docs')),
    viewport: desktopViewport, theme: 'porcelain', mode: 'light',
    provenance_note: 'Seeded recorded research tool events on example domains; no web requests or live inference.',
  }, null, 2), { mode: 0o600 });
});

test('active documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'active-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const agentResponse = await page.request.get(new URL(`/api/v1/agents/${seed.resources.agent_id}`, seed.base_url).href);
  if (!agentResponse.ok()) throw new Error(`Cannot inspect showcase agent: ${agentResponse.status()}`);
  const agent = await agentResponse.json();
  const providerResponse = await page.request.get(new URL(`/api/v1/llm-providers/${agent.llm_config?.provider_id}`, seed.base_url).href);
  if (!providerResponse.ok()) throw new Error(`Cannot inspect showcase provider: ${providerResponse.status()}`);
  const provider = await providerResponse.json();
  if (provider.config?.api_base !== 'http://mock-llm:8090/v1' || agent.llm_config?.model !== 'mock-model') {
    throw new Error('Active view requires the isolated mock-only model');
  }
  const catalog = await fetch(`http://127.0.0.1:${seed.runtime.ports.MOCK_LLM_PORT}/__mock/scenarios`);
  if (!catalog.ok) throw new Error(`Cannot inspect mock scenarios: ${catalog.status}`);
  const scenarios = (await catalog.json()).scenarios;
  if (scenarios.some((scenario: { active: boolean }) => scenario.active) ||
      !scenarios.some((scenario: { id: string }) => scenario.id === 'single-phase-stream')) {
    throw new Error('Active-view scenario is unavailable or another scenario is active');
  }
  const created = await page.request.post(new URL('/api/v1/conversations', seed.base_url).href, {
    data: { agent_id: seed.resources.agent_id, project_id: seed.project_id,
      title: 'Shop window copy',
      context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} } },
  });
  if (!created.ok()) throw new Error(`Cannot create dedicated activity conversation: ${created.status()}`);
  const probeId = (await created.json()).conversation_id;
  const probeUrl = new URL(`/chat/${probeId}`, seed.base_url).href;
  const snapshotUrl = new URL(`/api/v1/chat/v2/conversations/${probeId}/snapshot`, seed.base_url);
  const snapshotResponse = await page.request.get(snapshotUrl.href);
  if (!snapshotResponse.ok()) throw new Error(`Cannot inspect conversation: ${snapshotResponse.status()}`);
  if ((await snapshotResponse.json()).state?.active_turn?.has_active_turn) {
    throw new Error('Existing active turn requires reconciliation');
  }
  await page.goto(probeUrl);
  const mobileViewport = { width: 390, height: 848 };
  const context = await browser.newContext({
    storageState: await page.context().storageState(), viewport: mobileViewport,
    deviceScaleFactor: 62 / 39, isMobile: true, hasTouch: true, colorScheme: 'light',
    reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  try {
    const mobile = await context.newPage();
    await mobile.goto(probeUrl);
    await mobile.getByTestId('chat-header').getByRole('button', { name: 'Open conversations', exact: true }).click();
    const drawer = mobile.getByRole('dialog', { name: 'Conversation list', exact: true });
    const row = drawer.getByTestId(`sidebar-conversation-row-${probeId}`);
    await expect(row).toBeVisible();
    await test.step('Mobile conversation list with active mock turn', async () => {
      await sendMessage(page, 'scenario:single-phase-stream — illustrative active mock; no live inference');
      await expect(row.getByTestId('activity-avatar-orbit')).toBeVisible();
      await shot(mobile, dir, 'mobile-conversations', 'Open the conversation list during a mock turn',
        'Actual active local mock turn and conversation list visible', checkpoints);
    });
  } finally {
    await context.close();
    await expect.poll(async () => {
      const response = await page.request.get(snapshotUrl.href);
      if (!response.ok()) throw new Error(`Cannot inspect conversation: ${response.status()}`);
      return !(await response.json()).state?.active_turn?.has_active_turn;
    }, { timeout: 60_000, intervals: [500, 1000, 2000] }).toBe(true);
  }
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'active-docs')),
    viewport: mobileViewport, deviceScaleFactor: 62 / 39, theme: 'porcelain', mode: 'light',
    provenance_note: 'Actual active local mock-model turn; mobile browser emulation; no live inference.',
  }, null, 2), { mode: 0o600 });
});

test('ongoing documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'ongoing-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const providersResponse = await page.request.get(new URL('/api/v1/llm-providers', seed.base_url).href);
  if (!providersResponse.ok()) throw new Error(`Cannot inspect fixture providers: ${providersResponse.status()}`);
  const providers = (await providersResponse.json()).items;
  if (!providers.length || providers.some((provider: { config: { api_base?: string } }) =>
    provider.config?.api_base !== 'http://mock-llm:8090/v1')) {
    throw new Error('Ongoing view requires exclusively isolated mock providers');
  }
  const mock = new URL(`http://127.0.0.1:${seed.runtime.ports.MOCK_LLM_PORT}`);
  const catalogResponse = await fetch(new URL('/__mock/scenarios', mock));
  if (!catalogResponse.ok) throw new Error(`Cannot inspect mock scenarios: ${catalogResponse.status}`);
  const scenarios = (await catalogResponse.json()).scenarios;
  if (scenarios.some((scenario: { active: boolean }) => scenario.active)) {
    throw new Error('Another mock scenario is active');
  }
  const scenarioId = 'showcase-ongoing-text-v2';
  const trigger = 'Draft the holiday gift guide for the Autumn collection.';
  const description = 'A bounded text-only ongoing turn using the local mock model.';
  const registered = scenarios.find((scenario: { id: string }) => scenario.id === scenarioId);
  if (registered && (registered.trigger !== trigger || registered.description !== description)) {
    throw new Error('Existing ongoing scenario requires reconciliation');
  }
  if (!registered) {
    const scenario = {
      id: scenarioId, trigger, description,
      turns: [{ role: 'assistant', steps: [
        { type: 'text', chunks: ['Drafting the gift guide around three price bands.', ' Amber glass lamps lead the top band.', ' The draft is ready for review.'], delay_ms: 6000 },
      ] }],
    };
    const response = await fetch(new URL('/__mock/scenario', mock), {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(scenario),
    });
    if (!response.ok) throw new Error(`Cannot register ongoing scenario: ${response.status}`);
  }
  const created = await page.request.post(new URL('/api/v1/conversations', seed.base_url).href, {
    data: { agent_id: 'iris', project_id: seed.project_id, title: 'Holiday gift guide',
      context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} } },
  });
  if (!created.ok()) throw new Error(`Cannot create ongoing-work conversation: ${created.status()}`);
  const conversationId: string = (await created.json()).conversation_id;
  await writeFile(resolve(dir, 'ongoing-source.json'), JSON.stringify({ conversation_id: conversationId,
    scenario: scenarioId, provenance: 'Text-only local mock; native active turn' }, null, 2), { mode: 0o600 });
  const snapshotUrl = new URL(`/api/v1/chat/v2/conversations/${conversationId}/snapshot`, seed.base_url);
  const chatUrl = new URL(`/chat/${conversationId}`, seed.base_url).href;
  await page.goto(chatUrl);
  const mobileViewport = { width: 390, height: 848 };
  const context = await browser.newContext({
    storageState: await page.context().storageState(), viewport: mobileViewport,
    deviceScaleFactor: 62 / 39, isMobile: true, hasTouch: true, colorScheme: 'light',
    reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  try {
    const mobile = await context.newPage();
    await mobile.goto(chatUrl);
    await inspector(mobile, 'Overview');
    await test.step('Mobile active session overview', async () => {
      await sendMessage(page, trigger);
      await expect.poll(async () => {
        const response = await page.request.get(snapshotUrl.href);
        if (!response.ok()) throw new Error(`Cannot inspect conversation: ${response.status()}`);
        return Boolean((await response.json()).state?.active_turn?.has_active_turn);
      }, { timeout: 15_000, intervals: [250, 500] }).toBe(true);
      const overview = mobile.getByTestId('inspector-overview');
      const session = overview.getByRole('listitem').filter({
        has: mobile.getByRole('button', { name: 'View session Holiday gift guide', exact: true }),
      });
      await expect(session.getByRole('status')).toHaveText(/^running$/i);
      await shot(mobile, dir, 'mobile-ongoing-work', 'Open the active local mock session overview',
        'Actual active native session using the local mock model', checkpoints);
    });
  } finally {
    await context.close();
    await expect.poll(async () => {
      const response = await page.request.get(snapshotUrl.href);
      if (!response.ok()) throw new Error(`Cannot inspect conversation: ${response.status()}`);
      return !(await response.json()).state?.active_turn?.has_active_turn;
    }, { timeout: 90_000, intervals: [500, 1000, 2000] }).toBe(true);
  }
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'ongoing-docs')),
    viewport: mobileViewport, deviceScaleFactor: 62 / 39, theme: 'porcelain', mode: 'light',
    provenance_note: 'Actual active native session using the isolated mock model; mobile browser emulation.',
    conversation_id: conversationId,
  }, null, 2), { mode: 0o600 });
});

test('mobile documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'mobile-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const state = await page.context().storageState();
  const mobileViewport = { width: 390, height: 848 };
  const context = await browser.newContext({
    storageState: state, viewport: mobileViewport, deviceScaleFactor: 62 / 39,
    isMobile: true, hasTouch: true, colorScheme: 'light',
    reducedMotion: 'reduce', serviceWorkers: 'block',
  });
  const mobile = await context.newPage();
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  try {
    await test.step('Mobile chat activity', async () => {
      await mobile.goto(chatUrl(seed, carts(seed)));
      await verifyTheme(mobile, 'porcelain');
      await expect(mobile.locator('[data-kind="message"][data-role="assistant"]').first()).toBeVisible();
      await shot(mobile, dir, 'mobile-chat', 'Open the mobile conversation',
        'Coding conversation and activity visible', checkpoints);
    });
    await test.step('Mobile conversation drawer', async () => {
      await mobile.getByTestId('chat-header').getByRole('button', { name: 'Open conversations', exact: true }).click();
      const drawer = mobile.getByRole('dialog', { name: 'Conversation list', exact: true });
      await expect(drawer).toBeVisible();
      await expect(drawer.getByText('Checkout · saved carts', { exact: true })).toBeVisible();
      await shot(mobile, dir, 'mobile-conversation-drawer', 'Open the mobile conversation drawer',
        'Populated conversation list visible', checkpoints);
      await drawer.getByRole('button', { name: 'Close conversation list', exact: true }).click();
    });
    await test.step('Mobile Work file list', async () => {
      await inspector(mobile, 'Work');
      await expect(mobile.getByTestId('work-files-tree')
        .getByText(WORK_FILE, { exact: true })).toBeVisible();
      await shot(mobile, dir, 'mobile-work-files', 'Open mobile Work files',
        'Changed-file list visible', checkpoints);
    });
    await test.step('Mobile Work file diff', async () => {
      await mobile.getByTestId('work-files-tree')
        .getByText(WORK_FILE, { exact: true }).click();
      const diff = mobile.getByTestId('work-diff-pane');
      await expect(diff).toBeVisible();
      await expect(diff).toContainText('def load(self, session)');
      await shot(mobile, dir, 'mobile-work-diff', 'Open mobile Work diff',
        'Seeded source diff visible', checkpoints);
    });
  } finally {
    await context.close();
  }
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'mobile-docs')),
    viewport: mobileViewport, deviceScaleFactor: 62 / 39, theme: 'porcelain', mode: 'light',
    provenance_note: 'Chromium mobile emulation; not an installed iOS PWA.',
  }, null, 2), { mode: 0o600 });
});

/**
 * Screencast recorder for README GIFs. The frames are the real product UI; the only addition is a
 * presentation cursor so viewers can follow the pointer, removed when recording stops.
 */
async function recordGif(page: Page, dir: string, name: string, flow: () => Promise<void>) {
  const frames = resolve(dir, `frames-${name}`);
  await mkdir(frames, { recursive: true, mode: 0o700 });
  await page.evaluate(() => {
    const cursor = document.createElement('div');
    cursor.id = 'showcase-cursor';
    cursor.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M4 2l15 11-6.5 1.2L16 21l-3 1.4-3.4-6.9L4 20z" fill="#111" stroke="#fff" stroke-width="1.5"/></svg>';
    Object.assign(cursor.style, { position: 'fixed', left: '-40px', top: '-40px', zIndex: '2147483647',
      pointerEvents: 'none', transition: 'transform 120ms ease-out' });
    document.documentElement.appendChild(cursor);
    addEventListener('mousemove', event => {
      cursor.style.left = `${event.clientX - 3}px`; cursor.style.top = `${event.clientY - 2}px`;
    }, true);
    addEventListener('mousedown', () => { cursor.style.transform = 'scale(0.82)'; }, true);
    addEventListener('mouseup', () => { cursor.style.transform = 'scale(1)'; }, true);
  });
  const session = await page.context().newCDPSession(page);
  const recorded: { file: string; t: number }[] = [];
  const writes: Promise<void>[] = [];
  session.on('Page.screencastFrame', ({ data, metadata, sessionId }) => {
    const file = resolve(frames, `${String(recorded.length).padStart(5, '0')}.jpg`);
    recorded.push({ file, t: metadata.timestamp ?? 0 });
    writes.push(writeFile(file, Buffer.from(data, 'base64')));
    void session.send('Page.screencastFrameAck', { sessionId }).catch(() => undefined);
  });
  await page.mouse.move(pointer.x, pointer.y);
  await session.send('Page.startScreencast', { format: 'jpeg', quality: 92,
    maxWidth: viewport.width * DOCS_SCALE, maxHeight: viewport.height * DOCS_SCALE });
  try {
    await page.waitForTimeout(700);
    await flow();
    await page.waitForTimeout(1500);
  } finally {
    await session.send('Page.stopScreencast');
    await session.detach();
    await page.evaluate(() => document.getElementById('showcase-cursor')?.remove());
  }
  await Promise.all(writes);
  if (recorded.length < 10) throw new Error(`GIF ${name} recorded too few frames`);
  const list = recorded.map((frame, index) => {
    const next = recorded[index + 1];
    return `file '${frame.file}'\nduration ${(next ? Math.max(0.02, next.t - frame.t) : 1.5).toFixed(3)}`;
  }).join('\n') + `\nfile '${recorded.at(-1)!.file}'\n`;
  await writeFile(resolve(frames, 'list.txt'), list);
  // 25 fps keeps pointer motion fluid; 960 px with one shared palette keeps text crisp and files small.
  execFileSync('ffmpeg', ['-nostdin', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0',
    '-i', resolve(frames, 'list.txt'), '-vf',
    'fps=25,scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle',
    '-loop', '0', resolve(dir, `${name}.gif`)]);
}
const pointer = { x: viewport.width - 2, y: viewport.height - 2 };
/** Eased pointer movement at roughly display refresh rate, so recordings show a smooth cursor. */
async function moveTo(page: Page, x: number, y: number, duration = 380) {
  const from = { ...pointer };
  const frames = Math.max(6, Math.round(duration / 16));
  for (let index = 1; index <= frames; index++) {
    const t = index / frames;
    const eased = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
    await page.mouse.move(from.x + (x - from.x) * eased, from.y + (y - from.y) * eased);
    await page.waitForTimeout(16);
  }
  pointer.x = x; pointer.y = y;
}
async function glide(page: Page, target: ReturnType<Page['locator']>, pause = 200) {
  await target.scrollIntoViewIfNeeded();
  const box = await target.boundingBox();
  if (!box) throw new Error('Target is not visible');
  await moveTo(page, box.x + box.width / 2, box.y + box.height / 2);
  await page.waitForTimeout(pause);
}
async function press(page: Page, target: ReturnType<Page['locator']>) {
  await glide(page, target);
  await page.mouse.down();
  await page.waitForTimeout(90);
  await page.mouse.up();
}
async function dashboard(page: Page, seed: Seed) {
  await page.goto(new URL('/', seed.base_url).href);
  await expect(page.getByTestId('control-center')).toBeVisible();
  const projects = page.getByRole('tab', { name: /^Projects/ }).first();
  if (await projects.getAttribute('aria-selected') !== 'true') await projects.click();
  await expect(page.getByText('Atlas', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Launch readiness brief', { exact: true }).first()).toBeVisible();
}

test('hero documentation', async ({ page, browser }) => {
  const { seed, output, ownerPassword, manifestHash } = await inputs();
  const dir = resolve(output, 'hero-docs');
  await mkdir(dir, { recursive: true, mode: 0o700 });
  await signIn(page, seed, seed.users.owner_email, ownerPassword);
  await theme(page, seed, 'porcelain');
  const preference = page.getByRole('checkbox', { name: /Dashboard workspace windows/ });
  const originallyEnabled = await preference.isChecked();
  if (!originallyEnabled) await preference.check();
  const mock = `http://127.0.0.1:${seed.runtime.ports.MOCK_LLM_PORT}`;
  const feedbackReply = 'Please fold this into the plan.';
  const commentText = 'Can we reach 9,000 if the partner newsletter goes out first?';
  const checkpoints: Checkpoint[] = [];
  captureStart = performance.now();
  const range = usageRange(seed);
  const usageUrl = new URL(seed.routes.usage);
  usageUrl.searchParams.set('usage_start', range.start);
  usageUrl.searchParams.set('usage_end', range.end);
  try {
    await test.step('Main dashboard', async () => {
      await dashboard(page, seed);
      await shot(page, dir, 'dashboard', 'Open the dashboard', 'Agents, schedules, tasks and conversations visible', checkpoints);
    });
    await test.step('Command palette', async () => {
      await page.keyboard.press('Control+k');
      const palette = page.getByRole('dialog', { name: 'Command palette', exact: true });
      await palette.getByRole('combobox', { name: 'Search Cognis' }).fill('launch');
      await expect(palette.getByRole('option').filter({ hasText: 'Launch readiness brief' }).first()).toBeVisible();
      await shot(page, dir, 'command-palette', 'Search everything from the command palette',
        'Tasks, schedules and conversations matched', checkpoints);
      await page.keyboard.press('Escape');
    });
    await test.step('Project dashboard', async () => {
      await page.goto(seed.routes.project);
      await expect(page.getByTestId('project-role')).toHaveText('Owner');
      await expect(page.getByText('Waiting for you', { exact: false }).first()).toBeVisible();
      await shot(page, dir, 'project-dashboard', 'Open the project dashboard',
        'Project agents, conversations and counts visible', checkpoints);
    });
    await test.step('Usage overview', async () => {
      await usage(page, seed);
      await shot(page, dir, 'usage-overview', 'Open Usage', '29 days of priced usage visible', checkpoints);
    });
    await test.step('Usage cost flow', async () => {
      await page.getByRole('radio', { name: '7 days', exact: true }).click();
      await page.getByRole('tab', { name: 'Costs & tokens', exact: true }).click();
      const flow = page.getByRole('region', { name: 'Cost flow between levels' });
      await expect(flow).toBeVisible();
      await flow.getByRole('combobox').first().selectOption('project-agent-conversation');
      // Node and link labels are SVG titles, so check attachment rather than visibility.
      await expect(flow.locator('title').filter({ hasText: 'Checkout · saved carts' }).first()).toBeAttached();
      await flow.scrollIntoViewIfNeeded();
      await page.mouse.move(0, 0);
      await page.waitForTimeout(1200);
      await flow.screenshot({ path: resolve(dir, 'usage-flow.png'), animations: 'disabled' });
      checkpoints.push({ name: 'usage-flow', route: new URL(page.url()).pathname,
        action: 'Show cost flow from project to agent to conversation', expected: 'Sankey cost flow visible',
        file: 'usage-flow.png', elapsed_ms: Math.round(performance.now() - captureStart) });
    });
    await test.step('Dashboard windows GIF', async () => {
      await dashboard(page, seed);
      const conversation = carts(seed);
      const document = seed.resources.feedback_revised_deliverable_id;
      await recordGif(page, dir, 'dashboard-windows', async () => {
        await press(page, page.getByTestId(`dashboard-conversation-row-${conversation}`).first()
          .getByText('Checkout · saved carts'));
        await expect(page.getByTestId(`workspace-window-conversation:${conversation}`)
          .getByText('Carts now follow the account', { exact: false })).toBeVisible();
        await page.waitForTimeout(1100);
        await page.keyboard.press('Control+k');
        await page.waitForTimeout(400);
        await page.getByRole('combobox', { name: 'Search Cognis' }).pressSequentially('launch overview', { delay: 50 });
        const option = page.getByRole('dialog', { name: 'Command palette' }).getByRole('option')
          .filter({ hasText: seed.resources.rich_title }).first();
        await expect(option).toBeVisible();
        await page.waitForTimeout(600);
        await press(page, option);
        await expect(page.getByTestId(`workspace-window-deliverable:${document}`)
          .getByText('7,200', { exact: true })).toBeVisible();
        await page.waitForTimeout(1400);
        await press(page, page.getByTestId(`workspace-dock-item-conversation:${conversation}`).getByRole('button').first());
        await page.waitForTimeout(1100);
        await press(page, page.getByTestId(`workspace-dock-item-deliverable:${document}`).getByRole('button').first());
        await page.waitForTimeout(1100);
        await press(page, page.getByTestId('workspace-dock-dashboard'));
        await page.waitForTimeout(1400);
      });
      checkpoints.push({ name: 'dashboard-windows', route: '/', action: 'Open a chat and a document as windows and switch between them',
        expected: 'Workspace windows and dock', file: 'dashboard-windows.gif', elapsed_ms: Math.round(performance.now() - captureStart) });
    });
    await test.step('Usage drill-down GIF', async () => {
      await page.goto(usageUrl.href);
      await expect(page.getByRole('region', { name: 'Usage summary' }).getByText('All priced', { exact: true })).toBeVisible();
      const bars = page.getByRole('region', { name: 'Usage over time' }).getByRole('button');
      const labels = await bars.evaluateAll(nodes => nodes.map(node => node.getAttribute('aria-label') ?? ''));
      const amounts = labels.map(label => Number(label.match(/\$([\d.]+)/)?.[1] ?? 0));
      const peak = amounts.indexOf(Math.max(...amounts));
      await page.waitForTimeout(1500);
      await recordGif(page, dir, 'usage-drilldown', async () => {
        await glide(page, bars.nth(peak), 600);
        await press(page, bars.nth(peak));
        await page.waitForTimeout(1400);
        await press(page, page.getByRole('tab', { name: 'Costs & tokens', exact: true }));
        const flow = page.getByRole('region', { name: 'Cost flow between levels' });
        await expect(flow).toBeVisible({ timeout: 20_000 });
        const select = flow.getByRole('combobox').first();
        await glide(page, select, 400);
        await select.selectOption('project-agent-conversation');
        await page.waitForTimeout(1800);
        const heavy = flow.getByRole('img', { name: /saved carts/ }).first();
        if (await heavy.count()) await glide(page, heavy, 1300);
        await page.waitForTimeout(1200);
      });
      checkpoints.push({ name: 'usage-drilldown', route: '/usage', action: 'Drill from the most expensive day to the cost flow',
        expected: 'Day drill-down and Sankey cost flow', file: 'usage-drilldown.gif', elapsed_ms: Math.round(performance.now() - captureStart) });
    });
    await test.step('Work feedback GIF', async () => {
      await page.goto(seed.routes.feedback);
      await expect(page.locator('[data-kind="message"]').first()).toBeVisible({ timeout: 90_000 });
      const value = page.locator('.rich-metric-value').filter({ hasText: '7,200' }).last();
      // The timeline settles at the bottom once history loads; scroll to the metric afterwards.
      await page.waitForTimeout(3000);
      await value.scrollIntoViewIfNeeded();
      await page.waitForTimeout(1000);
      await expect(value).toBeInViewport();
      // The mock picks a reply step by the number of assistant messages in the prompt. In this
      // conversation every model response is either one tool call or one text message, so the
      // timeline gives that count. Continue after it so the agent answers with comment_reply.
      const feedbackConversation = new URL(seed.routes.feedback).pathname.split('/').pop();
      const timelineResponse = await page.request.get(
        new URL(`/api/v1/chat/v2/conversations/${feedbackConversation}/snapshot`, seed.base_url).href);
      if (!timelineResponse.ok()) throw new Error(`Cannot inspect feedback conversation: ${timelineResponse.status()}`);
      const items = (await timelineResponse.json()).timeline.items as { kind: string; role?: string }[];
      const nextTurn = items.filter(item => item.kind === 'tool_call' ||
        (item.kind === 'message' && item.role === 'assistant')).length;
      if (nextTurn < 1) throw new Error('Seeded feedback turn is missing');
      await recordGif(page, dir, 'work-feedback', async () => {
        await value.scrollIntoViewIfNeeded();
        const box = await value.boundingBox();
        if (!box) throw new Error('Reach metric is not visible');
        await moveTo(page, box.x + 1, box.y + box.height / 2);
        await page.waitForTimeout(250);
        await page.mouse.down();
        await moveTo(page, box.x + box.width - 1, box.y + box.height / 2, 300);
        await page.mouse.up();
        await press(page, page.getByRole('button', { name: 'Add comment', exact: true }));
        await page.getByRole('textbox', { name: 'Comment', exact: true }).pressSequentially(commentText, { delay: 22 });
        await page.waitForTimeout(500);
        const saved = page.waitForResponse(response => response.request().method() === 'POST' &&
          new URL(response.url()).pathname === '/api/v1/feedback/threads');
        await press(page, page.getByRole('region', { name: 'Add comment', exact: true })
          .getByRole('button', { name: /^(Send|Save comment)$/ }));
        const savedResponse = await saved;
        if (!savedResponse.ok()) throw new Error(`Comment was not saved: ${savedResponse.status()}`);
        const threadId: string = (await savedResponse.json()).thread_id;
        const commentReply = 'About 9,000 if the newsletter goes out first. I will add it to the next revision.';
        const step = (name: string, args: unknown, index: number) => ({ role: 'assistant', steps: [
          { type: 'tool_call', call_id: `call_showcase_feedback_${index}`, name, arguments: args, delay_ms: 300 }] });
        const registered = await fetch(`${mock}/__mock/scenario`, {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            id: 'showcase-feedback-reply-v2', trigger: `${feedbackReply} [showcase]`,
            description: 'Local mock reply to inline document feedback; answers the comment through comment_reply.',
            turns: [
              ...Array.from({ length: nextTurn }, () => ({ role: 'assistant', steps: [{ type: 'text', chunks: ['Unused.'] }] })),
              step('search_tools', { query: 'comment_reply', limit: 5 }, 0),
              step('call_tool', { tool: 'builtin:comment_reply', arguments: { comment_id: threadId, reply: commentReply } }, 1),
              { role: 'assistant', steps: [{ type: 'text', delay_ms: 120, chunks: [
                'Good call. With the partner newsletter going out first, expected reach rises to about 9,000. ',
                'I answered on your comment and will carry it into the next revision of the plan.',
              ] }] },
            ],
          }),
        });
        if (!registered.ok) throw new Error(`Cannot register feedback reply: ${registered.status}`);
        const tray = page.getByRole('region', { name: 'Message feedback', exact: true });
        await expect(tray.getByText(/ready to include/)).toBeVisible();
        await page.waitForTimeout(700);
        await press(page, tray.getByRole('button', { name: 'Review comments', exact: true }));
        await expect(tray.getByRole('checkbox').first()).toBeVisible();
        const includeAll = tray.getByRole('button', { name: 'Include all', exact: true });
        await expect(includeAll).toBeEnabled();
        await page.waitForTimeout(500);
        await press(page, includeAll);
        await expect(tray.getByRole('checkbox').first()).toBeChecked();
        await expect(tray.getByText(/comments? included/).first()).toBeVisible();
        await page.waitForTimeout(700);
        await press(page, tray.getByRole('button', { name: 'Close review', exact: true }));
        const composer = page.locator('textarea[placeholder*="Message"]').first();
        await press(page, composer);
        await composer.pressSequentially(feedbackReply, { delay: 30 });
        // The conversation's first message selects its seeded scenario; pin this turn's reply.
        const active = await fetch(`${mock}/__mock/active`, { method: 'POST',
          headers: { 'content-type': 'application/json' }, body: JSON.stringify({ id: 'showcase-feedback-reply-v2' }) });
        if (!active.ok) throw new Error(`Cannot select feedback reply: ${active.status}`);
        try {
          await page.keyboard.press('Enter');
          await expect(page.getByText('I answered on your comment', { exact: false }).last()).toBeVisible({ timeout: 30_000 });
          await expect(page.getByText(commentReply, { exact: false }).first()).toBeAttached({ timeout: 15_000 });
          await expect.poll(async () => {
            const snapshot = await page.request.get(new URL(`/api/v1/chat/v2/conversations/${new URL(page.url()).pathname.split('/').pop()}/snapshot`, seed.base_url).href);
            return snapshot.ok() && !(await snapshot.json()).state?.active_turn?.has_active_turn;
          }, { timeout: 30_000 }).toBe(true);
        } finally {
          const reset = await fetch(`${mock}/__mock/active`, { method: 'POST',
            headers: { 'content-type': 'application/json' }, body: JSON.stringify({ id: null }) });
          if (!reset.ok) throw new Error(`Mock scenario stayed pinned: ${reset.status}`);
        }
        await page.waitForTimeout(1600);
      });
      checkpoints.push({ name: 'work-feedback', route: new URL(page.url()).pathname,
        action: 'Select a figure in a Rich result, comment on it and send it to the agent',
        expected: 'Comment included and answered by the local mock agent', file: 'work-feedback.gif',
        elapsed_ms: Math.round(performance.now() - captureStart) });
    });
    await test.step('Pinned chats', async () => {
      // Pin through the chat header, as a user would; dashboard windows may already have pinned Forge.
      for (const key of ['saved-carts', 'assistant-research', 'staging-health']) {
        await page.goto(chatUrl(seed, seed.workspace.conversations[key].conversation_id));
        await expect(page.locator('[data-kind="message"]').first()).toBeVisible({ timeout: 60_000 });
        const pin = page.getByRole('button', { name: 'Pin conversation', exact: true });
        if (await pin.count()) await pin.click();
        await expect(page.getByRole('button', { name: 'Unpin conversation', exact: true })).toBeVisible();
      }
      await page.goto(new URL('/library', seed.base_url).href);
      await expect(page.getByText('Launch readiness brief', { exact: true }).first()).toBeVisible();
      const launcher = page.getByTestId('pinned-chat-launcher');
      await expect(launcher).toHaveAttribute('aria-label', 'Open 3 pinned agents');
      const iris = page.getByRole('button', { name: 'Open pinned chats with Iris', exact: true });
      const forge = page.getByRole('button', { name: 'Open pinned chats with Forge', exact: true });
      const panel = page.getByTestId('pinned-chat-panel');
      const tab = (title: string) => panel.getByRole('tab').filter({ hasText: title });
      await page.waitForTimeout(1000);
      await recordGif(page, dir, 'pinned-chats', async () => {
        await press(page, launcher);
        await expect(iris).toBeVisible();
        await page.waitForTimeout(500);
        await press(page, iris);
        await expect(panel).toBeVisible();
        await press(page, tab('Brief: AI shopping assistants'));
        await expect(panel.getByText('Brief: AI shopping assistants in home lighting', { exact: false })).toBeVisible();
        await page.waitForTimeout(1600);
        await press(page, forge);
        await expect(page.getByRole('dialog', { name: 'Pinned chat with Forge' })).toBeVisible();
        await press(page, tab('Checkout · saved carts'));
        await expect(panel.getByText('Carts now follow the account', { exact: false })).toBeVisible();
        await page.waitForTimeout(1600);
      });
      // The panel remembers Forge's tab; show Iris's research for the still.
      await press(page, iris);
      await expect(panel.getByText('Brief: AI shopping assistants in home lighting', { exact: false })).toBeVisible();
      await shot(page, dir, 'pinned-chats', 'Open a pinned agent chat from any page',
        'Pinned agent avatars and a compact chat over the Library', checkpoints);
      checkpoints.push({ name: 'pinned-chats-gif', route: '/library', action: 'Open pinned agents from any page and switch between them',
        expected: 'Pinned avatars spread and compact chats open', file: 'pinned-chats.gif',
        elapsed_ms: Math.round(performance.now() - captureStart) });
    });
  } finally {
    if (!originallyEnabled) {
      await page.goto(new URL('/settings?tab=display', seed.base_url).href);
      await page.getByRole('checkbox', { name: /Dashboard workspace windows/ }).uncheck();
    }
  }
  await writeFile(resolve(dir, 'manifest.json'), JSON.stringify({
    ...(await evidence(seed, manifestHash, browser, checkpoints, 'hero-docs')),
    viewport, deviceScaleFactor: DOCS_SCALE, theme: 'porcelain', mode: 'light',
    provenance_note: 'Seeded fixture data; the feedback reply is a real turn against the isolated local mock model.',
  }, null, 2), { mode: 0o600 });
});
