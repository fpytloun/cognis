/**
 * UI sweep captures: the same views at desktop and phone size, one test per
 * implementation batch, so later sweeps compare like for like.
 *
 * Runs only against the isolated Lumen showcase runtime (loopback base URL).
 * The first run adds three outcome tasks through the local mock model: a task
 * paused on a step question, a failed task and a draft. Output goes to
 * $SHOWCASE_OUTPUT/sweep/$SWEEP_LABEL/<project>/<batch>-<view>.png.
 */
import { expect, test, type APIRequestContext, type Page } from '../../../ui/node_modules/@playwright/test';
import { mkdir, readFile, stat } from 'node:fs/promises';
import { resolve } from 'node:path';
import { parseEnv } from 'node:util';

type Manifest = {
  base_url: string;
  runtime: { ports: { MOCK_LLM_PORT: number } };
  users: { owner_email: string };
  workspace: {
    conversations: Record<string, { conversation_id: string }>;
    tasks: Record<string, { task_id: string }>;
    projects: Record<string, string>;
    schedules: Record<string, string>;
  };
};

type Fixtures = { question: string; failed: string; draft: string };
type View = { name: string; path: (f: Fixtures, m: Manifest) => string; act?: (page: Page) => Promise<void>; phoneOnly?: boolean; desktopOnly?: boolean };

const QUESTION = { title: 'Choose the launch weekend courier', description: 'Choose the courier for the first launch weekend.' };
const FAILED = { title: 'Import the partner product feed', description: 'Import the partner product feed for the Autumn catalog.' };
const DRAFT = { title: 'Plan the winter collection teaser', description: 'Outline the teaser campaign for the winter collection.' };

async function manifest() {
  if (!process.env.SHOWCASE_MANIFEST || !process.env.SHOWCASE_OUTPUT || !process.env.SHOWCASE_PRIVATE_ENV) {
    throw new Error('SHOWCASE_MANIFEST, SHOWCASE_PRIVATE_ENV and SHOWCASE_OUTPUT are required');
  }
  const seed: Manifest = JSON.parse(await readFile(process.env.SHOWCASE_MANIFEST, 'utf8'));
  // A locally served UI build in front of the same runtime, for before/after comparisons.
  if (process.env.SWEEP_BASE_URL) seed.base_url = process.env.SWEEP_BASE_URL;
  const base = new URL(seed.base_url);
  if (!['localhost', '127.0.0.1', '[::1]'].includes(base.hostname)) throw new Error('Sweep runs only against a loopback showcase runtime');
  const privatePath = process.env.SHOWCASE_PRIVATE_ENV;
  if (((await stat(privatePath)).mode & 0o777) !== 0o600) throw new Error('SHOWCASE_PRIVATE_ENV must have mode 0600');
  const password = parseEnv(await readFile(privatePath, 'utf8')).SHOWCASE_OWNER_PASSWORD;
  if (!password) throw new Error('Owner password missing');
  return { seed, password };
}

function questionTurn(id: string, question: string, options: Array<[string, string]>) {
  return {
    role: 'assistant',
    steps: [{
      type: 'tool_call', call_id: `call_${id}`, name: 'step_request_questions', delay_ms: 200,
      arguments: { questions: [{ id, question, options: options.map(([value, label]) => ({ id: value, label })), allow_custom: true, required: true }] }
    }]
  };
}

function finishTurns(key: string, title: string, summary: string, outcome?: { status: string; reason: string }) {
  const write = { title, format: 'markdown', content: `# ${title}\n\n${summary}` };
  return [
    { role: 'assistant', steps: [{ type: 'tool_call', call_id: `call_write_${key}`, name: 'write_deliverable', arguments: write, delay_ms: 200 }] },
    {
      role: 'assistant',
      steps: [{
        type: 'tool_call', call_id: `call_complete_${key}`, name: 'step_complete', delay_ms: 200,
        arguments: {
          summary,
          ...(outcome ? { outcome } : {}),
          metadata: {
            result_status: outcome ? 'blocked' : 'completed',
            verification: { checks_run: [], results: [summary], checks_not_run: [] },
          }
        }
      }]
    }
  ];
}

async function findTask(api: APIRequestContext, base: string, title: string): Promise<string | null> {
  const response = await api.get(new URL(`/api/v1/tasks?limit=100&q=${encodeURIComponent(title)}`, base).href);
  expect(response.ok()).toBeTruthy();
  const items: Array<{ task_id: string; title: string }> = (await response.json()).items;
  return items.find((item) => item.title === title)?.task_id ?? null;
}

async function waitStatus(api: APIRequestContext, base: string, taskId: string, statuses: string[]) {
  await expect.poll(async () => (await (await api.get(new URL(`/api/v1/tasks/${taskId}`, base).href)).json()).status, { timeout: 120_000 }).toMatch(new RegExp(`^(${statuses.join('|')})$`));
}

