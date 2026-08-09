import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import StepCanvas from './StepCanvas.svelte';
import { createEmptyStep } from '$lib/workflows';

describe('StepCanvas', () => {
  it('selects and keyboard-reorders steps without drag interactions', async () => {
    const onselect = vi.fn();
    const onmove = vi.fn();
    const steps = [
      { ...createEmptyStep(), name: 'plan', objective: 'Plan the work.' },
      { ...createEmptyStep(), name: 'review', type: 'gate' as const, gateMessage: 'Approve the plan?' }
    ];
    render(StepCanvas, {
      phases: [{ id: 'main', title: 'Workflow', description: '' }],
      steps,
      selectedIndex: 0,
      onselect,
      onmove,
      onadd: vi.fn()
    });

    expect(screen.getByText('Plan the work.')).toBeInTheDocument();
    expect(screen.getByText('Approve the plan?')).toBeInTheDocument();
    await fireEvent.click(screen.getByTestId('workflow-step-card-1'));
    expect(onselect).toHaveBeenCalledWith(1);
    await fireEvent.click(screen.getByRole('button', { name: 'Move review up' }));
    expect(onmove).toHaveBeenCalledWith(1, -1);
  });

  it('renders a 20 phase by 8 step canvas without dropping steps', () => {
    const phases = Array.from({ length: 20 }, (_, index) => ({
      id: `phase-${index}`,
      title: `Phase ${index}`,
      description: ''
    }));
    const steps = phases.flatMap((phase, phaseIndex) =>
      Array.from({ length: 8 }, (_, stepIndex) => ({
        ...createEmptyStep(),
        phaseId: phase.id,
        name: `step-${phaseIndex}-${stepIndex}`
      }))
    );
    render(StepCanvas, {
      phases,
      steps,
      selectedIndex: 0,
      onselect: vi.fn(),
      onmove: vi.fn(),
      onadd: vi.fn()
    });

    expect(screen.getAllByTestId(/^workflow-step-card-/)).toHaveLength(160);
  });
});
