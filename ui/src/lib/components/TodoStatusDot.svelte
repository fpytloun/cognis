<script lang="ts">
  import { cn } from '$lib/utils';

  let {
    status,
    class: className = 'h-2 w-2',
    labelled = true
  } = $props<{
    status: string;
    class?: string;
    labelled?: boolean;
  }>();

  const normalized = $derived(status.trim().toLowerCase());
  const inProgress = $derived(['in_progress', 'active', 'running'].includes(normalized));
  const colorClass = $derived(
    ['completed', 'complete', 'done'].includes(normalized)
      ? 'bg-emerald-400'
      : ['cancelled', 'canceled'].includes(normalized)
        ? 'bg-slate-600'
        : inProgress
          ? 'bg-sky-400'
          : 'bg-slate-400'
  );
  const label = $derived(normalized.replaceAll('_', ' ') || 'pending');
</script>

<span
  class={cn(
    'inline-block shrink-0 rounded-full',
    colorClass,
    inProgress && 'todo-status-dot--in-progress',
    className
  )}
  data-testid="todo-status-dot"
  data-todo-status={normalized}
  data-animated={inProgress ? 'true' : 'false'}
  aria-label={labelled ? label : undefined}
  aria-hidden={labelled ? undefined : 'true'}
  title={labelled ? label : undefined}
></span>

<style>
  .todo-status-dot--in-progress {
    animation: todo-gentle-pulse 2.8s ease-in-out infinite;
    transform-origin: center;
  }

  @media (prefers-reduced-motion: reduce) {
    .todo-status-dot--in-progress {
      animation: none;
    }
  }

  @keyframes todo-gentle-pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.65; transform: scale(0.88); }
  }
</style>
