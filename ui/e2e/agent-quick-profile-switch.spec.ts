import { expect, test, type Page } from '@playwright/test';

test.use({ colorScheme: 'dark' });

// Exercise the production chat route against a deterministic HTTP boundary.
// No live agents, provider calls, or user configuration are modified.
async function mockChat(page: Page, toggle = false, many = false, overridden = false) {
  page.on('pageerror', error => console.error(error.stack));
  page.on('console', message => {
    if (message.type() === 'error') console.error(message.text());
  });
  let selected = 'developer';
  let fail = false;
  let running = false;
  let noticeText = '';
  let runtimeRevision = 1;
  const runtimeSelection = () => ({
    revision: runtimeRevision, profile_id: selected, profile_source: 'conversation',
    model: 'gpt-5', provider_id: 'openai',
    model_source: overridden ? 'session_override' : 'agent_profile',
    reasoning_effort: 'medium', reasoning_effort_source: 'agent_profile',
    fast_mode: false, fast_mode_source: overridden ? 'session_override' : 'agent_profile'
  });
  let sendSocket: ((data: string) => void) | undefined;
  const commands: string[] = [];
  const conversation = {
    conversation_id: 'quick-profile-acceptance',
    agent_id: 'quick-profile-agent',
    agent_profile_id: selected,
    title: 'Profile quick switching',
    status: 'active',
    active_session_id: 'quick-profile-session',
    context: { type: 'web', platform_data: {}, memory_labels: {} },
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
  const agent = {
    agent_id: conversation.agent_id, name: 'LaForge', display_name: 'LaForge',
    agent_type: 'primary', status: 'active', description: 'Software engineering',
    default_agent_profile_id: 'developer',
    llm_config: { model: 'gpt-5', provider_id: 'acceptance-provider' },
    capabilities: { memory_backend: 'none', guardrails_backend: 'none' },
    agent_profiles: {
      developer: { profile_id: 'developer', description: 'Everyday implementation' },
      ...(toggle ? { smart: { profile_id: 'smart', quick_switch: true } } : {
        worker: { profile_id: 'worker', quick_switch: true, quick_switch_index: 1, quick_switch_color: 'teal' },
        architect: { profile_id: 'architect', quick_switch: true, quick_switch_index: 2, quick_switch_color: 'violet' },
        critical: { profile_id: 'critical', quick_switch: true, quick_switch_index: 3, quick_switch_color: 'amber' }
      }),
      ...(many ? Object.fromEntries(Array.from({ length: 20 }, (_, index) => [
        `level-${index}`, { profile_id: `level-${index}`, quick_switch: true, quick_switch_index: index + 4 }
      ])) : {})
    }
  };
  const snapshot = () => ({
    schema_version: 2, projection_version: 'quick-profile-acceptance-v1',
    conversation: { ...conversation, agent_profile_id: selected },
    timeline: { items: noticeText ? [{
      id: 'system:command:profile:pending', kind: 'message', role: 'system',
      sort_key: '0000:000000000000001:000000:00:000000000',
      source_refs: [], attachments: [], status: 'complete', partial: false,
      content: noticeText, notice_id: 'command:profile:pending',
      notice_kind: 'command_result', notice_scope: 'session',
      created_at: conversation.updated_at
    }] : [], has_more_before: false, before_cursor: null },
    state: {
      state_version: 1, snapshot_generated_at: conversation.updated_at,
      capabilities: [], active_turn: running ? { turn_id: 'acceptance-turn', status: 'running' } : {}, pending: {}, active_session: {}
    },
    queue: { messages: [], queued_count: 0 },
    runtime: { runtime_epoch: 'quick-profile-runtime', runtime_revision: 1,
      generated_at: conversation.updated_at, has_active_turn: running,
      active_turn: running ? { turn_id: 'acceptance-turn', session_id: conversation.active_session_id, status: 'running' } : undefined,
      volatile_items: [] },
    cursor: 'quick-profile-cursor', server_time: conversation.updated_at
  });
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith('/api/')) {
      await route.continue();
      return;
    }
    let body: unknown = [];
    if (path === '/api/auth/me') body = { email: 'acceptance@cognis.local', name: 'Acceptance', role: 'admin' };
    else if (path === '/api/health') body = { status: 'ok', services: {}, llm_available: true };
    else if (path === '/api/bootstrap-status') body = { has_users: true, needs_bootstrap: false };
    else if (path === '/api/v1/dashboard/navigation-summary') body = {
      chat_groups: [], dashboard: { total: 0, critical: 0, warning: 0, info: 0, truncated: false },
      schedules: 0, tasks: 0
    };
    else if (path === '/api/v1/inbox') body = { items: [], unread_count: 0, next_cursor: null };
    else if (path === '/api/v1/agents') body = [agent];
    else if (path === `/api/v1/agents/${agent.agent_id}`) body = agent;
    else if (path.endsWith('/sidebar')) body = {
      agents: [agent], agent_direct_chats: [],
      conversations: { items: [conversation], next_cursor: null, has_more: false },
      context_types: ['web'],
      background_work: { items: [], truncated: false }
    };
    else if (path === '/api/v1/conversations') body = { items: [conversation], next_cursor: null, has_more: false };
    else if (path === `/api/v1/conversations/${conversation.conversation_id}`) body = { ...conversation, agent_profile_id: selected };
    else if (path.endsWith('/snapshot') || path.endsWith('/snapshot/cache-only')) body = snapshot();
    else if (path.endsWith('/sync')) body = {
      schema_version: 2, projection_version: 'quick-profile-acceptance-v1',
      conversation_id: conversation.conversation_id, cursor_before: 'quick-profile-cursor',
      cursor_after: 'quick-profile-cursor',
      ops: snapshot().timeline.items.map(item => ({ op: 'upsert_item', item })),
      runtime: snapshot().runtime,
      reset_required: false, has_more: false, server_time: conversation.updated_at
    };
    else if (path.includes('/commands/')) {
      const { content } = route.request().postDataJSON();
      commands.push(content);
      if (!fail && content.startsWith('/profile ')) {
        selected = content.slice('/profile '.length);
        noticeText = `Agent profile switched to: ${selected}`;
        overridden = false;
        runtimeRevision++;
      }
      body = {
        conversation_id: conversation.conversation_id, client_txn_id: path.split('/').pop(),
        status: 'completed', result_type: fail ? 'error' : 'system_message',
        text: fail ? 'Profile switch failed. Try again.' : `Agent profile switched to: ${selected}`,
        data: fail ? {} : { resolved_agent_profile_id: selected,
          command: content.startsWith('/profile ') ? '/profile' : '/model',
          notice_id: 'command:profile:pending', runtime_selection: runtimeSelection() },
        server_time: conversation.updated_at
      };
     } else if (path.startsWith('/api/v1/sessions/')) body = {
       session_id: conversation.active_session_id, intaris_session_id: conversation.active_session_id,
       status: 'active', events: [], transcript: [], total_calls: 0,
       runtime_selection: runtimeSelection()
     };
     else if (path.endsWith('/queue')) body = { messages: [], queued_count: 0 };
    else if (path.endsWith('/state')) body = snapshot().state;
    else if (path.endsWith('/user-preferences')) body = {};
    else if (path.includes('/ui-state')) body = {};
    else if (path.endsWith('/activity-overview')) body = {
      schema_version: 2, detail: 'lightweight', projection_version: 'quick-profile-acceptance-v1',
      scope: { key: `conversation:${conversation.conversation_id}`,
        kind: 'conversation', conversation_id: conversation.conversation_id },
      summary: { mutations: 0, commands: 0, changed_files: 0, artifacts: 0, deliverables: 0,
        additions: 0, deletions: 0, omitted_files: 0 },
      materialization: { state: 'live', completed_streams: 0, total_streams: 0,
        covered_events: 0, target_events: 0, failed_streams: 0, retry_after_ms: null },
      workstreams: [], recent: {}, recent_work: {
        commands: [], mutations: [], files: [], artifacts: [], deliverables: []
      },
      graph_fingerprint: 'quick-profile-empty', overview_revision: 'quick-profile-empty',
      work_revision: 0, graph_revision: 0, graph_truncated: false, server_time: conversation.updated_at
    };
    else if (path.includes('/llm/providers')) body = [{ provider_id: 'acceptance-provider', enabled: true }];
    else if (path.includes('/notifications')) body = [];
    await route.fulfill({ json: body });
  });
  let markSubscribed!: () => void;
  const subscribed = new Promise<void>(resolve => { markSubscribed = resolve; });
  await page.routeWebSocket('**/api/ws', ws => {
    sendSocket = data => ws.send(data);
    ws.onMessage(message => {
      if (typeof message === 'string' && JSON.parse(message).type === 'chat_v2_subscribe') markSubscribed();
    });
    ws.send(JSON.stringify({ type: 'authenticated' }));
  });
  return {
    commands, subscribed, setFail: (value: boolean) => { fail = value; },
    setRunning: (value: boolean) => { running = value; },
    emitProfileNotice: (profile: string) => {
      selected = profile;
      noticeText = `Agent profile switched to: ${profile}`;
      sendSocket?.(JSON.stringify({ type: 'system_message',
        conversation_id: conversation.conversation_id, command_result: true,
        command: '/profile', resolved_agent_profile_id: profile,
        text: noticeText, notice_id: 'command:profile:pending' }));
    }
  };
}

