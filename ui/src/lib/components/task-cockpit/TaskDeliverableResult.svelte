<script lang="ts">
  import AssistantDeliverableBlock from '$lib/components/AssistantDeliverableBlock.svelte';
  import type { AssistantDeliverableTimelineItem } from '$lib/chat-v2/types';
  import type { Deliverable } from '$lib/types/api';
  import { api } from '$lib/api/client';

  let {
    result, inline = null, stepRunId = null, collapsedByDefault = true, taskFreshness = null,
    loadDeliverableForStepRun = (step: string, id: string) => api.deliverables.getForStepRun(step, id),
  } = $props<{
    result: Pick<AssistantDeliverableTimelineItem,
      'deliverable_id' | 'format' | 'title' | 'content' | 'render_metadata' | 'export_metadata'>;
    inline?: Deliverable | null;
    stepRunId?: string | null;
    collapsedByDefault?: boolean;
    taskFreshness?: Deliverable['task_freshness'];
    loadDeliverableForStepRun?: (step: string, id: string) => Promise<Deliverable>;
  }>();
  const documentId = $derived(result.deliverable_id);
  const format = $derived(result.format);
  const title = $derived(result.title);
  const content = $derived(result.content);
  const renderMetadata = $derived(result.render_metadata);
  const exportMetadata = $derived(result.export_metadata);
  const item: AssistantDeliverableTimelineItem = $derived({
    deliverable_id: documentId, format, title, content,
    render_metadata: renderMetadata, export_metadata: exportMetadata,
    id: `deliverable:${documentId}`, kind: 'assistant_deliverable',
    deliverable: inline, lineage_id: inline?.lineage_id, revision: inline?.revision,
    sort_key: '', source_refs: [], stable: true,
  });
  function load(id: string): Promise<Deliverable> {
    if (!stepRunId) return Promise.reject(new Error('The task result has no step run.'));
    return loadDeliverableForStepRun(stepRunId, id);
  }
</script>

<section class="mt-4 border-t border-separator pt-4" aria-label="Task result" data-testid="task-final-result">
  <p class="mb-2 text-[10px] font-semibold uppercase tracking-widest text-success">Task result</p>
  <AssistantDeliverableBlock {item} {collapsedByDefault} inlineDisclosure
    taskFreshness={taskFreshness ?? inline?.task_freshness ?? null}
    loadDeliverable={stepRunId ? load : undefined} displayOnly={!inline && !stepRunId} />
</section>
