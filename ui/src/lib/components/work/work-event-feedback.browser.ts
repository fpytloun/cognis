import { mount } from 'svelte';
import { documentCoordination } from '$lib/document-coordination';
import Harness from './WorkEventFeedbackHarness.svelte';
import * as fixture from './work-event-feedback.fixture';
import '../../../app.css';

export { fixture };
export async function start() {
  await documentCoordination().initialize();
  await documentCoordination().beginLogin();
  await documentCoordination().verified('chromium-work-feedback@cognis.test');
  const target = document.getElementById('target');
  if (!target) throw new Error('Missing component qualification mount point.');
  mount(Harness, { target, props: { context: fixture.context, scope: fixture.scope,
    loadWork: async () => fixture.projection(), refreshIntervalMs: 0 } });
}