test.use({ serviceWorkers: 'block' });

test('full composer settings collapse by composer width and recover after resizing', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const mock = await mockChat(page, false, false, true);
  await page.goto('/chat/quick-profile-acceptance');
  const composer = page.getByTestId('chat-v2-composer');
  const trigger = composer.getByRole('button', { name: 'Chat settings', exact: true });
  const label = trigger.locator('.trigger-label');
  const icon = trigger.locator('.settings-icon');
  await expect(label).toBeVisible();
  await expect(icon).toBeHidden();

  // A narrow conversation pane inside a wide desktop must use its own width.
  await composer.evaluate(node => { node.style.maxWidth = '480px'; });
  await expect(label).toBeHidden();
  await expect(icon).toBeVisible();
  await expect(trigger.locator('.trigger-accent')).toBeHidden();
  await expect(trigger.locator('.trigger-chevron')).toBeHidden();
  await expect(trigger.getByLabel('Profile settings overridden', { exact: true })).toBeVisible();
  expect((await trigger.boundingBox())!.width).toBeLessThan(48);
  await trigger.click();
  await expect(page.getByRole('dialog', { name: 'Chat settings' })).toBeVisible();
  await page.getByRole('radio', { name: 'architect', exact: true }).click();
  await expect(trigger).toHaveAttribute('title', 'Profile: architect');
  expect(mock.commands).toEqual(['/profile architect']);
  await page.keyboard.press('Escape');
  await composer.evaluate(node => { node.style.removeProperty('max-width'); });
  await expect(label).toBeVisible();
  await expect(icon).toBeHidden();

  for (const width of [1000, 800, 600, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    const composerWidth = (await composer.boundingBox())!.width;
    if (composerWidth <= 639) {
      await expect(label).toBeHidden();
      await expect(icon).toBeVisible();
      expect((await trigger.boundingBox())!.width).toBeLessThan(48);
    } else {
      await expect(label).toBeVisible();
      await expect(icon).toBeHidden();
    }
    await expect(trigger).toHaveAttribute('title', 'Profile: architect');
  }
});

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`marks runtime overrides and restores the same profile at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const mock = await mockChat(page, false, false, true);
    await page.goto('/chat/quick-profile-acceptance');
    await expect(page.getByLabel('Profile settings overridden')).toBeVisible();
    await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
    await expect(page.locator('.profile-thumb')).toHaveClass(/overridden/);
    await expect(page.getByText(/Fast mode: off/)).toBeVisible();
    await page.getByRole('radio', { name: 'developer (Default)' }).click();
    await expect(page.getByLabel('Profile settings overridden')).toHaveCount(0);
    expect(mock.commands).toEqual(['/profile developer']);
    await page.reload();
    await expect(page.getByLabel('Profile settings overridden')).toHaveCount(0);
  });
  test(`mutates one persisted profile card and hides settings while running at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const mock = await mockChat(page);
    await page.goto('/chat/quick-profile-acceptance');
    await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
    const dotColors = await page.locator('.profile-dot').evaluateAll(dots =>
      dots.map(dot => getComputedStyle(dot).backgroundColor));
    expect(new Set(dotColors)).toEqual(new Set(['rgb(203, 213, 225)']));
    for (const profile of ['worker', 'critical', 'architect']) {
      await page.getByRole('radio', { name: profile, exact: true }).click();
      await expect(page.getByText(`Agent profile switched to: ${profile}`, { exact: true })).toBeVisible();
      await expect(page.getByText(/^Agent profile switched to:/)).toHaveCount(1);
    }
    await page.keyboard.press('Escape');
    await page.reload();
    await expect(page.getByText('Agent profile switched to: architect', { exact: true })).toBeVisible();
    await expect(page.getByText(/^Agent profile switched to:/)).toHaveCount(1);
    mock.setRunning(true);
    await page.reload();
    await expect(page.getByTestId('chat-v2-composer')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Chat settings', exact: true })).toHaveCount(0);
  });
  test(`switches levels, preserves drafts and reloads at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const mock = await mockChat(page);
    await page.goto('/chat/quick-profile-acceptance');
    const control = page.getByTestId('agent-quick-profile-switcher');
    await expect(control).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`profile-closed-${viewport.width}.png`) });
    const composerHeight = (await page.getByTestId('chat-v2-composer').boundingBox())!.height;
    await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
    const panel = page.getByRole('dialog', { name: 'Chat settings' });
    await expect(panel).toBeVisible();
    const bounds = (await panel.boundingBox())!;
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(viewport.width);
    expect((await page.getByTestId('chat-v2-composer').boundingBox())!.height).toBe(composerHeight);
    await page.keyboard.press('Escape');
    await expect(panel).toBeHidden();
    await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
    if (viewport.width > 639) await page.getByLabel('Chat message').fill('Keep this draft while switching.');
    await control.getByRole('radio', { name: 'architect', exact: true }).click();
    await expect(control.getByRole('radio', { name: 'architect', exact: true })).toHaveAttribute('aria-checked', 'true');
    expect(await page.locator('.profile-rail').evaluate(el => (el as HTMLElement).style.getPropertyValue('--position'))).toBe('2');
    expect(mock.commands).toEqual(['/profile architect']);
    if (viewport.width > 639) await expect(page.getByLabel('Chat message')).toHaveValue('Keep this draft while switching.');
    await page.reload();
    await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
    await expect(control.getByRole('radio', { name: 'architect', exact: true })).toHaveAttribute('aria-checked', 'true');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`profile-levels-${viewport.width}.png`) });
    if (viewport.width < 640) {
      await page.getByLabel('Chat message').fill('Help me review this change.');
      await expect(page.getByRole('button', { name: 'Chat settings', exact: true })).toBeHidden();
      await expect(page.getByRole('dialog', { name: 'Chat settings' })).toBeHidden();
      await page.screenshot({ path: testInfo.outputPath('mobile-typing.png') });
      await page.getByLabel('Chat message').fill('');
      await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
    }
    mock.setFail(true);
    await control.getByRole('radio', { name: 'critical', exact: true }).click();
    await expect(page.getByText('Profile switch failed. Try again.', { exact: true })).toBeVisible();
    await expect(control.getByRole('radio', { name: 'architect', exact: true })).toHaveAttribute('aria-checked', 'true');
    mock.setFail(false);
    await control.getByRole('radio', { name: 'developer (Default)' }).click();
    await expect(control.getByRole('radio', { name: 'developer (Default)' })).toHaveAttribute('aria-checked', 'true');
  });

  test(`toggles smart and restores default at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const mock = await mockChat(page, true);
    await page.goto('/chat/quick-profile-acceptance');
    await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
    const toggle = page.getByRole('switch', { name: 'Use smart profile' });
    await expect(toggle).toBeVisible();
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-checked', 'true');
    await page.screenshot({ path: testInfo.outputPath(`profile-toggle-${viewport.width}.png`) });
    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(mock.commands).toEqual(['/profile smart', '/profile developer']);
  });

  test(`contains many profiles and reaches the last level at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockChat(page, false, true);
    await page.goto('/chat/quick-profile-acceptance');
    const control = page.getByTestId('agent-quick-profile-switcher');
    await expect(control).toBeVisible();
    await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await control.getByRole('radio', { name: 'developer (Default)' }).focus();
    await page.keyboard.press('End');
    await expect(control.getByRole('radio', { name: 'level-19', exact: true })).toHaveAttribute('aria-checked', 'true');
    await expect(control.getByRole('radio', { name: 'level-19', exact: true })).toBeFocused();
  });

  test(`previews swipe and applies only on release at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const mock = await mockChat(page);
    await page.goto('/chat/quick-profile-acceptance');
    await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
    const rail = page.getByRole('radiogroup', { name: 'Agent profile' });
    const box = (await rail.boundingBox())!;
    const y = box.y + box.height / 2;
    await page.mouse.move(box.x + box.width / 8, y);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 7 / 8, y, { steps: 12 });
    expect(mock.commands).toEqual([]);
    await expect(page.locator('.profile-value')).toHaveText('critical');
    await expect(page.getByRole('radio', { name: 'developer (Default)' })).toHaveAttribute('aria-checked', 'true');
    await page.mouse.up();
    await expect(page.getByRole('radio', { name: 'critical', exact: true })).toHaveAttribute('aria-checked', 'true');
    expect(mock.commands).toEqual(['/profile critical']);
    await page.screenshot({ path: testInfo.outputPath(`colored-critical-${viewport.width}.png`) });
    await page.mouse.move(box.x + box.width * 7 / 8, y);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 8, y, { steps: 12 });
    await rail.dispatchEvent('pointercancel', { pointerId: 1 });
    await page.mouse.up();
    expect(mock.commands).toEqual(['/profile critical']);
    await expect(page.getByRole('radio', { name: 'critical', exact: true })).toHaveAttribute('aria-checked', 'true');
  });
}

