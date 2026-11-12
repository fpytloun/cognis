<script lang="ts">
  import { settledActivityLabel } from '$lib/chat-v2/tool-groups';

  let { label, active = false } = $props<{ label: string; active?: boolean }>();
  // Finished activity states what happened instead of reading as still in progress.
  const shown = $derived(active ? label : settledActivityLabel(label));
</script>

<span class:running-label={active}>{shown}</span>

<style>
  @supports (background-clip: text) {
    .running-label {
      background: linear-gradient(
        90deg,
        color-mix(in srgb, currentColor 40%, transparent) 30%,
        currentColor 50%,
        color-mix(in srgb, currentColor 40%, transparent) 70%
      );
      background-size: 250% 100%;
      background-clip: text;
      -webkit-text-fill-color: transparent;
      animation: activity-sweep var(--chat-motion-activity, 1.6s) linear infinite;
    }
  }

  @keyframes activity-sweep {
    from { background-position: 100% 0; }
    to { background-position: 0% 0; }
  }

  @media (prefers-reduced-motion: reduce), (forced-colors: active) {
    .running-label {
      animation: none;
      background: none;
      -webkit-text-fill-color: currentColor;
    }
  }
</style>
