// Film scene capture: the real Cognis UI on an isolated Lumen showcase runtime, 1280x720 CSS
// at DPR 1.5 (1920x1080 frames), CDP screencast, eased cursor composited afterwards at 30 fps.
// One MP4 per scene. Needs ffmpeg on PATH and `npm --prefix ui ci`.
// Usage: RUNTIME=<runtime dir> [FILM_OUT=<dir>] node tests/e2e/showcase/film/capture.mjs <scene...>
// Scenes: dashboard pinned feedback durable library work usage models project
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseEnv } from 'node:util';

const here = dirname(fileURLToPath(import.meta.url));
const { chromium } = await import(pathToFileURL(resolve(here, '../../../../ui/node_modules/playwright/index.mjs')).href);
const runtime = process.env.RUNTIME;
if (!runtime) throw new Error('set RUNTIME to the showcase runtime directory');
const info = JSON.parse(readFileSync(`${runtime}/runtime.json`, 'utf8'));
const seed = JSON.parse(readFileSync(`${runtime}/manifest.json`, 'utf8'));
const env = parseEnv(readFileSync(`${runtime}/private.env`, 'utf8'));
const base = info.base_url;
const mock = `http://127.0.0.1:${seed.runtime.ports.MOCK_LLM_PORT}`;
const out = resolve(process.env.FILM_OUT || resolve(here, 'output'));
mkdirSync(out, { recursive: true });
const W = 1280, H = 720, SCALE = 1.5;

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: SCALE,
  colorScheme: 'light', serviceWorkers: 'block' });
// The cursor is not drawn in the page. Heavy views (dashboard windows) screencast at ~9 fps,
// which made an in-page cursor jump. Instead pointer moves are logged as ideal eased segments
// and composited afterwards at a steady 30 fps (see record()).
const CURSOR_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><path d="M4 2l15 11-6.5 1.2L16 21l-3 1.4-3.4-6.9L4 20z" fill="#111" stroke="#fff" stroke-width="1.5"/></svg>';
async function renderCursorImages() {
  const p = await context.newPage();
  for (const [file, scale] of [['cursor.png', 1], ['cursor-down.png', 0.82]]) {
    await p.setContent(`<body style="margin:0;background:transparent"><div id="c" style="width:24px;height:24px;transform:scale(${scale});transform-origin:4px 2px">${CURSOR_SVG}</div></body>`);
    await p.locator('#c').screenshot({ path: resolve(out, file), omitBackground: true });
  }
  await p.close();
}
const page = await context.newPage();
await renderCursorImages();
const sleep = ms => page.waitForTimeout(ms);
const pointer = { x: W - 40, y: H - 40 };
const SPEED = 0.6; // pointer travel time multiplier (Filip: cursor was too slow)
let track = null; // { moves: [{t0,t1,x0,y0,x1,y1}], presses: [{t0,t1}] } while recording
const ease = t => t < .5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
async function moveTo(x, y, duration = 420) {
  const from = { ...pointer };
  const ms = Math.max(120, duration * SPEED);
  const t0 = Date.now() / 1000;
  track?.moves.push({ t0, t1: t0 + ms / 1000, x0: from.x, y0: from.y, x1: x, y1: y });
  const steps = Math.max(6, Math.round(ms / 16));
  for (let i = 1; i <= steps; i++) {
    const e = ease(i / steps);
    await page.mouse.move(from.x + (x - from.x) * e, from.y + (y - from.y) * e);
    await sleep(16);
  }
  pointer.x = x; pointer.y = y;
}
async function glide(locator, pause = 220, duration) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error('target not visible');
  await moveTo(box.x + box.width / 2, box.y + box.height / 2, duration);
  await sleep(pause * SPEED + 60);
}
async function press(locator, pause) {
  await glide(locator, pause);
  const t0 = Date.now() / 1000;
  track?.presses.push({ t0, t1: t0 + 0.14 });
  await page.mouse.down(); await sleep(90); await page.mouse.up();
}