test('WebSocket profile results refresh one canonical card instead of appending local cards', async ({ page }) => {
  const mock = await mockChat(page);
  await page.goto('/chat/quick-profile-acceptance');
  await expect(page.getByTestId('chat-v2-composer')).toBeVisible();
  // Emit each result exactly once, only after the client has subscribed.
  await mock.subscribed;
  for (const profile of ['critical', 'worker', 'architect']) {
    mock.emitProfileNotice(profile);
    await expect(page.getByText(`Agent profile switched to: ${profile}`, { exact: true })).toBeVisible();
    await expect(page.getByText(/^Agent profile switched to:/)).toHaveCount(1);
  }
});

test('desktop click glides the existing thumb across stops and honors reduced motion', async ({ page }) => {
  await mockChat(page);
  let release!: () => void;
  await page.route('**/commands/**', async route => {
    await new Promise<void>(resolve => { release = resolve; });
    await route.fallback();
  });
  await page.goto('/chat/quick-profile-acceptance');
  await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
  const thumb = page.locator('.profile-thumb');
  const readX = () => thumb.evaluate(element =>
    new DOMMatrix(getComputedStyle(element).transform).m41);
  const initial = await readX();
  const railWidth = (await page.getByRole('radiogroup').boundingBox())!.width;
  const finalX = railWidth * 3 / 4;
  await thumb.evaluate(element => {
    element.addEventListener('transitionrun', () => {
      for (const animation of element.getAnimations()) {
        animation.pause();
        animation.currentTime = 110;
      }
    }, { once: true });
  });
  await page.getByRole('radio', { name: 'critical', exact: true }).click();
  await expect.poll(readX).toBeGreaterThan(initial);
  const intermediate = await readX();
  expect(intermediate).toBeGreaterThan(initial);
  expect(intermediate).toBeLessThan(finalX);
  await thumb.evaluate(element => element.getAnimations().forEach(animation => animation.play()));
  release();
  await expect(page.getByRole('radio', { name: 'critical', exact: true })).toHaveAttribute('aria-checked', 'true');
  await expect.poll(() => thumb.evaluate(element => element.getAnimations().length)).toBe(0);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  expect(await thumb.evaluate(element => getComputedStyle(element).transitionDuration)).toBe('0s');
});

