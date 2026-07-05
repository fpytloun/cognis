import type { RuntimeOverlaySnapshot, TimelineItem } from './types';
import { maybeApplyRuntime, sortTimelineItems } from './sync-engine';

export { maybeApplyRuntime };

export function runtimeIsActive(runtime: RuntimeOverlaySnapshot | null): boolean {
  return runtime?.has_active_turn === true;
}

export function mergeRuntimeOverlay(
  canonicalItems: TimelineItem[],
  runtime: RuntimeOverlaySnapshot | null
): TimelineItem[] {
  if (!runtime?.volatile_items.length) return canonicalItems;
  const byId = new Map(canonicalItems.map((item) => [item.id, item]));
  for (const item of runtime.volatile_items) byId.set(item.id, item);
  return sortTimelineItems([...byId.values()]);
}
