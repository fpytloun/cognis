import { test, expect, type Page, type Request, type WebSocket as PlaywrightWebSocket } from '@playwright/test';
import { Buffer } from 'node:buffer';
import { RecoveryRequestEpoch } from './recovery-request-epoch';
import { bounded, cleanupIndependently } from './diagnostic-boundary';
import { OwnedAsyncWork } from './owned-async-work';
import { canonicalMetadata, durableMatch, type CanonicalMetadata } from './canonical-response-evidence';
import { clearScenario, injectScenario, login, openOrCreateConversation, MOCK_LLM_URL, sendMessage } from './helpers';
import { requestMockBarrier } from '../src/lib/testing/mock-llm-control';
import type { UserPreferences } from '../src/lib/types/api';

test.beforeEach(async ({}, info) => {
  test.skip(process.env.COGNIS_E2E_SCOPED_WITNESS_DIAGNOSTIC === '1'
    && info.title !== 'independent scoped recipient converges before sender application ACK (absorbed)');
});

// These tests use the real seeded controller/provider stack, never route-fulfilled
// timeline data. The only intercepted response is the sender's public admission ACK.
function userRow(page: Page, text: string) {
  return page.locator('[data-kind="message"][data-role="user"]').filter({ has: page.getByText(text, { exact: true }) });
}

test('queue-only text cannot satisfy the canonical user-row selector', async ({ page }) => {
  await page.setContent('<div data-testid="scoped-timeline-queue"><p>Queue-only fixture</p></div>');
  await expect(page.getByText('Queue-only fixture', { exact: true })).toHaveCount(1);
  await expect(userRow(page, 'Queue-only fixture')).toHaveCount(0);
});

test('held pre-disconnect response cannot enter a fresh request epoch', () => {
  const epoch = new RecoveryRequestEpoch<object>();
  const heldRequest = {};
  epoch.initiated(heldRequest);
  epoch.begin();
  epoch.initiated(heldRequest);
  expect(epoch.accepts(heldRequest)).toBe(false);
  const reconnectRequest = {};
  epoch.initiated(reconnectRequest);
  expect(epoch.accepts(reconnectRequest)).toBe(true);
});

