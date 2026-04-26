<script lang="ts">
  import FileSearch from 'lucide-svelte/icons/file-search';
import Logs from 'lucide-svelte/icons/logs';
  import type { WorkflowStepFormState } from '$lib/workflows';

  let {
    steps = [],
    interactionMode = 'explicit_gates',
    // Task-aware mode (optional — omit for workflow editor)
    activeStepName = '',
    selectedStepName = '',
    stepStatuses = {} as Record<string, string>,
    stepDurations = {} as Record<string, string>,
    stepAttemptCounts = {} as Record<string, number>,
    stepStateLabels = {} as Record<string, string>,
    stepHasLogs = {} as Record<string, boolean>,
    stepHasOutput = {} as Record<string, boolean>,
    skippedSteps = [] as string[],
    onStepSelect = (_stepName: string) => {},
    onStepLogsOpen = (_stepName: string) => {},
    onStepOutputOpen = (_stepName: string) => {},
  } = $props<{
    steps: WorkflowStepFormState[];
    interactionMode: string;
    activeStepName?: string;
    selectedStepName?: string;
    stepStatuses?: Record<string, string>;
    stepDurations?: Record<string, string>;
    stepAttemptCounts?: Record<string, number>;
    stepStateLabels?: Record<string, string>;
    stepHasLogs?: Record<string, boolean>;
    stepHasOutput?: Record<string, boolean>;
    skippedSteps?: string[];
    onStepSelect?: (stepName: string) => void;
    onStepLogsOpen?: (stepName: string) => void;
    onStepOutputOpen?: (stepName: string) => void;
  }>();

  let isTaskMode = $derived(activeStepName !== '' || selectedStepName !== '' || Object.keys(stepStatuses).length > 0);

  // Layout constants
  const NODE_W = 160;
  const NODE_H = 56;
  const GAP_X = 60;
  const PAD_X = 24;
  const PAD_TOP = 24;
  const LOOP_ARC_Y = 52;
  const BADGE_ROW_H = 22; // space for badges + duration below nodes

  interface RejectArc {
    fromIndex: number;
    toIndex: number;
    maxLoops: number;
    label: string;
  }

  function getRejectArcs(steps: WorkflowStepFormState[]): RejectArc[] {
    const arcs: RejectArc[] = [];
    const nameToIndex = new Map(steps.map((s, i) => [s.name, i]));
    for (let i = 0; i < steps.length; i++) {
      const step = steps[i];
      if (step.evaluatorRejectTarget) {
        const targetIdx = nameToIndex.get(step.evaluatorRejectTarget);
        if (targetIdx !== undefined && targetIdx < i) {
          arcs.push({
            fromIndex: i,
            toIndex: targetIdx,
            maxLoops: step.evaluatorRejectMaxLoops || 2,
            label: 'eval revise'
          });
        }
      }
      if (step.outcomeSuccessAction === 'revise' && step.outcomeSuccessTarget) {
        const targetIdx = nameToIndex.get(step.outcomeSuccessTarget);
        if (targetIdx !== undefined && targetIdx < i) {
          arcs.push({
            fromIndex: i,
            toIndex: targetIdx,
            maxLoops: step.outcomeSuccessMaxLoops || 2,
            label: 'success route'
          });
        }
      }
      if (step.outcomeRejectedAction === 'revise' && step.outcomeRejectedTarget) {
        const targetIdx = nameToIndex.get(step.outcomeRejectedTarget);
        if (targetIdx !== undefined && targetIdx < i) {
          arcs.push({
            fromIndex: i,
            toIndex: targetIdx,
            maxLoops: step.outcomeRejectedMaxLoops || 2,
            label: 'rejected outcome'
          });
        }
      }
      if (step.outcomeFailedAction === 'revise' && step.outcomeFailedTarget) {
        const targetIdx = nameToIndex.get(step.outcomeFailedTarget);
        if (targetIdx !== undefined && targetIdx < i) {
          arcs.push({
            fromIndex: i,
            toIndex: targetIdx,
            maxLoops: step.outcomeFailedMaxLoops || 2,
            label: 'failed outcome'
          });
        }
      }
    }
    return arcs;
  }

  function nodeX(index: number): number {
    return PAD_X + index * (NODE_W + GAP_X);
  }

  function arcY(arcIndex: number, totalArcs: number): number {
    return PAD_TOP + (totalArcs - 1 - arcIndex) * 18;
  }

  // Status-based styling for task mode. Status stays authoritative even when
  // selected, otherwise a completed selected step looks blue until focus moves.
  function statusColor(stepName: string, fallback: string): string {
    if (!isTaskMode) return fallback;
    if (skippedSteps.includes(stepName)) return '#334155';
    const status = stepStatuses[stepName];
    if (!status) return stepName === activeStepName ? '#0ea5e9' : fallback;
    if (status === 'approved' || status === 'completed') return '#059669';
    if (status === 'failed' || status === 'cancelled') return '#dc2626';
    if (status === 'rejected' || status === 'revise') return '#0284c7';
    if (status === 'evaluating') return '#a855f7';
    if (status === 'running') return '#0ea5e9';
    if (status === 'paused') return '#eab308';
    return fallback;
  }

  function nodeStroke(stepName: string, defaultStroke: string): string {
    return statusColor(stepName, defaultStroke);
  }

  function nodeAccent(stepName: string): string {
    return statusColor(stepName, '#0ea5e9');
  }

  function nodeStrokeWidth(stepName: string): string {
    if (isTaskMode && stepName === activeStepName) return '2.5';
    return '1.5';
  }

  function isSelected(stepName: string): boolean {
    return selectedStepName !== '' && selectedStepName === stepName;
  }

  function nodeFill(stepName: string, defaultFill: string, selected = false): string {
    if (!isTaskMode) return defaultFill;
    if (skippedSteps.includes(stepName)) return '#0c0a0940';
    const status = stepStatuses[stepName];
    if (status === 'approved' || status === 'completed') return selected ? '#05966924' : '#05966910';
    if (status === 'failed' || status === 'cancelled') return selected ? '#dc262624' : '#dc262610';
    if (status === 'evaluating') return selected ? '#a855f724' : '#a855f710';
    if (status === 'running') return selected ? '#0ea5e924' : '#0ea5e910';
    if (status === 'paused') return selected ? '#eab30824' : '#eab30810';
    if (selected) return '#0ea5e914';
    return defaultFill;
  }

  function nodeOpacity(stepName: string): string {
    if (isTaskMode && skippedSteps.includes(stepName)) return '0.4';
    return '1';
  }

  let arcs = $derived(getRejectArcs(steps));
  let arcHeadroom = $derived(arcs.length > 0 ? LOOP_ARC_Y + (arcs.length - 1) * 18 : 0);
  let nodesY = $derived(PAD_TOP + arcHeadroom);
  let svgW = $derived(Math.max(300, PAD_X * 2 + steps.length * NODE_W + (steps.length - 1) * GAP_X));
  let svgH = $derived(nodesY + NODE_H + BADGE_ROW_H + 16);

  function stepStatusLabel(stepName: string): string {
    return stepStateLabels[stepName] ?? '';
  }

  function attemptLabel(stepName: string): string {
    const attempts = stepAttemptCounts[stepName] ?? 0;
    if (attempts <= 1) return '';
    return `x${attempts}`;
  }

  function handleNodeSelect(stepName: string): void {
    if (!stepName) return;
    onStepSelect(stepName);
  }

  function handleNodeKeydown(event: KeyboardEvent, stepName: string): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    handleNodeSelect(stepName);
  }

  function handleStepLogsOpen(stepName: string): void {
    if (!stepHasLogs[stepName]) return;
    onStepLogsOpen(stepName);
  }

  function handleStepLogsClick(event: MouseEvent, stepName: string): void {
    event.stopPropagation();
    handleStepLogsOpen(stepName);
  }

  function handleStepOutputOpen(stepName: string): void {
    if (!stepHasOutput[stepName]) return;
    onStepOutputOpen(stepName);
  }

  function handleStepOutputClick(event: MouseEvent, stepName: string): void {
    event.stopPropagation();
    handleStepOutputOpen(stepName);
  }
