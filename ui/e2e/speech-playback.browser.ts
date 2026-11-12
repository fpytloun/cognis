import { expect, test, type Locator, type Route } from '@playwright/test';
import { Buffer } from 'node:buffer';
import { speechRuntime } from '../src/lib/utils/conversation-speech.test-fixtures';
import type { TimelineItem, ToolCallTimelineItem } from '../src/lib/chat-v2/types';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email: 'speech-browser@cognis.test', role: 'user', name: 'Speech browser' },
  }));
  page.on('pageerror', error => console.error('Speech fixture runtime:', error.name, error.message));
  page.on('console', message => {
    if (message.text().startsWith('Speech fixture toast:')) console.error(message.text());
  });
});

function silence(seconds: number): Buffer {
  const rate = 8000;
  const size = seconds * rate * 2;
  const result = Buffer.alloc(44 + size);
  result.write('RIFF', 0); result.writeUInt32LE(36 + size, 4);
  result.write('WAVEfmt ', 8); result.writeUInt32LE(16, 16);
  result.writeUInt16LE(1, 20); result.writeUInt16LE(1, 22);
  result.writeUInt32LE(rate, 24); result.writeUInt32LE(rate * 2, 28);
  result.writeUInt16LE(2, 32); result.writeUInt16LE(16, 34);
  result.write('data', 36); result.writeUInt32LE(size, 40);
  return result;
}

for (const width of [1200, 390]) {
  test.describe(`${width}px audio diagnostics`, () => {
    test.use({ hasTouch: width === 390, isMobile: width === 390 });
    for (const theme of ['light', 'dark']) {
      test(`safe failures and subscription credits in ${theme}`, async ({ page }, testInfo) => {
        const unexpected: string[] = [];
        const runtimeErrors: string[] = [];
        page.on('pageerror', error => runtimeErrors.push(error.message));
        await page.setViewportSize({ width, height: 900 });
        await page.route(url => url.pathname.startsWith('/api/'), async route => {
          const path = new URL(route.request().url()).pathname;
          if (path === '/api/auth/me') return route.fallback();
          if (path === '/api/v1/audio/jobs/source' && route.request().method() === 'GET') {
            return route.fulfill({ json: {
              supported: true, can_generate: true, can_cancel: false, detail: null,
              jobs: [{
                job_id: 'audio-browser-failed', deliverable_id: 'dlv_11111111111111111111111111111111',
                mode: 'narration', status: 'failed', error_code: 'provider_quota_exhausted',
                completed_parts: 0, total_parts: 2, parts: [], script_artifact_id: null,
                script_url: null, created_at: '2026-01-01T00:00:00Z'
              }]
            } });
          }
          unexpected.push(`${route.request().method()} ${path}`);
          return route.fulfill({ status: 500, json: { detail: 'Unexpected fixture request' } });
        });
        await page.goto('/speech-playback-browser-fixture?audio-diagnostics');
        await expect(page.getByRole('progressbar', { name: 'Subscription credits usage' })).toBeVisible();
        await page.evaluate(value => {
          document.documentElement.dataset.themeFamily = 'paper';
          document.documentElement.dataset.resolvedTheme = value;
        }, theme);
        await expect(page.getByText('8,464 / 10,000 left')).toBeVisible();
        await page.getByRole('button', { name: 'Listen to this document' }).click();
        await expect(page.getByRole('alert')).toContainText('insufficient quota or credits');
        await expect(page.getByRole('alert')).toContainText('No automatic retries.');
        await expect(page.getByRole('button', { name: 'Generate narration', exact: true })).toBeVisible();
        const panel = await page.getByRole('dialog').boundingBox();
        expect(panel!.x).toBeGreaterThanOrEqual(0);
        expect(panel!.x + panel!.width).toBeLessThanOrEqual(width);
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
        expect(unexpected).toEqual([]);
        expect(runtimeErrors).toEqual([]);
        await page.screenshot({ path: testInfo.outputPath(`audio-diagnostics-${width}-${theme}.png`) });
      });
    }
  });
}

function speechResponse(streamed = false) {
  const part = { artifact_id: 'speech-browser-audio',
    audio_url: streamed ? '/native-finished.wav' : '/speech-fixture.wav', content_type: 'audio/wav' };
  return {
    ...part, parts: [part], voice: 'alloy', model: 'gpt-4o-mini-tts',
    cached: false, adapted: false, script_artifact_id: null, warnings: [], duration_seconds: null,
  };
}

