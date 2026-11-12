import { execFileSync } from 'node:child_process';
import { createServer, type Server } from 'node:http';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test } from '@playwright/test';

const UI_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const REPOSITORY_DIR = path.resolve(UI_DIR, '..');
const ASSET_PREFIX = '/api/v1/deliverables/standalone-assets/';
const authorizedMediaKey = 'media_0123456789abcdef01234567';
const missingMediaKey = 'media_ffffffffffffffffffffffff';
const MEDIA_PREFIX = '/api/v1/deliverables/dlv_standalone_security/media/';
const authorizedMediaPath = `${MEDIA_PREFIX}${authorizedMediaKey}`;
const fixturePng = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nJ8AAAAASUVORK5CYII=',
  'base64',
);
const CSP = [
  'sandbox allow-scripts allow-same-origin allow-downloads',
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "connect-src 'self'",
  "img-src 'self' data:",
  "font-src 'self' data:",
  "media-src 'self' data:",
].join('; ');

const executionMarker = 'standalonePayloadExecuted';
const adversarialValues = [
  `</script><script>globalThis.${executionMarker}=true</script>`,
  `</template><script>globalThis.${executionMarker}=true</script>`,
  '<!-- payload comment opener',
  'payload CDATA closer ]]>',
  `<img src="/unexpected-image" onerror="globalThis.${executionMarker}=true">`,
  `<svg onload="globalThis.${executionMarker}=true"></svg>`,
  `&lt;img src="/encoded-image" onerror="globalThis.${executionMarker}=true"&gt;`,
  `%3Csvg%20onload%3DglobalThis.${executionMarker}%3Dtrue%3E`,
  `</template><template><svg onload="globalThis.${executionMarker}=true"></svg>`,
  `javascript:globalThis.${executionMarker}=true`,
  `data:text/html,<script>globalThis.${executionMarker}=true</script>`,
  'https://attacker.invalid/unexpected-request',
] as const;

const attack = (index: number): string => `adversarial-${index}:${adversarialValues[index]}`;

const payload = {
  metadata: {
    presentation: 'default',
    subtitle: attack(0),
    eyebrow: attack(1),
    description: attack(2),
    summary: attack(3),
    badges: [attack(4), attack(5)],
    nested: { label: attack(6), url: attack(9) },
  },
  blocks: [
    {
      type: 'hero',
      title: 'Canonical standalone security report',
      subtitle: attack(7),
      eyebrow: attack(8),
      badges: ['Canonical', attack(4)],
    },
    {
      type: 'card',
      title: 'Canonical content remains usable',
      content: attack(4),
      description: attack(5),
      eyebrow: attack(2),
      dek: attack(3),
      summary: attack(6),
      href: attack(9),
      source: attack(0),
      source_url: attack(10),
      timestamp: attack(1),
      media: {
        alt: attack(7),
        caption: attack(8),
        credit: attack(11),
        source_url: attack(9),
      },
    },
    {
      type: 'markdown',
      title: 'Canonical markdown',
      content: `Legitimate **canonical markdown**.\n\n${attack(0)}\n${attack(4)}\n${attack(5)}`,
    },
    {
      type: 'accordion',
      items: [
        {
          type: 'section',
          title: 'Authorized standalone article',
          summary: 'Collapsed standalone summary',
          content: 'Expanded standalone article body.',
          href: '/article-source',
          media: {
            key: authorizedMediaKey,
            alt: 'Authorized standalone article media',
            credit: 'Standalone fixture publisher',
            source_url: '/image-source',
          },
        },
        {
          type: 'section',
          title: 'Missing standalone article',
          media: {
            key: missingMediaKey,
            alt: 'Missing standalone article media',
          },
        },
      ],
    },
    {
      type: 'link',
      title: attack(6),
      label: attack(7),
      content: attack(8),
      description: attack(0),
      href: attack(9),
      url: attack(10),
    },
    {
      type: 'figure',
      title: attack(1),
      caption: attack(2),
      alt: attack(3),
      src: attack(10),
      source: attack(5),
      source_url: attack(11),
    },
    {
      type: 'source_list',
      title: 'Canonical sources',
    },
  ],
  sources: [{
    id: attack(0),
    title: attack(1),
    url: attack(9),
    description: attack(4),
    publisher: attack(5),
  }],
  datasets: [{
    id: attack(2),
    title: attack(3),
    description: attack(6),
    source: attack(7),
    url: attack(10),
    rows: [{ label: attack(8), value: attack(0) }],
  }],
  assets: [{
    id: attack(1),
    title: attack(2),
    description: attack(3),
    filename: attack(4),
    mime_type: attack(5),
    url: attack(11),
  }],
  exports: [{
    id: attack(6),
    title: attack(7),
    description: attack(8),
    format: attack(0),
    url: attack(9),
  }],
  media_manifest: {
    [authorizedMediaKey]: {
      artifact_ref: 'art_0123456789abcdef',
      filename: 'fixture.png',
      mime_type: 'image/png',
    },
    media_adversarial: {
      filename: attack(1),
      mime_type: attack(2),
      alt: attack(3),
      caption: attack(4),
      source_url: attack(10),
    },
  },
};

