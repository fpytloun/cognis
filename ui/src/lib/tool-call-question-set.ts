import type { ToolCallTimelineItem } from '$lib/chat';

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

export function formatStepQuestionResponse(
  item: Pick<ToolCallTimelineItem, 'arguments'>,
  parsed: Record<string, unknown> | null,
): string {
  const response = parsed?.response;
  const reply = parsed?.reply;
  const answers = Array.isArray(parsed?.answers)
    ? parsed.answers
    : reply && typeof reply === 'object' && Array.isArray((reply as Record<string, unknown>).answers)
      ? (reply as Record<string, unknown>).answers
      : response && typeof response === 'object' && Array.isArray((response as Record<string, unknown>).answers)
        ? (response as Record<string, unknown>).answers
        : null;
  if (Array.isArray(answers)) {
    const questions = normalizeStepQuestions(item);
    return answers
      .map((answer) => {
        if (!answer || typeof answer !== 'object') return '';
        const record = answer as Record<string, unknown>;
        const questionId = typeof record.question_id === 'string' ? record.question_id : '';
        const question = questions.find((item) => item.id === questionId);
        const selected = Array.isArray(record.selected_option_ids)
          ? record.selected_option_ids
              .filter((value): value is string => typeof value === 'string')
              .map((value) => question ? stepQuestionOptionLabel(question, value) : value)
          : [];
        const custom = typeof record.custom_answer === 'string' && record.custom_answer.trim()
          ? record.custom_answer.trim()
          : '';
        const parts = [...selected, custom].filter((part) => part.length > 0);
        if (parts.length === 0) return '';
        const label = question?.header || question?.question || questionId || 'Question';
        return `${label}: ${parts.join(', ')}`;
      })
      .filter((line) => line.length > 0)
      .join('\n');
  }
  return typeof response === 'string' ? response : '';
}
