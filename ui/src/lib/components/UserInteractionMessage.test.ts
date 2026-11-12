import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it } from 'vitest';
import UserInteractionMessage from './UserInteractionMessage.svelte';
import type { UserInteractionTimelineItem } from '$lib/timeline-render-model';

afterEach(cleanup);

const item: UserInteractionTimelineItem = {
  id: 'question-card', kind: 'user_interaction', timestamp: null,
  interactionType: 'step_question', title: 'You answered questions', status: 'complete',
  answers: [
    { question: 'Use **staging**?\n\n- Run `smoke` first', answer: 'Yes, staging only.' },
    { question: 'Which checks?', answer: 'Smoke tests\nThen integration tests' },
    { question: 'Publish?', answer: 'No, keep it local.' },
  ],
};

it('pages question-answer pairs with accessible bounds and rendered Markdown', async () => {
  const { container } = render(UserInteractionMessage, { item });
  expect(screen.getByText('You answered 3 questions')).toBeTruthy();
  expect(screen.getByText('Answer 1 / 3')).toBeTruthy();
  expect(container.querySelector('strong')?.textContent).toBe('staging');
  expect(container.querySelector('code')?.textContent).toBe('smoke');
  expect(screen.getByRole('group', { name: 'Agent request content' }).textContent).not.toContain('Which checks?');
  expect((screen.getByRole('button', { name: 'Previous answer' }) as HTMLButtonElement).disabled).toBe(true);
  await fireEvent.click(screen.getByRole('button', { name: 'Next answer' }));
  expect(screen.getByText('Answer 2 / 3')).toBeTruthy();
  expect(screen.getByText('Which checks?')).toBeTruthy();
  expect(screen.getByRole('group', { name: 'Your answer content' }).textContent).not.toContain('Yes, staging only.');
  await fireEvent.click(screen.getByRole('button', { name: 'Next answer' }));
  expect(screen.getByText('Answer 3 / 3')).toBeTruthy();
  expect((screen.getByRole('button', { name: 'Next answer' }) as HTMLButtonElement).disabled).toBe(true);
  await fireEvent.click(screen.getByRole('button', { name: 'Previous answer' }));
  expect(screen.getByText('Answer 2 / 3')).toBeTruthy();
});

it('uses singular copy without pagination for one answer and sanitizes requests', () => {
  const { container } = render(UserInteractionMessage, {
    item: { ...item, answers: [{ question: 'Continue? <script>alert(1)</script>', answer: '<b>Keep this literal</b>' }] },
  });
  expect(screen.getByText('You answered question')).toBeTruthy();
  expect(screen.queryByRole('navigation')).toBeNull();
  expect(container.querySelector('script')).toBeNull();
  expect(screen.getByText('<b>Keep this literal</b>')).toBeTruthy();
});

it('resets pagination when the component receives a different interaction', async () => {
  const { rerender } = render(UserInteractionMessage, { item });
  await fireEvent.click(screen.getByRole('button', { name: 'Next answer' }));
  await rerender({ item: { ...item, id: 'another-question-card' } });
  expect(screen.getByText('Answer 1 / 3')).toBeTruthy();
});
