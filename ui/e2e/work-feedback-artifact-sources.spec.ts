import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { test, expect } from '@playwright/test';
import { login } from './helpers';

test('creates exact HTML, image, PDF, text and code sources for feedback review', async ({
  page, context
}, testInfo) => {
  await login(page);
  const html = await readFile(resolve(
    testInfo.config.rootDir, 'fixtures/work-feedback-wireframe.html'
  ), 'utf8');
  const preview = await context.newPage();
  await preview.setViewportSize({ width: 1120, height: 820 });
  await preview.setContent(html);
  const png = await preview.locator('#wireframe').screenshot({ type: 'png' });
  await preview.setContent(`<!doctype html><html lang="en"><head><style>
    @page { size: A4; margin: 24mm; }
    body { font: 15px/1.6 Arial, sans-serif; color: #17212f; }
    h1 { font-size: 28px; } h2 { margin-top: 28px; font-size: 20px; }
    .fixture { color: #687586; font-size: 12px; }
    .next-page { break-before: page; }
  </style></head><body>
    <p class="fixture">Synthetic Work Feedback acceptance fixture</p>
    <h1>Weekly infrastructure review</h1>
    <p>Review outcomes and choose the next action. This is test content.</p>
    <h2>Completed</h2><p>Staging backup verification passed in the isolated test environment.</p>
    <h2>Needs human review</h2>
    <p>The staging-cluster upgrade is waiting for a reviewed plan. Production is unchanged.</p>
    <p>A comment can ask for a rollback plan, request a follow-up, or clarify an outcome.
    It does not bypass an approval gate.</p>
    <section class="next-page"><h1>Review checklist</h1>
      <p>Confirm the staging scope, backup evidence, and rollback procedure.</p>
      <p>Include only the feedback entries you intend to send.</p>
      <p>Inspect the resulting revision and work receipt before resolving a thread.</p>
    </section>
  </body></html>`);
  const pdf = await preview.pdf({ format: 'A4', printBackground: true });
  await preview.close();

  const sources: Record<string, unknown>[] = [];
  for (const file of [
    { name: 'atlas-wireframe.png', mimeType: 'image/png', buffer: png },
    { name: 'weekly-infrastructure-review.pdf', mimeType: 'application/pdf', buffer: pdf }
  ]) {
    const response = await page.request.post('/api/v1/library/artifacts/upload', {
      multipart: { file, agent_ids: JSON.stringify(['e2e-test-agent']) }
    });
    expect(response.ok(), `Upload ${file.name}: HTTP ${response.status()}`).toBeTruthy();
    const source = await response.json();
    expect(source.kind).toBe('artifact');
    sources.push(source);
  }
  for (const source of [
    { filename: 'atlas-wireframe.html', mime_type: 'text/html', content: html },
    {
      filename: 'release-review.txt',
      mime_type: 'text/plain',
      content: [
        'Release review — synthetic fixture',
        '',
        '🚀 Prepare the staging deployment plan.',
        'Keep the current production deployment unchanged.',
        'Confirm backup evidence and document the rollback command.',
        '',
        'Select this paragraph to request clearer acceptance criteria.'
      ].join('\r\n')
    },
    {
      filename: 'deployment-guard.py',
      mime_type: 'text/x-python',
      content: [
        '"""Synthetic staging-only deployment guard; not executed."""',
        '',
        'def deployment_allowed(environment: str) -> bool:',
        '    return environment == "staging"',
        '',
      ].join('\r\n')
    }
  ]) {
    const response = await page.request.post('/api/v1/library/artifacts', {
      data: { ...source, agent_ids: ['e2e-test-agent'] }
    });
    expect(response.ok(), `Create ${source.filename}: HTTP ${response.status()}`).toBeTruthy();
    sources.push(await response.json());
  }
  expect(sources).toHaveLength(5);
  const directory = resolve(testInfo.config.rootDir, '../review-artifacts/work-feedback');
  await mkdir(directory, { recursive: true });
  await writeFile(resolve(directory, 'artifact-sources.json'), JSON.stringify(sources, null, 2));
  await testInfo.attach('artifact-sources', {
    body: JSON.stringify(sources, null, 2),
    contentType: 'application/json'
  });
});
