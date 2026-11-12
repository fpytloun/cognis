import { expect, test } from '@playwright/test';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

test.use({
  serviceWorkers: 'block',
  // Static preview lacks controller-generated CSP hashes; this is a mocked UI test.
  bypassCSP: process.env.COGNIS_E2E_STATIC_PREVIEW === '1',
});

for (const shared of [false, true]) {
 for (const mobile of [false, true]) {
  test(`managed app contact uses normal chat without recipient execution (shared=${shared}, mobile=${mobile})`, async ({ page }) => {
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1360, height: 900 });
    await installTaskCockpitFixture(page);
    const agent = {
      agent_id: 'incoming-agent', name: 'Incoming agent', display_name: 'Incoming agent',
      owner_email: 'sender@cognis.test', status: 'active', agent_type: 'primary',
      is_contact_only: !shared, contact_conversation_id: 'app-recipient-chat',
    };
    const conversation = {
      conversation_id: 'app-recipient-chat', agent_id: agent.agent_id,
      user_email: 'owner@cognis.test', context: {
        type: 'web', ref: 'app-channel-binding', platform_data: {
          app_contact_binding_id: 'app-channel-binding',
          sender_email: 'sender@cognis.test', agent_name: agent.name,
        },
      },
      title: 'Schedule a review', status: 'active', active_session_id: null,
      is_readonly_for_caller: false, project_assignable: false,
      agent_display_name: agent.name, visibility: 'private',
      created_at: '2026-10-28T11:00:00Z', updated_at: '2026-10-28T11:00:00Z',
      has_active_turn: false, has_unread: true,
      last_message_at: '2026-10-28T11:00:00Z', last_read_at: null as string | null,
    };
    let contact = {
      id: 'app-channel-binding', conversation_id: conversation.conversation_id,
      agent_name: agent.name, sender_email: agent.owner_email,
      title: conversation.title, state: 'waiting_external', expires_at: '2036-10-28T11:00:00Z',
      can_reply: true, blocked: false, main_conversation_id: null,
      messages: [{
        id: 'app-delivery', direction: 'incoming', content: 'Would Tuesday work for a review?',
        occurred_at: '2026-10-28T11:00:00Z', attachments: [],
      }],
    };
    const replies: { client_message_id: string; content: string }[] = [];
    let privateSnapshots = 0;
    let reads = 0;
    await page.routeWebSocket('**/api/ws', socket => {
      socket.send(JSON.stringify({ type: 'authenticated' }));
      socket.onMessage(message => {
        if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' }));
      });
    });
    await page.route('**/api/auth/me', route => route.fulfill({ json: { email: 'owner@cognis.test', role: 'user', name: 'Owner' } }));
    await page.route('**/api/health**', route => route.fulfill({ json: { status: 'ok', providers: {} } }));
    await page.route('**/api/v1/user-preferences', route => route.fulfill({ json: { display: { theme: 'dark', language: 'en' } } }));
    await page.route('**/api/v1/agents**', route => route.fulfill({ json: { items: [agent], cursor: null, has_more: false } }));
    await page.route('**/api/v1/conversations/sidebar**', route => route.fulfill({ json: {
      agents: [agent], agent_direct_chats: [{ agent, conversation }], context_types: ['web'],
      conversations: { items: [conversation], cursor: null, has_more: false },
      active_conversation_ids: [], sidebar_revision: 'app-contact-1',
      synced_at: '2026-10-28T11:00:00Z', projection_mode: 'snapshot',
    } }));
    await page.route(/\/api\/v1\/conversations\/app-recipient-chat(?:\?|$)/, async route => {
      if (route.request().method() === 'PATCH') {
        conversation.title = route.request().postDataJSON().title;
        contact = { ...contact, title: conversation.title };
      }
      await route.fulfill({ json: conversation });
    });
    await page.route('**/api/v1/conversations/app-recipient-chat/read', route => {
      reads++;
      conversation.has_unread = false;
      conversation.last_read_at = '2026-10-28T11:02:00Z';
      return route.fulfill({ json: conversation });
    });
    await page.route(/\/api\/v1\/conversations(?:\?|$)/, route => route.fulfill({ json: {
      items: [conversation], cursor: null, has_more: false,
    } }));
    await page.route('**/api/v1/chat/v2/conversations/app-recipient-chat/**', route => {
      privateSnapshots += 1;
      return route.fulfill({ status: 409, json: { detail: 'No recipient execution session' } });
    });
    await page.route('**/api/v1/app-contacts/app-channel-binding', route => route.fulfill({ json: contact }));
    await page.route('**/api/v1/app-contacts/app-channel-binding/replies', async route => {
      const body = route.request().postDataJSON();
      replies.push(body);
      if (replies.length === 1) {
        await route.fulfill({ status: 503, json: { detail: 'Please retry' } });
      } else {
        contact = { ...contact, messages: [...contact.messages, {
          id: 'app-reply', direction: 'outgoing', content: body.content,
          occurred_at: '2026-10-28T11:01:00Z', attachments: [],
        }] };
        await route.fulfill({ json: { status: 'accepted' } });
      }
    });
    await page.goto('/');
    if (shared) {
      await page.getByTestId('dashboard-conversation-row-app-recipient-chat').first().click();
    } else {
      await page.getByRole('button', { name: 'Chat with Incoming agent', exact: true }).click();
    }
    const chat = page.getByTestId('app-contact-chat');
    await expect(chat).toBeVisible();
    await expect(chat.getByText('Would Tuesday work for a review?')).toBeVisible();
    await expect(chat.getByRole('heading', { name: conversation.title })).toBeVisible();
    await expect(chat.getByText('On behalf of sender@cognis.test')).toBeVisible();
    await expect(chat.getByTitle('Attach file')).toHaveCount(0);
    await chat.getByRole('textbox', { name: 'Message', exact: true }).fill('Tuesday works');
    await chat.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(chat.getByRole('alert')).toBeVisible();
    await expect(chat.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue('Tuesday works');
    await chat.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(chat.getByText('Tuesday works', { exact: true })).toBeVisible();
    expect(replies).toHaveLength(2);
    expect(replies[0].client_message_id).toBe(replies[1].client_message_id);
    expect(privateSnapshots).toBe(0);
    const initialPath = shared && mobile ? '/chat/app-recipient-chat' : '/';
    expect(new URL(page.url()).pathname).toBe(initialPath);
    const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(horizontalOverflow).toBe(false);
    await page.screenshot({ path: test.info().outputPath(`native-app-contact-${shared}-${mobile}.png`) });
    await page.goto('/chat/app-recipient-chat');
    await expect(chat).toBeVisible();
    await expect(chat.getByText('Tuesday works', { exact: true })).toBeVisible();
    await expect.poll(() => reads).toBeGreaterThan(0);
    await chat.getByLabel('Conversation actions').click();
    await chat.getByRole('button', { name: 'Rename conversation' }).click();
    await chat.getByRole('textbox', { name: 'Conversation title' }).fill('My review plan');
    await chat.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(chat.getByRole('heading', { name: 'My review plan' })).toBeVisible();
    await page.reload();
    await expect(chat.getByRole('heading', { name: 'My review plan' })).toBeVisible();
    await expect(chat.getByText('Tuesday works', { exact: true })).toBeVisible();
    const composerBox = await chat.getByRole('textbox', { name: 'Message', exact: true }).boundingBox();
    expect(composerBox).not.toBeNull();
    expect(composerBox!.y + composerBox!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
    await page.goto('/');
    await expect(page.getByRole('button', { name: 'Chat with Incoming agent', exact: true })).toBeVisible();
    expect(privateSnapshots).toBe(0);
  });
 }
}
