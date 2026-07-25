import type { ToolCallTimelineItem } from '$lib/timeline-render-model';

export type StepQuestionOption = { id: string; label: string; description?: string };
export type StepQuestion = {
  id: string;
  question: string;
  header?: string;
  options: StepQuestionOption[];
  multiple?: boolean;
  allow_custom?: boolean;
  required?: boolean;
};

export type StepQuestionAnswerChoice = StepQuestionOption & { unknown?: boolean };
export type StepQuestionAnswer = {
  question: StepQuestion;
  selected: StepQuestionAnswerChoice[];
  custom: string;
};

export function normalizeStepQuestions(item: Pick<ToolCallTimelineItem, 'arguments'>): StepQuestion[] {
  const rawQuestions = item.arguments?.questions;
  if (Array.isArray(rawQuestions)) {
    const questions: StepQuestion[] = [];
    rawQuestions.forEach((raw, index) => {
      if (!raw || typeof raw !== 'object') return;
      const record = raw as Record<string, unknown>;
      const question = typeof record.question === 'string' ? record.question.trim() : '';
      if (!question) return;
      const options: StepQuestionOption[] = [];
      if (Array.isArray(record.options)) {
        record.options.forEach((option, optionIndex) => {
          if (typeof option === 'string') {
            options.push({ id: option, label: option });
            return;
          }
          if (!option || typeof option !== 'object') return;
          const optionRecord = option as Record<string, unknown>;
          const label = typeof optionRecord.label === 'string' ? optionRecord.label.trim() : '';
          if (!label) return;
          options.push({
            id: typeof optionRecord.id === 'string' && optionRecord.id.trim()
              ? optionRecord.id.trim()
              : `option_${optionIndex + 1}`,
            label,
            description: typeof optionRecord.description === 'string' ? optionRecord.description : undefined,
          });
        });
      }
      questions.push({
        id: typeof record.id === 'string' && record.id.trim() ? record.id.trim() : `q${index + 1}`,
        header: typeof record.header === 'string' ? record.header : undefined,
        question,
        options,
        multiple: record.multiple === true,
        allow_custom: record.allow_custom !== false,
        required: record.required !== false,
      });
    });
    return questions;
  }

  const legacyQuestion = typeof item.arguments?.question === 'string' ? item.arguments.question.trim() : '';
  if (!legacyQuestion) return [];
  return [
    {
      id: 'q1',
      question: legacyQuestion,
      options: legacyStepRequestOptions(item).map((option, index) => ({
        id: `option_${index + 1}`,
        label: option,
      })),
      allow_custom: true,
      required: true,
    },
  ];
}

export function legacyStepRequestOptions(item: Pick<ToolCallTimelineItem, 'arguments'>): string[] {
  if (!Array.isArray(item.arguments?.options)) return [];
  return item.arguments.options
    .map((option: unknown) => {
      if (typeof option === 'string') return option;
      if (option && typeof option === 'object') {
        const label = (option as Record<string, unknown>).label;
        return typeof label === 'string' ? label : '';
      }
      return '';
    })
    .filter((option: string) => option.length > 0);
}

export function stepQuestionOptionLabel(question: StepQuestion, optionId: string): string {
  return question.options.find((option) => option.id === optionId)?.label ?? optionId;
}

function structuredAnswerRecords(parsed: Record<string, unknown> | null): unknown[] | null {
  const response = parsed?.response;
  const reply = parsed?.reply;
  if (parsed && Array.isArray(parsed.answers)) return parsed.answers;
  if (reply && typeof reply === 'object') {
    const answers = (reply as Record<string, unknown>).answers;
    if (Array.isArray(answers)) return answers;
  }
  if (response && typeof response === 'object') {
    const answers = (response as Record<string, unknown>).answers;
    if (Array.isArray(answers)) return answers;
  }
  return null;
}

function fallbackQuestion(questionId: string, index: number): StepQuestion {
  return {
    id: questionId || `q${index + 1}`,
    question: questionId || 'Question',
    options: [],
    allow_custom: true,
    required: true,
  };
}

function normalizeAnswerRecord(
  questions: StepQuestion[],
  raw: unknown,
  index: number,
): StepQuestionAnswer | null {
  if (!raw || typeof raw !== 'object') return null;
  const record = raw as Record<string, unknown>;
  const questionId = typeof record.question_id === 'string' ? record.question_id : '';
  const question = questions.find((item) => item.id === questionId) ?? fallbackQuestion(questionId, index);
  const selected = Array.isArray(record.selected_option_ids)
    ? record.selected_option_ids
        .filter((value): value is string => typeof value === 'string' && value.length > 0)
        .map((value) => {
          const option = question.options.find((item) => item.id === value);
          return option ? { ...option } : { id: value, label: value, unknown: true };
        })
    : [];
  const custom = typeof record.custom_answer === 'string' && record.custom_answer.trim()
    ? record.custom_answer.trim()
    : '';
  if (selected.length === 0 && !custom) return null;
  return { question, selected, custom };
}

export function normalizeStepQuestionAnswers(
  item: Pick<ToolCallTimelineItem, 'arguments'>,
  parsed: Record<string, unknown> | null,
): StepQuestionAnswer[] {
  const records = structuredAnswerRecords(parsed);
  if (!records) return [];

  const questions = normalizeStepQuestions(item);
  const answers = records
    .map((record, index) => normalizeAnswerRecord(questions, record, index))
    .filter((answer): answer is StepQuestionAnswer => answer !== null);

  if (questions.length === 0) return answers;

  const questionOrder = new Map(questions.map((question, index) => [question.id, index]));
  return [...answers].sort((left, right) => {
    const leftIndex = questionOrder.get(left.question.id);
    const rightIndex = questionOrder.get(right.question.id);
    if (leftIndex == null && rightIndex == null) return 0;
    if (leftIndex == null) return 1;
    if (rightIndex == null) return -1;
    return leftIndex - rightIndex;
  });
}

export function formatStepQuestionResponse(
  item: Pick<ToolCallTimelineItem, 'arguments'>,
  parsed: Record<string, unknown> | null,
): string {
  const response = parsed?.response;
  const answers = normalizeStepQuestionAnswers(item, parsed);
  if (answers.length > 0) {
    return answers
      .map((answer) => {
        const parts = [
          ...answer.selected.map((option) => option.label),
          answer.custom,
        ].filter((part) => part.length > 0);
        return `${answer.question.question}: ${parts.join(', ')}`;
      })
      .join('\n');
  }
  return typeof response === 'string' ? response : '';
}