function cursorCommands(start, end) {
  // Position in CSS px at wall time t; hidden before the first logged move.
  const at = t => {
    let pos = null;
    for (const m of track.moves) {
      if (t < m.t0) break;
      const k = m.t1 > m.t0 ? Math.min(1, (t - m.t0) / (m.t1 - m.t0)) : 1;
      pos = { x: m.x0 + (m.x1 - m.x0) * ease(k), y: m.y0 + (m.y1 - m.y0) * ease(k) };
    }
    return pos;
  };
  const lines = [];
  for (let f = 0; f * (1 / 30) <= end - start + 0.5; f++) {
    const time = f / 30, wall = start + time, pos = at(wall);
    const down = track.presses.some(p => wall >= p.t0 && wall < p.t1);
    // Hotspot is the arrow tip at (4,2) of the 24 px glyph.
    const x = pos ? Math.round(pos.x * SCALE - 4 * SCALE) : -200, y = pos ? Math.round(pos.y * SCALE - 2 * SCALE) : -200;
    lines.push(`${time.toFixed(4)} overlay@up x ${down ? -200 : x}, overlay@up y ${down ? -200 : y}, overlay@down x ${down ? x : -200}, overlay@down y ${down ? y : -200};`);
  }
  return lines.join('\n');
}

async function record(name, flow) {
  const frames = resolve(out, `frames-${name}`);
  rmSync(frames, { recursive: true, force: true });
  mkdirSync(frames, { recursive: true });
  await page.mouse.move(pointer.x, pointer.y);
  const session = await context.newCDPSession(page);
  const shots = [], writes = [];
  session.on('Page.screencastFrame', ({ data, metadata, sessionId }) => {
    const file = resolve(frames, `${String(shots.length).padStart(5, '0')}.jpg`);
    shots.push({ file, t: metadata.timestamp });
    writes.push(Promise.resolve().then(() => writeFileSync(file, Buffer.from(data, 'base64'))));
    session.send('Page.screencastFrameAck', { sessionId }).catch(() => {});
  });
  track = { moves: [], presses: [] };
  await session.send('Page.startScreencast', { format: 'jpeg', quality: 95, maxWidth: W * SCALE, maxHeight: H * SCALE });
  try {
    await sleep(600);
    await flow();
    await sleep(1200);
  } finally {
    await session.send('Page.stopScreencast');
    await session.detach();
  }
  await Promise.all(writes);
  const list = shots.map((s, i) => `file '${s.file}'\nduration ${(shots[i + 1] ? Math.max(.005, shots[i + 1].t - s.t) : .5).toFixed(4)}`)
    .join('\n') + `\nfile '${shots.at(-1).file}'\n`;
  writeFileSync(resolve(frames, 'list.txt'), list);
  writeFileSync(resolve(frames, 'cursor.cmd'), cursorCommands(shots[0].t, shots.at(-1).t));
  writeFileSync(resolve(frames, 'track.json'), JSON.stringify(track));
  track = null;
  const graph = '[0:v]fps=30,scale=1920:1080:flags=lanczos,sendcmd=f=' + resolve(frames, 'cursor.cmd').replace(/:/g, '\\:') + '[b];' +
    '[b][1:v]overlay@up=x=-200:y=-200:eval=frame:shortest=1[c];[c][2:v]overlay@down=x=-200:y=-200:eval=frame:shortest=1,' +
    'scale=in_range=full:out_range=tv,format=yuv420p[v]';
  execFileSync('ffmpeg', ['-nostdin', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', resolve(frames, 'list.txt'),
    '-loop', '1', '-i', resolve(out, 'cursor.png'), '-loop', '1', '-i', resolve(out, 'cursor-down.png'),
    '-filter_complex', graph, '-map', '[v]',
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '14', '-colorspace', 'bt709', '-color_primaries', 'bt709',
    '-color_trc', 'bt709', '-color_range', 'tv', '-movflags', '+faststart', resolve(out, `${name}.mp4`)]);
  console.log('recorded', name, shots.length, 'frames');
}

