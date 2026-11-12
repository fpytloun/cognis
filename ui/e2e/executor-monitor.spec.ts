import { expect, test, type Page, type WebSocketRoute } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';
import type { ExecutorConfig, ExecutorResourceSnapshot, RuntimeCapabilityStatus } from '../src/lib/types/api';

const GiB = 1024 ** 3;
const ready: RuntimeCapabilityStatus = { state: 'ready', reason_code: 'available', message: 'Ready' };

function snapshot(os: string, cpu = 24): ExecutorResourceSnapshot {
  return {
    schema_version: 1, observed_at: new Date().toISOString(),
    freshness: { age_seconds: 1, stale_after_seconds: 120, stale: false },
    os, arch: os === 'darwin' ? 'arm64' : 'x86_64',
    cpu: { model: os === 'darwin' ? 'Apple M4 Max' : 'AMD Ryzen 9 7950X', physical_cores: 16, logical_cores: 32, utilization_percent: cpu },
    memory: { total_bytes: 64 * GiB, used_bytes: 22 * GiB, available_bytes: 42 * GiB, unified: os === 'darwin' },
    accelerators: os === 'darwin'
      ? [{ backend: 'metal', name: 'Apple M4 Max', utilization_percent: null, total_memory_bytes: 64 * GiB, used_memory_bytes: null }]
      : [{ backend: 'nvidia', name: 'NVIDIA RTX 4090', utilization_percent: 36, total_memory_bytes: 24 * GiB, used_memory_bytes: 8 * GiB }],
    runtime: { uptime_seconds: 190_000, active_calls: 2, configured: true, state: 'active' },
    ollama: { status: 'reachable', version: '0.12.3', installed_model_count: 4, running_model_count: 1, running_models: ['qwen3:30b'] },
    ollama_model_store: { total_bytes: 1000 * GiB, free_bytes: 600 * GiB }
  };
}

test('same-version runtime invalidations survive a short background interval and refresh once', async ({ page }) => {
  const state = await fixture(page);
  await page.goto('/settings?tab=executors');
  const card = page.getByTestId('executor-card-sample-desktop');
  await expect(card).toBeVisible();
  await expect.poll(() => state.frames.includes('executor_monitor_subscribe')).toBe(true);
  await page.waitForTimeout(200);
  const reads = state.reads();
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  const executor = state.executors[0];
  executor.runtime_metadata.monitor_runtime_revision = 'mcp-recovery';
  executor.runtime_metadata.warnings = [];
  executor.observed_capabilities!.components.mcp = { state: 'ready', reason_code: 'available', message: 'MCP retry recovered.' };
  executor.observed_tools = [{ name: 'mcp_calendar__events', description: 'List calendar events', parameters: {} }];
  for (let frame = 0; frame < 3; frame++) {
    state.send({
      type: 'executor_monitor_update', executor_id: 'sample-desktop',
      details_revision: 'mcp-recovery', status: 'active', runtime_state: 'active',
      desired_config_version: 3, applied_config_version: 3,
      last_observed_at: new Date().toISOString(), resource_snapshot: snapshot('linux', 28)
    });
  }
  await page.waitForTimeout(200);
  expect(state.reads()).toEqual(reads);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect(card.locator('[title="MCP: ready — MCP retry recovered."]')).toBeVisible();
  expect(state.reads()).toEqual(reads.map(count => count + 1));
  state.send({
    type: 'executor_monitor_update', executor_id: 'sample-desktop',
    details_revision: 'mcp-recovery', status: 'active', runtime_state: 'active',
    desired_config_version: 3, applied_config_version: 3,
    last_observed_at: new Date().toISOString(), resource_snapshot: snapshot('linux', 29)
  });
  await page.waitForTimeout(200);
  expect(state.reads()).toEqual(reads.map(count => count + 1));
});

