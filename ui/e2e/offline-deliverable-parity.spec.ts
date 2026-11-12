import { execFileSync } from 'node:child_process';
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { expect, test } from '@playwright/test';

// Uses the actual production bundles, not the dev server or mocked components.
// Run npm run build:standalone before this test.
const renderScript = String.raw`
import json, sys, os
from pathlib import Path
from bs4 import BeautifulSoup
from types import SimpleNamespace
from cognis.rendering.deliverables import render_standalone_shell
from cognis.rendering.offline_deliverables import render_offline_html
from cognis.rendering.rich_visuals import ResolvedMedia
media = {}
payload = json.load(sys.stdin)
row = SimpleNamespace(deliverable_id="dlv_parity", title="Export parity",
    format="rich", rich_payload=payload, content="Fallback")
if os.environ.get("COGNIS_PARITY_SOURCE_HTML"):
    soup = BeautifulSoup(Path(os.environ["COGNIS_PARITY_SOURCE_HTML"]).read_text(), "html.parser")
    data = json.loads(soup.select_one("#cognis-deliverable-payload").get_text())
    payload = data["payload"]
    row = SimpleNamespace(deliverable_id=data["instanceId"], title=data["title"],
        format="rich", rich_payload=payload, content=data["content"])
    if payload.get("media_manifest"):
        import base64, httpx
        from urllib.parse import urljoin
        base = urljoin(os.environ["COGNIS_PARITY_URL"],
            soup.select_one("template")["data-media-base"])
        with httpx.Client(timeout=30) as client:
            for key in payload["media_manifest"]:
                response = client.get(base + "/" + key)
                response.raise_for_status()
                mime = response.headers["content-type"].split(";")[0]
                media[key] = ResolvedMedia(src="data:" + mime + ";base64,"
                    + base64.b64encode(response.content).decode(), mime_type=mime)
else:
    import base64
    from io import BytesIO
    from PIL import Image
    image = BytesIO()
    Image.new("RGB", (96, 64), color="#159fef").save(image, format="PNG")
    key = "media_0123456789abcdef01234567"
    media[key] = ResolvedMedia(src="data:image/png;base64,"
        + base64.b64encode(image.getvalue()).decode(), mime_type="image/png")
    payload["media_manifest"] = {key: {"mime_type": "image/png"}}
    payload["blocks"].append({"type": "figure", "title": "Authorized image",
        "media": {"key": key, "alt": "Publication evidence"}})
def diagrams(blocks):
    return sum((1 if block.get("type") == "mermaid" else 0)
        + diagrams(block.get("blocks") or block.get("children") or []) for block in blocks)
print(json.dumps({
    "diagrams": diagrams(payload["blocks"]),
    "media": {key: item.src for key, item in media.items()},
    "hosted": render_standalone_shell(row, media_base="/fixture-media", standalone_url="", pdf_url=""),
    "offline": render_offline_html(row, payload=payload, media_resolver=lambda ref, _: media.get(ref.ref_id)),
}))
`;

const payload = {
  metadata: { toc: { enabled: true } },
  blocks: [
    { type: 'hero', title: 'Export parity', subtitle: 'Diagrams, tables and Markdown remain identical offline.' },
    { type: 'mermaid', title: 'Investigation', source: 'flowchart LR\nAlertmanager --> Intake --> Investigation --> RCA' },
    { type: 'table', title: 'Projects', columns: ['Project', 'Intake', 'Harness', 'Remediation', 'Surfaces', 'License'], rows: [
      ['HolmesGPT', 'Alertmanager CLI', 'LiteLLM tool-calling loop', 'GitHub toolset', 'CLI / Slack', 'Apache-2.0'],
      ['Robusta', 'Native webhook', 'Deterministic playbooks', 'Approved playbooks', 'Slack / UI', 'MIT'],
    ] },
    { type: 'markdown', content: '## Markdown\n\n**Evidence** and [references](https://cognis.fpy.cz).\n\n| Service | Status |\n| --- | --- |\n| Alertmanager | Healthy |\n\n```mermaid\nflowchart TD\nMetrics --> Evidence\n```' },
    { type: 'code', language: 'python', content: 'def investigate():\n    return "evidence"' },
    { type: 'link_preview', url: 'https://cognis.fpy.cz/research' },
    { type: 'card', title: 'Read more', content: 'Reference material', href: 'https://cognis.fpy.cz/docs' },
    { type: 'source_list', sources: [{ title: 'Research source', url: 'https://cognis.fpy.cz/evidence' }] },
    { type: 'chart', title: 'Alert volume', spec_version: 'cognis.chart.v1', chart_type: 'line',
      series: [{ id: 'alerts', label: 'Alerts', points: [{ x: 'Monday', y: 5 }, { x: 'Tuesday', y: 3 }, { x: 'Wednesday', y: 1 }] }] },
    { type: 'section', title: 'Conclusion', content: 'Keep the real content, not the application controls.' },
  ],
};