// Sign in and apply the documentation appearance.
await page.goto(base + '/');
await page.getByLabel('Email', { exact: true }).fill(seed.users.owner_email);
await page.getByLabel('Password', { exact: true }).fill(env.SHOWCASE_OWNER_PASSWORD);
await page.getByRole('button', { name: 'Sign in', exact: true }).click();
await page.getByTestId('control-center').waitFor();
await page.goto(base + '/settings?tab=display');
const main = page.getByRole('main');
await main.getByRole('combobox', { name: /^Family\b/ }).selectOption('porcelain');
await main.getByRole('combobox', { name: /^Appearance\b/ }).selectOption('light');
const windows = page.getByRole('checkbox', { name: /Dashboard workspace windows/ });
if (!(await windows.isChecked())) await windows.check();
await sleep(1200);

const carts = seed.workspace.conversations['saved-carts'].conversation_id;
const doc = seed.resources.feedback_revised_deliverable_id;
async function dashboard() {
  await page.goto(base + '/');
  await page.getByTestId('control-center').waitFor();
  const projects = page.getByRole('tab', { name: /^Projects/ }).first();
  if (await projects.count() && await projects.getAttribute('aria-selected') !== 'true') await projects.click();
  await page.getByText('Launch readiness brief', { exact: true }).first().waitFor();
  await sleep(1500);
}