/** Idempotently add the outcome tasks the sweep needs; scenarios run on the local mock model only. */
async function ensureFixtures(page: Page, seed: Manifest): Promise<Fixtures> {
  const api = page.request;
  const base = seed.base_url;
  const create = async (spec: { title: string; description: string }, turns: unknown[] | null, status: string, interaction: string) => {
    const existing = await findTask(api, base, spec.title);
    if (existing) return existing;
    return createTask(page, seed, spec, turns, status, interaction);
  };
  const question = await create(QUESTION, [questionTurn('courier', 'Which courier should handle the first launch weekend?', [['parcelone', 'ParcelOne'], ['swiftbox', 'SwiftBox']])], 'queued', 'step_requests');
  const failed = await create(FAILED, finishTurns('failed', 'Partner feed import', 'The partner feed rejected the API credentials; nothing was imported.', { status: 'failed', reason: 'The partner feed rejected the API credentials.' }), 'queued', 'none');
  const draft = await create(DRAFT, null, 'draft', 'none');
  await waitStatus(api, base, question, ['paused']);
  await waitStatus(api, base, failed, ['failed']);
  return { question, failed, draft };
}

/** Create one task in the Autumn project; scenario turns are registered on the local mock model only. */
async function createTask(page: Page, seed: Manifest, spec: { title: string; description: string }, turns: unknown[] | null, status: string, interaction: string): Promise<string> {
  const api = page.request;
  const base = seed.base_url;
  const mock = `http://127.0.0.1:${seed.runtime.ports.MOCK_LLM_PORT}`;
  const workflows: Array<{ workflow_id: string }> = (await (await api.get(new URL('/api/v1/workflows', base).href)).json()).items;
  const workflow = workflows.find((item) => item.workflow_id.endsWith('general-task'))!.workflow_id;
  const project = seed.workspace.projects.autumn;
  if (turns) {
    const registered = await api.post(`${mock}/__mock/scenario`, { data: { id: `sweep-${spec.title}`, trigger: spec.description, description: 'UI sweep outcome fixture on the local mock model.', turns } });
    expect(registered.ok()).toBeTruthy();
  }
  const created = await api.post(new URL('/api/v1/tasks', base).href, {
    data: { agent_id: 'iris', project_id: project, title: spec.title, description: spec.description, workflow_id: workflow, interaction_mode_override: interaction, status }
  });
  expect(created.ok()).toBeTruthy();
  return (await created.json()).task_id as string;
}

async function signIn(page: Page, seed: Manifest, password: string) {
  const response = await page.request.post(new URL('/api/auth/login', seed.base_url).href, { data: { email: seed.users.owner_email, password } });
  expect(response.ok()).toBeTruthy();
}

async function capture(page: Page, seed: Manifest, fixtures: Fixtures, batch: string, views: View[], project: string) {
  const out = resolve(process.env.SHOWCASE_OUTPUT!, 'sweep', process.env.SWEEP_LABEL ?? 'current', project);
  await mkdir(out, { recursive: true, mode: 0o700 });
  const phone = project.endsWith('phone');
  for (const view of views) {
    if ((view.phoneOnly && !phone) || (view.desktopOnly && phone)) continue;
    await test.step(view.name, async () => {
      await page.goto(new URL(view.path(fixtures, seed), seed.base_url).href);
      await expect(page.getByRole('status', { name: 'Loading Cognis', exact: true })).toHaveCount(0);
      await expect(page.getByRole('main')).toBeVisible();
      await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => undefined);
      await page.waitForTimeout(1_500);
      if (view.act) await view.act(page);
      await page.screenshot({ path: resolve(out, `${batch}-${view.name}.png`) });
    });
  }
}

/** Optional interaction: missing controls are recorded, not fatal, so before/after runs share one spec. */
async function tryClick(page: Page, locator: ReturnType<Page['locator']>, wait = 1_500) {
  if (await locator.first().isVisible().catch(() => false)) {
    await locator.first().click();
    await page.waitForTimeout(wait);
  } else {
    test.info().annotations.push({ type: 'missing-control', description: String(locator) });
  }
}