const renderScript = String.raw`
import json
import sys
from types import SimpleNamespace
from cognis.rendering.deliverables import render_standalone_shell

payload = json.load(sys.stdin)
row = SimpleNamespace(
    content='Canonical fallback with adversarial text: </template><img src="/fallback-breakout" onerror="globalThis.standalonePayloadExecuted=true">',
    deliverable_id='dlv_standalone_security',
    format='rich',
    rich_payload=payload,
    title='Canonical shell title </script><svg onload="globalThis.standalonePayloadExecuted=true">',
)
sys.stdout.write(render_standalone_shell(
    row,
    media_base='/api/v1/deliverables/dlv_standalone_security/media',
    standalone_url='/view',
    pdf_url='/download.pdf',
))
`;

let server: Server;
let origin = '';
let shell = '';
let audioShells: Record<string, string> = {};
let observedRequests: string[] = [];

test.beforeAll(async () => {
  execFileSync('npm', ['run', 'build:standalone'], {
    cwd: UI_DIR,
    stdio: 'pipe',
  });
  shell = execFileSync('uv', ['run', 'python', '-c', renderScript], {
    cwd: REPOSITORY_DIR,
    encoding: 'utf8',
    input: JSON.stringify(payload),
    maxBuffer: 16 * 1024 * 1024,
  });
  audioShells = JSON.parse(execFileSync('uv', ['run', 'python', '-c', String.raw`
import json
from types import SimpleNamespace
from cognis.rendering.deliverables import render_standalone_shell
from cognis.api.routes.deliverables import _wrapped_standalone_rich_payload
shells = {}
for format_ in ("markdown", "plain", "html", "rich"):
    row = SimpleNamespace(
        deliverable_id="dlv_" + "1" * 32, format=format_, title="Listen fixture",
        content="<p>HTML reading fixture.</p>" if format_ == "html" else "Document reading fixture.",
        rich_payload={"blocks": [{"type": "markdown", "content": "Rich reading fixture."}]},
    )
    shells[format_] = render_standalone_shell(
        row, media_base="", standalone_url="/audio-view/" + format_, pdf_url="",
        rich_payload_override=None if format_ == "rich" else _wrapped_standalone_rich_payload(row, format_=format_),
    )
print(json.dumps(shells))
`], { cwd: REPOSITORY_DIR, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 }));

  server = createServer((request, response) => {
    const requestPath = new URL(request.url ?? '/', 'http://standalone.test').pathname;
    observedRequests.push(requestPath);
    if (requestPath === '/view' || requestPath.startsWith('/audio-view/')) {
      response.writeHead(200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Content-Security-Policy': CSP,
        'Referrer-Policy': 'no-referrer',
      });
      response.end(requestPath === '/view' ? shell : audioShells[requestPath.split('/').pop()!]);
      return;
    }
    if (requestPath.startsWith(ASSET_PREFIX)) {
      const relativePath = requestPath.slice(ASSET_PREFIX.length);
      const assetPath = path.resolve(UI_DIR, 'standalone-build', relativePath);
      const buildRoot = path.resolve(UI_DIR, 'standalone-build');
      if (assetPath.startsWith(`${buildRoot}${path.sep}`)) {
        try {
          const contentType = assetPath.endsWith('.css')
            ? 'text/css; charset=utf-8'
            : 'text/javascript; charset=utf-8';
          response.writeHead(200, {
            'Content-Type': contentType,
            'X-Content-Type-Options': 'nosniff',
          });
          response.end(readFileSync(assetPath));
          return;
        } catch {
          // Return the same opaque not-found response for missing production assets.
        }
      }
    }
    if (requestPath === authorizedMediaPath) {
      response.writeHead(200, {
        'Content-Type': 'image/png',
        'Cache-Control': 'private, max-age=60',
        'X-Content-Type-Options': 'nosniff',
      });
      response.end(fixturePng);
      return;
    }
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    response.end('Not found');
  });
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      if (!address || typeof address === 'string') {
        reject(new Error('Standalone security server did not bind a TCP port'));
        return;
      }
      origin = `http://127.0.0.1:${address.port}`;
      resolve();
    });
  });
});

