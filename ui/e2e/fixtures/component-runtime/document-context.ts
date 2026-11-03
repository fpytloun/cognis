import { documentCoordination } from '../../../src/lib/document-coordination';
import { offlineCache } from '../../../src/lib/offline/cache';
import { startAppearance } from '../../../src/lib/theme/appearance';

export async function initializeFixtureDocumentContext(): Promise<void> {
  const coordination = documentCoordination();
  await coordination.initialize();
  // Fictional coordination metadata; real principal-scoped dispatch fences stay active.
  await coordination.verified('fixture@example.com');
  await offlineCache.setOwner(coordination.storageName('offline-cache'));
  const stopAppearance = startAppearance();
  window.addEventListener('pagehide', stopAppearance, { once: true });
}
