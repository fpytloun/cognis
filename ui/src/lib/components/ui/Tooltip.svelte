<script lang="ts">
  import type { Snippet } from 'svelte';

  import Popover from './Popover.svelte';

  /**
   * Thin wrapper around Popover that keeps the historical `<Tooltip>` API.
   *
   * Semantics:
   *   - On devices with hover (desktop), reveals on mouse/keyboard hover.
   *   - On touch devices, reveals on tap — no longer unreachable.
   */

  let { text, children } = $props<{ text: string; children: Snippet }>();
</script>

<Popover {text}>{@render children()}</Popover>