const scenes = {
  async dashboard() {
    await dashboard();
    await record('dashboard', async () => {
      await sleep(2200);
      await press(page.getByTestId(`dashboard-conversation-row-${carts}`).first().getByText('Checkout · saved carts'));
      await page.getByTestId(`workspace-window-conversation:${carts}`).getByText('Carts now follow the account', { exact: false }).waitFor();
      await sleep(1800);
      await page.keyboard.press('Control+k');
      await sleep(350);
      await page.getByRole('combobox', { name: 'Search Cognis' }).pressSequentially('launch overview', { delay: 55 });
      const option = page.getByRole('dialog', { name: 'Command palette' }).getByRole('option').filter({ hasText: seed.resources.rich_title }).first();
      await option.waitFor();
      await sleep(500);
      await press(option);
      await page.getByTestId(`workspace-window-deliverable:${doc}`).getByText('7,200', { exact: true }).waitFor();
      await sleep(2200);
      await press(page.getByTestId(`workspace-dock-item-conversation:${carts}`).getByRole('button').first());
      await sleep(1300);
      await press(page.getByTestId(`workspace-dock-item-deliverable:${doc}`).getByRole('button').first());
      await sleep(1600);
    });
  },
  async pinned() {
    for (const key of ['saved-carts', 'assistant-research', 'staging-health']) {
      await page.goto(`${base}/chat/${seed.workspace.conversations[key].conversation_id}`);
      await page.locator('[data-kind="message"]').first().waitFor({ timeout: 60000 });
      const pin = page.getByRole('button', { name: 'Pin conversation', exact: true });
      if (await pin.count()) await pin.click();
      await page.getByRole('button', { name: 'Unpin conversation', exact: true }).waitFor();
    }
    await page.goto(base + '/library');
    await page.getByText('Launch readiness brief', { exact: true }).first().waitFor();
    await sleep(1500);
    const launcher = page.getByTestId('pinned-chat-launcher');
    const iris = page.getByRole('button', { name: 'Open pinned chats with Iris', exact: true });
    const forge = page.getByRole('button', { name: 'Open pinned chats with Forge', exact: true });
    const panel = page.getByTestId('pinned-chat-panel');
    const tab = title => panel.getByRole('tab').filter({ hasText: title });
    await record('pinned', async () => {
      await sleep(1200);
      await press(launcher);
      await iris.waitFor();
      await sleep(500);
      await press(iris);
      await panel.waitFor();
      await press(tab('Brief: AI shopping assistants'));
      await panel.getByText('Brief: AI shopping assistants in home lighting', { exact: false }).waitFor();
      await sleep(1800);
      await press(forge);
      await press(tab('Checkout · saved carts'));
      await panel.getByText('Carts now follow the account', { exact: false }).waitFor();
      await sleep(1800);
    });
  },
  async feedback() {
    await page.goto(seed.routes.feedback);
    await page.locator('[data-kind="message"]').first().waitFor({ timeout: 90000 });
    const value = page.locator('.rich-metric-value').filter({ hasText: '7,200' }).last();
    await sleep(3000);
    await value.scrollIntoViewIfNeeded();
    await page.mouse.wheel(0, 140);
    await sleep(1000);
    const conversation = new URL(seed.routes.feedback).pathname.split('/').pop();
    const snapshot = await (await page.request.get(`${base}/api/v1/chat/v2/conversations/${conversation}/snapshot`)).json();
    const nextTurn = snapshot.timeline.items.filter(item => item.kind === 'tool_call' ||
      (item.kind === 'message' && item.role === 'assistant')).length;
    const commentText = 'Can we reach 9,000 if the partner newsletter goes out first?';
    const reply = 'Please fold this into the plan.';
    await record('feedback', async () => {
      await sleep(1000);
      // Selecting text in the result offers "Add comment" for that exact span.
      await value.scrollIntoViewIfNeeded();
      const box = await value.boundingBox();
      await moveTo(box.x + 1, box.y + box.height / 2);
      await sleep(250);
      await page.mouse.down();
      await moveTo(box.x + box.width - 1, box.y + box.height / 2, 320);
      await page.mouse.up();
      await press(page.getByRole('button', { name: 'Add comment', exact: true }));
      await page.getByRole('textbox', { name: 'Comment', exact: true }).pressSequentially(commentText, { delay: 24 });
      await sleep(400);
      const saved = page.waitForResponse(r => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/v1/feedback/threads');
      await press(page.getByRole('region', { name: 'Add comment', exact: true }).getByRole('button', { name: /^(Send|Save comment)$/ }));
      const threadId = (await (await saved).json()).thread_id;
      const step = (name, args, i) => ({ role: 'assistant', steps: [{ type: 'tool_call', call_id: `call_film_feedback_${i}`, name, arguments: args, delay_ms: 300 }] });
      const registered = await fetch(`${mock}/__mock/scenario`, { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ id: 'film-feedback-reply-v1', trigger: `${reply} [film]`, description: 'Local mock reply to inline feedback.',
          turns: [
            ...Array.from({ length: nextTurn }, () => ({ role: 'assistant', steps: [{ type: 'text', chunks: ['Unused.'] }] })),
            step('search_tools', { query: 'comment_reply', limit: 5 }, 0),
            step('call_tool', { tool: 'builtin:comment_reply', arguments: { comment_id: threadId,
              reply: 'About 9,000 if the newsletter goes out first. I will add it to the next revision.' } }, 1),
            { role: 'assistant', steps: [{ type: 'text', delay_ms: 120, chunks: [
              'Good call. With the partner newsletter going out first, expected reach rises to about 9,000. ',
              'I answered on your comment and will carry it into the next revision of the plan.'] }] },
          ] }) });
      if (!registered.ok) throw new Error('cannot register reply');
      const tray = page.getByRole('region', { name: 'Message feedback', exact: true });
      await tray.getByText(/ready to include/).waitFor();
      await sleep(400);
      await press(tray.getByRole('button', { name: 'Review comments', exact: true }));
      const includeAll = tray.getByRole('button', { name: 'Include all', exact: true });
      await tray.getByRole('checkbox').first().waitFor();
      await sleep(300);
      await press(includeAll);
      await tray.getByText(/comments? included/).first().waitFor();
      await sleep(500);
      await press(tray.getByRole('button', { name: 'Close review', exact: true }));
      const composer = page.locator('textarea[placeholder*="Message"]').first();
      await press(composer);
      await composer.pressSequentially(reply, { delay: 32 });
      const active = await fetch(`${mock}/__mock/active`, { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ id: 'film-feedback-reply-v1' }) });
      if (!active.ok) throw new Error('cannot pin reply');
      try {
        await page.keyboard.press('Enter');
        await page.getByText('I answered on your comment', { exact: false }).last().waitFor({ timeout: 120000 });
      } finally {
        await fetch(`${mock}/__mock/active`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ id: null }) });
      }
      await sleep(2400);
    });
  },
  async usage() {
    const end = new Date(`${seed.usage.end}T12:00:00Z`); end.setUTCDate(end.getUTCDate() - 1);
    await page.goto(`${base}/usage?usage_start=${seed.usage.start}&usage_end=${end.toISOString().slice(0, 10)}`);
    await page.getByRole('region', { name: 'Usage summary' }).getByText('All priced', { exact: true }).waitFor();
    const bars = page.getByRole('region', { name: 'Usage over time' }).getByRole('button');
    const labels = await bars.evaluateAll(nodes => nodes.map(n => n.getAttribute('aria-label') ?? ''));
    const amounts = labels.map(l => Number(l.match(/\$([\d.]+)/)?.[1] ?? 0));
    const peak = amounts.indexOf(Math.max(...amounts));
    await sleep(1500);
    await record('usage', async () => {
      await sleep(1600);
      await glide(bars.nth(peak), 700, 600);
      await press(bars.nth(peak));
      await sleep(1800);
      await press(page.getByRole('tab', { name: 'Costs & tokens', exact: true }));
      const flow = page.getByRole('region', { name: 'Cost flow between levels' });
      await flow.waitFor({ timeout: 20000 });
      const select = flow.getByRole('combobox').first();
      await glide(select, 400);
      await select.selectOption('project-agent-conversation');
      await sleep(700);
      // Bring the whole Sankey into view with a smooth scroll before pointing at the heavy chat.
      await flow.evaluate(element => element.scrollIntoView({ block: 'start', behavior: 'smooth' }));
      await sleep(1600);
      const heavy = flow.getByRole('img', { name: /saved carts/ }).first();
      if (await heavy.count()) await glide(heavy, 1800);
      await sleep(800);
    });
  },
  async work() {
    await page.goto(`${base}/chat/${carts}`);
    await page.locator('[data-kind="message"]').first().waitFor({ timeout: 60000 });
    await sleep(1500);
    await record('work', async () => {
      await sleep(1000);
      const info = page.getByTestId('chat-header-info');
      if (await info.count()) await press(info);
      const workTab = page.getByRole('tab', { name: 'Work', exact: true });
      await workTab.waitFor();
      await press(workTab);
      const expand = page.getByRole('button', { name: 'Expand inspector', exact: true });
      if (await expand.count()) await press(expand);
      await sleep(400);
      const file = page.getByTestId('work-files-tree').getByText('cart.py', { exact: true });
      await file.waitFor();
      await sleep(600);
      await press(file);
      await page.getByTestId('work-diff-pane').getByText('def load(self, session)', { exact: false }).waitFor();
      await sleep(2600);
    });
  },
  async durable() {
    // Demo workflow with a deterministic tool step; created on the isolated runtime before recording.
    const body = readFileSync(resolve(here, 'staging-watch-workflow.json'), 'utf8');
    const existing = await page.request.get(`${base}/api/v1/workflows/lumen-staging-watch`);
    if (!existing.ok()) {
      const created = await page.request.post(`${base}/api/v1/workflows`, { data: JSON.parse(body) });
      if (!created.ok()) throw new Error(`cannot create workflow: ${created.status()}`);
    }
    // Origin/main task views: schedule detail -> its last (mock-completed) run -> task cockpit -> result.
    await page.goto(base + '/schedules');
    await page.getByRole('heading', { name: 'Nightly staging check', level: 3 }).waitFor();
    await sleep(1500);
    await record('durable', async () => {
      await sleep(900);
      await press(page.getByRole('heading', { name: 'Friday launch digest', level: 3 }));
      await page.getByRole('heading', { name: 'Run history', level: 2 }).waitFor();
      await sleep(1400);
      const lastRun = page.getByRole('button', { name: /^Friday launch digest .* Completed$/ }).first();
      await lastRun.evaluate(element => element.scrollIntoView({ block: 'center', behavior: 'smooth' }));
      await sleep(900);
      await press(lastRun);
      await page.getByText('Task Cockpit', { exact: true }).waitFor();
      await sleep(1800);
      await press(page.getByRole('main').getByRole('button', { name: 'Result', exact: true }).first());
      await sleep(2600);
      if (process.env.FILM_DURABLE_WORKFLOW !== '1') return;
      await page.keyboard.press('Escape');
      await press(page.getByRole('link', { name: 'Workflows' }).first());
      await page.getByRole('heading', { name: 'Workflows', level: 1 }).waitFor();
      await sleep(600);
      await press(page.getByRole('button', { name: /^Staging watch user/ }));
      const canvas = page.getByRole('list', { name: 'Staging watch steps' });
      await canvas.waitFor();
      await canvas.evaluate(element => element.scrollIntoView({ block: 'center', behavior: 'smooth' }));
      await sleep(1400);
      for (const step of ['check_staging', 'any_failures', 'investigate']) {
        await glide(canvas.getByText(step, { exact: true }).first(), 700);
      }
      await press(canvas.getByText('check_staging', { exact: true }).first());
      await sleep(2200);
    });
  },
  async library() {
    await page.goto(base + '/library');
    await page.getByText('Launch readiness brief', { exact: true }).first().waitFor();
    await sleep(1500);
    await record('library', async () => {
      await sleep(1600);
      const search = page.getByRole('searchbox', { name: 'Search titles and filenames' });
      await press(search);
      await search.pressSequentially('launch', { delay: 90 });
      await page.getByText('Weekly status report', { exact: true }).waitFor({ state: 'hidden' });
      await sleep(1400);
      await press(page.getByRole('link', { name: /^Launch readiness brief/ }).first());
      await page.getByText('Courier capacity for the first weekend', { exact: false }).first().waitFor({ timeout: 20000 });
      await sleep(2800);
    });
  },
  async models() {
    await page.goto(base + '/local-models');
    await page.getByRole('heading', { name: 'Local Models', exact: true }).waitFor();
    await sleep(1800);
    await record('models', async () => {
      await sleep(1800);
      const search = page.getByRole('textbox', { name: 'Search local model catalog' });
      await press(search);
      await search.pressSequentially('qwen', { delay: 90 });
      await press(page.getByRole('button', { name: 'Search', exact: true }));
      await sleep(900);
      await page.locator('main').evaluate(() => {
        const card = [...document.querySelectorAll('main *')].find(e => /^Qwen/.test(e.textContent ?? '') && e.children.length === 0);
        card?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      });
      await sleep(1400);
      await glide(page.getByRole('button', { name: 'Plan deployment' }).first(), 2200);
    });
  },
  async project() {
    await page.goto(seed.routes.project);
    await page.getByTestId('project-role').waitFor();
    await sleep(2000);
    await record('project', async () => {
      await sleep(2500);
      await moveTo(W / 2, H * .6, 900);
      await page.mouse.wheel(0, 260);
      await sleep(2000);
    });
  },
};

for (const name of process.argv.slice(2)) {
  try { await scenes[name](); }
  catch (error) { console.log('FAILED', name, String(error).slice(0, 500)); await page.screenshot({ path: resolve(out, `failed-${name}.png`) }); }
}
await browser.close();