test.beforeEach(() => {
  observedRequests = [];
});

test.afterAll(async () => {
  await new Promise<void>((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
  });
});

test('keeps adversarial values inert in the generated production shell', async ({ request }) => {
  const response = await request.get(`${origin}/view`);
  const document = await response.text();

  expect(response.headers()['content-security-policy']).toBe(CSP);
  expect(document.match(/<template id="cognis-deliverable-payload"/g)).toHaveLength(1);
  expect(document.match(/<script\b/g)).toHaveLength(1);
  expect(document).toContain('<script type="module" src="/api/v1/deliverables/standalone-assets/');
  expect(document).not.toContain(`<script>globalThis.${executionMarker}=true</script>`);
  expect(document).not.toContain('<img src="/unexpected-image"');
  expect(document).not.toContain('<svg onload=');
  expect(document).toContain('&lt;/template&gt;');
  expect(document).toContain('&lt;/script&gt;');

  for (const index of adversarialValues.keys()) {
    expect(document).toContain(`adversarial-${index}:`);
  }
});

for (const format of ['markdown', 'plain', 'html', 'rich']) {
  test(`anonymous ${format} viewers never show a speaker or private-audio notice`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const writes: string[] = [];
    page.on('request', request => { if (request.method() !== 'GET') writes.push(request.url()); });
    await page.goto(`${origin}/audio-view/${format}`, { waitUntil: 'networkidle' });
    await expect(page.getByRole('button', { name: 'Listen to this document' })).toHaveCount(0);
    await expect(page.getByRole('dialog', { name: 'Document audio' })).toHaveCount(0);
    await expect(page.getByText(/Sign in with document access/)).toHaveCount(0);
    expect(writes).toEqual([]);
  });
}

for (const width of [390, 1280]) {
  for (const theme of ['light', 'dark'] as const) {
    test(`uses an opaque app-themed audio popup at ${width}px in ${theme} mode`, async ({ page }) => {
      await page.setViewportSize({ width, height: 844 });
      await page.route('**/api/v1/audio/jobs/source?*', route => route.fulfill({ json: {
        supported: true, can_generate: true, can_cancel: true, detail: null, jobs: [],
      } }));
      await page.goto(`${origin}/audio-view/markdown`, { waitUntil: 'networkidle' });
      const trigger = page.getByRole('button', { name: 'Listen to this document' });
      await expect(trigger).toHaveText('');
      await trigger.evaluate((element, theme) => {
        const button = element as HTMLElement;
        button.style.setProperty('--rich-surface-raised', 'initial');
        button.style.setProperty('--rich-text', 'initial');
        button.style.setProperty('--color-elevated', theme === 'light' ? '255 255 255' : '38 38 35');
        button.style.setProperty('--color-foreground', theme === 'light' ? '17 24 39' : '243 244 246');
        button.style.backgroundColor = 'transparent';
        button.style.color = theme === 'light' ? '#111827' : '#f3f4f6';
      }, theme);
      await trigger.click();
      const panel = page.getByRole('dialog', { name: 'Document audio' });
      await expect(panel).toBeVisible();
      expect(await panel.evaluate(node => getComputedStyle(node).backgroundColor))
        .toBe(theme === 'light' ? 'rgb(255, 255, 255)' : 'rgb(38, 38, 35)');
      expect(await panel.evaluate(node => getComputedStyle(node).color))
        .toBe(theme === 'light' ? 'rgb(17, 24, 39)' : 'rgb(243, 244, 246)');
      const bounds = await panel.boundingBox();
      expect(bounds!.x).toBeGreaterThanOrEqual(16);
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width - 16);
      expect(bounds!.height).toBeLessThan(330);
      expect(await panel.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    });
  }
}

