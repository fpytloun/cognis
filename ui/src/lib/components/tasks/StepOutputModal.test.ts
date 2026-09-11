import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import { api } from '$lib/api/client';
import type { Deliverable, StepRun } from '$lib/types/api';
import StepOutputModal from './StepOutputModal.svelte';

vi.mock('$lib/api/client', () => ({
  api: {
    deliverables: {
      getForStepRun: vi.fn(),
    },
  },
}));

function runWithoutDeliverables(): StepRun {
  return {
    step_run_id: 'run-projection',
    task_id: 'task-1',
    step_name: 'prepare',
    step_type: 'run',
    status: 'completed',
    agent_id: 'riker',
    attempt: 1,
    is_projection: false,
    output: {
      summary: 'Hydrated output without a deliverable collection.',
      content: 'Result body',
    },
    evaluation: null,
    runtime_info: null,
    todos: [],
    started_at: null,
    completed_at: null,
    updated_at: null,
  } as unknown as StepRun;
}

function projectedRichRun(): StepRun {
  return {
    ...runWithoutDeliverables(),
    step_run_id: 'run-rich',
    step_name: 'operate',
    status: 'approved',
    deliverable_id: 'deliverable-rich',
    deliverables: [{
      deliverable_id: 'deliverable-rich',
      step_run_id: 'run-rich',
      version: 1,
      content: '',
      format: 'rich',
      title: 'Operations dashboard',
      outputs: {},
      rich_payload: null,
      validation_warnings: [],
      render_metadata: { block_count: 2 },
      export_metadata: {},
      status: 'approved',
      evaluator_feedback: null,
      created_at: null,
      updated_at: null,
    }],
  } as unknown as StepRun;
}

function hydratedRichDeliverable(richPayload: Deliverable['rich_payload']): Deliverable {
  return {
    ...(projectedRichRun().deliverables?.[0] as Deliverable),
    content: 'Fallback content',
    rich_payload: richPayload,
  };
}

describe('StepOutputModal', () => {
  it('renders an output whose partial projection omits deliverables', async () => {
    const escapedToParent = vi.fn();
    window.addEventListener('keydown', escapedToParent);
    const view = render(StepOutputModal, {
      stepRun: runWithoutDeliverables(),
      agentName: 'Riker',
      visibleStatus: 'completed',
      onclose: vi.fn(),
    });

    expect(await screen.findByRole('dialog', { name: 'Step output: prepare' })).toBeVisible();
    expect(screen.getByText('Hydrated output without a deliverable collection.')).toBeVisible();
    expect(screen.queryByText(/Deliverable v/)).not.toBeInTheDocument();
    await fireEvent.keyDown(document, { key: 'Escape' });
    expect(escapedToParent).not.toHaveBeenCalled();
    view.unmount();
    window.removeEventListener('keydown', escapedToParent);
    await Promise.resolve();
  });

  it('does not render an empty rich shell while the full payload is loading', async () => {
    vi.mocked(api.deliverables.getForStepRun).mockReturnValue(new Promise(() => {}));

    const view = render(StepOutputModal, {
      stepRun: projectedRichRun(),
      agentName: 'Riker',
      visibleStatus: 'failed',
      onclose: vi.fn(),
    });

    expect(await screen.findByText('Loading full deliverable…')).toBeVisible();
    expect(screen.getAllByText('Operations dashboard')).toHaveLength(1);
    expect(document.querySelector('.rich-deliverable')).not.toBeInTheDocument();
    view.unmount();
  });

  it('renders the original rich blocks after hydration for a failed task step', async () => {
    vi.mocked(api.deliverables.getForStepRun).mockResolvedValue(hydratedRichDeliverable({
      blocks: [{ type: 'markdown', content: 'Persisted rich body' }],
      assets: [],
      sources: [],
      datasets: [],
      exports: [],
      metadata: {},
    }));

    const view = render(StepOutputModal, {
      stepRun: projectedRichRun(),
      agentName: 'Riker',
      visibleStatus: 'failed',
      onclose: vi.fn(),
    });

    expect(await screen.findByText('Persisted rich body')).toBeVisible();
    expect(screen.queryByText('Loading full deliverable…')).not.toBeInTheDocument();
    view.unmount();
  });

  it.each([
    ['missing', null],
    ['empty', {
      blocks: [],
      assets: [],
      sources: [],
      datasets: [],
      exports: [],
      metadata: {},
    }],
  ])('shows a load error for a %s rich payload', async (_case, richPayload) => {
    vi.mocked(api.deliverables.getForStepRun).mockResolvedValue(
      hydratedRichDeliverable(richPayload as Deliverable['rich_payload'])
    );

    const view = render(StepOutputModal, {
      stepRun: projectedRichRun(),
      agentName: 'Riker',
      visibleStatus: 'failed',
      onclose: vi.fn(),
    });

    expect(await screen.findByText('Full deliverable content could not be loaded.')).toBeVisible();
    expect(document.querySelector('.rich-deliverable')).not.toBeInTheDocument();
    view.unmount();
  });
});
