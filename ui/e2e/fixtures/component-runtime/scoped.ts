import { mount } from 'svelte';
import '../../../src/app.css';
import ScopedFixture from '../../../src/routes/scoped-chat-v2-fixture/+page.svelte';
import { documentCoordination } from '../../../src/lib/document-coordination';
import { offlineCache } from '../../../src/lib/offline/cache';
import { startAppearance } from '../../../src/lib/theme/appearance';

async function start(): Promise<void> {
  const coordination = documentCoordination();
  await coordination.initialize();
  // Fictional coordination metadata; real principal-scoped dispatch fences stay active.
  await coordination.verified('fixture@example.com');
  await offlineCache.setOwner(coordination.storageName('offline-cache'));
  const stopAppearance = startAppearance();
  window.addEventListener('pagehide', stopAppearance, { once: true });
  mount(ScopedFixture, { target: document.getElementById('app')! });
}

void start().catch(() => {
  const root = document.getElementById('app')!;
  root.setAttribute('role', 'alert');
  root.textContent = 'Scoped fixture initialization failed.';
});