const tasksPath = (id: string) => `/tasks/${id}`;
const BATCHES: Record<string, View[]> = {
  'b1-status': [
    { name: 'failed-task', path: (f) => tasksPath(f.failed) },
    { name: 'paused-task-phases', path: (f) => tasksPath(f.question), act: async (page) => { await page.getByTestId('task-cockpit-phases').scrollIntoViewIfNeeded().catch(() => undefined); } },
    { name: 'schedules', path: () => '/schedules' },
    { name: 'schedule-modal', path: () => '/', act: async (page) => tryClick(page, page.getByTestId('dashboard-tasks-upcoming').getByRole('button').first(), 2_500) },
    { name: 'finished-chat', path: (_f, m) => `/chat/${m.workspace.conversations['saved-carts'].conversation_id}` }
  ],
  'b2-attention': [
    { name: 'paused-question-task', path: (f) => tasksPath(f.question) },
    { name: 'failed-task', path: (f) => tasksPath(f.failed) },
    { name: 'actions-sheet', path: (f) => tasksPath(f.failed), phoneOnly: true, act: async (page) => tryClick(page, page.getByRole('button', { name: 'More task actions' })) },
    { name: 'rerun-confirm', path: (_f, m) => tasksPath(m.workspace.tasks['readiness-brief'].task_id), desktopOnly: true, act: async (page) => tryClick(page, page.getByRole('button', { name: /^(Re-run task|Start a new run)$/ })) },
    { name: 'dashboard', path: () => '/' },
    { name: 'dashboard-task-modal', path: () => '/', act: async (page) => tryClick(page, page.getByTestId('dashboard-tasks-waiting-list').getByRole('button').first(), 3_000) },
    { name: 'schedule-run-now', path: () => '/schedules', desktopOnly: true, act: async (page) => tryClick(page, page.getByRole('button', { name: /^(Trigger schedule now|Run now)/ })) }
  ],
  'b3-lists': [
    { name: 'task-board', path: () => '/tasks' },
    { name: 'task-board-failed', path: () => '/tasks?status=failed' },
    { name: 'task-board-no-match', path: () => '/tasks?q=zzzz-no-match' },
    { name: 'dashboard', path: () => '/' }
  ],
  'b4-step-viewer': [
    { name: 'draft-task', path: (f) => tasksPath(f.draft) },
    { name: 'step-output', path: (f) => tasksPath(f.failed), act: async (page) => tryClick(page, page.getByRole('button', { name: /^(Output|Open output|Result)$/ }), 2_500) },
    { name: 'step-logs', path: (f) => tasksPath(f.failed), act: async (page) => tryClick(page, page.getByRole('button', { name: /^(Logs|Open logs|Transcript)$/ }), 2_500) }
  ],
  'b5-editing': [
    { name: 'schedule-detail', path: (_f, m) => `/schedules/${Object.values(m.workspace.schedules)[0]}` },
    { name: 'workflow-system', path: () => '/workflows' },
    { name: 'task-configure', path: (f) => tasksPath(f.draft), desktopOnly: true, act: async (page) => tryClick(page, page.getByRole('button', { name: /^Configure/ })) }
  ],
  'b6-polish': [
    { name: 'create-task', path: () => '/tasks', act: async (page) => tryClick(page, page.getByRole('button', { name: 'Create task' })) },
    { name: 'new-chat', path: () => '/chat/new' },
    { name: 'chat', path: (_f, m) => `/chat/${m.workspace.conversations['saved-carts'].conversation_id}` },
    { name: 'settings', path: () => '/settings' },
    { name: 'project', path: (_f, m) => `/projects/${m.workspace.projects.autumn}` },
    { name: 'usage', path: () => '/usage' }
  ]
};

for (const [batch, views] of Object.entries(BATCHES)) {
  test(`UI sweep ${batch}`, async ({ page }, info) => {
    const { seed, password } = await manifest();
    await signIn(page, seed, password);
    const fixtures = await ensureFixtures(page, seed);
    await capture(page, seed, fixtures, batch, views, info.project.name);
  });
}

test('UI sweep question answer dialog', async ({ page }, info) => {
  test.skip(info.project.name !== 'sweep-desktop', 'The answer flow is viewport independent; it runs once per sweep.');
  const { seed, password } = await manifest();
  await signIn(page, seed, password);
  // A fresh task per run, so an earlier answer or a recovered run cannot skip the dialog.
  const run = Date.now().toString(36);
  const spec = { title: `Confirm the launch photo shoot location ${run}`, description: `Confirm where the Autumn launch photo shoot takes place (${run}).` };
  const answer = await createTask(page, seed, spec, [
    questionTurn(`location_${run}`, 'Where should the Autumn launch photo shoot take place?', [['studio', 'Studio Nord'], ['orchard', 'The orchard']]),
    ...finishTurns(`answer_${run}`, 'Photo shoot location', 'The photo shoot takes place at the chosen location.')
  ], 'queued', 'step_requests');
  await expect.poll(async () => {
    const response = await page.request.get(new URL(`/api/v1/tasks/${answer}/summary`, seed.base_url).href);
    expect(response.ok()).toBeTruthy();
    const task = await response.json();
    return { status: task.status, pause_type: task.pending_pause?.pause_type };
  }, { timeout: 120_000 }).toEqual({ status: 'paused', pause_type: 'step_question' });
  const out = resolve(process.env.SHOWCASE_OUTPUT!, 'sweep', process.env.SWEEP_LABEL ?? 'current', info.project.name);
  await mkdir(out, { recursive: true, mode: 0o700 });
  await page.goto(new URL(`/tasks/${answer}`, seed.base_url).href);
  const attention = page.getByTestId('task-attention');
  await expect(attention).toBeVisible();
  await page.screenshot({ path: resolve(out, 'question-before-answer.png') });
  await attention.getByRole('radio', { name: 'Studio Nord' }).check();
  await attention.getByRole('button', { name: 'Send response' }).click();
  await waitStatus(page.request, seed.base_url, answer, ['completed']);
  await page.reload();
  await expect(page.getByTestId('task-status')).toContainText('Completed');
  await page.screenshot({ path: resolve(out, 'question-after-answer.png') });
});
