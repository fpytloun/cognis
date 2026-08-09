<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import type { TaskDetail } from '$lib/types/api';
  import { formatAbsoluteTime } from '$lib/time';

  let {
    task,
    workflowLabel,
    projectLabel,
    agentLabel
  }: {
    task: TaskDetail;
    workflowLabel: string;
    projectLabel: string;
    agentLabel: string;
  } = $props();
</script>

<section class="rounded-2xl border border-slate-800 bg-slate-900/60 p-4 sm:p-5" data-testid="task-brief">
  <p class="text-xs font-semibold uppercase tracking-[0.25em] text-sky-300">Task brief</p>
  <div class="mt-4 grid gap-5 lg:grid-cols-2">
    <div>
      <h2 class="text-xs font-medium uppercase tracking-[0.2em] text-slate-500">Input and context</h2>
      <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-200">{task.description || 'No additional description was provided.'}</p>
    </div>
    <div>
      <h2 class="text-xs font-medium uppercase tracking-[0.2em] text-slate-500">Expected output</h2>
      <p class="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-200">{task.expected_output || 'No explicit output contract was provided.'}</p>
    </div>
  </div>
  <div class="mt-5 grid gap-3 border-t border-slate-800 pt-4 text-xs sm:grid-cols-2 lg:grid-cols-4">
    <div><span class="text-slate-500">Workflow</span><p class="mt-1 break-words text-slate-200">{workflowLabel}</p></div>
    <div><span class="text-slate-500">Project</span><p class="mt-1 break-words text-slate-200">{projectLabel}</p></div>
    <div><span class="text-slate-500">Agent</span><p class="mt-1 break-words text-slate-200">{agentLabel}</p></div>
    <div><span class="text-slate-500">Attempt</span><p class="mt-1 text-slate-200">#{task.attempt_number}</p></div>
  </div>
  <details class="mt-4 border-t border-slate-800 pt-3">
    <summary class="flex cursor-pointer list-none items-center gap-2 text-xs font-medium text-slate-400">
      <ChevronDown class="h-3.5 w-3.5" /> Technical metadata
    </summary>
    <dl class="mt-3 grid gap-x-5 gap-y-3 text-xs sm:grid-cols-2 lg:grid-cols-4">
      <div><dt class="text-slate-500">Source</dt><dd class="mt-1 break-all text-slate-300">{task.source_type}{task.source_ref ? ` · ${task.source_ref}` : ''}</dd></div>
      <div><dt class="text-slate-500">Created by</dt><dd class="mt-1 break-all text-slate-300">{task.created_by}</dd></div>
      <div><dt class="text-slate-500">Created</dt><dd class="mt-1 text-slate-300">{task.created_at ? formatAbsoluteTime(task.created_at) : 'Not recorded'}</dd></div>
      <div><dt class="text-slate-500">Updated</dt><dd class="mt-1 text-slate-300">{task.updated_at ? formatAbsoluteTime(task.updated_at) : 'Not recorded'}</dd></div>
      <div><dt class="text-slate-500">Started</dt><dd class="mt-1 text-slate-300">{task.started_at ? formatAbsoluteTime(task.started_at) : 'Not started'}</dd></div>
      <div><dt class="text-slate-500">Completed</dt><dd class="mt-1 text-slate-300">{task.completed_at ? formatAbsoluteTime(task.completed_at) : 'Not completed'}</dd></div>
      <div><dt class="text-slate-500">Queue</dt><dd class="mt-1 text-slate-300">{task.queue_name}</dd></div>
      <div><dt class="text-slate-500">Task ID</dt><dd class="mt-1 break-all font-mono text-slate-300">{task.task_id}</dd></div>
    </dl>
  </details>
</section>