test('touch swipes preview without applying, and overflow controls reach the final profile', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  const page = await context.newPage();
  try {
    const mock = await mockChat(page);
    await page.goto('/chat/quick-profile-acceptance');
    await page.getByRole('button', { name: 'Chat settings', exact: true }).tap();
    const rail = page.getByRole('radiogroup');
    const rect = (await rail.boundingBox())!;
    const session = await context.newCDPSession(page);
    const y = rect.y + rect.height / 2;
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart',
      touchPoints: [{ x: rect.x + rect.width / 8, y }] });
    await session.send('Input.dispatchTouchEvent', { type: 'touchMove',
      touchPoints: [{ x: rect.x + rect.width * 7 / 8, y }] });
    await expect(page.locator('.profile-value')).toHaveText('critical');
    expect(mock.commands).toEqual([]);
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await expect(page.getByRole('radio', { name: 'critical', exact: true })).toHaveAttribute('aria-checked', 'true');
    expect(mock.commands).toEqual(['/profile critical']);
    await page.unrouteAll({ behavior: 'wait' });
    await mockChat(page, false, true);
    await page.reload();
    // A tap during post-reload bootstrap can land before the route settles.
    await page.waitForLoadState('networkidle');
    const dialog = page.getByRole('dialog', { name: 'Chat settings' });
    await expect(async () => {
      if (!(await dialog.isVisible())) await page.getByRole('button', { name: 'Chat settings', exact: true }).tap();
      await expect(dialog).toBeVisible({ timeout: 1000 });
    }).toPass({ timeout: 10000 });
    for (let index = 0; index < 6; index++) await page.getByRole('button', { name: 'Scroll profiles right' }).tap();
    await page.getByRole('radio', { name: 'level-19', exact: true }).tap();
    await expect(page.getByRole('radio', { name: 'level-19', exact: true })).toHaveAttribute('aria-checked', 'true');
  } finally {
    await context.close();
  }
});

