import { mount } from 'svelte';
import '../../../src/app.css';
import SessionLogsFixture from '../../../src/routes/session-logs-drawer-fixture/+page.svelte';
import { initializeFixtureDocumentContext } from './document-context';

async function start(): Promise<void> {
  await initializeFixtureDocumentContext();
  mount(SessionLogsFixture, { target: document.getElementById('app')! });
}

void start().catch(() => {
  const root = document.getElementById('app')!;
  root.setAttribute('role', 'alert');
  root.textContent = 'Session logs fixture initialization failed.';
});