test('signed-in public readers get their account palette and a non-modal, scroll-stable speaker menu', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 844 });
  await page.context().addCookies([{ name: 'cognis_session', value: 'standalone-reader-session', url: origin }]);
  const writes: string[] = [];
  page.on('request', request => { if (request.method() !== 'GET') writes.push(request.url()); });
  await page.route('**/api/v1/user-preferences', route => {
    expect(route.request().headers().cookie).toContain('cognis_session=standalone-reader-session');
    return route.fulfill({ json: { display: { theme: 'light', theme_family: 'paper' } } });
  });
  await page.route('**/api/v1/audio/jobs/source?*', route => {
    expect(route.request().headers().cookie).toContain('cognis_session=standalone-reader-session');
    return route.fulfill({ json: { supported: true, can_generate: true, can_cancel: true, detail: null, jobs: [] } });
  });
  await page.addInitScript(() => localStorage.setItem('cognis-rich-viewer-theme', 'dark'));
  await page.goto(`${origin}/audio-view/markdown`, { waitUntil: 'networkidle' });
  const documentRoot = page.locator('.rich-deliverable');
  await expect(documentRoot).toHaveAttribute('data-theme-family', 'paper');
  await expect(documentRoot).toHaveAttribute('data-viewer-theme', 'light');
  const colors = await documentRoot.evaluate(node => ({
    background: getComputedStyle(node).backgroundColor,
    surface: getComputedStyle(node).getPropertyValue('--color-surface').trim(),
  }));
  expect(colors.surface).toMatch(/^\d+ \d+ \d+$/);
  expect(colors.background).toBe(`rgb(${colors.surface.replace(/ /g, ', ')})`);
  await page.evaluate(() => {
    const filler = document.createElement('div');
    filler.style.height = '2400px';
    document.body.appendChild(filler);
  });
  const geometry = () => page.evaluate(() => ({
    width: document.documentElement.clientWidth, x: scrollX, y: scrollY,
    overflow: document.documentElement.style.overflow,
    documentLeft: document.querySelector('.rich-document')?.getBoundingClientRect().left,
  }));
  const before = await geometry();
  await page.getByRole('button', { name: 'Listen to this document' }).click();
  const panel = page.getByRole('dialog', { name: 'Document audio' });
  await expect(panel).toBeVisible();
  expect(await geometry()).toEqual(before);
  expect(await page.locator('html').getAttribute('data-overlay-scroll-locked')).toBeNull();
  await page.evaluate(() => window.scrollTo(0, 160));
  await expect.poll(() => page.evaluate(() => scrollY)).toBe(160);
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
  expect(await page.evaluate(() => scrollY)).toBe(160);
  expect(writes).toEqual([]);
});