test('serializes quick switching against composer command admission in both directions', async ({ page }) => {
  await mockChat(page);
  let release!: () => void;
  let observed!: () => void;
  let held = new Promise<void>(resolve => { release = resolve; });
  let started = new Promise<void>(resolve => { observed = resolve; });
  await page.route('**/commands/**', async route => {
    observed();
    await held;
    await route.fallback();
  });
  await page.goto('/chat/quick-profile-acceptance');
  const control = page.getByTestId('agent-quick-profile-switcher');
  const composer = page.getByLabel('Chat message');
  await expect(control).toBeVisible();
  await page.getByRole('button', { name: 'Chat settings', exact: true }).click();
  await composer.fill('/model gpt-5');
  await composer.press('Enter');
  await started;
  await expect(control.getByRole('radio', { name: 'architect', exact: true })).toBeDisabled();
  release();
  await expect(control.getByRole('radio', { name: 'architect', exact: true })).toBeEnabled();
  held = new Promise<void>(resolve => { release = resolve; });
  started = new Promise<void>(resolve => { observed = resolve; });
  await composer.fill('Draft to preserve');
  await control.getByRole('radio', { name: 'architect', exact: true }).click();
  await started;
  await composer.press('Enter');
  await expect(composer).toHaveValue('Draft to preserve');
  release();
  await expect(control.getByRole('radio', { name: 'architect', exact: true })).toHaveAttribute('aria-checked', 'true');
});
