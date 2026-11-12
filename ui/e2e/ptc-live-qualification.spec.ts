import { mkdir, writeFile } from 'node:fs/promises';
import { test, expect, type Page, type TestInfo } from '@playwright/test';
import {
  MOCK_LLM_URL,
  clearScenario,
  injectScenario,
  login,
} from './helpers';

const POLICY_MODEL = 'ptc-policy-model';
const SCENARIO_ID = 'ptc-live-qualification';
const FIRST_MESSAGE =
  'Please run the first PTC qualification cell in the isolated local test services. ' +
  'Set the local sentinel and invoke one read-only memory_search for the qualification ' +
  'marker; do not use any external or production service. scenario:ptc-live-qualification';
const SECOND_MESSAGE =
  "In this same conversation, run the second PTC qualification cell and verify that " +
  "the first cell's sentinel remains in the same Python kernel.";
const SENTINEL = 'ptc-live-qualification-v1';

interface ApiResult {
  status: number;
  payload: any;
  text: string;
}

interface TimelineItem {
  id: string;
  kind: string;
  call_id?: string;
  tool_name?: string;
  parent_call_id?: string | null;
  program_id?: string | null;
  kernel_generation?: string | number | null;
  status?: string;
  stable?: boolean;
  role?: string;
  content?: string;
  result_preview?: string;
  evaluation?: { decision?: string; path?: string };
}

async function api(
  page: Page,
  path: string,
  method = 'GET',
  body?: unknown,
): Promise<ApiResult> {
  return page.evaluate(async ({ path, method, body }) => {
    const response = await fetch(path, {
      method,
      credentials: 'include',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let payload: any = undefined;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = text;
      }
    }
    return { status: response.status, payload, text };
  }, { path, method, body });
}

async function apiOk(
  page: Page,
  path: string,
  method = 'GET',
  body?: unknown,
): Promise<any> {
  const response = await api(page, path, method, body);
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`${method} ${path} failed (${response.status}): ${response.text.slice(0, 800)}`);
  }
  return response.payload;
}

async function createQualificationResources(page: Page): Promise<{
  agentId: string;
  executorId: string;
  conversationId: string;
}> {
  const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  const executorId = `ptc-qualification-executor-${suffix}`;
  const agentId = `ptc-qualification-agent-${suffix}`;
  await apiOk(page, '/api/v1/executors', 'POST', {
    executor_id: executorId,
    name: 'PTC live qualification executor',
    executor_type: 'in_process',
    labels: { role: 'e2e-ptc-qualification' },
    enabled_tool_groups: [],
    enabled_tools: ['python_execute'],
    is_default: false,
    config: { program_runtime: { host_enabled: true } },
  });
  await apiOk(page, '/api/v1/agents', 'POST', {
    agent_id: agentId,
    name: 'PTC live qualification agent',
    display_name: 'PTC live qualification agent',
    description: 'Isolated PTC test agent using normal Intaris guardrails.',
    system_prompt:
      'You are a deterministic local qualification agent. Follow the user’s scoped test request and use only the requested Python cells.',
    capabilities: { memory_backend: 'mnemory', guardrails_backend: 'intaris' },
    tools: {
      builtin_tools: [
        'memory_search',
        'python_execute',
        'search_tools',
        'describe_tool',
        'call_tool',
      ],
      tool_groups: [],
    },
    permissions: {
      tool_permissions: {
        python_execute: 'allow',
        memory_search: 'allow',
        search_tools: 'allow',
        describe_tool: 'allow',
        call_tool: 'allow',
      },
      can_delegate: false,
    },
    llm_config: { provider_id: 'e2e-mock-llm', model: 'mock-model' },
    execution: { executor_id: executorId },
    agent_type: 'primary',
    status: 'draft',
  });
  await apiOk(page, `/api/v1/agents/${agentId}/activate`, 'POST');
  const agent = await apiOk(page, `/api/v1/agents/${agentId}`);
  expect(agent.capabilities.guardrails_backend).toBe('intaris');
  expect(agent.execution.executor_id).toBe(executorId);
  expect(agent.tools.builtin_tools).toEqual(
    expect.arrayContaining([
      'python_execute',
      'search_tools',
      'describe_tool',
      'call_tool',
      'memory_search',
    ]),
  );
  const effectiveTools = await apiOk(page, `/api/v1/agents/${agentId}/effective-tools`);
  const configuredToolNames = effectiveTools.configured_state.tools.map(
    (tool: { name: string }) => tool.name,
  );
  const missingEffectiveTools = ['python_execute', 'search_tools', 'describe_tool', 'memory_search']
    .filter((name) => !configuredToolNames.includes(name));
  expect(missingEffectiveTools, `Missing configured tool names: ${missingEffectiveTools}`).toEqual([]);

  const conversation = await apiOk(page, '/api/v1/conversations', 'POST', {
    agent_id: agentId,
    title: `PTC live qualification ${suffix}`,
    context: { type: 'web', ref: null, platform_data: {}, memory_labels: {} },
  });
  return { agentId, executorId, conversationId: conversation.conversation_id };
}