test('configuration confirmation received while hidden refreshes on visibility without a reconnect', async ({ page }) => {
  const state = await fixture(page);
  const executor = state.executors[0];
  executor.desired_config_version = 4;
  executor.local_inference_config_status = 'applying';
  await page.goto('/settings?tab=executors');
  const card = page.getByTestId('executor-card-sample-desktop');
  await expect(card).toBeVisible();
  await expect.poll(() => state.frames.includes('executor_monitor_subscribe')).toBe(true);
  await card.click();
  const dialog = page.getByRole('dialog', { name: 'Executor: Sample desktop' });
  await dialog.getByRole('button', { name: 'Inference', exact: true }).click();
  await expect(dialog.getByText('Applying', { exact: true })).toBeVisible();
  const reads = state.reads();
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  executor.applied_config_version = 4;
  executor.local_inference_config_status = 'confirmed';
  state.send({
    type: 'executor_monitor_update', executor_id: 'sample-desktop', details_revision: null,
    status: 'active', runtime_state: 'active', desired_config_version: 4, applied_config_version: 4,
    last_observed_at: new Date().toISOString(), resource_snapshot: snapshot('linux', 28)
  });
  await page.waitForTimeout(200);
  expect(state.reads()).toEqual(reads);
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect(dialog.getByText('Executor confirmed', { exact: true })).toBeVisible();
  expect(state.reads()).toEqual(reads.map(count => count + 1));
  expect(state.frames.filter(type => type === 'executor_monitor_subscribe')).toHaveLength(1);
});

test('runtime invalidation deferred behind mobile tool saving does not block later revisions', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await fixture(page);
  const executor = state.executors[0];
  executor.enabled_tools = [];
  await page.route('**/api/v1/tools/executor', route => route.fulfill({ json: [
    { name: 'read', description: 'Read a file', category: 'filesystem', read_only: true }
  ] }));
  let release!: () => void;
  let saving = false;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route('**/api/v1/executors/sample-desktop', async route => {
    if (route.request().method() === 'PUT') {
      saving = true;
      await gate;
      Object.assign(executor, route.request().postDataJSON());
    }
    await route.fulfill({ json: executor });
  });
  await page.goto('/settings?tab=executors');
  const card = page.getByTestId('executor-card-sample-desktop');
  await card.click();
  await page.getByRole('dialog', { name: 'Executor: Sample desktop' }).getByRole('button', { name: 'Tools & MCP', exact: true }).click();
  await page.getByRole('button', { name: 'Configure (0)', exact: true }).click();
  const picker = page.getByRole('dialog', { name: 'Configure tools for Sample desktop' });
  await picker.getByRole('button', { name: /read filesystem/ }).click();
  await expect.poll(() => saving).toBe(true);
  function runtimeChange(revision: string) {
    executor.runtime_metadata.monitor_runtime_revision = revision;
    executor.observed_capabilities!.components.mcp = { state: 'ready', reason_code: 'available', message: revision };
    state.send({
      type: 'executor_monitor_update', executor_id: 'sample-desktop', details_revision: revision,
      status: 'active', runtime_state: 'active', desired_config_version: 3, applied_config_version: 3,
      last_observed_at: new Date().toISOString(), resource_snapshot: snapshot('linux', 28)
    });
  }
  runtimeChange('Recovered during tool save');
  await page.waitForTimeout(100);
  release();
  await picker.getByRole('button', { name: 'Done', exact: true }).click();
  await page.keyboard.press('Escape');
  await expect(card.locator('[title="MCP: ready — Recovered during tool save"]')).toBeVisible();
  await page.waitForTimeout(200);
  runtimeChange('Recovered after tool save');
  await expect(card.locator('[title="MCP: ready — Recovered after tool save"]')).toBeVisible();
});

