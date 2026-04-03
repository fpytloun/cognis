<script lang="ts">
  import type { Snippet } from 'svelte';

  let { text, children } = $props<{ text: string; children: Snippet }>();
</script>

<span class="group relative inline-flex items-center">
  {@render children()}
  <span
    role="tooltip"
    class="pointer-events-none absolute bottom-full left-1/2 z-50 mb-2 -translate-x-1/2 whitespace-normal rounded-xl border border-slate-700 bg-slate-900 px-3 py-2 text-xs font-normal leading-relaxed text-slate-300 opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100"
    style="width: max-content; max-width: 320px;"
  >
    {text}
    <span class="absolute left-1/2 top-full -translate-x-1/2 border-4 border-transparent border-t-slate-700"></span>
  </span>
</span>
