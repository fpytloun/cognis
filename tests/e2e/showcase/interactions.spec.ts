import { expect, test, type APIResponse } from '../../../ui/node_modules/@playwright/test';
import { readFile, stat, writeFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { dirname, join } from 'node:path';
import { parseEnv, promisify } from 'node:util';

async function json(response: APIResponse) {
  expect(response.ok(), `${response.status()} ${await response.text()}`).toBeTruthy();
  return response.json();
}

for (const size of [
  { name: 'desktop', width: 1440, height: 1000, isMobile: false, hasTouch: false },
  { name: 'phone', width: 390, height: 844, isMobile: true, hasTouch: true },
]) {
  test(`${size.name} task interactions`, async ({ browser }, info) => {
    const manifest = process.env.SHOWCASE_MANIFEST;
    const privateEnv = process.env.SHOWCASE_PRIVATE_ENV;
    if (!manifest || !privateEnv) throw new Error('Showcase manifest and private env are required');
    expect((await stat(privateEnv)).mode & 0o777).toBe(0o600);
    const seed = JSON.parse(await readFile(manifest, 'utf8'));
    const credentials = parseEnv(await readFile(privateEnv, 'utf8'));
    const base = new URL(seed.base_url);
    expect(['localhost', '127.0.0.1', '[::1]']).toContain(base.hostname);
    const context = await browser.newContext({
      baseURL: base.href, viewport: size, isMobile: size.isMobile, hasTouch: size.hasTouch,
      reducedMotion: 'reduce', serviceWorkers: 'block',
    });
    await context.tracing.start({ screenshots: true, snapshots: true });
    const page = await context.newPage();
    try {
      await page.goto('/');
      await page.getByLabel('Email', { exact: true }).fill(seed.users.owner_email);
      await page.getByLabel('Password', { exact: true }).fill(credentials.SHOWCASE_OWNER_PASSWORD!);
      await page.getByRole('button', { name: 'Sign in', exact: true }).click();
      await expect(page.getByTestId('control-center')).toBeVisible();
      const source = seed.workspace.tasks['readiness-brief'];
      const workflow = await json(await page.request.post('/api/v1/workflows', {
        data: {
          name: `Task interaction audit ${size.name}`,
          steps: [
            { name: 'decision', type: 'gate', gate: { message: 'Approve the isolated audit?' } },
            { name: 'finish', type: 'complete', complete: { summary: 'Audit gate approved.' } },
          ],
        },
      }));
      const task = await json(await page.request.post('/api/v1/tasks', {
        data: {
          agent_id: source.agent_id, title: `Interaction audit ${size.name}`,
          description: 'Isolated deterministic interaction audit.',
          workflow_id: workflow.workflow_id, status: 'queued',
          interaction_mode_override: 'step_requests', delivery_mode: 'silent',
        },
      }));
      const detail = () => page.request.get(`/api/v1/tasks/${task.task_id}`).then(json);
      await expect.poll(async () => (await detail()).status).toBe('paused');
      await page.goto(`/tasks/${task.task_id}`);
      await expect(page.getByTestId('task-attention')).toBeVisible();
      await expect(page.getByText('Iris', { exact: true }).first()).toBeVisible();
      await expect(page.getByText(/Loading pending requests/)).toHaveCount(0);
      const pause = (await detail()).pending_pause;
      expect(pause.options.map((option: { action: string }) => option.action)).toEqual([
        'continue', 'cancel'
      ]);
      await expect(page.getByTestId('task-attention')
        .getByRole('button', { name: 'Cancel task', exact: true })).toBeVisible();
      await expect(page.getByTestId('task-attention')
        .getByRole('button', { name: 'Reject / revise', exact: true })).toHaveCount(0);
      await page.screenshot({ path: info.outputPath(`${size.name}-paused.png`), fullPage: true });
      const gateInstruction = page.getByPlaceholder('Add guidance for the next attempt.');
      await gateInstruction.fill(`Keep ${size.name} gate input`);
      await page.getByTestId('task-attention')
        .getByRole('button', { name: 'Cancel task', exact: true }).click();
      let confirm = page.getByRole('dialog', { name: 'Cancel task?' });
      await expect(confirm).toBeVisible();
      await page.evaluate(() => new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
      await page.screenshot({ path: info.outputPath(`${size.name}-cancel-confirmation.png`), fullPage: true });
      await confirm.getByRole('button', { name: 'Keep task', exact: true }).click();
      await expect(confirm).toHaveCount(0);
      const afterDismiss = await detail();
      expect(afterDismiss.status).toBe('paused');
      expect(afterDismiss.pending_pause.pause_id).toBe(pause.pause_id);
      await expect(gateInstruction).toHaveValue(`Keep ${size.name} gate input`);
      await gateInstruction.clear();
      await page.getByText('Collaboration, revisions, and notes', { exact: true }).click();
      await page.getByRole('radio', { name: 'Note', exact: true }).click();
      const body = page.getByRole('textbox', { name: 'Comment body', exact: true });
      await body.fill(`Record-only ${size.name} note`);
      await page.getByRole('button', { name: 'Add note', exact: true }).click();
      await expect.poll(async () => {
        const comments = await json(await page.request.get(`/api/v1/tasks/${task.task_id}/comments`));
        return comments.filter((item: { body: string }) => item.body === `Record-only ${size.name} note`).length;
      }).toBe(1);
      expect((await detail()).status).toBe('paused');
      await expect(page.locator('#task-comments-anchor')).toHaveJSProperty('open', true);
      await expect(page.getByText(`Record-only ${size.name} note`, { exact: true })).toBeVisible();
      await page.screenshot({ path: info.outputPath(`${size.name}-note-after.png`), fullPage: true });
      await page.getByRole('radio', { name: 'Guide next agent cycle', exact: true }).click();
      await body.fill(`Queued ${size.name} guidance`);
      await page.getByRole('button', { name: 'Guide next cycle', exact: true }).click();
      await expect.poll(async () => {
        const comments = await json(await page.request.get(`/api/v1/tasks/${task.task_id}/comments`));
        return comments.filter((item: { body: string }) => item.body === `Queued ${size.name} guidance`).length;
      }).toBe(1);
      expect((await detail()).status).toBe('paused');
      const firstChat = await json(await page.request.post(`/api/v1/tasks/${task.task_id}/control-chat`));
      const secondChat = await json(await page.request.post(`/api/v1/tasks/${task.task_id}/control-chat`));
      expect(secondChat.conversation_id).toBe(firstChat.conversation_id);
      await page.getByTestId('task-attention')
        .getByRole('button', { name: 'Approve & continue', exact: true }).click();
      await expect.poll(async () => (await detail()).status).toBe('completed');
      const completed = await detail();
      expect(completed.step_runs.filter((run: { step_name: string; status: string }) =>
        run.step_name === 'finish' && run.status === 'approved')).toHaveLength(1);
      await page.reload();
      await expect(page.getByTestId('task-status')).toContainText('Completed');
      await expect(page.getByTestId('task-attention')).toHaveCount(0);
      await page.screenshot({ path: info.outputPath(`${size.name}-completed.png`), fullPage: true });
      expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
      for (const attempt of [2, 3]) {
        const collaboration = page.locator('details').filter({
          has: page.getByText('Collaboration, revisions, and notes', { exact: true }),
        });
        if (attempt === 2) {
          await expect(collaboration).toHaveJSProperty('open', false);
          await page.getByRole('button', { name: 'Revise result', exact: true }).click();
          await expect(collaboration).toHaveJSProperty('open', true);
          await expect(collaboration.getByRole('radio', { name: 'Revise result', exact: true })).toBeChecked();
        } else {
          await page.getByText('Collaboration, revisions, and notes', { exact: true }).click();
          await collaboration.getByRole('radio', { name: 'Revise result', exact: true }).click();
        }
        await collaboration.getByRole('combobox', { name: /Target step/ }).selectOption('decision');
        await collaboration.getByRole('textbox', { name: 'Comment body', exact: true })
          .fill(`Round ${attempt} ${size.name}: reconsider the decision.`);
        await collaboration.getByRole('button', { name: 'Revise result', exact: true }).click();
        await expect.poll(async () => {
          const revised = await detail();
          return [revised.attempt_number, revised.status];
        }).toEqual([attempt, 'paused']);
        const revised = await detail();
        expect(revised.step_runs.filter((run: { attempt_number: number; status: string }) =>
          run.attempt_number < attempt && run.status !== 'superseded')).toHaveLength(0);
        await expect(page.getByTestId('task-attention')).toBeVisible();
        if (attempt === 2) {
          await page.screenshot({ path: info.outputPath(`${size.name}-revised.png`), fullPage: true });
        }
        await page.getByTestId('task-attention')
          .getByRole('button', { name: 'Approve & continue', exact: true }).click();
        await expect.poll(async () => (await detail()).status).toBe('completed');
        await page.reload();
      }
      await page.getByTestId('task-agent-dock-launcher').click();
      const chat = page.getByTestId('task-control-native-chat');
      const message = `Inspect the completed ${size.name} audit task without changing it.`;
      await chat.getByRole('textbox', { name: 'Task control message', exact: true }).fill(message);
      await chat.getByRole('button', { name: 'Send task control message', exact: true }).click();
      await expect(chat.getByText('Mock response — no scenario matched.', { exact: true })).toBeVisible();
      await expect(chat.getByText(message, { exact: true })).toHaveCount(1);
      expect((await detail()).status).toBe('completed');
      expect((await detail()).attempt_number).toBe(3);
      await page.screenshot({ path: info.outputPath(`${size.name}-control-chat.png`), fullPage: true });
      const cancelTask = await json(await page.request.post('/api/v1/tasks', {
        data: {
          agent_id: source.agent_id, title: `Cancellation audit ${size.name}`,
          description: 'Isolated cancellation confirmation audit.',
          workflow_id: workflow.workflow_id, status: 'queued',
          interaction_mode_override: 'step_requests', delivery_mode: 'silent',
        },
      }));
      const cancellationDetail = () =>
        page.request.get(`/api/v1/tasks/${cancelTask.task_id}`).then(json);
      await expect.poll(async () => (await cancellationDetail()).status).toBe('paused');
      await page.goto(`/tasks/${cancelTask.task_id}`);
      await page.getByTestId('task-attention')
        .getByRole('button', { name: 'Cancel task', exact: true }).click();
      confirm = page.getByRole('dialog', { name: 'Cancel task?' });
      await expect(confirm).toBeVisible();
      await confirm.getByRole('button', { name: 'Cancel task', exact: true }).click();
      await expect.poll(async () => (await cancellationDetail()).status).toBe('cancelled');

      const runtime = JSON.parse(await readFile(join(dirname(manifest), 'runtime.json'), 'utf8'));
      const execute = promisify(execFile);
      const { stdout } = await execute('docker', [
        'ps', '-q', '--filter', `label=com.docker.compose.project=${runtime.project}`,
        '--filter', 'label=com.docker.compose.service=mock-llm',
      ]);
      const containers = stdout.trim().split('\n').filter(Boolean);
      expect(containers).toHaveLength(1);
      const work = `qa-native-evaluator-work-${size.name}-${Date.now()}`;
      const review = `qa-native-evaluator-review-${size.name}-${Date.now()}`;
      const turns = (tag: string, title: string) => [
        { role: 'assistant', steps: [{
          type: 'tool_call', call_id: `call_${tag}_write`, name: 'write_deliverable',
          arguments: { title, format: 'markdown', content: `# ${title}\n\nNative retry output.` },
        }] },
        { role: 'assistant', steps: [{
          type: 'tool_call', call_id: `call_${tag}_finish`, name: 'step_complete',
          arguments: { summary: title },
        }] },
      ];
      const verdict = (decision: string) => [{ role: 'assistant', steps: [{
        type: 'text', chunks: [JSON.stringify({
          decision, reasoning: 'Check the canonical revised output.',
          feedback: decision === 'revise' ? 'Produce the corrected output.' : '',
        })],
      }] }];
      const mock = `http://127.0.0.1:${seed.runtime.ports.MOCK_LLM_PORT}`;
      const control = async (method: string, data: object | null = null) => {
        await execute('docker', ['exec', containers[0], 'python', '-c', [
          'import json,sys,urllib.request',
          'body=json.loads(sys.argv[2])',
          'request=urllib.request.Request("http://127.0.0.1:8090/__mock/barrier",',
          ' data=None if body is None else json.dumps(body).encode(),',
          ' method=sys.argv[1],headers={"Content-Type":"application/json"})',
          'with urllib.request.urlopen(request,timeout=35) as response: print(response.status)',
        ].join('\n'), method, JSON.stringify(data)]);
      };
      await json(await page.request.post(`${mock}/__mock/scenario`, {
        data: { id: work, trigger: work, turns: turns(work, 'Draft requiring revision') },
      }));
      await json(await page.request.post(`${mock}/__mock/scenario`, {
        data: { id: review, trigger: review, turns: verdict('revise') },
      }));
      await control('POST', { scenario_id: review, turn_index: 0 });
      let barrierArmed = true;
      try {
        const evaluatedWorkflow = await json(await page.request.post('/api/v1/workflows', {
          data: { name: `Native evaluator retry ${size.name}`, steps: [{
            name: 'work', type: 'run', prompt: work,
            completion: {
              evaluate: true, evaluator_prompt: review, max_attempts: 3, on_exhausted: 'fail',
            },
          }] },
        }));
        const evaluatedTask = await json(await page.request.post('/api/v1/tasks', {
          data: {
            agent_id: source.agent_id, title: `Native evaluator retry ${size.name}`,
            description: work, workflow_id: evaluatedWorkflow.workflow_id,
            project_id: seed.workspace.projects.autumn, status: 'queued',
            interaction_mode_override: 'step_requests', delivery_mode: 'silent',
          },
        }));
        const evaluatedDetail = () =>
          page.request.get(`/api/v1/tasks/${evaluatedTask.task_id}`).then(json);
        await control('GET');
        await json(await page.request.post(`${mock}/__mock/scenario`, {
          data: { id: review, trigger: review, turns: verdict('approved') },
        }));
        await json(await page.request.post(`${mock}/__mock/scenario`, {
          data: {
            id: work, trigger: work, turns: [
              ...turns(`${work}_corrected`, 'Corrected evaluator draft'),
              ...turns(`${work}_retry`, 'Corrected evaluator draft'),
            ],
          },
        }));
        await control('PUT');
        await control('DELETE');
        barrierArmed = false;
        await expect.poll(async () => (await evaluatedDetail()).status, {
          timeout: 120_000,
        }).toBe('completed');
        const evaluated = await evaluatedDetail();
        expect(evaluated.step_runs).toHaveLength(1);
        expect(evaluated.step_runs[0].attempt).toBe(2);
        expect(evaluated.step_runs[0].evaluation.decision).toBe('approved');
        expect(evaluated.step_runs[0].deliverables.find(
          (item: { title: string }) => item.title === 'Draft requiring revision',
        ).status).toBe('rejected');
        expect(evaluated.final_deliverable.title).toBe('Corrected evaluator draft');
        await page.goto(`/tasks/${evaluatedTask.task_id}`);
        await expect(page.getByTestId('task-status')).toContainText('Completed');
      } finally {
        if (barrierArmed) await control('DELETE');
      }
    } catch (error) {
      await page.screenshot({ path: info.outputPath(`${size.name}-failure.png`), fullPage: true });
      await writeFile(info.outputPath(`${size.name}-failure.html`), await page.content());
      throw error;
    } finally {
      await context.tracing.stop({ path: info.outputPath(`${size.name}-trace.zip`) });
      await context.close();
    }
  });
}
