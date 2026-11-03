import { mount } from 'svelte';
import '../../../src/app.css';
import ScopedFixture from '../../../src/routes/scoped-chat-v2-fixture/+page.svelte';
import { initializeFixtureDocumentContext } from './document-context';

async function start(): Promise<void> {
  await initializeFixtureDocumentContext();
  mount(ScopedFixture, { target: document.getElementById('app')! });
}

void start().catch(() => {
  const root = document.getElementById('app')!;
  root.setAttribute('role', 'alert');
  root.textContent = 'Scoped fixture initialization failed.';
});
