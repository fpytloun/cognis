import { describe, expect, it } from 'vitest';

import { formatStepQuestionResponse, normalizeStepQuestionAnswers, normalizeStepQuestions } from '$lib/tool-call-question-set';
import type { ToolCallTimelineItem } from '$lib/chat';

function questionToolItem(overrides: Partial<ToolCallTimelineItem> = {}): ToolCallTimelineItem {
  return {
    id: 'tool_call_1',
    kind: 'tool_call',
    callId: 'call_1',
    toolName: 'step_request_questions',
    status: 'completed',
    timestamp: null,
    arguments: {
      questions: [
        {
          id: 'architecture',
          header: 'Architecture',
          question: 'Which architecture should we use?',
          options: [
            { id: 'shared', label: 'Shared interaction primitive' },
            { id: 'workflow', label: 'Workflow-only interaction' },
          ],
          multiple: false,
          allow_custom: true,
          required: true,
        },
        {
          id: 'validation',
          header: 'Validation',
          question: 'Which validation areas should be included?',
          options: [
            { id: 'api', label: 'Backend API contract' },
            { id: 'ui', label: 'Web UI rendering' },
          ],
          multiple: true,
          allow_custom: false,
          required: true,
        },
      ],
    },
    ...overrides,
  };
}

describe('tool-call question sets', () => {
  it('normalizes structured tool arguments', () => {
    const questions = normalizeStepQuestions(questionToolItem());

    expect(questions).toHaveLength(2);
    expect(questions[0]).toMatchObject({
      id: 'architecture',
      header: 'Architecture',
      question: 'Which architecture should we use?',
      multiple: false,
      allow_custom: true,
      required: true,
    });
    expect(questions[1]?.options.map((option) => option.label)).toEqual([
      'Backend API contract',
      'Web UI rendering',
    ]);
  });

  it('formats structured answers with option labels', () => {
    const formatted = formatStepQuestionResponse(questionToolItem(), {
      mode: 'structured',
      answers: [
        { question_id: 'architecture', selected_option_ids: ['shared'], custom_answer: null },
        { question_id: 'validation', selected_option_ids: ['api', 'ui'], custom_answer: null },
      ],
    });

    expect(formatted).toBe(
      'Which architecture should we use?: Shared interaction primitive\nWhich validation areas should be included?: Backend API contract, Web UI rendering',
    );
  });

  it('formats nested structured replies and skips unanswered questions', () => {
    const formatted = formatStepQuestionResponse(questionToolItem(), {
      reply: {
        mode: 'structured',
        answers: [
          { question_id: 'architecture', selected_option_ids: ['workflow'], custom_answer: null },
          { question_id: 'validation', selected_option_ids: [], custom_answer: null },
        ],
      },
    });

    expect(formatted).toBe('Which architecture should we use?: Workflow-only interaction');
  });

  it('normalizes submitted answers in input question order with question text', () => {
    const answers = normalizeStepQuestionAnswers(questionToolItem(), {
      mode: 'structured',
      answers: [
        { question_id: 'validation', selected_option_ids: ['api', 'ui'], custom_answer: null },
        { question_id: 'architecture', selected_option_ids: ['shared'], custom_answer: 'plus notes' },
      ],
    });

    expect(answers.map((answer) => answer.question.question)).toEqual([
      'Which architecture should we use?',
      'Which validation areas should be included?',
    ]);
    expect(answers[0]?.question.header).toBe('Architecture');
    expect(answers[0]?.selected.map((option) => option.label)).toEqual(['Shared interaction primitive']);
    expect(answers[0]?.custom).toBe('plus notes');
    expect(answers[1]?.selected.map((option) => option.label)).toEqual([
      'Backend API contract',
      'Web UI rendering',
    ]);
  });

  it('normalizes nested response answers and preserves unknown option ids', () => {
    const answers = normalizeStepQuestionAnswers(questionToolItem(), {
      response: {
        mode: 'structured',
        answers: [
          { question_id: 'architecture', selected_option_ids: ['missing_option'], custom_answer: null },
        ],
      },
    });

    expect(answers).toHaveLength(1);
    expect(answers[0]?.selected).toEqual([
      { id: 'missing_option', label: 'missing_option', unknown: true },
    ]);
  });

  it('normalizes custom-only legacy question answers', () => {
    const answers = normalizeStepQuestionAnswers(
      questionToolItem({
        arguments: {
          question: 'Approve this change?',
          options: [{ label: 'Approve' }, { label: 'Reject' }],
        },
      }),
      {
        answers: [
          { question_id: 'q1', selected_option_ids: [], custom_answer: 'Approve with mobile QA' },
        ],
      },
    );

    expect(answers).toHaveLength(1);
    expect(answers[0]?.question.question).toBe('Approve this change?');
    expect(answers[0]?.custom).toBe('Approve with mobile QA');
  });

  it('returns plain string response when no structured answers are present', () => {
    expect(formatStepQuestionResponse(questionToolItem(), { response: 'continue' })).toBe('continue');
  });
});
