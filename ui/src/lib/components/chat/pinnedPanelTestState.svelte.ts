import { createPinnedPanel, type PinnedPanel } from './pinned-panel.svelte';

export function pinnedPanelTestState(onClose: () => void, visible: () => boolean = () => false) {
  let panel!: PinnedPanel;
  const destroy = $effect.root(() => {
    panel = createPinnedPanel({ visible, projectId: () => null, onClose });
  });
  return { panel, destroy };
}