</script>

{#if steps.length === 0}
  <p class="text-sm text-slate-500">Add steps to see the pipeline diagram.</p>
{:else}
  <div class="overflow-x-auto rounded-xl">
    <svg
      viewBox="0 0 {svgW} {svgH}"
      width={svgW}
      height={svgH}
      class="block"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <marker id="arrow" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <path d="M0,0 L8,3 L0,6" fill="#475569" />
        </marker>
        <marker id="arrow-sky" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
          <path d="M0,0 L8,3 L0,6" fill="#0284c7" />
        </marker>
        <!-- Pulsing animation for active step -->
        {#if isTaskMode}
          <style>
            @keyframes pulse-stroke {
              0%, 100% { opacity: 1; }
              50% { opacity: 0.5; }
            }
            @keyframes node-spin {
              from { stroke-dashoffset: 28; }
              to { stroke-dashoffset: 0; }
            }
            .node-active { animation: pulse-stroke 2s ease-in-out infinite; }
            .node-spinner { animation: node-spin 1s linear infinite; }
          </style>
        {/if}
      </defs>

      <!-- Forward flow arrows -->
      {#each steps as _, i}
        {#if i < steps.length - 1}
          <line
            x1={nodeX(i) + NODE_W}
            y1={nodesY + NODE_H / 2}
            x2={nodeX(i + 1)}
            y2={nodesY + NODE_H / 2}
            stroke="#475569"
            stroke-width="1.5"
            marker-end="url(#arrow)"
          />
        {/if}
      {/each}

      <!-- Reject loop arcs -->
      {#each arcs as arc, ai}
        {@const fromCx = nodeX(arc.fromIndex) + NODE_W / 2}
        {@const toCx = nodeX(arc.toIndex) + NODE_W / 2}
        {@const topY = arcY(ai, arcs.length)}
        <path
          d="M {fromCx} {nodesY} C {fromCx} {topY}, {toCx} {topY}, {toCx} {nodesY}"
          fill="none"
          stroke="#0284c7"
          stroke-width="1.5"
          stroke-dasharray="6 3"
          marker-end="url(#arrow-sky)"
        />
        <text
          x={(fromCx + toCx) / 2}
          y={topY - 4}
          text-anchor="middle"
          class="fill-sky-500 text-[10px]"
        >
          {arc.label} (max {arc.maxLoops})
        </text>
      {/each}

      <!-- Step nodes -->
      {#each steps as step, i}
        {@const x = nodeX(i)}
        {@const y = nodesY}
        {@const isGate = step.type === 'gate'}
        {@const hasAgent = !!step.agentOverride}
        {@const hasEval = step.evaluate && step.type === 'run'}
        {@const hasQuestions = step.allowQuestions && interactionMode === 'step_requests'}
        {@const isActive = isTaskMode && step.name === activeStepName}
        {@const selected = isTaskMode && isSelected(step.name)}
        {@const duration = stepDurations[step.name] ?? ''}
        {@const statusLabel = stepStatusLabel(step.name)}
        {@const attempt = attemptLabel(step.name)}
        {@const hasLogs = stepHasLogs[step.name] ?? false}
        {@const hasOutput = stepHasOutput[step.name] ?? false}

        {#if isGate}
          <!-- Gate: diamond shape -->
          <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions a11y_no_noninteractive_tabindex -->
          <g
            opacity={nodeOpacity(step.name)}
            onclick={() => handleNodeSelect(step.name)}
            onkeydown={(event) => handleNodeKeydown(event, step.name)}
            class={isTaskMode ? 'cursor-pointer' : ''}
            role={isTaskMode ? 'button' : undefined}
            tabindex={isTaskMode ? 0 : undefined}
          >
            <polygon
              points="{x + NODE_W / 2},{y} {x + NODE_W},{y + NODE_H / 2} {x + NODE_W / 2},{y + NODE_H} {x},{y + NODE_H / 2}"
              fill={nodeFill(step.name, '#1c1917', selected)}
              stroke={selected ? nodeAccent(step.name) : nodeStroke(step.name, '#0284c7')}
              stroke-width={selected ? '2.5' : nodeStrokeWidth(step.name)}
              class={isActive ? 'node-active' : ''}
            />
            <text
              x={x + NODE_W / 2}
              y={y + NODE_H / 2 - 4}
              text-anchor="middle"
              dominant-baseline="central"
              class="fill-sky-200 text-[12px] font-medium"
            >
              {step.name || `step_${i + 1}`}
            </text>
            <text
              x={x + NODE_W / 2}
              y={y + NODE_H / 2 + 12}
              text-anchor="middle"
              class="fill-sky-500/60 text-[9px] uppercase tracking-widest"
            >
              gate
            </text>
            {#if isActive}
              <g>
                <circle cx={x + NODE_W - 18} cy={y + 18} r="7" fill="none" stroke={`${nodeAccent(step.name)}33`} stroke-width="2" />
                <circle cx={x + NODE_W - 18} cy={y + 18} r="7" fill="none" stroke={nodeAccent(step.name)} stroke-width="2" stroke-linecap="round" stroke-dasharray="9 19" class="node-spinner" />
              </g>
            {/if}
            {#if attempt}
              <rect x={x + NODE_W - 28} y={y + NODE_H - 18} width="22" height="12" rx="6" fill="#0ea5e91a" stroke="#0ea5e966" stroke-width="0.75" />
              <text x={x + NODE_W - 17} y={y + NODE_H - 9} text-anchor="middle" class="fill-sky-300 text-[8px] font-semibold">{attempt}</text>
            {/if}
            {#if isTaskMode && (hasLogs || hasOutput)}
              <foreignObject x={x + 4} y={y + 4} width="64" height="30">
                <div class="flex gap-1">
                {#if hasOutput}
                <button
                  class="flex h-7 w-7 md:h-5 md:w-5 items-center justify-center rounded-md border border-slate-600/80 bg-slate-950/90 text-slate-200 transition hover:border-cyan-400/60 hover:text-white"
                  onclick={(event) => handleStepOutputClick(event, step.name)}
                  type="button"
                  aria-label={`Open full output for ${step.name}`}
                  title="Open full output"
                >
                  <FileSearch class="h-4 w-4 md:h-3 md:w-3" />
                </button>
                {/if}
                {#if hasLogs}
                <button
                  class="flex h-7 w-7 md:h-5 md:w-5 items-center justify-center rounded-md border border-slate-600/80 bg-slate-950/90 text-slate-200 transition hover:border-sky-400/60 hover:text-white"
                  onclick={(event) => handleStepLogsClick(event, step.name)}
                  type="button"
                  aria-label={`Open logs for ${step.name}`}
                  title="Open logs"
                >
                  <Logs class="h-4 w-4 md:h-3 md:w-3" />
                </button>
                {/if}
                </div>
              </foreignObject>
            {/if}
          </g>
        {:else}
          <!-- Run: rounded rectangle -->
          <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions a11y_no_noninteractive_tabindex -->
          <g
            opacity={nodeOpacity(step.name)}
            onclick={() => handleNodeSelect(step.name)}
            onkeydown={(event) => handleNodeKeydown(event, step.name)}
            class={isTaskMode ? 'cursor-pointer' : ''}
            role={isTaskMode ? 'button' : undefined}
            tabindex={isTaskMode ? 0 : undefined}
          >
            <rect
              {x}
              {y}
              width={NODE_W}
              height={NODE_H}
              rx="12"
              fill={nodeFill(step.name, '#0c0a09', selected)}
              stroke={selected ? nodeAccent(step.name) : isActive ? nodeAccent(step.name) : nodeStroke(step.name, hasAgent ? '#0ea5e9' : '#334155')}
              stroke-width={selected ? '2.5' : nodeStrokeWidth(step.name)}
              class={isActive ? 'node-active' : ''}
            />
            <text
              x={x + NODE_W / 2}
              y={y + (hasAgent ? NODE_H / 2 - 6 : NODE_H / 2)}
              text-anchor="middle"
              dominant-baseline="central"
              class="fill-slate-100 text-[12px] font-medium"
            >
              {step.name || `step_${i + 1}`}
            </text>
            {#if hasAgent}
              <text
                x={x + NODE_W / 2}
                y={y + NODE_H / 2 + 10}
                text-anchor="middle"
                class="fill-sky-400/70 text-[9px]"
              >
                {step.agentOverride}
              </text>
            {/if}
            {#if isActive}
              <g>
                <circle cx={x + NODE_W - 16} cy={y + 16} r="7" fill="none" stroke={`${nodeAccent(step.name)}33`} stroke-width="2" />
                <circle cx={x + NODE_W - 16} cy={y + 16} r="7" fill="none" stroke={nodeAccent(step.name)} stroke-width="2" stroke-linecap="round" stroke-dasharray="9 19" class="node-spinner" />
              </g>
            {/if}
            {#if attempt}
              <rect x={x + NODE_W - 28} y={y + NODE_H - 18} width="22" height="12" rx="6" fill="#0ea5e91a" stroke="#38bdf866" stroke-width="0.75" />
              <text x={x + NODE_W - 17} y={y + NODE_H - 9} text-anchor="middle" class="fill-sky-300 text-[8px] font-semibold">{attempt}</text>
            {/if}
            {#if isTaskMode && (hasLogs || hasOutput)}
              <foreignObject x={x + 4} y={y + 4} width="64" height="30">
                <div class="flex gap-1">
                {#if hasOutput}
                <button
                  class="flex h-7 w-7 md:h-5 md:w-5 items-center justify-center rounded-md border border-slate-600/80 bg-slate-950/90 text-slate-200 transition hover:border-cyan-400/60 hover:text-white"
                  onclick={(event) => handleStepOutputClick(event, step.name)}
                  type="button"
                  aria-label={`Open full output for ${step.name}`}
                  title="Open full output"
                >
                  <FileSearch class="h-4 w-4 md:h-3 md:w-3" />
                </button>
                {/if}
                {#if hasLogs}
                <button
                  class="flex h-7 w-7 md:h-5 md:w-5 items-center justify-center rounded-md border border-slate-600/80 bg-slate-950/90 text-slate-200 transition hover:border-sky-400/60 hover:text-white"
                  onclick={(event) => handleStepLogsClick(event, step.name)}
                  type="button"
                  aria-label={`Open logs for ${step.name}`}
                  title="Open logs"
                >
                  <Logs class="h-4 w-4 md:h-3 md:w-3" />
                </button>
                {/if}
                </div>
              </foreignObject>
            {/if}
          </g>
        {/if}

        <!-- Below-node row: badges (editor mode) or duration (task mode) -->
        {#if isTaskMode}
          {#if statusLabel}
            <text
              x={x + NODE_W / 2}
              y={y + NODE_H + 14}
              text-anchor="middle"
              class="fill-slate-300 text-[9px]"
              opacity={nodeOpacity(step.name)}
            >
              {statusLabel}
            </text>
          {/if}
          {#if duration}
            <text
              x={x + NODE_W / 2}
              y={y + NODE_H + (statusLabel ? 25 : 16)}
              text-anchor="middle"
              class="fill-slate-400 text-[10px]"
              opacity={nodeOpacity(step.name)}
            >
              {duration}
            </text>
          {/if}
        {:else if !isTaskMode}
          {#if hasEval || hasQuestions}
            {@const badges = [
              ...(hasEval ? ['eval'] : []),
              ...(hasQuestions ? ['ask'] : []),
            ]}
            {#each badges as badge, bi}
              {@const bx = x + NODE_W / 2 - (badges.length * 18) + bi * 36}
              <rect
                x={bx}
                y={y + NODE_H + 4}
                width="32"
                height="14"
                rx="7"
                fill={badge === 'eval' ? '#065f4620' : '#0284c720'}
                stroke={badge === 'eval' ? '#065f46' : '#0284c7'}
                stroke-width="0.75"
              />
              <text
                x={bx + 16}
                y={y + NODE_H + 13}
                text-anchor="middle"
                class="text-[8px] {badge === 'eval' ? 'fill-emerald-400' : 'fill-cyan-400'}"
              >
                {badge === 'eval' ? 'eval' : 'ask'}
              </text>
            {/each}
          {/if}
        {/if}

        <!-- Input mode label on incoming arrow (editor mode only) -->
        {#if !isTaskMode && i > 0 && step.inputMode && step.inputMode !== 'null' && step.inputMode !== 'auto'}
          <text
            x={nodeX(i) - GAP_X / 2}
            y={nodesY + NODE_H / 2 - 8}
            text-anchor="middle"
            class="fill-slate-500 text-[9px]"
          >
            {step.inputMode === 'last' ? 'output' : step.inputMode}
          </text>
        {/if}
      {/each}
    </svg>
  </div>
{/if}
