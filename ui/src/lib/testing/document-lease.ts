import type { DocumentLease } from '$lib/document-coordination';

/** Canonical actor lease with a controllable account-change signal. */
export function testDocumentLease(): { lease: DocumentLease; controller: AbortController } {
  const controller = new AbortController();
  const assertCurrent = () => {
    if (controller.signal.aborted) throw new Error('The account changed.');
  };
  return { controller, lease: {
    owner: 'owner@cognis.test', signal: controller.signal, epoch: crypto.randomUUID(),
    assertCurrent, validate: async () => { assertCurrent(); },
  } };
}
