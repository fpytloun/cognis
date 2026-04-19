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

  let { text, children, placement = 'top', class: className = '' } = $props<{
    text: string;
    children: Snippet;
    placement?: 'top' | 'bottom' | 'left' | 'right';
    class?: string;
  }>();
</script>

<Popover {text} {placement} class={className}>{@render children()}</Popover>