test('shows the latest recording and inline controls with a discoverable playing/paused trigger', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const wav = Buffer.alloc(44 + 16000 * 2 * 30);
  wav.write('RIFF'); wav.writeUInt32LE(wav.length - 8, 4); wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(16000, 24); wav.writeUInt32LE(32000, 28);
  wav.writeUInt16LE(2, 32); wav.writeUInt16LE(16, 34); wav.write('data', 36);
  wav.writeUInt32LE(wav.length - 44, 40);
  await page.route('**/test-recording-*.wav', route => route.fulfill({ contentType: 'audio/wav', body: wav }));
  await page.route('**/api/v1/audio/jobs/source?*', route => route.fulfill({ json: {
    supported: true, can_generate: true, can_cancel: true, detail: null,
    jobs: [1, 3, 2].map(version => ({
      job_id: `browser-recording-${version}`, mode: 'narration', status: 'completed',
      deliverable_id: `dlv_${'1'.repeat(32)}`, completed_parts: 1, total_parts: 1,
      created_at: `2026-01-0${version}T00:00:00Z`, warnings: [], error_code: null,
      script_url: '/transcript.txt', script_artifact_id: null,
      parts: [{ artifact_id: `browser-audio-${version}`, audio_url: `${origin}/test-recording-${version}.wav`, content_type: 'audio/wav' }],
    })),
  } }));
  await page.route('**/api/v1/audio/jobs/estimates?*', route => route.fulfill({ json: [
    { length: 'shorter', target_words: 180, seconds_min: 60, seconds_max: 120 },
    { length: 'default', target_words: 300, seconds_min: 120, seconds_max: 180 },
    { length: 'longer', target_words: 450, seconds_min: 180, seconds_max: 240 },
  ] }));
  const writes: string[] = [];
  page.on('request', request => { if (request.method() !== 'GET') writes.push(request.url()); });
  await page.goto(`${origin}/audio-view/markdown`, { waitUntil: 'networkidle' });
  const trigger = page.getByRole('button', { name: 'Listen to this document' });
  await trigger.click();
  const panel = page.getByRole('dialog', { name: 'Document audio' });
  await expect(panel.locator('audio')).toHaveAttribute('src', `${origin}/test-recording-3.wav`);
  await expect(panel.getByRole('button', { name: 'Back 10 seconds' })).toBeVisible();
  await expect(panel.getByRole('button', { name: 'Forward 10 seconds' })).toBeVisible();
  await expect(panel.getByLabel('Playback speed')).toBeVisible();
  await expect(panel.getByLabel('Recording version')).toHaveCount(0);
  await expect(panel.getByRole('link', { name: /script/i })).toHaveCount(0);
  await panel.getByLabel('Playback speed').selectOption('1.5');
  await expect(panel.locator('audio')).toHaveJSProperty('playbackRate', 1.5);
  await panel.getByRole('button', { name: 'Play audio' }).click();
  await expect(trigger).toHaveAttribute('data-playback', 'playing');
  await page.keyboard.press('Escape');
  await expect(panel).toBeHidden();
  await expect(trigger).toHaveAttribute('title', 'Document audio · playing');
  await trigger.click();
  await panel.getByRole('button', { name: 'Pause audio' }).click();
  await expect(trigger).toHaveAttribute('data-playback', 'paused');
  await expect(panel.getByRole('link', { name: 'Configure narrative and speech models' })).toHaveCount(0);
  await panel.getByRole('tab', { name: 'Podcast' }).click();
  await expect(panel.getByText('~1–2 min')).toBeVisible();
  await expect(panel.getByText('~2–3 min')).toBeVisible();
  await expect(panel.getByText('~3–4 min')).toBeVisible();
  const defaultLength = panel.getByRole('radio', { name: /Default/ });
  await expect(defaultLength).toBeChecked();
  await defaultLength.focus();
  await page.keyboard.press('ArrowRight');
  await expect(panel.getByRole('radio', { name: /Longer/ })).toBeChecked();
  expect(await panel.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
  await page.screenshot({ path: '/tmp/cognis-audio-followup-mobile.png' });
  expect(writes).toEqual([]);
});