function host(executor_id: string, name: string, os: string): ExecutorConfig {
  return {
    executor_id, name, executor_type: 'websocket', available: true, unavailable_reason: null,
    owner_email: 'owner@cognis.test', shared: false, is_default: false,
    status: 'active', runtime_state: 'active', desired_config_version: 3, applied_config_version: 3,
    config: {}, enabled_tools: ['*'], enabled_tool_groups: [], labels: { location: 'home' },
    local_inference_enabled: true, ollama_management_enabled: true, ollama_port: 11434,
    ollama_endpoint: 'http://127.0.0.1:11434', local_inference_config_status: 'confirmed',
    last_observed_at: new Date().toISOString(), created_at: null, updated_at: null,
    resource_snapshot: snapshot(os), runtime_metadata: { platform: { os, arch: os === 'darwin' ? 'arm64' : 'x86_64' } },
    observed_capabilities: {
      schema_version: 1, observed_at: new Date().toISOString(), executor_version: '0.17.1',
      components: { browser: ready, documents: ready, inference: ready, lsp: ready, mcp: ready },
      supported_tools: ['bash', 'read'], supported_components: ['browser', 'documents', 'inference', 'lsp', 'mcp'],
      desired_tools: ['*'], observed_tools: ['bash', 'read'],
      browser: { runtimes: {}, engines: {}, channels: {} }, officecli: ready, mcp_launch: ready,
      git: ready, node: ready, uv: ready, lsp: ready
    }
  };
}

async function fixture(page: Page) {
  await installTaskCockpitFixture(page);
  await page.route('**/api/auth/me', route => route.fulfill({ json: { email: 'owner@cognis.test', name: 'Owner', role: 'user' } }));
  await page.route('**/api/health**', route => route.fulfill({ json: { status: 'ok', providers: {} } }));
  for (const path of ['secrets', 'credentials', 'auth/api-keys', 'tools/executor', 'mcp-servers', 'local-models/deployments']) {
    await page.route(`**/api/v1/${path}`, route => route.fulfill({ json: [] }));
  }
  const executors = [
    { ...host('sample-desktop', 'Sample desktop', 'linux'), is_default: true },
    host('sample-laptop', 'Sample laptop · Mac', 'darwin'),
    { ...host('build-runner', 'Build runner', 'linux'), runtime_state: 'degraded',
      resource_snapshot: snapshot('linux', 88),
      runtime_metadata: { warnings: ['One MCP server needs attention.'] } },
    { ...host('travel', 'Travel workstation', 'windows'), runtime_state: 'offline', resource_snapshot: null, observed_capabilities: null }
  ];
  for (const executor of executors) {
    if (executor.resource_snapshot) executor.resource_snapshot.observed_at = new Date(Date.now() - 11 * 60_000).toISOString();
  }
  let listReads = 0;
  let statusReads = 0;
  const mutations: unknown[] = [];
  await page.route('**/api/v1/executors', route => {
    listReads += 1;
    return route.fulfill({ json: executors });
  });
  await page.route('**/api/v1/executors/*', route => {
    const id = new URL(route.request().url()).pathname.split('/').slice(-1)[0];
    const executor = executors.find(item => item.executor_id === id);
    if (route.request().method() === 'PUT') {
      const update = route.request().postDataJSON();
      mutations.push(update);
      Object.assign(executor!, update);
    }
    return route.fulfill({ json: executor });
  });
  await page.route('**/api/v1/executor/status', route => {
    statusReads += 1;
    return route.fulfill({ json: { executor_type: 'websocket', status: 'healthy', active_executors: 3, capabilities: {}, native_tools: [], available_executor_types: ['websocket'] } });
  });
  let socket: WebSocketRoute;
  const frames: string[] = [];
  await page.routeWebSocket('**/api/ws', ws => {
    socket = ws;
    ws.send(JSON.stringify({ type: 'authenticated' }));
    ws.onMessage(message => {
      const frame = JSON.parse(String(message));
      frames.push(frame.type);
      if (frame.type === 'ping') ws.send(JSON.stringify({ type: 'pong' }));
    });
  });
  return {
    executors, mutations, frames,
    reads: () => [listReads, statusReads],
    send: (value: object) => socket.send(JSON.stringify(value))
  };
}

