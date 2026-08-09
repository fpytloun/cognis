<script lang="ts">
  import ArrowDown from 'lucide-svelte/icons/arrow-down';
  import ArrowUp from 'lucide-svelte/icons/arrow-up';
  import Bot from 'lucide-svelte/icons/bot';
  import CheckCircle from 'lucide-svelte/icons/check-circle';
  import GitBranch from 'lucide-svelte/icons/git-branch';
  import Hand from 'lucide-svelte/icons/hand';
  import Wrench from 'lucide-svelte/icons/wrench';

  import type { WorkflowPhaseFormState, WorkflowStepFormState } from '$lib/workflows';
  import { workflowStepSummary } from '$lib/workflows';

  export let phases: WorkflowPhaseFormState[] = [];
  export let steps: WorkflowStepFormState[] = [];
  export let selectedIndex = 0;
  export let disabled = false;
  export let onselect: (index: number) => void;
  export let onmove: (index: number, delta: -1 | 1) => void;
  export let onadd: (phaseId: string) => void;

  const accents: Record<WorkflowStepFormState['type'], string> = {
    run: 'border-l-emerald-400',
    gate: 'border-l-amber-400',
    tool_call: 'border-l-violet-400',
    condition: 'border-l-sky-400',
    complete: 'border-l-teal-400'
  };
</script>

<div class="space-y-4" data-testid="workflow-step-canvas">
  {#each phases as phase}
    <section class="rounded-2xl border border-slate-800 bg-slate-950/40 p-3" data-testid={`workflow-canvas-phase-${phase.id}`}>
      <div class="mb-2 flex items-center justify-between gap-2">
        <div class="min-w-0">
          <h3 class="truncate text-sm font-semibold text-slate-100">{phase.title}</h3>
          {#if phase.description}<p class="truncate text-xs text-slate-500">{phase.description}</p>{/if}
        </div>
        <button class="h-9 rounded-xl border border-slate-700 px-3 text-xs text-slate-300 hover:border-slate-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400 disabled:opacity-40" type="button" disabled={disabled} onclick={() => onadd(phase.id)}>Add step</button>
      </div>
      <div class="space-y-2" role="list" aria-label={`${phase.title} steps`}>
        {#each steps as step, index}
          {#if step.phaseId === phase.id}
            <div class="flex items-stretch gap-1">
              <button
                type="button"
                class={`min-w-0 flex-1 rounded-xl border border-l-4 px-3 py-2 text-left focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400 ${accents[step.type]} ${selectedIndex === index ? 'border-sky-400/60 bg-sky-500/10' : 'border-slate-800 bg-slate-900/70 hover:border-slate-600'}`}
                aria-current={selectedIndex === index ? 'true' : undefined}
                data-testid={`workflow-step-card-${index}`}
                onclick={() => onselect(index)}
              >
                <span class="flex items-center gap-2">
                  {#if step.type === 'run'}<Bot class="h-4 w-4 text-emerald-300" aria-label="Agent step" />
                  {:else if step.type === 'gate'}<Hand class="h-4 w-4 text-amber-300" aria-label="Gate step" />
                  {:else if step.type === 'tool_call'}<Wrench class="h-4 w-4 text-violet-300" aria-label="Tool step" />
                  {:else if step.type === 'condition'}<GitBranch class="h-4 w-4 text-sky-300" aria-label="Condition step" />
                  {:else}<CheckCircle class="h-4 w-4 text-teal-300" aria-label="Complete step" />{/if}
                  <span class="truncate text-sm font-medium text-slate-100">{step.name || `Step ${index + 1}`}</span>
                </span>
                <span class="mt-1 block truncate text-xs text-slate-400">{workflowStepSummary(step)}</span>
              </button>
              <div class="grid shrink-0 grid-rows-2">
                <button class="w-9 rounded-t-lg text-slate-400 hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400 disabled:opacity-30" aria-label={`Move ${step.name || `step ${index + 1}`} up`} disabled={disabled || index === 0} type="button" onclick={() => onmove(index, -1)}><ArrowUp class="mx-auto h-3.5 w-3.5" /></button>
                <button class="w-9 rounded-b-lg text-slate-400 hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400 disabled:opacity-30" aria-label={`Move ${step.name || `step ${index + 1}`} down`} disabled={disabled || index === steps.length - 1} type="button" onclick={() => onmove(index, 1)}><ArrowDown class="mx-auto h-3.5 w-3.5" /></button>
              </div>
            </div>
          {/if}
        {/each}
      </div>
    </section>
  {/each}
</div>