function speechStream(seconds = 0) {
  const response = speechResponse(seconds > 0);
  const part = response.parts[0];
  const events: unknown[] = [];
  if (seconds) {
    const samples = Math.round(seconds * 24000);
    events.push({ type: 'audio_start', part_index: 0, sample_rate: 24000 });
    for (let offset = 0; offset < samples; offset += 16384) {
      const size = Math.min(16384, samples - offset);
      events.push({ type: 'audio', part_index: 0, sample_rate: 24000,
        sample_offset: offset, audio_base64: Buffer.alloc(size * 2).toString('base64') });
    }
    events.push({ type: 'audio_done', part_index: 0, sample_offset: samples });
  }
  events.push({ type: 'part', part_index: 0, part, streamed: seconds > 0 }, { type: 'complete', response });
  return { contentType: 'application/x-ndjson', body: events.map(event => JSON.stringify(event) + '\n').join('') };
}

async function expectCompactMessagePlaybackFace(message: Locator, playback: Locator) {
  const copy = message.getByRole('button', { name: 'Copy raw markdown', exact: true });
  const copyBox = await copy.boundingBox();
  const face = await playback.boundingBox();
  expect(face!.width).toBe(copyBox!.width);
  expect(face!.height).toBe(copyBox!.height);
  const icon = await playback.locator('svg').boundingBox();
  const copyIcon = await copy.locator('svg').boundingBox();
  expect(icon!.width).toBe(copyIcon!.width);
  expect(icon!.height).toBe(copyIcon!.height);
}

async function expectBorderlessPlaybackControl(control: Locator) {
  const borders = await control.evaluate(element => {
    const style = getComputedStyle(element);
    return [style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth];
  });
  expect(borders).toEqual(['0px', '0px', '0px', '0px']);
}

async function expectPlainPlaybackSpeed(control: Locator) {
  await expectBorderlessPlaybackControl(control);
  const appearance = await control.evaluate(element => {
    const style = getComputedStyle(element);
    return {
      background: style.backgroundColor,
      shadow: style.boxShadow,
      keyboardFocus: element.matches(':focus-visible'),
    };
  });
  expect(appearance.background).toBe('rgba(0, 0, 0, 0)');
  if (appearance.keyboardFocus) expect(appearance.shadow).not.toBe('none');
  else expect(appearance.shadow).toBe('none');
}