async function snapshot(page: Page, conversationId: string): Promise<any> {
  return apiOk(page, `/api/v1/chat/v2/conversations/${conversationId}/snapshot`);
}

function timelineItems(value: any): TimelineItem[] {
  return value.timeline.items as TimelineItem[];
}

function pythonCalls(value: any): TimelineItem[] {
  return timelineItems(value).filter(
    (item) => item.kind === 'tool_call' && item.tool_name === 'python_execute',
  );
}

function nestedCalls(value: any): TimelineItem[] {
  return timelineItems(value).filter(
    (item) => item.kind === 'tool_call' && typeof item.parent_call_id === 'string',
  );
}

function programResult(item: TimelineItem): any {
  expect(item.result_preview).toBeTruthy();
  const result = JSON.parse(item.result_preview!);
  expect(result.protocol_version).toBe('cognis.program.v1');
  return result;
}

async function saveMockRequestMetadata(page: Page, testInfo: TestInfo): Promise<void> {
  const response = await page.request.get(`${MOCK_LLM_URL}/__mock/history?limit=50`);
  if (!response.ok()) {
    await writeFile(
      testInfo.outputPath('ptc-live-mock-request-metadata.json'),
      JSON.stringify({ status: response.status() }, null, 2),
    );
    return;
  }
  const history = (await response.json()).history ?? [];
  const requests = history.map((item: any) => ({
    scenario_id: item.scenario_id,
    model: item.request?.model,
    turn_index: item.request?.turn_index,
    messages_count: item.request?.messages_count,
    stream: item.request?.stream,
    policy_model: item.request?.policy_model,
    exposed_tool_names: item.request?.exposed_tool_names,
  }));
  await writeFile(
    testInfo.outputPath('ptc-live-mock-request-metadata.json'),
    JSON.stringify(requests, null, 2),
  );
}

async function waitForAssistantText(
  page: Page,
  conversationId: string,
  expectedText: string,
): Promise<void> {
  await expect.poll(
    async () => page.evaluate(async ({ conversationId, expectedText }) => {
      const response = await fetch(
        `/api/v1/chat/v2/conversations/${conversationId}/snapshot`,
        { credentials: 'include' },
      );
      if (!response.ok) return false;
      const value = await response.json();
      const finalMessage = value.timeline.items.some(
        (item: TimelineItem) =>
          item.kind === 'message' &&
          item.role === 'assistant' &&
          item.stable === true &&
          String(item.content ?? '').includes(expectedText),
      );
      return (
        finalMessage &&
        value.runtime?.has_active_turn === false &&
        value.state?.active_turn?.has_active_turn === false
      );
    }, { conversationId, expectedText }),
    { timeout: 100_000, intervals: [250, 500, 1_000, 2_000] },
  ).toBe(true);
}

async function expandPythonCards(page: Page): Promise<void> {
  const cards = page.locator('[data-kind="python_execution"]');
  for (const card of await cards.all()) {
    const details = card.locator('details').first();
    if ((await details.getAttribute('open')) === null) {
      await details.locator('summary').click();
    }
  }
}

