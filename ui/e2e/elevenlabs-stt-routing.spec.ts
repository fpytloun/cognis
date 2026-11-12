import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({ bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1' });

for (const width of [390, 1440]) {
  test(`ElevenLabs STT models remain selectable after save/reload at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const fixture = await installTaskCockpitFixture(page);
    for (const path of ['/api/v1/secrets', '/api/v1/settings', '/api/v1/auth/api-keys',
      '/api/v1/executors', '/api/v1/local-model-deployments']) {
      await page.route(`**${path}`, route => route.fulfill({ json: [] }));
    }
    await page.route('**/api/v1/system/diagnostics', route => route.fulfill({ json: { checks: [], items: [] } }));
    await page.route('**/api/auth/me', route => route.fulfill({
      json: { email: 'owner@cognis.test', name: 'Owner', role: 'admin' }
    }));
    await page.route(/\/api\/v1\/inbox(?:\?.*)?$/, route => route.request().method() !== 'GET'
      ? route.fallback() : route.fulfill({ json: { items: [], unread_count: 0, next_cursor: null } }));
    await page.route('**/api/v1/dashboard/navigation-summary', route => route.request().method() !== 'GET'
      ? route.fallback() : route.fulfill({ json: {
        chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
        schedules: 0, tasks: 0
      } }));
    await page.route('**/api/v1/inference-routes', route => route.request().method() !== 'GET'
      ? route.fallback() : route.fulfill({ json: { routes: [] } }));
    await page.route('**/api/v1/provider-monitor/elevenlabs', route => route.request().method() !== 'GET'
      ? route.fallback() : route.fulfill({ json: {
        provider_id: 'elevenlabs', display_name: 'ElevenLabs',
        monitoring: { enabled: false, interval_minutes: 15, warning_percent: 20, critical_percent: 0 },
        health_status: 'unknown', health_detail: 'Not checked in browser fixture', health_checked_at: null,
        checked_at: null, next_refresh_at: null, usage: null, error: null, alert_severity: null, stale: false
      } }));
    const modelIds = ['scribe_v1', 'scribe_v2', 'scribe_v2_realtime'];
    const discovered = modelIds.map(model_id => ({
      model_id, display_name: `ElevenLabs ${model_id}`, model_mode: 'audio_transcription',
      supports_transcription: true, supports_speech_synthesis: false, supports_tools: false,
      supports_streaming: model_id === 'scribe_v2_realtime',
      provider_metadata: { field_sources: {
        display_name: 'provider', model_mode: 'provider', supports_transcription: 'provider',
        supports_speech_synthesis: 'provider', supports_tools: 'provider', supports_streaming: 'provider'
      } }
    }));
    let configuredModels: Record<string, unknown>[] = [
      { model_id: 'eleven_multilingual_v2', model_mode: 'audio_speech', supports_speech_synthesis: true },
      { model_id: 'chat-model', model_mode: 'chat' }
    ];
    let providerConfig: Record<string, unknown> = {
      preset: 'elevenlabs', scope: 'system', base_url: 'https://api.elevenlabs.io',
      auth_config: { mode: 'env', env_var: 'ELEVENLABS_API_KEY' }, models: configuredModels
    };
    const provider = () => ({
        provider_id: 'elevenlabs', display_name: 'ElevenLabs', status: 'active',
        owner_email: 'system@cognis.local', location: 'controller', backend: 'litellm',
        config: providerConfig, models: configuredModels, is_default: false,
        created_at: null, updated_at: null, last_test: null,
    });
    await page.route('**/api/v1/llm-providers*', route => route.fulfill({ json: {
      items: [provider()], has_more: false, next_cursor: null
    } }));
    let providerSaves = 0;
    await page.route('**/api/v1/llm-providers/elevenlabs', async route => {
      if (route.request().method() === 'PUT') {
        providerConfig = route.request().postDataJSON().config;
        configuredModels = providerConfig.models as Record<string, unknown>[];
        for (const modelId of modelIds) {
          expect(configuredModels.find(model => model.model_id === modelId)).toMatchObject({
            supports_transcription: true, supports_speech_synthesis: false, model_mode: 'audio_transcription'
          });
        }
        providerSaves++;
      }
      await route.fulfill({ json: provider() });
    });
    await page.route('**/api/v1/llm-providers/elevenlabs/discover-models', route => route.fulfill({
      json: { provider_id: 'elevenlabs', models: discovered }
    }));
    let saved = Object.fromEntries([
      'default', 'classifier', 'auxiliary_generation', 'compaction', 'evaluator', 'audio_narrative',
      'speech_to_text', 'text_to_speech', 'image_generation', 'video_generation', 'video_editing',
      'video_analysis', 'attachment_analysis', 'embedding'
    ].map(key => [key, { model: null as string | null, reasoning_effort: null as string | null }]));
    let saves = 0;
    await page.route('**/api/v1/model-routing', async route => {
      if (route.request().method() === 'PUT') {
        saved = route.request().postDataJSON();
        saves++;
      }
      await route.fulfill({ json: saved });
    });
    await page.goto('/settings?tab=providers');
    await page.getByRole('button', { name: /ElevenLabs/ }).click();
    await page.getByRole('button', { name: 'Discover', exact: true }).click();
    const discovery = page.getByRole('dialog', { name: 'Add discovered models' });
    await expect(discovery).toBeVisible();
    for (const modelId of modelIds) {
      await discovery.getByRole('button', { name: new RegExp(`^${modelId}\\s`) }).click();
    }
    await discovery.getByRole('button', { name: 'Add selected', exact: true }).click();
    await page.getByRole('button', { name: 'Save provider', exact: true }).click();
    await expect.poll(() => providerSaves).toBe(1);
    await page.reload();
    await page.goto('/settings?tab=routing');
    const stt = page.getByText('speech_to_text', { exact: true }).locator('..').locator('select');
    const tts = page.getByText('Speech / voice (text_to_speech)', { exact: true }).locator('..').locator('select');
    const chat = page.getByText('default', { exact: true }).locator('..').locator('select');
    await expect(stt).toBeVisible();
    for (const modelId of modelIds) {
      await expect(stt.locator(`option[value="${modelId}"]`)).toHaveCount(1);
      await expect(tts.locator(`option[value="${modelId}"]`)).toHaveCount(0);
      await expect(chat.locator(`option[value="${modelId}"]`)).toHaveCount(0);
    }
    await expect(stt.locator('option[value="eleven_multilingual_v2"]')).toHaveCount(0);
    for (const [index, modelId] of modelIds.entries()) {
      await stt.selectOption(modelId);
      await page.getByRole('button', { name: 'Save routing', exact: true }).click();
      await expect.poll(() => saves).toBe(index + 1);
      expect(saved.speech_to_text).toEqual({ model: modelId, reasoning_effort: null });
      await page.reload();
      await expect(stt).toHaveValue(modelId);
    }
    expect(fixture.unmockedRequests()).toEqual([]);
    await page.screenshot({ path: test.info().outputPath(`elevenlabs-stt-routing-${width}.png`), fullPage: true });
  });
}