for (const width of [1200, 390]) {
  test.describe(`${width}px playback`, () => {
  test.use({ hasTouch: width === 390, isMobile: width === 390 });
  for (const theme of ['light', 'dark'] as const) {
  test(`message transport controls stay grouped and change speed at ${width}px in ${theme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route(url => url.pathname.startsWith('/api/'), route => route.fulfill({
      json: route.request().url().includes('/auth/')
        ? { email: 'speech-browser@cognis.test', role: 'user', name: 'Speech browser' } : {}
    }));
    await page.route('**/api/v1/tts/capabilities*', route => route.fulfill({
      json: { tts: true, stt: true, streaming_tts: false, streaming_stt: false, detail: null }
    }));
    let requests = 0;
    let releaseSynthesis!: () => void;
    const synthesisReady = new Promise<void>(resolve => { releaseSynthesis = resolve; });
    await page.route('**/api/v1/tts/synthesize', async route => {
      requests++;
      await synthesisReady;
      return route.fulfill({ json: speechResponse() });
    });
    await page.route('**/speech-fixture.wav', route => route.fulfill({
      contentType: 'audio/wav', body: silence(30)
    }));
    await page.goto('/speech-playback-browser-fixture');
    const message = page.locator('#speech-message-fixture');
    await expect.poll(() => page.evaluate(() => window.innerWidth)).toBe(width);
    const read = message.getByRole('button', { name: 'Read aloud', exact: true });
    await expect(read).toBeVisible();
    await page.evaluate(theme => {
      document.documentElement.dataset.themeFamily = 'paper';
      document.documentElement.dataset.resolvedTheme = theme;
    }, theme);
    await message.evaluate(async element => {
      await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
      await Promise.all(element.getAnimations({ subtree: true })
        .filter(animation => animation.effect?.getTiming().iterations !== Infinity)
        .map(animation => animation.finished.catch(() => {})));
    });
    await message.screenshot({ path: testInfo.outputPath('message-playback-idle.png') });
    await expectCompactMessagePlaybackFace(message, read);
    try {
      if (width === 390) await read.tap(); else await read.click();
      const generating = message.getByRole('button', { name: 'Cancel audio generation', exact: true });
      await expect(generating).toBeVisible();
      await expectCompactMessagePlaybackFace(message, generating);
    } finally {
      releaseSynthesis();
    }
    const back = message.getByRole('button', { name: 'Back 10 seconds' });
    const pause = message.getByRole('button', { name: 'Pause reading', exact: true });
    const forward = message.getByRole('button', { name: 'Forward 10 seconds' });
    await expect(pause).toBeVisible();
    for (const control of [back, pause, forward]) {
      await expectCompactMessagePlaybackFace(message, control);
    }
    await message.screenshot({ path: testInfo.outputPath('message-playback-controls.png') });
    await expectBorderlessPlaybackControl(back);
    await expectBorderlessPlaybackControl(forward);
    const boxes = await Promise.all([back, pause, forward].map(button => button.boundingBox()));
    console.log('Playback control geometry', width, boxes);
    expect(boxes[1]!.x - (boxes[0]!.x + boxes[0]!.width)).toBeLessThanOrEqual(1);
    expect(boxes[2]!.x - (boxes[1]!.x + boxes[1]!.width)).toBeLessThanOrEqual(1);
    expect(boxes[1]!.x - (boxes[0]!.x + boxes[0]!.width)).toBeGreaterThanOrEqual(0);
    expect(boxes[2]!.x - (boxes[1]!.x + boxes[1]!.width)).toBeGreaterThanOrEqual(0);
    const speed = message.getByRole('button', { name: 'Playback speed: 1×' });
    if (width === 1200) {
      await speed.hover();
      expect(await speed.evaluate(element => element.matches(':focus-visible'))).toBe(false);
    }
    await message.screenshot({ path: testInfo.outputPath('message-playback-speed-hover.png') });
    await expectPlainPlaybackSpeed(speed);
    const speedBox = await speed.boundingBox();
    expect(speedBox!.height).toBe(boxes[1]!.height);
    expect(speedBox!.x + speedBox!.width).toBeLessThanOrEqual(width);
    if (width === 390) {
      for (const box of boxes) {
        expect(box!.x + box!.width).toBeLessThanOrEqual(width);
      }
      await speed.tap();
    } else {
      await speed.focus();
      await page.keyboard.press('Space');
    }
    const changedSpeed = message.getByRole('button', { name: 'Playback speed: 1.25×' });
    await expect(changedSpeed).toBeVisible();
    if (width === 1200) {
      expect(await changedSpeed.evaluate(element => element.matches(':focus-visible'))).toBe(true);
    }
    await expectPlainPlaybackSpeed(changedSpeed);
    await pause.click();
    await expect(message.getByRole('button', { name: 'Back 10 seconds' })).toBeVisible();
    await expectCompactMessagePlaybackFace(message, message.getByRole('button', { name: 'Resume reading', exact: true }));
    await expectBorderlessPlaybackControl(back);
    await expectBorderlessPlaybackControl(forward);
    await message.screenshot({ path: testInfo.outputPath('message-playback-paused.png') });
    await message.getByRole('button', { name: 'Playback speed: 1.25×' }).click();
    await message.getByRole('button', { name: 'Resume reading', exact: true }).click();
    await expect(pause).toBeVisible();
    expect(requests).toBe(1);
    await message.screenshot({ path: testInfo.outputPath('message-playback-speed.png') });
  });
  }

  test(`compact player expands only during playback at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const recording = silence(30);
    await page.route('**/speech-fixture.wav', (route) => {
      const range = /^bytes=(\d+)-(\d*)$/.exec(route.request().headers().range ?? '');
      const start = range ? Number(range[1]) : 0;
      const end = range?.[2] ? Math.min(Number(range[2]), recording.length - 1) : recording.length - 1;
      return route.fulfill({
        status: range ? 206 : 200, contentType: 'audio/wav',
        headers: {
          'accept-ranges': 'bytes', 'content-length': String(end - start + 1),
          ...(range ? { 'content-range': `bytes ${start}-${end}/${recording.length}` } : {})
        },
        body: recording.subarray(start, end + 1)
      });
    });
    await page.goto('/speech-playback-browser-fixture');
    const player = page.locator('#speech-fixture');
    // Cold Vite compilation is fixture setup, not a playback transition.
    await player.waitFor();
    await expect(player.getByRole('button', { name: 'Play audio' })).toBeVisible();
    await expect(player.getByRole('button', { name: 'Back 10 seconds' })).toHaveCount(0);
    await expect.poll(() => player.locator('audio').evaluate((audio: HTMLAudioElement) => audio.duration)).toBe(30);
    await player.getByRole('button', { name: 'Play audio' }).click();
    await expect(player.getByRole('button', { name: 'Pause audio' })).toBeVisible();
    await expect(player.getByRole('button', { name: 'Back 10 seconds' })).toBeVisible();
    const playerSpeed = player.getByRole('button', { name: /^Playback speed:/ });
    if (width === 1200) await playerSpeed.hover(); else await playerSpeed.tap();
    await player.screenshot({ path: testInfo.outputPath('speech-player-speed-hover.png') });
    await expectPlainPlaybackSpeed(playerSpeed);
    await page.screenshot({ path: testInfo.outputPath('speech-player-playing.png') });
    await player.getByRole('button', { name: 'Forward 10 seconds' }).click();
    await expect.poll(() => player.locator('audio').evaluate((audio: HTMLAudioElement) => audio.currentTime)).toBeGreaterThan(9);
    await player.getByRole('button', { name: 'Back 10 seconds' }).click();
    await expect.poll(() => player.locator('audio').evaluate((audio: HTMLAudioElement) => audio.currentTime)).toBeLessThan(3);
    await player.getByRole('button', { name: 'Pause audio' }).click();
    await expect(player.getByRole('button', { name: 'Back 10 seconds' })).toHaveCount(0);
    await expect(player.getByRole('button', { name: 'Forward 10 seconds' })).toHaveCount(0);
    await expect(player).not.toContainText(/\d+:\d+ \/ \d+:\d+/);
    expect(await player.locator('audio').evaluate((audio: HTMLAudioElement) => audio.controls)).toBe(false);
    await player.getByRole('button', { name: 'Play audio' }).click();
    await player.locator('audio').evaluate((audio: HTMLAudioElement) => {
      audio.currentTime = audio.duration - 0.1;
    });
    await expect(player.getByRole('button', { name: 'Play audio' })).toBeVisible();
    await expect(player.getByRole('button', { name: 'Back 10 seconds' })).toHaveCount(0);
    await expect(player.getByRole('button', { name: 'Forward 10 seconds' })).toHaveCount(0);
  });
  });
}

