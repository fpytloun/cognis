<script lang="ts">
  import '../../../src/app.css';
  import { onMount } from 'svelte';
  import UserInteractionMessage from '../../../src/lib/components/UserInteractionMessage.svelte';
  import type { UserInteractionTimelineItem } from '../../../src/lib/timeline-render-model';
  import PendingRequestPanel from '../../../src/lib/components/PendingRequestPanel.svelte';
  import EscalationPrompt from '../../../src/lib/components/EscalationPrompt.svelte';
  import TimelineOngoingWorkDrawer from '../../../src/lib/components/timeline/TimelineOngoingWorkDrawer.svelte';
  let answer = $state('');

  const single: UserInteractionTimelineItem = {
    id: 'single-question', kind: 'user_interaction', timestamp: null,
    interactionType: 'step_question', status: 'complete', title: 'You answered questions',
    answers: [{
      question: 'Prepare **one sample label**?\n\n- Use only the fictional museum catalog and its **blue exhibit marker**.\n- Preserve the other three sample descriptions and their ordering.\n- Keep the curator attribution separate from the demonstration text.\n\nThis exercise has **no live publishing step** and does not change a real collection.',
      answer: 'Yes, prepare one label.',
    }],
  };
  const multi: UserInteractionTimelineItem = {
    ...single, id: 'multiple-questions',
    answers: [
      { question: 'Which **environment** should receive the change?', answer: 'Staging only. Keep production unchanged.' },
      { question: 'What should the validation cover?\n\n' + Array.from({ length: 12 }, (_, i) => `${i + 1}. Verify request ${i + 1}: **single rendering**, canonical history, and reconnect behavior.`).join('\n'),
        answer: Array.from({ length: 10 }, (_, i) => `Check ${i + 1}: validate the question and its matching answer.`).join('\n') },
      { question: 'Should the result be **published**?', answer: 'No. Keep the commit local for review.' },
    ],
  };
  const compact = { ...multi, id: 'compact-questions', answers: [
    { question: 'Which **environment**?', answer: 'Staging.' },
    { question: 'Run tests?', answer: 'Yes.' },
    { question: 'Publish?', answer: 'No.' },
  ] };
  onMount(() => {
    const query = new URLSearchParams(location.search);
    document.documentElement.dataset.themeFamily = 'paper';
    document.documentElement.dataset.resolvedTheme = query.get('mode') ?? 'dark';
  });
</script>

<main class="h-screen overflow-y-auto bg-canvas px-4 py-6 text-foreground">
  <div class="mx-auto max-w-2xl space-y-6">
    <section data-testid="single"><UserInteractionMessage item={single} /></section>
    <section data-testid="multiple"><UserInteractionMessage item={multi} /></section>
    <section data-testid="compact"><UserInteractionMessage item={compact} /></section>
    <section data-testid="pending-question">
      <PendingRequestPanel id="question-form" title="Assistant requested more input">
        <p>{'Review this question and provide a scoped answer. '.repeat(100)}</p>
        <label>Answer<textarea bind:value={answer}></textarea></label>
        {#snippet footer()}<button type="button">Submit</button>{/snippet}
      </PendingRequestPanel>
    </section>
    <section data-testid="pending-approval">
      <EscalationPrompt
        item={{ call_id: 'approval-browser', session_id: null, tool_name: 'deploy_service', decision: 'escalate', resolved: false,
          reasoning: 'Review the staging deployment and affected resources. '.repeat(100), risk: 'Service restart', arguments_display: { environment: 'staging' } }}
        secondsRemaining={42} queuedCount={2} onApprove={() => false} onDeny={() => false} />
    </section>
    <section data-testid="ongoing">
      <TimelineOngoingWorkDrawer todos={[{ content: 'Validate request panels', status: 'in_progress' }]} work={[]} />
    </section>
  </div>
</main>
