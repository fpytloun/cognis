export interface WorkspaceDockSlot {
  key: string;
  left: number;
  width: number;
}

export interface WorkspaceDockInsertion {
  index: number;
  order: string[];
}

export function workspaceDockInsertion(
  slots: readonly WorkspaceDockSlot[],
  sourceKey: string,
  pointerX: number,
  previousIndex: number,
  hysteresis = 6,
): WorkspaceDockInsertion {
  const candidates = slots.filter((slot) => slot.key !== sourceKey);
  let index = Math.max(0, Math.min(previousIndex, candidates.length));

  while (
    index < candidates.length
    && pointerX > candidates[index].left + candidates[index].width / 2 + hysteresis
  ) {
    index += 1;
  }
  while (
    index > 0
    && pointerX < candidates[index - 1].left + candidates[index - 1].width / 2 - hysteresis
  ) {
    index -= 1;
  }

  const order = candidates.map((slot) => slot.key);
  order.splice(index, 0, sourceKey);
  return { index, order };
}

export function workspaceDockOrderLefts(
  slots: readonly WorkspaceDockSlot[],
  order: readonly string[],
  gap: number,
): Record<string, number> {
  if (slots.length === 0 || order.length === 0) return {};
  const widths = new Map(slots.map((slot) => [slot.key, slot.width]));
  let left = slots[0].left;
  return Object.fromEntries(order.map((key) => {
    const itemLeft = left;
    left += (widths.get(key) ?? 0) + gap;
    return [key, itemLeft];
  }));
}