for (const standalone of [false, true]) {
  for (const scenario of ['normal', 'autoplay-denied', 'cancel-priming']) {
  test(`assistant message ${scenario} in ${standalone ? 'standalone PWA' : 'browser'} mode`, async ({ page }) => {
    await page.addInitScript((scenario) => {
      const original = HTMLMediaElement.prototype.play;
      let denied = false;
      HTMLMediaElement.prototype.play = function () {
        if (scenario === 'cancel-priming' && this.src.startsWith('data:audio/')) {
          return new Promise<void>(() => {});
        }
        if (scenario === 'autoplay-denied' && !denied && this.src.includes('speech-fixture.wav')) {
          denied = true;
          return Promise.reject(new DOMException('Playback needs another tap.', 'NotAllowedError'));
        }
        return original.call(this);
      };
    }, scenario);
    if (standalone) {
      await page.addInitScript(() => {
        Object.defineProperty(navigator, 'standalone', { get: () => true });
        const original = window.matchMedia.bind(window);
        window.matchMedia = (query: string) => {
          const media = original(query);
          if (query === '(display-mode: standalone)') {
            Object.defineProperty(media, 'matches', { value: true });
          }
          return media;
        };
      });
    }
    let synthesisRequests = 0;
    // Isolate the mounted message from the development server's live API and
    // background preference/session requests. No real provider can be billed.
    await page.route((url) => url.pathname.startsWith('/api/'), (route) => route.fulfill({
      json: route.request().url().includes('/auth/')
        ? { user: { email: 'speech-browser@cognis.test', role: 'user' }, expires_at: '2099-01-01T00:00:00Z' }
        : {}
    }));
    await page.route('**/api/auth/me', (route) => route.fulfill({
      json: { email: 'speech-browser@cognis.test', role: 'user', name: 'Speech browser' }
    }));
    await page.route('**/api/v1/tts/synthesize', (route) => {
      synthesisRequests += 1;
      return route.fulfill({ json: speechResponse() });
    });
    await page.route('**/api/v1/tts/capabilities*', route => route.fulfill({
      json: { tts: true, stt: false, streaming_tts: false, streaming_stt: false,
        stt_sample_rate: 24000, detail: null },
    }));
    await page.route('**/speech-fixture.wav', (route) =>
      route.fulfill({ contentType: 'audio/wav', body: silence(30) }));
    await page.goto('/speech-playback-browser-fixture');
    const message = page.locator('#speech-message-fixture');
    await message.waitFor();
    await expect(message.getByRole('button', { name: 'Read aloud', exact: true })).toBeVisible();
    if (scenario === 'cancel-priming') await page.clock.install();
    await message.getByRole('button', { name: 'Read aloud', exact: true }).click();
    if (scenario === 'cancel-priming') {
      await message.getByRole('button', { name: 'Cancel audio generation', exact: true }).click();
      await page.clock.runFor(3000);
      await expect(message.getByRole('button', { name: 'Read aloud', exact: true })).toBeVisible();
      expect(synthesisRequests).toBe(0);
      return;
    }
    await expect.poll(() => synthesisRequests).toBe(1);
    if (scenario === 'autoplay-denied') {
      await message.getByRole('button', { name: 'Resume reading', exact: true }).click();
    }
    await expect(message.getByRole('button', { name: 'Pause reading', exact: true })).toBeVisible();
    await message.getByRole('button', { name: 'Pause reading', exact: true }).click();
    await expect(message.getByRole('button', { name: 'Resume reading', exact: true })).toBeVisible();
    expect(synthesisRequests).toBe(1);
  });
  }
}

