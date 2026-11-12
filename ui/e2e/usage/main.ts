import { mount } from 'svelte';
import Harness from './Harness.svelte';
import '../../src/app.css';
import { documentCoordination } from '../../src/lib/document-coordination';

// Synthetic fixture account, confined to this development-only harness.
async function start() {
  const coordination = documentCoordination();
  await coordination.initialize();
  await coordination.verified('usage-fixture@test.invalid');
  mount(Harness, { target: document.getElementById('app')! });
}
void start();