async function assertRenderedQualification(
  page: Page,
  value: any,
  options: {
    expectedParents: number;
    expectedChildren: number;
    firstGeneration?: string | number | null;
  },
): Promise<{ parents: TimelineItem[]; children: TimelineItem[] }> {
  const { expectedParents, expectedChildren, firstGeneration } = options;
  const parents = pythonCalls(value);
  const children = nestedCalls(value);
  expect(parents).toHaveLength(expectedParents);
  expect(children).toHaveLength(expectedChildren);
  const ids = timelineItems(value).map((item) => item.id);
  expect(new Set(ids).size).toBe(ids.length);
  expect(parents.every((item) => item.status === 'complete')).toBe(true);
  expect(children.every((item) => item.status === 'complete')).toBe(true);
  for (const parent of parents) {
    const program = programResult(parent);
    expect(program.program_id).toBe(parent.call_id);
    expect(program.status).toBe('completed');
    expect(program.kernel_generation).not.toBeNull();
    expect(parent.evaluation?.decision).toBe('approve');
  }
  if (children.length) {
    const child = children[0];
    const parent = parents.find((item) => item.call_id === child.parent_call_id);
    expect(parent).toBeTruthy();
    const program = programResult(parent!);
    expect(child.tool_name).toBe('memory_search');
    expect(child.program_id).toBe(program.program_id);
    expect(child.kernel_generation).toBe(program.kernel_generation);
    expect(timelineItems(value).indexOf(parent!)).toBeLessThan(timelineItems(value).indexOf(child));
    expect(parent?.evaluation?.decision).toBe('approve');
  }
  if (firstGeneration !== undefined) {
    expect(programResult(parents[0]).kernel_generation).toBe(firstGeneration);
    expect(programResult(parents[1]).kernel_generation).toBe(firstGeneration);
  }

  const renderedCards = page.locator('[data-kind="python_execution"]');
  await expect(renderedCards).toHaveCount(expectedParents);
  if (children.length) {
    const firstCard = page.locator(
      `[data-kind="python_execution"][data-call-id="${parents[0].call_id}"]`,
    );
    await expect(firstCard.locator('[aria-label="Nested tool activity"] article')).toHaveCount(1);
  }
  return { parents, children };
}

function recordWebSocket(page: Page, frames: string[]): void {
  page.on('websocket', (socket) => {
    if (!new URL(socket.url()).pathname.endsWith('/api/ws')) return;
    socket.on('framereceived', (data) => {
      const frame = typeof data === 'string' ? data : data.toString();
      try {
        const parsed = JSON.parse(frame);
        if (parsed.type === 'authenticated') return;
        if (parsed.type === 'chat_v2_frame') {
          frames.push(JSON.stringify({
            direction: 'received',
            type: parsed.type,
            scope: parsed.scope,
            cursor_before: parsed.cursor_before,
            cursor_after: parsed.cursor_after,
            frame: parsed.frame,
          }));
          return;
        }
        frames.push(JSON.stringify({
          direction: 'received',
          type: parsed.type,
          seq: parsed.seq,
          reason: parsed.reason,
        }));
      } catch {
        frames.push(JSON.stringify({ direction: 'received', frame: '[non-JSON frame]' }));
      }
    });
    socket.on('framesent', (data) => {
      const frame = typeof data === 'string' ? data : data.toString();
      try {
        const parsed = JSON.parse(frame);
        if (parsed.type === 'auth') return;
        frames.push(JSON.stringify({ direction: 'sent', frame: parsed }));
      } catch {
        frames.push(JSON.stringify({ direction: 'sent', frame: '[non-JSON frame]' }));
      }
    });
  });
}