for (const live of [false, true]) {
test(`conversation mode ${live ? 'streams' : 'records'}, waits, plays a Chat v2 answer, and listens again`, async ({ page }) => {
  await page.route((url) => url.pathname.startsWith('/api/'), route => route.fulfill({
    json: route.request().url().includes('/auth/')
      ? { user: { email: 'speech-browser@cognis.test', role: 'user' }, expires_at: '2099-01-01T00:00:00Z' }
      : {},
  }));
  await page.route('**/api/auth/me', route => route.fulfill({
    json: { email: 'speech-browser@cognis.test', role: 'user', name: 'Speech browser' },
  }));
  await page.route('**/speech-fixture.wav', route => route.fulfill({
    contentType: 'audio/wav', body: silence(0.5),
  }));
  await page.route('**/api/v1/tts/capabilities*', route => route.fulfill({
    json: { tts: true, stt: true, detail: null, streaming_tts: live, streaming_stt: live, stt_sample_rate: 16000 },
  }));
  await page.route('**/api/v1/stt/transcribe', route => route.fulfill({
    json: { text: 'Explain voice mode.' },
  }));
  const synthesis: string[] = [];
  const synthesize = (route: Route) => {
    synthesis.push(route.request().postDataJSON().text);
    return route.fulfill(live ? speechStream(0.5) : { json: speechResponse() });
  };
  await page.route('**/api/v1/tts/stream', synthesize);
  await page.route('**/api/v1/tts/synthesize', synthesize);
  let microphoneFrames = 0, connections = 0;
  if (live) await page.routeWebSocket('**/api/ws', socket => {
    connections += 1;
    socket.send(JSON.stringify({ type: 'authenticated' }));
    socket.onMessage(raw => {
      const message = JSON.parse(String(raw));
      const send = (event: object) => socket.send(JSON.stringify({
        type: 'speech_stt_event', request_id: message.request_id, event,
      }));
      if (message.type === 'speech_stt_start') send({ type: 'start', sample_rate: 16000 });
      if (message.type === 'speech_stt_audio') microphoneFrames += 1;
      if (message.type === 'speech_stt_commit') {
        send({ type: 'transcript', text: 'Explain voice mode.' });
        send({ type: 'done' });
      }
    });
  });
  await page.addInitScript(() => {
    const recordings: MediaRecorder[] = [];
    let bytes = 0;
    const NativeRecorder = window.MediaRecorder;
    window.MediaRecorder = class extends NativeRecorder {
      constructor(stream: MediaStream, options?: MediaRecorderOptions) {
        super(stream, options);
        recordings.push(this);
        this.addEventListener('dataavailable', event => { bytes += event.data.size; });
      }
    };
    Object.defineProperty(window, '__voiceRecordings', { value: recordings });
    Object.defineProperty(window, '__voiceBytes', { get: () => bytes });
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      const output = context.createMediaStreamDestination();
      const oscillator = context.createOscillator();
      oscillator.connect(output);
      oscillator.start();
      await context.resume();
      return output.stream;
    };
  });
  await page.goto('/speech-playback-browser-fixture');
  await page.getByRole('button', { name: 'Start voice conversation' }).click();
  const dialog = page.getByRole('dialog', { name: 'Voice conversation mode' });
  await expect(dialog.getByText('Listening…', { exact: true })).toBeVisible();
  const enablePlayback = dialog.getByRole('button', { name: 'Enable audio playback' });
  if (await enablePlayback.isVisible()) await enablePlayback.click();
  await expect(dialog.getByRole('button', { name: 'Finish speaking' })).toBeVisible();
  if (live) await expect.poll(() => microphoneFrames).toBeGreaterThan(0);
  else await expect.poll(() => page.evaluate(() => {
    const state = window as unknown as {
      __voiceRecordings: MediaRecorder[]; __voiceBytes: number;
    };
    state.__voiceRecordings[0].requestData();
    return state.__voiceBytes;
  })).toBeGreaterThan(0);
  await dialog.getByRole('button', { name: 'Finish speaking' }).click();
  await expect(page.getByTestId('voice-submitted-text')).toHaveText('Explain voice mode.');
  await expect(dialog.getByText('Waiting for the assistant…')).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('voice-waiting.png') });
  await page.evaluate(() => window.dispatchEvent(new Event('voice-fixture-response')));
  await expect.poll(() => synthesis).toEqual(['This is the spoken response.']);
  await expect(dialog.getByRole('button', { name: 'Finish speaking' })).toBeVisible();
  if (live) await expect.poll(() => connections).toBe(2);
  else await expect.poll(() => page.evaluate(() => (
    window as unknown as { __voiceRecordings: MediaRecorder[] }
  ).__voiceRecordings.length)).toBe(2);
  await page.screenshot({ path: test.info().outputPath('voice-listening.png') });
  await dialog.getByRole('button', { name: 'End conversation' }).first().click();
  await expect(dialog).toHaveCount(0);
});
}