for (const recipientSurface of ['main', 'scoped'] as const) {
 for (const mode of ['normal', 'absorbed', 'queued_after_ui_stop'] as const) {
  test(`independent ${recipientSurface} recipient converges before sender application ACK (${mode})`, async ({ browser }) => {
    const senderContext = await browser.newContext();
    const recipientContext = await browser.newContext();
    const sender = await senderContext.newPage();
    const recipient = await recipientContext.newPage();
    const observedSockets: PlaywrightWebSocket[] = [];
    recipient.on('websocket', socket => {
      const expected = new URL('/api/ws', recipient.url());
      expected.protocol = expected.protocol === 'https:' ? 'wss:' : 'ws:';
      const actual = new URL(socket.url());
      if (actual.origin === expected.origin && actual.pathname === expected.pathname) {
        observedSockets.push(socket);
      }
    });
    const recoveryEpoch = new RecoveryRequestEpoch<Request>();
    recipient.on('request', request => {
      if (/\/chat\/v2\/conversations\/[^/]+\/(sync|snapshot)(\?|$)/.test(request.url())) {
        recoveryEpoch.initiated(request);
      }
    });
    let releaseAck!: () => void;
    const heldAck = new Promise<void>(resolve => { releaseAck = resolve; });
    let requestObserved!: (value: { client_message_id: string; attachments: Array<{ artifact_id: string }> }) => void;
    const admission = new Promise<{ client_message_id: string; attachments: Array<{ artifact_id: string }> }>(
      resolve => { requestObserved = resolve; },
    );
    let ackDelivered = false;
    let admissionRequest: Request | undefined;
    let releaseSnapshot: () => void = () => {};
    let primaryFailure: unknown;
    let lastBoundary = 'sender login';
    const owned = new OwnedAsyncWork();
    const completedBoundaries: string[] = [];
    let bodyPassed = false;
    const canonicalEvidence: Array<CanonicalMetadata & { request_id: number; path: string }> = [];
    const witnessEvidence: Array<CanonicalMetadata & { request_id: number; path: string }> = [];
    let witnessIdentity: { clientId: string; transactionId: string } | undefined;
    let witnessEvidenceRead: CanonicalMetadata | undefined;
    let resolveDurable: ((item: any) => void) | undefined;
    let expectedClientId: string | undefined;
    let recipientConversationId: string | undefined;
    let previousPreferences: UserPreferences | undefined;
    const canonicalRequests = new WeakMap<Request, number>();
    let nextRequestId = 0;
    const checkDurable = () => {
      if (!expectedClientId || !resolveDurable) return;
      for (const evidence of canonicalEvidence) {
        if (evidence.conversation_id !== recipientConversationId) continue;
        const match = durableMatch(evidence, expectedClientId, `conversation:${recipientConversationId}`);
        if (match) { resolveDurable(match); return; }
      }
    };
    async function step<T>(name: string, action: () => Promise<T>): Promise<T> {
      lastBoundary = name;
      return test.step(name, async () => {
        const result = await bounded(name, owned.guard(action()));
        completedBoundaries.push(name);
        return result;
      });
    }
    try {
      await recipient.addInitScript(() => {
        const NativeWebSocket = window.WebSocket;
        const sockets: InstanceType<typeof NativeWebSocket>[] = [];
        const state = window as typeof window & {
          __acceptanceSockets: InstanceType<typeof NativeWebSocket>[];
          __acceptanceClosedSocket?: InstanceType<typeof NativeWebSocket>;
        };
        state.__acceptanceSockets = sockets;
        window.WebSocket = class extends NativeWebSocket {
          constructor(...args: ConstructorParameters<typeof NativeWebSocket>) {
            super(...args);
            const expected = new URL('/api/ws', location.href);
            expected.protocol = expected.protocol === 'https:' ? 'wss:' : 'ws:';
            const actual = new URL(this.url);
            if (actual.origin === expected.origin && actual.pathname === expected.pathname) {
              sockets.push(this);
            }
          }
        };
      });
      await step('sender login', () => login(sender));
      await step('sender conversation selection', () => openOrCreateConversation(sender));
      await step('sender public composer enabled', () => expect(sender.locator('textarea').first()).toBeEnabled());
      const conversationId = new URL(sender.url()).pathname.split('/').pop()!;
      recipientConversationId = conversationId;
      const expectedOrigin = new URL(sender.url()).origin;
      const canonicalPath = new RegExp(`^/api/v1/chat/v2/conversations/${conversationId}/(snapshot|sync|backfill)$`);
      recipient.on('request', request => {
        const url = new URL(request.url());
        if (url.origin === expectedOrigin && canonicalPath.test(url.pathname)) {
          canonicalRequests.set(request, ++nextRequestId);
        }
      });
      recipient.on('response', response => owned.run('canonical response observer', async () => {
        const requestId = canonicalRequests.get(response.request());
        if (!requestId || !response.ok()) return;
          if (await response.finished()) return;
          const metadata = canonicalMetadata(await response.json());
          canonicalEvidence.push({
            ...metadata, request_id: requestId, path: new URL(response.url()).pathname,
          });
          if (canonicalEvidence.length > 64) canonicalEvidence.shift();
          checkDurable();
      }));
      // Establish the active-leader precondition before adding the recipient's
      // live-view reads. The recipient still subscribes before the tested admission.
      if (mode !== 'normal') {
        await injectScenario('single-phase-stream');
         const armed = await requestMockBarrier(MOCK_LLM_URL, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ scenario_id: 'single-phase-stream', turn_index: 0 }),
        });
        expect(armed.status).toBe(201);
        await sendMessage(sender, 'Synthetic active leader');
        await expect(userRow(sender, 'Synthetic active leader')).toBeVisible();
         const entered = await step('leader entered LLM barrier', () => requestMockBarrier(MOCK_LLM_URL, { signal: AbortSignal.timeout(10_000) }));
        expect(entered.status).toBe(204);
      }
      await step('recipient login', () => login(recipient));
      if (recipientSurface === 'scoped') {
        const existing = await recipient.request.get('/api/v1/user-preferences');
        expect(existing.status()).toBe(200);
        const preferences: UserPreferences = await existing.json();
        previousPreferences = preferences;
        const updated = await recipient.request.put('/api/v1/user-preferences', {
          data: { ...preferences, display: { ...preferences.display, dashboard_workspace_windows: true } },
        });
        expect(updated.status()).toBe(200);
      }
      const heldSnapshot = new Promise<void>(resolve => { releaseSnapshot = resolve; });
      let snapshotCaptured!: () => void;
      const capturedSnapshot = new Promise<void>(resolve => { snapshotCaptured = resolve; });
      if (mode === 'normal') {
        await recipient.route(`**/api/v1/chat/v2/conversations/${conversationId}/snapshot*`, route => owned.run('initial snapshot route', async () => {
          const response = await route.fetch();
          snapshotCaptured();
          await bounded('initial snapshot release', heldSnapshot, 30_000);
          await route.fulfill({ response });
        }), { times: 1 });
      }
      if (recipientSurface === 'main') {
        await recipient.goto(`/chat/${conversationId}`);
        if (mode !== 'normal') await expect(recipient.locator('textarea').first()).toBeVisible();
      } else {
        await recipient.goto('/');
        await recipient.getByTestId(`dashboard-conversation-row-${conversationId}`).click();
        await expect(recipient.getByTestId(`workspace-window-conversation:${conversationId}`)).toBeVisible();
        if (mode !== 'normal') await expect(recipient.getByTestId('scoped-timeline-viewport')).toBeVisible();
      }
      if (mode === 'normal') await step('initial snapshot captured', () => capturedSnapshot);
      if (mode === 'normal') await injectScenario('single-phase-stream');
      const text = `Independent admission ${crypto.randomUUID()}`;
      await step('upload synthetic attachment', () => sender.locator('input[type=file]').first().setInputFiles({
        name: 'cross-client-fixture.txt', mimeType: 'text/plain',
        buffer: Buffer.from('Synthetic attachment for cross-client acceptance'),
      }));
      await sender.route(`**/api/v1/chat/v2/conversations/${conversationId}/messages/*`, route => owned.run('admission ACK route', async () => {
        if (route.request().method() !== 'PUT') return route.continue();
        admissionRequest = route.request();
        const payload = route.request().postDataJSON();
        const response = await route.fetch({ maxRedirects: 0 });
        expect(response.status()).toBe(202);
        requestObserved(payload);
        await bounded('sender ACK release', heldAck, 30_000);
        await route.fulfill({ response });
        ackDelivered = true;
      }));
      await step('pre-send composer enabled', () => expect(sender.locator('textarea').first()).toBeEnabled());
      await step('submit synthetic admission', async () => {
        await sender.locator('textarea').first().fill(text);
        // The textarea stays editable while uploads are pending; submit does not.
        const submit = sender.getByRole('button', {
          name: mode === 'normal' ? 'Send' : 'Queue message', exact: true,
        });
        await expect(submit).toBeVisible();
        await expect(submit).toBeEnabled();
        await sender.locator('textarea').first().press('Enter');
      });
      const request = await step('real admission response captured', () => admission);
      expectedClientId = request.client_message_id;
      expect(admissionRequest).toBeDefined();
      const heldAdmissionRequest = admissionRequest!;
      const transactionId = new URL(heldAdmissionRequest.url()).pathname.split('/').pop();
      expect(transactionId).toBeTruthy();
      expect(transactionId).not.toBe(request.client_message_id);
      expect(request.attachments.length).toBe(1);
      expect(request.attachments[0].artifact_id).toBeTruthy();
      releaseSnapshot();
      if (mode !== 'normal') {
        const held = await step('leader remains held through tested admission', () =>
           requestMockBarrier(MOCK_LLM_URL, { signal: AbortSignal.timeout(10_000) }));
        expect(held.status).toBe(204);
      }
      if (mode === 'absorbed') {
         expect((await requestMockBarrier(MOCK_LLM_URL, { method: 'PUT' })).status).toBe(204);
      } else if (mode === 'queued_after_ui_stop') {
        const status = await recipient.evaluate(async id => {
          const response = await fetch(`/api/v1/chat/v2/conversations/${id}/cancel`, {
            method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ client_txn_id: crypto.randomUUID() }),
          });
          return response.status;
        }, conversationId);
        expect(status).toBe(200);
         expect((await requestMockBarrier(MOCK_LLM_URL, { method: 'DELETE' })).status).toBe(204);
      }
      // No recipient fetch/reload is used to make the message appear.
      const recipientRow = userRow(recipient, text);
      await step('recipient canonical row before ACK', () => expect(recipientRow).toHaveCount(1));
      await expect(recipientRow.getByText('cross-client-fixture.txt', { exact: true })).toBeVisible();
      await expect(recipient.getByTestId('scoped-timeline-queue').getByText(text, { exact: true })).toHaveCount(0);
      await expect(recipient.getByText(text, { exact: true })).toHaveCount(1);
      expect(ackDelivered).toBe(false);
      const durable = await step('production-driven durable canonical response before ACK', () =>
        new Promise<any>(resolve => { resolveDurable = resolve; checkDurable(); }));
      await expect(recipientRow).toHaveAttribute('data-message-id', durable.id);
      if (recipientSurface === 'scoped') {
        await expect(recipient.getByTestId('scoped-timeline-viewport')).toBeVisible();
      }
      expect(ackDelivered).toBe(false);
      const items = await recipient.evaluate(async id => {
        const response = await fetch(`/api/v1/chat/v2/conversations/${id}/snapshot`, { credentials: 'include' });
        if (!response.ok) throw new Error(`Evidence snapshot failed: ${response.status}`);
        return (await response.json()).timeline.items;
      }, conversationId);
      const matching = items.filter((item: { client_message_id?: string }) =>
        item.client_message_id === request.client_message_id);
      expect(matching).toHaveLength(1);
      expect(matching[0].content).toBe(text);
      expect(matching[0].attachments.map((attachment: { artifact_id: string }) => attachment.artifact_id))
        .toEqual(request.attachments.map(attachment => attachment.artifact_id));
      expect(matching[0].source_refs.length).toBeGreaterThan(0);
      expect(matching[0].stable).toBe(true);
      expect(matching[0].id).toBe(durable.id);
      await expect(recipientRow).toHaveAttribute('data-message-id', matching[0].id);
      const ackHttpCompleted = owned.observe('admission HTTP completion', sender.waitForEvent('requestfinished', {
        predicate: finished => finished === heldAdmissionRequest,
      }));
      releaseAck();
      await step('sender admission HTTP completion', () => ackHttpCompleted);
      await expect(userRow(sender, text)).toHaveCount(1);
      await expect(userRow(sender, text)).toHaveAttribute('data-message-id', matching[0].id);
      await expect(userRow(sender, text)).toHaveAttribute('data-stable', 'true');
      // Separate post-ACK recovery phase: create genuinely new canonical data
      // while the recipient's original socket is confirmed closed.
      // Explicitly closed-connection recovery, not an abrupt partition test.
      const openSockets = observedSockets.filter(socket => !socket.isClosed());
      expect(openSockets).toHaveLength(1);
      const oldSocket = openSockets[0];
      const closed = owned.observe('old socket close', oldSocket.waitForEvent('close'));
      await recipientContext.setOffline(true);
      await step('offline fence and public native socket close', async () => {
        expect(await recipient.evaluate(url => {
          if (navigator.onLine) throw new Error('Recipient is not offline');
          const state = window as typeof window & {
            __acceptanceSockets: InstanceType<typeof window.WebSocket>[];
            __acceptanceClosedSocket?: InstanceType<typeof window.WebSocket>;
          };
          const matches = state.__acceptanceSockets.filter(
            socket => socket.url === url && socket.readyState === WebSocket.OPEN,
          );
          if (matches.length !== 1) throw new Error(`Expected one native socket, got ${matches.length}`);
          state.__acceptanceClosedSocket = matches[0];
          matches[0].close();
          return navigator.onLine;
        }, oldSocket.url())).toBe(false);
      });
      await step('old recipient socket closed', () => closed);
      expect(oldSocket.isClosed()).toBe(true);
      expect(await recipient.evaluate(() => {
        const state = window as typeof window & {
          __acceptanceClosedSocket?: InstanceType<typeof window.WebSocket>;
        };
        return !navigator.onLine && state.__acceptanceClosedSocket?.readyState === WebSocket.CLOSED;
      })).toBe(true);
      if (mode !== 'normal') {
         await requestMockBarrier(MOCK_LLM_URL, { method: 'DELETE' });
      }
      const witnessText = `Disconnected recovery witness ${crypto.randomUUID()}`;
      witnessIdentity = { clientId: crypto.randomUUID(), transactionId: crypto.randomUUID() };
      expect(witnessIdentity.clientId).not.toBe(witnessIdentity.transactionId);
      const witnessRequests = new WeakMap<Request, number>();
      let witnessRequestNumber = 0;
      let resolveWitness!: (item: NonNullable<ReturnType<typeof durableMatch>>) => void;
      const durableWitness = new Promise<NonNullable<ReturnType<typeof durableMatch>>>(
        resolve => { resolveWitness = resolve; },
      );
      sender.on('request', request => {
        const url = new URL(request.url());
        if (url.origin === expectedOrigin && canonicalPath.test(url.pathname)) {
          witnessRequests.set(request, ++witnessRequestNumber);
        }
      });
      // Production post-append invalidation drives this read; no fixture sync.
      sender.on('response', response => owned.run('sender durable witness observer', async () => {
        const requestId = witnessRequests.get(response.request());
        if (!requestId || !response.ok() || await response.finished()) return;
        const metadata = canonicalMetadata(await response.json());
        const path = new URL(response.url()).pathname;
        if (metadata.conversation_id !== conversationId) return;
        metadata.scope_key ??= `conversation:${conversationId}`;
        witnessEvidence.push({ ...metadata, request_id: requestId, path });
        if (witnessEvidence.length > 64) witnessEvidence.shift();
        const item = durableMatch(metadata, witnessIdentity!.clientId, `conversation:${conversationId}`);
        if (item) resolveWitness(item);
      }));
      const witness = await sender.evaluate(async ({ id, content, clientId, transactionId }) => {
        const response = await fetch(`/api/v1/chat/v2/conversations/${id}/messages/${transactionId}`, {
          method: 'PUT', credentials: 'include', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ client_message_id: clientId, content, attachments: [] }),
        });
        if (response.status !== 202) throw new Error(`Witness admission: ${response.status}`);
        return { clientId };
      }, { id: conversationId, content: witnessText, ...witnessIdentity });
      const durableWitnessItem = await step('sender production durable witness response', () => durableWitness);
      await expect(userRow(sender, witnessText)).toHaveCount(1);
      await expect(userRow(sender, witnessText)).toHaveAttribute('data-stable', 'true');
      await expect(userRow(sender, witnessText)).toHaveAttribute('data-message-id', durableWitnessItem.id);
      const witnessSnapshot = await sender.evaluate(async ({ id }) => {
        const response = await fetch(`/api/v1/chat/v2/conversations/${id}/snapshot`, { credentials: 'include' });
        if (!response.ok) throw new Error(`Witness evidence: ${response.status}`);
        return response.json();
      }, { id: conversationId });
      witnessEvidenceRead = canonicalMetadata(witnessSnapshot);
      const witnessItems = witnessSnapshot.timeline.items.filter(
        (item: { client_message_id?: string }) => item.client_message_id === witness.clientId,
      );
      expect(witnessItems).toHaveLength(1);
      const witnessItem = witnessItems[0];
      expect(durableMatch(witnessEvidenceRead, witness.clientId, `conversation:${conversationId}`)?.id)
        .toBe(durableWitnessItem.id);
      expect(durableMatch(witnessEvidenceRead, witness.clientId, `conversation:${conversationId}`)?.refs)
        .toEqual(durableWitnessItem.refs);
      expect(await recipient.evaluate(() => navigator.onLine)).toBe(false);
      await expect(userRow(recipient, witnessText)).toHaveCount(0);
      let witnessResponseDelivered = false;
      await recipient.route(`**/api/v1/chat/v2/conversations/${conversationId}/*`, route => owned.run('reconnect canonical route', async () => {
        const request = route.request();
        if (!/\/(sync|snapshot)(\?|$)/.test(request.url()) || !recoveryEpoch.accepts(request)) {
          return route.continue();
        }
        const response = await route.fetch({ maxRedirects: 0 });
        const body = await response.json();
        const items = body.timeline?.items ?? body.ops?.filter(
          (op: { op: string }) => op.op === 'upsert_item',
        ).map((op: { item: unknown }) => op.item) ?? [];
        if (items.some((item: { id: string }) => item.id === witnessItem.id)) {
          // Network capture is not application. The witness must still be
          // absent while this specific fresh response is held by routing.
          await expect(userRow(recipient, witnessText)).toHaveCount(0);
          await route.fulfill({ response });
          witnessResponseDelivered = true;
        } else {
          await route.fulfill({ response });
        }
      }));
      const rehydrated = new Promise<void>(resolve => {
        recipient.once('websocket', socket => {
          let authenticated = false;
          socket.on('framereceived', frame => {
            const event = JSON.parse(String(frame.payload));
            if (event.type === 'authenticated') authenticated = true;
            if (authenticated && event.type === 'chat_v2_frame'
              && event.scope?.key === `conversation:${conversationId}`) resolve();
          });
        });
      });
      const recoveryRead = owned.observe('fresh witness response observer', recipient.waitForResponse(async response => {
        if (!response.ok() || !recoveryEpoch.accepts(response.request())
          || !response.url().includes(`/chat/v2/conversations/${conversationId}/`)) return false;
        if (await response.finished() !== null) return false;
        const body = await response.json();
        const items = body.timeline?.items ?? body.ops?.filter(
          (op: { op: string }) => op.op === 'upsert_item',
        ).map((op: { item: unknown }) => op.item) ?? [];
        return items.some((item: { id: string }) => item.id === witnessItem.id);
      }));
      recoveryEpoch.begin();
      await recipientContext.setOffline(false);
      await step('authenticated reconnect subscription hydrated', () => rehydrated);
      const recovered = await step('fresh witness response completed', () => recoveryRead);
      expect(await recovered.finished()).toBeNull();
      expect(witnessResponseDelivered).toBe(true);
      await expect(userRow(recipient, witnessText)).toHaveCount(1);
      await expect(userRow(recipient, witnessText)).toHaveAttribute('data-message-id', witnessItem.id);
      await expect(userRow(recipient, witnessText)).toHaveAttribute('data-stable', 'true');
      await expect(userRow(recipient, text)).toHaveCount(1);
      await expect(userRow(recipient, text)).toHaveAttribute('data-message-id', matching[0].id);
      await expect(userRow(recipient, text).getByText('cross-client-fixture.txt', { exact: true })).toBeVisible();
      completedBoundaries.push('all reconnect application assertions');
      bodyPassed = true;
    } catch (error) {
      primaryFailure = error;
      test.info().annotations.push({ type: 'last-semantic-boundary', description: lastBoundary });
      // Only allowlisted identities/positions; never request bodies or cursor tokens.
      const rows = await recipient.locator('[data-kind="message"][data-role="user"]')
        .evaluateAll(elements => elements.slice(-128).map(element => element.getAttribute('data-message-id')))
        .catch(() => []);
      await test.info().attach('canonical-identity-evidence', {
        body: JSON.stringify({ boundary: lastBoundary, completedBoundaries, bodyPassed, callbacks: owned.failures, pending: owned.pendingLabels, client_message_id: expectedClientId, rows, responses: canonicalEvidence, witnessIdentity, witnessEvidence, witnessEvidenceRead }),
        contentType: 'application/json',
      }).catch(() => {});
      throw error;
    } finally {
      owned.phase = 'cleanup';
      const failures = await cleanupIndependently([
        async () => { releaseAck(); },
        async () => { releaseSnapshot(); },
        // Unregister first and drain actual handlers before disposing requests.
        async () => { await sender.unrouteAll({ behavior: 'wait' }); },
        async () => { await recipient.unrouteAll({ behavior: 'wait' }); },
        async () => { await recipient.removeAllListeners('response', { behavior: 'wait' }); },
        async () => { await sender.removeAllListeners('response', { behavior: 'wait' }); },
        async () => { await owned.drainCallbacks(); },
        async () => {
          if (previousPreferences) {
            const restored = await recipient.request.put('/api/v1/user-preferences', { data: previousPreferences });
            expect(restored.status()).toBe(200);
          }
        },
        async () => { await senderContext.close(); },
        async () => { await recipientContext.close(); },
        // Closing contexts rejects any remaining public event waiters. Their
        // rejections are owned and retained, never unhandled or ignored.
        async () => { await owned.drain(); },
        async () => {
           if (mode !== 'normal') await requestMockBarrier(MOCK_LLM_URL, { method: 'DELETE', signal: AbortSignal.timeout(5_000) }, 4500);
        },
        async () => { await clearScenario(); },
      ]);
      try { await test.info().attach('canonical-lifecycle-evidence', {
        body: JSON.stringify({ boundary: lastBoundary, completedBoundaries, bodyPassed,
          hadPrimaryFailure: !!primaryFailure, cleanupFailureCount: failures.length,
          callbacks: owned.failures, pending: owned.pendingLabels,
          client_message_id: expectedClientId, responses: canonicalEvidence, witnessIdentity, witnessEvidence, witnessEvidenceRead }),
        contentType: 'application/json',
      }); } catch (error) { failures.push(error); }
      if (failures.length || owned.failures.length) {
        const detail = `cleanup failures=${failures.length}; callbacks=${JSON.stringify(owned.failures)}`;
        test.info().annotations.push({ type: 'secondary-cleanup-failure', description: detail });
        if (!primaryFailure) throw new Error(`Fixture cleanup failed: ${detail}`);
      }
    }
  });
 }
}