async function preflightLiveServices(page: Page, testInfo: TestInfo): Promise<void> {
  const intarisUpstreamUrl = process.env.COGNIS_PTC_INTARIS_UPSTREAM_URL;
  const intarisProxyUrl = process.env.COGNIS_PTC_INTARIS_PROXY_URL;
  expect(intarisUpstreamUrl).toBeTruthy();
  expect(intarisProxyUrl).toBeTruthy();

  const [intarisUpstream, intarisProxy, mockLlm, cognisHealth, diagnostics] =
    await Promise.all([
      page.request.get(`${intarisUpstreamUrl}/health`),
      page.request.get(`${intarisProxyUrl}/health`),
      page.request.get(`${MOCK_LLM_URL}/health`),
      apiOk(page, '/api/health'),
      apiOk(page, '/api/v1/system/diagnostics'),
    ]);
  const evidence = {
    intaris_upstream_url: intarisUpstreamUrl,
    intaris_upstream_status: intarisUpstream.status(),
    intaris_configured_proxy_url: intarisProxyUrl,
    intaris_configured_proxy_status: intarisProxy.status(),
    mock_llm_url: MOCK_LLM_URL,
    mock_llm_status: mockLlm.status(),
    provider_statuses: Object.fromEntries(
      Object.entries(cognisHealth.providers ?? {}).map(([name, value]: [string, any]) => [
        name,
        value.status,
      ]),
    ),
    readiness: diagnostics.readiness,
  };
  await writeFile(
    testInfo.outputPath('ptc-live-services-preflight.json'),
    JSON.stringify(evidence, null, 2),
  );

  expect(intarisUpstream.status()).toBe(200);
  expect(intarisProxy.status()).toBe(200);
  expect(mockLlm.status()).toBe(200);
  expect(cognisHealth.providers?.llm?.status).toBe('healthy');
  expect(cognisHealth.providers?.guardrails?.status).toBe('healthy');
  expect(diagnostics.readiness?.intaris_reachable).toBe(true);
  expect(diagnostics.readiness?.mnemory_reachable).toBe(true);
}