for (const [width, groupToolCalls] of [
  [1200, true], [390, true], [1200, false], [390, false],
] as const) {
  test.describe(`${width}px conversation feedback ${groupToolCalls ? 'grouped' : 'ungrouped'}`, () => {
    test.use({ hasTouch: width === 390, isMobile: width === 390 });
    test('shows live tools and reads a deliverable-only answer', async ({ page }, testInfo) => {
      const activate = (button: Locator) => width === 390 ? button.tap() : button.click();
      await page.setViewportSize({ width, height: 900 });
      const unexpected: string[] = [];
      const errors: string[] = [];
      const generated: unknown[] = [];
      let played = 0;
      page.on('pageerror', error => errors.push(error.message));
      await page.route(url => url.pathname.startsWith('/api/'), route => {
        const path = new URL(route.request().url()).pathname;
        if (path === '/api/auth/me') return route.fallback();
        if (path === '/api/v1/user-preferences') return route.fulfill({
          json: { chat: { group_tool_calls: groupToolCalls } },
        });
        if (path === '/api/v1/tts/capabilities') return route.fulfill({ json: {
          tts: true, stt: true, streaming_tts: false, streaming_stt: false, stt_sample_rate: 24000,
        } });
        if (path === '/api/v1/stt/transcribe') return route.fulfill({ json: { text: 'Read the report.' } });
        if (path === '/api/v1/audio/jobs/source') return route.fulfill({ json: {
          supported: true, can_generate: true, can_cancel: true, detail: null, jobs: [],
        } });
        if (path === '/api/v1/audio/jobs' && route.request().method() === 'POST') {
          generated.push(route.request().postDataJSON());
          return route.fulfill({ json: {
            job_id: 'voice-report-recording', deliverable_id: 'dlv_11111111111111111111111111111111',
            mode: 'narration', status: 'completed', completed_parts: 1, total_parts: 1,
            parts: [{ artifact_id: 'voice-report-audio', audio_url: '/speech-fixture.wav', content_type: 'audio/wav' }],
            script_artifact_id: null, script_url: null, error_code: null, created_at: '2026-11-07T09:00:00Z',
          } });
        }
        // Auth bootstrapping and message controls are unrelated to this fixture.
        if (path.startsWith('/api/auth/') || path === '/api/v1/preferences') return route.fulfill({ json: {} });
        unexpected.push(`${route.request().method()} ${path}`);
        return route.fulfill({ status: 500, json: { detail: 'Unexpected fixture request' } });
      });
      await page.route('**/speech-fixture.wav', route => {
        played++;
        return route.fulfill({ contentType: 'audio/wav', body: silence(1) });
      });
      await page.addInitScript(() => {
        const tones: number[] = [];
        Object.defineProperty(window, '__conversationTones', { value: tones });
        const create = AudioContext.prototype.createOscillator;
        AudioContext.prototype.createOscillator = function() {
          const node = create.call(this);
          const set = node.frequency.setValueAtTime.bind(node.frequency);
          node.frequency.setValueAtTime = (frequency, time) => {
            tones.push(frequency);
            return set(frequency, time);
          };
          return node;
        };
        navigator.mediaDevices.getUserMedia = async () => {
          const context = new AudioContext();
          const output = context.createMediaStreamDestination();
          const oscillator = context.createOscillator();
          oscillator.connect(output); oscillator.start(); await context.resume();
          return output.stream;
        };
      });
      await page.goto(`/speech-playback-browser-fixture?group_tools=${groupToolCalls}`);
      await activate(page.getByRole('button', { name: 'Start voice conversation' }));
      const dialog = page.getByRole('dialog', { name: 'Voice conversation mode' });
      await expect(dialog.getByRole('button', { name: 'Finish speaking' })).toBeVisible();
      await expect(dialog.getByRole('button', { name: 'Finish speaking' })).toBeEnabled();
      const tones = () => page.evaluate(() => (window as unknown as { __conversationTones: number[] }).__conversationTones);
      expect(await tones()).toContain(520);
      expect(await tones()).toContain(660);
      await activate(dialog.getByRole('button', { name: 'Finish speaking' }));
      await expect(page.getByTestId('voice-submitted-text')).toHaveText('Read the report.');
      const tool: ToolCallTimelineItem = {
        id: 'voice-tool-read', kind: 'tool_call', call_id: 'voice-read-call', tool_name: 'read',
        display_name: 'Read report sources', turn_id: 'voice-turn', status: 'running',
        sort_key: '002', stable: false, source_refs: [], is_error: false,
        attachments: [], file_diffs: [], truncated: false, has_full_output: false,
      };
      await page.evaluate(detail => window.dispatchEvent(new CustomEvent('voice-fixture-turn', { detail })), {
        runtime: speechRuntime('', { runtime_revision: 2, volatile_items: [
          tool, { ...tool, id: 'voice-tool-read-2', call_id: 'voice-read-call-2', sort_key: '003' },
          { ...tool, id: 'foreign-tool', call_id: 'foreign-call', turn_id: 'other-turn', display_name: 'Other turn work' },
        ] }), items: [],
      });
      await expect(dialog.getByRole('region', { name: 'Assistant activity' })).toBeVisible();
      await expect(dialog.getByText('Other turn work', { exact: true })).not.toBeVisible();
      const group = dialog.getByRole('button', { name: 'Exploring… 2 tools', exact: true });
      if (groupToolCalls) await expect(group).toBeVisible();
      else {
        await expect(group).toHaveCount(0);
        await expect(dialog.getByText('Read report sources', { exact: true })).toHaveCount(2);
      }
      await expect.poll(tones).toContain(440);
      await page.evaluate(() => new Promise<void>(resolve => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      }));
      await page.screenshot({ path: testInfo.outputPath('conversation-tools.png') });
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);

      const activity = dialog.getByRole('region', { name: 'Assistant activity' });
      const bounds = await activity.boundingBox();
      expect(bounds).not.toBeNull();
      if (width === 1200) expect(bounds!.width).toBeLessThanOrEqual(width / 3 + 1);
      else expect(bounds!.height).toBeLessThanOrEqual(900 / 3 + 1);
      expect(bounds!.width * bounds!.height).toBeLessThanOrEqual(width * 900 / 3 + 1);

      await page.evaluate(detail => window.dispatchEvent(new CustomEvent('voice-fixture-turn', { detail })), {
        runtime: speechRuntime('', { runtime_revision: 3, volatile_items: Array.from({ length: 16 }, (_, i) => ({
          ...tool, id: `voice-source-${i}`, call_id: `voice-source-call-${i}`,
          sort_key: String(i + 2).padStart(3, '0'),
        })) }), items: [],
      });
      if (groupToolCalls) {
        const expandedGroup = activity.getByRole('button', { name: 'Exploring… 16 tools', exact: true });
        await expect(expandedGroup).toBeVisible();
        if (await expandedGroup.getAttribute('aria-expanded') !== 'true') await activate(expandedGroup);
      }
      await expect(activity.getByText('Read report sources', { exact: true })).toHaveCount(16);
      await activity.evaluate(async node => {
        await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
        await Promise.all(node.getAnimations({ subtree: true })
          .filter(animation => animation.effect?.getComputedTiming().iterations !== Infinity)
          .map(animation => animation.finished.catch(() => {})));
      });
      await expect.poll(() => activity.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
      const beforeScroll = await activity.evaluate(node => node.scrollTop);
      const pane = (await activity.boundingBox())!;
      const direction = beforeScroll > 0 ? -1 : 1;
      if (width === 1200) {
        await page.mouse.move(pane.x + pane.width / 2, pane.y + pane.height / 2);
        await page.mouse.wheel(0, direction * 400);
      } else {
        const touch = await page.context().newCDPSession(page);
        const x = pane.x + pane.width / 2;
        const startY = direction > 0 ? pane.y + pane.height - 24 : pane.y + 24;
        const endY = direction > 0 ? pane.y + 24 : pane.y + pane.height - 24;
        await touch.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y: startY }] });
        for (let i = 1; i <= 8; i++) {
          await touch.send('Input.dispatchTouchEvent', {
            type: 'touchMove', touchPoints: [{ x, y: startY + (endY - startY) * i / 8 }],
          });
          await page.waitForTimeout(20);
        }
        await touch.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
        await touch.detach();
      }
      await expect.poll(() => activity.evaluate(node => node.scrollTop)).not.toBe(beforeScroll);
      const expandedBounds = (await activity.boundingBox())!;
      if (width === 1200) expect(expandedBounds.width).toBeLessThanOrEqual(width / 3 + 1);
      else expect(expandedBounds.height).toBeLessThanOrEqual(900 / 3 + 1);
      expect(expandedBounds.width * expandedBounds.height).toBeLessThanOrEqual(width * 900 / 3 + 1);
      const controls = (await dialog.getByRole('button', { name: 'End conversation', exact: true })
        .filter({ hasText: 'End conversation' }).boundingBox())!;
      expect(expandedBounds.y + expandedBounds.height).toBeLessThanOrEqual(controls.y);
      await page.screenshot({ path: testInfo.outputPath('conversation-tools-expanded.png') });
      const deliverable: TimelineItem = {
        id: 'voice-deliverable', kind: 'assistant_deliverable',
        deliverable_id: 'dlv_11111111111111111111111111111111', format: 'markdown',
        title: 'Report', turn_id: 'voice-turn', sort_key: '003', stable: true, source_refs: [],
      };
      const playedBefore = played;
      await page.evaluate(detail => window.dispatchEvent(new CustomEvent('voice-fixture-turn', { detail })), {
        runtime: speechRuntime('', { has_active_turn: false, runtime_revision: 4 }), items: [deliverable],
      });
      await expect.poll(() => generated.length).toBe(1);
      expect(generated[0]).toMatchObject({
        content_ref: 'dlv_11111111111111111111111111111111', mode: 'narration',
      });
      await expect.poll(() => played).toBeGreaterThan(playedBefore);
      await expect(dialog.getByText('Speaking…', { exact: true })).toBeVisible();
      await expect(dialog.getByRole('button', { name: 'Finish speaking' })).toBeVisible();
      await activate(dialog.getByRole('button', { name: 'End conversation' }).first());
      const stoppedTones = await tones();
      await page.clock.install();
      await page.clock.fastForward(8500);
      expect(await tones()).toEqual(stoppedTones);
      expect(unexpected).toEqual([]);
      expect(errors).toEqual([]);
    });
  });
}

