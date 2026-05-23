<script lang="ts">
  import { onMount } from 'svelte';
  import type { TodoSnapshotItem } from '$lib/chat';

  let {
    todos,
    size = 'md',
    class: className = '',
    placement = 'bottom',
    label = 'Todo progress'
  } = $props<{
    todos: TodoSnapshotItem[];
    size?: 'sm' | 'md';
    class?: string;
    placement?: 'top' | 'bottom' | 'left' | 'right' | 'bottom-right';
    label?: string;
  }>();

  const DONUT_RADIUS = 7;
  const DONUT_CIRCUMFERENCE = 2 * Math.PI * DONUT_RADIUS;

  let open = $state(false);
  let root = $state<HTMLSpanElement | null>(null);
  let panelStyle = $state('');
  const visibleTodos = $derived.by(() => todos.filter((todo: TodoSnapshotItem) => todo.status !== 'cancelled'));
  const progress = $derived.by(() => summarizeTodos(todos));
  const dimensionClass = $derived(size === 'sm' ? 'h-4 w-4' : 'h-5 w-5');
  const statusTitle = $derived(progress.total > 0
    ? `${Math.round(progress.progress * 100)}% (${progress.completed}/${progress.total}) done`
    : 'No active todos'
  );

  function summarizeTodos(items: TodoSnapshotItem[]): {
    total: number;
    completed: number;
    progress: number;
  } {
    const activeTodos = items.filter((todo) => todo.status !== 'cancelled');
    const total = activeTodos.length;
    const completed = activeTodos.filter((todo) => todo.status === 'completed').length;
    const inProgress = activeTodos.filter((todo) => todo.status === 'in_progress').length;
    const weightedDone = completed + inProgress * 0.5;
    return {
      total,
      completed,
      progress: total > 0 ? Math.max(0, Math.min(weightedDone / total, 1)) : 0
    };
  }

  function donutDashOffset(progressValue: number): number {
    return DONUT_CIRCUMFERENCE * (1 - Math.max(0, Math.min(progressValue, 1)));
  }

  function statusClass(status: string): string {
    if (status === 'completed') return 'border-emerald-400/30 bg-emerald-400/10 text-emerald-100';
    if (status === 'in_progress') return 'border-sky-400/30 bg-sky-400/10 text-sky-100';
    return 'border-slate-700 bg-slate-800/70 text-slate-300';
  }

  function updatePosition(): void {
    if (!root) return;
    const rect = root.getBoundingClientRect();
    const maxWidth = 'min(22rem, calc(100vw - 2rem))';
    if (placement === 'top') {
      panelStyle = `position: fixed; left: ${rect.left + rect.width / 2}px; top: ${rect.top - 8}px; transform: translate(-50%, -100%); width: max-content; max-width: ${maxWidth};`;
    } else if (placement === 'left') {
      panelStyle = `position: fixed; left: ${rect.left - 8}px; top: ${rect.top + rect.height / 2}px; transform: translate(-100%, -50%); width: max-content; max-width: ${maxWidth};`;
    } else if (placement === 'right') {
      panelStyle = `position: fixed; left: ${rect.right + 8}px; top: ${rect.top + rect.height / 2}px; transform: translateY(-50%); width: max-content; max-width: ${maxWidth};`;
    } else if (placement === 'bottom-right') {
      const left = Math.min(rect.left + 8, window.innerWidth - 16);
      const top = Math.min(rect.bottom + 8, window.innerHeight - 16);
      panelStyle = `position: fixed; left: ${left}px; top: ${top}px; transform: none; width: max-content; max-width: ${maxWidth};`;
    } else {
      panelStyle = `position: fixed; left: ${rect.left + rect.width / 2}px; top: ${rect.bottom + 8}px; transform: translateX(-50%); width: max-content; max-width: ${maxWidth};`;
    }
  }

  function toggle(event: MouseEvent | PointerEvent): void {
    event.preventDefault();
    event.stopPropagation();
    if ('stopImmediatePropagation' in event) {
      event.stopImmediatePropagation();
    }
    open = !open;
    if (open) updatePosition();
  }

  function swallow(event: MouseEvent | PointerEvent): void {
    event.preventDefault();
    event.stopPropagation();
    if ('stopImmediatePropagation' in event) {
      event.stopImmediatePropagation();
    }
  }

  function onDocumentPointerDown(event: PointerEvent): void {
    if (!open) return;
    if (root && event.target instanceof Node && !root.contains(event.target)) {
      open = false;
    }
  }

  function onKey(event: KeyboardEvent): void {
    if (event.key === 'Escape') open = false;
  }

  onMount(() => {
    document.addEventListener('pointerdown', onDocumentPointerDown, true);
    document.addEventListener('scroll', updatePosition, true);
    window.addEventListener('resize', updatePosition);
    return () => {
      document.removeEventListener('pointerdown', onDocumentPointerDown, true);
      document.removeEventListener('scroll', updatePosition, true);
      window.removeEventListener('resize', updatePosition);
    };
  });

  $effect(() => {
    if (open) updatePosition();
  });
</script>

<svelte:window onkeydown={onKey} />

<span bind:this={root} class={`relative inline-flex ${className}`}>
  <button
    type="button"
    class={`inline-flex ${dimensionClass} items-center justify-center rounded-full text-current transition hover:scale-110 focus:outline-none focus:ring-2 focus:ring-emerald-300/60 focus:ring-offset-2 focus:ring-offset-slate-950`}
    aria-label={`${label}: ${Math.round(progress.progress * 100)}% complete`}
    aria-expanded={open}
    title={statusTitle}
    onpointerdown={toggle}
    onclick={swallow}
  >
    <svg class={`${dimensionClass} -rotate-90`} viewBox="0 0 20 20" aria-hidden="true">
      <circle cx="10" cy="10" r={DONUT_RADIUS} fill="none" stroke="currentColor" stroke-opacity="0.22" stroke-width="2.5" />
      <circle
        cx="10"
        cy="10"
        r={DONUT_RADIUS}
        fill="none"
        stroke="currentColor"
        stroke-linecap="round"
        stroke-width="2.5"
        stroke-dasharray={DONUT_CIRCUMFERENCE}
        stroke-dashoffset={donutDashOffset(progress.progress)}
      />
    </svg>
  </button>

  {#if open}
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="z-[2147483647] w-80 rounded-2xl border border-slate-700 bg-slate-950 p-3 text-left text-xs text-slate-200 shadow-2xl ring-1 ring-black/30"
      style={panelStyle}
      role="dialog"
      tabindex="-1"
      aria-label="Todo progress"
      onpointerdown={(event) => event.stopPropagation()}
    >
      <div class="mb-2 flex items-center justify-between gap-3">
        <p class="font-semibold uppercase tracking-[0.2em] text-slate-400">Todos</p>
        <span class="text-slate-500">{progress.completed}/{progress.total} done</span>
      </div>
      <div class="space-y-1.5">
        {#each visibleTodos as todo}
          <div class={`flex items-start gap-2 rounded-xl border px-2.5 py-2 leading-5 ${statusClass(todo.status)}`}>
            <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-current opacity-80"></span>
            <span class={`min-w-0 flex-1 break-words ${todo.status === 'completed' ? 'line-through opacity-70' : ''}`}>
              {todo.content}
            </span>
          </div>
        {/each}
      </div>
    </div>
  {/if}
</span>