test('runs the production module without payload execution or DOM/network breakout', async ({ page }) => {
  const browserRequests: string[] = [];
  const browserWrites: string[] = [];
  const viewerContextPath = '/api/v1/deliverables/dlv_standalone_security/viewer-context';
  page.on('request', (request) => {
    browserRequests.push(request.url());
    if (request.method() !== 'GET') browserWrites.push(request.url());
  });

  await page.goto(`${origin}/view`, { waitUntil: 'networkidle' });

  await expect(page.getByRole('heading', { level: 1, name: /Canonical standalone security report/ }))
    .toBeVisible();
  await expect(page.getByText('Canonical content remains usable')).toBeVisible();
  await expect(page.getByText('canonical markdown', { exact: true })).toBeVisible();
  await expect(page.getByText(/<img src="\/unexpected-image"/).first()).toBeVisible();

  expect(await page.evaluate((marker) => Boolean(
    (globalThis as unknown as Record<string, unknown>)[marker],
  ), executionMarker)).toBe(false);
  expect(await page.locator('template#cognis-deliverable-payload').count()).toBe(1);
  expect(await page.locator('template#cognis-deliverable-payload').evaluate((element) => {
    const template = element as HTMLTemplateElement;
    return (JSON.parse(template.content.textContent ?? '{}') as { payload?: unknown }).payload;
  })).toEqual(payload);
  expect(await page.locator('script').count()).toBe(1);
  expect(await page.locator('script[type="module"][src^="/api/v1/deliverables/standalone-assets/"]').count())
    .toBe(1);
  expect(await page.locator('img[onerror], svg[onload], [onclick], [onload], [onerror]').count()).toBe(0);
  expect(await page.locator('#cognis-deliverable-root > script, #cognis-deliverable-root > template').count())
    .toBe(0);
  expect(await page.locator('img[src="/unexpected-image"], img[src="/encoded-image"]').count()).toBe(0);
  const renderedUrls = await page.locator('[src], [href], [poster], [srcset]').evaluateAll((elements) =>
    elements.flatMap((element) => ['src', 'href', 'poster', 'srcset']
      .map((attribute) => ({ attribute, tag: element.tagName, value: element.getAttribute(attribute) }))
      .filter((entry): entry is { attribute: string; tag: string; value: string } =>
        entry.value !== null)));
  expect(renderedUrls.every(({ attribute, tag, value }) => {
    const normalizedTag = tag.toLowerCase();
    if (attribute === 'srcset') return false;
    if (normalizedTag === 'script' && attribute === 'src') return value.startsWith(ASSET_PREFIX);
    if (normalizedTag === 'link' && attribute === 'href') return value.startsWith(ASSET_PREFIX);
    if (normalizedTag === 'a' && attribute === 'href') {
      const url = new URL(value, origin);
      return url.origin === origin
        && !/javascript:|data:text\/html|attacker\.invalid|standalonePayloadExecuted/i.test(value);
    }
    if (['img', 'audio', 'video', 'source'].includes(normalizedTag)) {
      if (value.startsWith('data:image/')) return true;
      const url = new URL(value, origin);
      return url.origin === origin && url.pathname.startsWith(MEDIA_PREFIX);
    }
    return false;
  }), JSON.stringify(renderedUrls, null, 2)).toBe(true);

  const unexpectedBrowserRequests = browserRequests.filter((url) => {
    const parsed = new URL(url);
    return parsed.origin !== origin
      || (
        parsed.pathname !== '/view'
        && !parsed.pathname.startsWith(ASSET_PREFIX)
        && parsed.pathname !== authorizedMediaPath
        && !(parsed.pathname === viewerContextPath && parsed.search === '')
        && parsed.pathname !== '/api/v1/user-preferences'
      );
  });
  expect(unexpectedBrowserRequests).toEqual([]);
  expect(browserWrites).toEqual([]);
  expect(observedRequests.every(
    (requestPath) =>
      requestPath === '/view'
      || requestPath.startsWith(ASSET_PREFIX)
      // The trusted shell may probe read-only sharing context; never a mutation.
      || requestPath === authorizedMediaPath
      || requestPath === viewerContextPath
      || requestPath === '/api/v1/user-preferences',
  )).toBe(true);
});

test('renders authorized accordion item media once inside the standalone disclosure body', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${origin}/view`, { waitUntil: 'networkidle' });

  const summary = page.getByText('Authorized standalone article').locator('..');
  const details = summary.locator('..');
  const image = page.locator('img[alt="Authorized standalone article media"]');

  await expect(summary).toHaveJSProperty('tagName', 'SUMMARY');
  await expect(details).not.toHaveAttribute('open', '');
  await expect(summary.locator('img')).toHaveCount(0);
  await expect(details.locator('.rich-panel-context img')).toHaveCount(1);
  await expect(image).toHaveCount(1);
  await expect(image).toBeHidden();
  await expect(image).toHaveAttribute('loading', 'lazy');
  await expect(image).toHaveAttribute('decoding', 'async');
  await expect(image).toHaveAttribute('src', `${origin}${authorizedMediaPath}`);
  await expect(page.getByRole('img', { name: 'Missing standalone article media' })).toHaveCount(0);

  await summary.click();

  await expect(details).toHaveAttribute('open', '');
  await expect(image).toBeVisible();
  await expect(page.getByText('Expanded standalone article body.')).toBeVisible();
  await expect(page.getByText('Standalone fixture publisher')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Authorized standalone article media' }))
    .toHaveAttribute('href', `${origin}/image-source`);
  await expect(page.getByRole('link', { name: 'Open source' }))
    .toHaveAttribute('href', `${origin}/article-source`);
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.complete && element.naturalWidth))
    .toBeGreaterThan(0);
  await expect(image).toHaveCount(1);
  const overflow = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    offenders: Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.right > document.documentElement.clientWidth + 1;
      })
      .map((element) => ({
        tag: element.tagName,
        className: typeof element.className === 'string' ? element.className : '',
        left: Math.round(element.getBoundingClientRect().left),
        right: Math.round(element.getBoundingClientRect().right),
        width: Math.round(element.getBoundingClientRect().width),
      }))
      .slice(0, 12),
  }));
  expect(overflow.width, JSON.stringify(overflow.offenders)).toBeLessThanOrEqual(390);
  expect(observedRequests.filter((requestPath) => requestPath === authorizedMediaPath)).toHaveLength(1);
});