for (const width of [1440, 390]) {
  test(`executor cards, live telemetry, tabs and safe editing at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('requestfailed', request => console.error('Request failed:', request.url(), request.failure()?.errorText));
    const state = await fixture(page);
    await page.goto('/settings?tab=executors');
    const grid = page.getByTestId('executor-grid');
    await expect(grid.getByRole('button')).toHaveCount(4);
    for (const platform of ['Linux', 'macOS', 'Windows']) {
      const logo = grid.getByRole('img', { name: `${platform} logo`, exact: true }).first();
      await expect(logo).toBeVisible();
      await expect.poll(() => logo.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
    }
    await expect.poll(() => state.frames.includes('executor_monitor_subscribe')).toBe(true);
    await expect(page.getByRole('status').filter({ hasText: 'Live updates' })).toBeVisible();
    const card = page.getByTestId('executor-card-sample-desktop');
    await expect(card.getByText('24%', { exact: true })).toBeVisible();

    // Deliver actual protocol frames, building a chart without fixture-only UI paths.
    const start = Date.now() - 10 * 60_000;
    for (let point = 0; point <= 20; point++) {
      for (const executor of state.executors.filter(item => item.resource_snapshot)) {
        const resource = structuredClone(executor.resource_snapshot!);
        resource.observed_at = new Date(start + point * 30_000).toISOString();
        resource.cpu!.utilization_percent = Math.round(20 + Math.sin(point * .7) * 8 + point * .4);
        resource.memory!.used_bytes = (22 + Math.sin(point * .2) * 2) * GiB;
        state.send({
          type: 'executor_monitor_update', executor_id: executor.executor_id,
          status: executor.status, runtime_state: executor.runtime_state,
          desired_config_version: 3, applied_config_version: 3,
          last_observed_at: resource.observed_at, resource_snapshot: resource
        });
      }
      await page.waitForTimeout(20);
    }
    await expect(card.getByRole('img', { name: /CPU:.*22 observed samples/ })).toBeVisible();
    // Wait beyond the old five-second interval: resource updates must not cause inventory polling.
    const reads = state.reads();
    await page.waitForTimeout(5500);
    expect(state.reads()).toEqual(reads);
    await page.screenshot({ path: testInfo.outputPath(`executor-cards-${width}.png`), fullPage: true });

    await card.click();
    const dialog = page.getByRole('dialog', { name: /Executor: Sample desktop/ });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText('Processor', { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`executor-overview-${width}.png`) });
    await dialog.getByRole('button', { name: 'Runtime', exact: true }).click();
    await expect(dialog.getByText('Browser Automation', { exact: false }).first()).toBeVisible();
    await dialog.getByRole('button', { name: 'Tools & MCP', exact: true }).click();
    await expect(dialog.getByRole('heading', { name: 'Runtime capabilities', exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Inference', exact: true }).click();
    await expect(dialog.getByText('Local inference', { exact: true })).toBeVisible();
    await dialog.getByRole('button', { name: 'Settings', exact: true }).click();
    const name = dialog.getByLabel('Name', { exact: true });
    await name.fill('Sample desktop edited');
    state.send({
      type: 'executor_monitor_update', executor_id: 'sample-desktop', status: 'active',
      runtime_state: 'active', desired_config_version: 3, applied_config_version: 3,
      last_observed_at: new Date().toISOString(), resource_snapshot: snapshot('linux', 67)
    });
    await expect(name).toHaveValue('Sample desktop edited');
    await dialog.getByRole('button', { name: 'Save changes', exact: true }).click();
    await expect.poll(() => state.mutations.length).toBe(1);
    await expect(dialog.getByRole('heading', { name: 'Sample desktop edited', exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`executor-settings-${width}.png`) });
    await page.keyboard.press('Escape');
    await expect(dialog).not.toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    expect(errors).toEqual([]);
    await page.getByRole('tab', { name: 'account', exact: true }).click();
    await expect.poll(() => state.frames.includes('executor_monitor_unsubscribe')).toBe(true);
  });
}
