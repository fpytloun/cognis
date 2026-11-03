import { mount } from 'svelte';
import Fixture from './Fixture.svelte';
import { documentCoordination } from '../../../src/lib/document-coordination';

async function start(): Promise<void> {
  const coordination = documentCoordination();
  await coordination.initialize();
  await coordination.verified('review@example.test');
  mount(Fixture, { target: document.getElementById('fixture')! });
}

void start().catch(() => {
  const root = document.getElementById('fixture')!;
  root.setAttribute('role', 'alert');
  root.textContent = 'Review fixture initialization failed.';
});
