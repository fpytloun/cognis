import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({ bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1' });

for (const width of [390, 1440]) {
  test(`companion model and effort save/reload at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await installTaskCockpitFixture(page);
    for (const path of ['/api/v1/secrets', '/api/v1/settings', '/api/v1/auth/api-keys']) {
      await page.route(`**${path}`, (route) => route.fulfill({ json: [] }));
    }
    await page.route('**/api/v1/system/diagnostics', (route) => route.fulfill({ json: { checks: [], items: [] } }));
    await page.route('**/api/auth/me', (route) => route.fulfill({
      json: { email: 'owner@cognis.test', name: 'Owner', role: 'admin' }
    }));
    await page.route('**/api/v1/llm-providers*', (route) => route.fulfill({ json: {
      items: [{
        provider_id: 'shared', display_name: 'Shared provider', status: 'active',
        owner_email: 'system@cognis.local', location: 'controller', backend: 'litellm',
        config: { preset: 'openai_compatible' },
        models: [
          { model_id: 'openai/gpt-oss-20b', display_name: 'Lite', reasoning_efforts: ['none', 'low', 'medium', 'high'] },
          { model_id: 'openai/gpt-oss-120b', display_name: 'Smart', reasoning_efforts: ['none', 'low', 'medium', 'high'] },
          { model_id: 'jev/decision', display_name: 'Jev', model_mode: 'systemone' },
          { model_id: 'text-embedding-3-small', supports_embedding: true }
        ]
      }], has_more: false, next_cursor: null
    } }));
    let saved: Record<string, { model: string | null; reasoning_effort: string | null }> = {
      ...Object.fromEntries([
        'default', 'classifier', 'auxiliary_generation', 'compaction', 'evaluator', 'speech_to_text',
        'text_to_speech', 'image_generation', 'attachment_analysis', 'embedding'
      ].map((key) => [key, { model: null, reasoning_effort: null }])),
      companion_lite: { model: 'shared/openai/gpt-oss-20b', reasoning_effort: null },
      companion_smart: { model: 'shared/openai/gpt-oss-120b', reasoning_effort: null },
      companion_embedding: { model: 'shared/text-embedding-3-small', reasoning_effort: null }
    };
    let saves = 0;
    await page.route('**/api/v1/model-routing', async (route) => {
      if (route.request().method() === 'PUT') {
        saved = route.request().postDataJSON();
        saves++;
      }
      await route.fulfill({ json: saved });
    });
    await page.goto('/settings?tab=routing');
    const classifier = page.getByText('classifier', { exact: true }).locator('..').locator('select');
    const auxiliary = page.getByText('auxiliary_generation', { exact: true }).locator('..').locator('select');
    await expect(classifier.locator('option[value="jev/decision"]')).toHaveCount(1);
    await expect(auxiliary.locator('option[value="jev/decision"]')).toHaveCount(0);
    await classifier.selectOption('jev/decision');
    await auxiliary.selectOption('openai/gpt-oss-20b');
    const heading = page.getByRole('heading', { name: 'Intaris and Mnemory' });
    await expect(heading).toBeVisible();
    const lite = page.getByRole('combobox', { name: 'Lite model thinking effort' });
    await lite.selectOption('low');
    await page.getByRole('combobox', { name: 'Smart model thinking effort' }).selectOption('high');
    await page.getByText('Advanced task overrides').click();
    const task = page.getByRole('combobox', { name: 'Intaris safety evaluation thinking effort' });
    await task.selectOption('medium');
    await page.getByRole('button', { name: 'Save routing', exact: true }).click();
    await expect.poll(() => saves).toBe(1);
    expect(saved.intaris_evaluation).toEqual({ model: null, reasoning_effort: 'medium' });
    expect(saved.companion_lite.reasoning_effort).toBe('low');
    expect(saved.classifier.model).toBe('jev/decision');
    expect(saved.auxiliary_generation.model).toBe('openai/gpt-oss-20b');
    await expect(lite).toHaveValue('low');
    await page.reload();
    await expect(classifier).toHaveValue('jev/decision');
    await expect(auxiliary).toHaveValue('openai/gpt-oss-20b');
    await expect(lite).toHaveValue('low');
    await page.getByText('Advanced task overrides').click();
    await expect(task).toHaveValue('medium');
    await page.getByRole('combobox', { name: /Lite model(?! thinking)/ })
      .selectOption('shared/openai/gpt-oss-120b');
    await expect(lite).toHaveValue('low');
    await page.getByRole('button', { name: 'Save routing', exact: true }).click();
    await expect.poll(() => saves).toBe(2);
    expect(saved.companion_lite).toEqual({
      model: 'shared/openai/gpt-oss-120b', reasoning_effort: 'low'
    });
  });
}
