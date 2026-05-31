<script lang="ts">
  import type { ToolCallTimelineItem } from '$lib/chat';
  import { displayToolName } from '$lib/tools-display';

  let { item } = $props<{ item: ToolCallTimelineItem }>();
</script>

<article class="rounded-3xl border border-slate-800 bg-slate-900/80 px-4 py-4 text-sm shadow-card">
  <div class="flex items-center justify-between gap-4">
    <div>
      <p class="text-xs font-medium uppercase tracking-[0.25em] text-slate-400">Tool call</p>
      <h3 class="mt-1 text-base font-semibold text-white" title={item.toolName}>{displayToolName(item.toolName)}</h3>
    </div>
    <span class="rounded-full border border-slate-700 bg-slate-800 px-3 py-1 text-xs font-medium text-slate-200">
      {item.status}
    </span>
  </div>
</article>
