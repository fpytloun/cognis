import { describe, expect, it } from 'vitest';

import { formatStepQuestionResponse, normalizeStepQuestions } from '$lib/tool-call-question-set';
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
      'Architecture: Shared interaction primitive\nValidation: Backend API contract, Web UI rendering',
    );
  });
});