test('real PTC cells render once through policy, reconciliation, and reload', async ({
  page,
  context,
}, testInfo: TestInfo) => {
  test.setTimeout(240_000);
  await login(page);
  const outputDir = testInfo.outputPath();
  await mkdir(outputDir, { recursive: true });
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
  const wsFrames: string[] = [];
  recordWebSocket(page, wsFrames);

  let resources: Awaited<ReturnType<typeof createQualificationResources>> | undefined;
  try {
    resources = await createQualificationResources(page);
    await injectScenario(SCENARIO_ID);
    await page.goto(`/chat/${resources.conversationId}`, { waitUntil: 'domcontentloaded' });
    const composer = page.getByRole('textbox', { name: 'Chat message' });
    await expect(composer).toBeVisible();
    const initialSnapshot = await snapshot(page, resources.conversationId);

    await composer.fill(FIRST_MESSAGE);
    await preflightLiveServices(page, testInfo);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await waitForAssistantText(
      page,
      resources.conversationId,
      'The first Python cell completed with its nested local memory search.',
    );
    const firstSnapshot = await snapshot(page, resources.conversationId);
    await writeFile(
      testInfo.outputPath('ptc-live-first-snapshot-metadata.json'),
      JSON.stringify(
        {
          items: timelineItems(firstSnapshot).map((item) => ({
            id: item.id,
            kind: item.kind,
            role: item.role,
            call_id: item.call_id,
            tool_name: item.tool_name,
            parent_call_id: item.parent_call_id,
            status: item.status,
            evaluation_decision: item.evaluation?.decision,
          })),
          has_active_turn: firstSnapshot.runtime?.has_active_turn,
          runtime_active_turn: firstSnapshot.runtime?.active_turn,
          state_active_turn: firstSnapshot.state?.active_turn,
        },
        null,
        2,
      ),
    );
    const first = await assertRenderedQualification(page, firstSnapshot, {
      expectedParents: 1,
      expectedChildren: 1,
    });
    const firstGeneration = programResult(first.parents[0]).kernel_generation;
    const firstCard = page.locator(
      `[data-kind="python_execution"][data-call-id="${first.parents[0].call_id}"]`,
    );
    await firstCard.locator('details > summary').click();
    await expect(firstCard.locator('[aria-label="Nested tool activity"] article')).toHaveCount(1);
    await firstCard.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath('ptc-live-first-cell.png'),
      fullPage: true,
      animations: 'disabled',
    });

    // Use the local E2E-only projection generation control, then the real sync
    // endpoint. No timeline frame is fulfilled or injected into the browser.
    const generationControl = await apiOk(
      page,
      '/api/v1/chat/v2/e2e/projection-generation',
      'POST',
    );
    const reset = await apiOk(
      page,
      `/api/v1/chat/v2/conversations/${resources.conversationId}/sync?cursor=${encodeURIComponent(initialSnapshot.cursor)}&limit=1`,
    );
    expect(reset.reset_required).toBe(true);
    const recovered = await snapshot(page, resources.conversationId);
    await assertRenderedQualification(page, recovered, {
      expectedParents: 1,
      expectedChildren: 1,
    });
    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('textbox', { name: 'Chat message' })).toBeVisible();
    const reloaded = await snapshot(page, resources.conversationId);
    await assertRenderedQualification(page, reloaded, {
      expectedParents: 1,
      expectedChildren: 1,
    });
    await expandPythonCards(page);

    const reloadedFirstCard = page.locator(
      `[data-kind="python_execution"][data-call-id="${first.parents[0].call_id}"]`,
    );
    await expect(reloadedFirstCard.locator('[aria-label="Nested tool activity"] article')).toHaveCount(1);

    await page.getByRole('textbox', { name: 'Chat message' }).fill(SECOND_MESSAGE);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await waitForAssistantText(
      page,
      resources.conversationId,
      "The second Python cell observed the first cell's persisted state.",
    );
    const finalSnapshot = await snapshot(page, resources.conversationId);
    const final = await assertRenderedQualification(page, finalSnapshot, {
      expectedParents: 2,
      expectedChildren: 1,
      firstGeneration,
    });
    const secondParent = final.parents[1];
    expect(secondParent.call_id).not.toBe(final.parents[0].call_id);
    expect(programResult(secondParent).result.persisted_sentinel).toBe(SENTINEL);
    await expandPythonCards(page);
    await page.screenshot({
      path: testInfo.outputPath('ptc-live-two-cells.png'),
      fullPage: true,
      animations: 'disabled',
    });

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('textbox', { name: 'Chat message' })).toBeVisible();
    const afterSecondReload = await snapshot(page, resources.conversationId);
    await assertRenderedQualification(page, afterSecondReload, {
      expectedParents: 2,
      expectedChildren: 1,
      firstGeneration,
    });
    await expandPythonCards(page);

    const history = await page.request.get(`${MOCK_LLM_URL}/__mock/history?limit=50`);
    expect(history.ok()).toBe(true);
    const modelHistory = (await history.json()).history;
    expect(
      modelHistory.filter(
        (item: any) => item.request.model === POLICY_MODEL && item.request.policy_model === true,
      ).length,
    ).toBeGreaterThanOrEqual(2);

    await writeFile(
      testInfo.outputPath('ptc-live-browser-websocket.jsonl'),
      wsFrames.map((frame) => `${frame}\n`).join(''),
    );
    await writeFile(
      testInfo.outputPath('ptc-live-terminal-snapshots.json'),
      JSON.stringify(
        {
          policy_model: POLICY_MODEL,
          guardrails_backend: 'intaris',
          generation_control: generationControl,
          terminal_reset: reset,
          first_snapshot: firstSnapshot,
          recovered_snapshot: recovered,
          final_snapshot: finalSnapshot,
          after_second_reload: afterSecondReload,
          policy_model_requests: modelHistory.filter(
            (item: any) => item.request.model === POLICY_MODEL,
          ).length,
        },
        null,
        2,
      ),
    );
  } finally {
    await clearScenario();
    if (resources) {
      for (const path of [
        `/api/v1/chat/v2/conversations/${resources.conversationId}`,
        `/api/v1/agents/${resources.agentId}`,
        `/api/v1/executors/${resources.executorId}`,
      ]) {
        await api(page, path, 'DELETE').catch(() => undefined);
      }
    }
    await writeFile(
      testInfo.outputPath('ptc-live-browser-websocket.jsonl'),
      wsFrames.map((frame) => `${frame}\n`).join(''),
    );
    await saveMockRequestMetadata(page, testInfo).catch(() => undefined);
    await context.tracing
      .stop({ path: testInfo.outputPath('ptc-live-trace.zip') })
      .catch(() => undefined);
  }
});