test('message reading plays native PCM without fetching the finished artifact', async ({ page }) => {
  let artifacts = 0, synthesis = 0;
  await page.route('**/api/v1/tts/capabilities*', route => route.fulfill({
    json: { tts: true, stt: false, streaming_tts: true, streaming_stt: false,
      stt_sample_rate: 24000, detail: null },
  }));
  await page.route('**/api/v1/tts/stream', route => {
    synthesis += 1;
    return route.fulfill(speechStream(10));
  });
  await page.route('**/speech-fixture.wav', route => {
    return route.fulfill({ contentType: 'audio/wav', body: silence(30) });
  });
  await page.route('**/native-finished.wav', route => {
    artifacts += 1;
    return route.fulfill({ contentType: 'audio/wav', body: silence(10) });
  });
  await page.goto('/speech-playback-browser-fixture');
  const message = page.locator('#speech-message-fixture');
  await message.getByRole('button', { name: 'Read aloud', exact: true }).click();
  await expect(message.getByRole('button', { name: 'Pause reading', exact: true })).toBeVisible();
  await message.getByRole('button', { name: 'Pause reading', exact: true }).click();
  await expect(message.getByRole('button', { name: 'Resume reading', exact: true })).toBeVisible();
  expect(synthesis).toBe(1);
  expect(artifacts).toBe(0);
  await page.screenshot({ path: test.info().outputPath('message-native-pcm-paused.png') });
});