const fixture = JSON.parse(execFileSync('uv', ['run', 'python', '-c', renderScript], {
  cwd: '..', encoding: 'utf8', input: JSON.stringify(payload), maxBuffer: 40 * 1024 * 1024,
})) as { hosted: string; offline: string; diagrams: number; media: Record<string, string> };

for (const width of [390, 1440]) {
  test(`offline file matches hosted content at ${width}px without network access`, async ({ browser }, testInfo) => {
    const directory = mkdtempSync(path.join(tmpdir(), 'cognis-export-parity-'));
    const filename = path.join(directory, 'publication.html');
    writeFileSync(filename, fixture.offline);
    const context = await browser.newContext({ viewport: { width, height: 900 }, colorScheme: 'dark' });
    try {
      const hosted = await context.newPage();
      await hosted.route('https://publication.test/**', route => {
        const mediaKey = new URL(route.request().url()).pathname.split('/fixture-media/')[1];
        if (mediaKey) {
          const src = fixture.media[mediaKey];
          return route.fulfill({ body: Buffer.from(src.split(',')[1], 'base64'), contentType: 'image/png' });
        }
        const asset = new URL(route.request().url()).pathname.split('/standalone-assets/')[1];
        return route.fulfill(asset ? {
          body: readFileSync(path.join('standalone-build', asset)),
          contentType: asset.endsWith('.css') ? 'text/css' : asset.endsWith('.woff2') ? 'font/woff2' : 'application/javascript',
        } : { body: fixture.hosted, contentType: 'text/html' });
      });
      await hosted.goto(process.env.COGNIS_PARITY_URL ?? 'https://publication.test/view');
      await expect(hosted.locator('.rich-mermaid svg')).toHaveCount(fixture.diagrams);
      await hosted.evaluate(() => document.fonts.ready);
      await hosted.locator('[data-testid="rich-deliverable-body"] img').evaluateAll(images =>
        Promise.all(images.map(image => {
          (image as HTMLImageElement).loading = 'eager';
          return (image as HTMLImageElement).decode();
        })));
      const offline = await context.newPage();
      const requests: string[] = [];
      const errors: string[] = [];
      offline.on('pageerror', error => errors.push(error.message));
      await offline.route('**/*', route => {
        if (route.request().url().startsWith('file:')) return route.continue();
        requests.push(route.request().url());
        return route.abort();
      });
      await context.setOffline(true);
      await offline.goto(pathToFileURL(filename).href);
      await expect(offline.locator('.rich-mermaid svg')).toHaveCount(fixture.diagrams);
      await offline.evaluate(() => document.fonts.ready);
      await offline.locator('[data-testid="rich-deliverable-body"] img').evaluateAll(images =>
        Promise.all(images.map(image => {
          (image as HTMLImageElement).loading = 'eager';
          return (image as HTMLImageElement).decode();
        })));
      await expect(offline.getByRole('button', { name: 'Copy document' })).toHaveCount(0);
      await expect(offline.getByRole('button', { name: /Theme:/ })).toHaveCount(0);
      // Compare text, all table cell contents, geometry, computed typography,
      // and pixels through the same real content boundary, excluding chrome.
      const body = '[data-testid="rich-deliverable-body"]';
      const inspect = (element: Element) => ({
        text: (element as HTMLElement).innerText,
        cells: [...element.querySelectorAll('th,td')].map(cell => cell.textContent),
        links: [...element.querySelectorAll('a')].map(link => link.getAttribute('href')),
        width: element.getBoundingClientRect().width,
        height: element.getBoundingClientRect().height,
        typography: [...element.querySelectorAll('h1,h2,p,td')].map(node => {
          const style = getComputedStyle(node);
          return [style.fontFamily, style.fontSize, style.lineHeight, style.color];
        }),
      });
      expect(await offline.locator(body).evaluate(inspect)).toEqual(await hosted.locator(body).evaluate(inspect));
      expect(await offline.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
      const hostedPixels = await hosted.locator(body).screenshot({ animations: 'disabled' });
      const offlinePixels = await offline.locator(body).screenshot({ animations: 'disabled' });
      await testInfo.attach('hosted-content.png', { body: hostedPixels, contentType: 'image/png' });
      await testInfo.attach('offline-content.png', { body: offlinePixels, contentType: 'image/png' });
      expect(offlinePixels.equals(hostedPixels)).toBe(true);
      if (!process.env.COGNIS_PARITY_URL) {
        await offline.getByAltText('Publication evidence', { exact: true }).click();
        await expect(offline.getByRole('dialog')).toBeVisible();
        await offline.keyboard.press('Escape');
        await expect(offline.getByRole('dialog')).toHaveCount(0);
      }
      expect(requests).toEqual([]);
      expect(errors).toEqual([]);
    } finally {
      await context.close();
      rmSync(directory, { recursive: true });
    }
  });
}
