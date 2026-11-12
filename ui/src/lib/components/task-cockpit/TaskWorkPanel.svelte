<script module lang="ts">
  import type TaskWorkPanelBody from './TaskWorkPanelBody.svelte';
  let request: Promise<typeof TaskWorkPanelBody> | null = null;
  function loadPanel(): Promise<typeof TaskWorkPanelBody> {
    return request ??= import('./TaskWorkPanelBody.svelte')
      .then(module => module.default)
      .catch(error => { request = null; throw error; });
  }
</script>

<script lang="ts">
  import { onMount, untrack, type ComponentProps } from 'svelte';
  import TaskDeliverableResult from './TaskDeliverableResult.svelte';
  let props: Omit<ComponentProps<typeof TaskWorkPanelBody>, 'hideResult'> = $props();
  const view = $derived(props.view ?? 'all');
  const inline = $derived(props.finalDeliverable
    && (!props.canonicalDeliverableId || props.finalDeliverable.deliverable_id === props.canonicalDeliverableId)
    ? props.finalDeliverable : null);
  const result = $derived(props.outputPresentation?.deliverable_id
    ? {
      deliverable_id: props.outputPresentation.deliverable_id,
      step_run_id: props.outputPresentation.step_run_id,
      format: props.outputPresentation.format,
      title: props.outputPresentation.title,
      content: '',
      task_freshness: props.outputPresentation.freshness,
    }
    : props.outputPresentation === undefined ? inline : null);
  const showInline = $derived(view !== 'activity' && result !== null);
  const needsActivity = $derived(view !== 'deliverable' || !result);
  let Panel = $state.raw<typeof TaskWorkPanelBody | null>(null);
  let error = $state(false);
  let mounted = $state(false);
  let loading = false;
  async function load(): Promise<void> {
    if (Panel || loading) return;
    loading = true;
    error = false;
    try {
      const component = await loadPanel();
      if (mounted) Panel = component;
    } catch {
      if (mounted) error = true;
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    mounted = true;
    return () => { mounted = false; };
  });
  $effect(() => {
    if (mounted && needsActivity) untrack(() => void load());
  });
</script>

{#if showInline && result}
  <TaskDeliverableResult {result} inline={inline?.deliverable_id === result.deliverable_id ? inline : null} stepRunId={result.step_run_id}
    taskFreshness={result.task_freshness}
    collapsedByDefault={props.deliverableCollapsedByDefault ?? true}
    loadDeliverableForStepRun={props.loadDeliverableForStepRun} />
{/if}
{#if needsActivity}
  {#if Panel}
    <Panel {...props} hideResult={showInline} />
  {:else if error}
    <div role="alert" class="text-sm text-secondary">
      <p>Could not load task activity.</p>
      <button type="button" class="text-accent" onclick={load}>Try again</button>
    </div>
  {:else}
    <p role="status" class="text-sm text-muted">Loading task activity…</p>
  {/if}
{/if}
