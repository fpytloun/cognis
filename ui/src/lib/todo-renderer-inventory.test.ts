import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf8');
}

describe('todo renderer inventory', () => {
  it.each([
    ['chat todo drawer', './components/timeline/TimelineTodoDrawer.svelte'],
    ['ongoing work drawer', './components/timeline/TimelineOngoingWorkDrawer.svelte'],
    ['todo progress popover', './components/TodoProgressPopover.svelte'],
    ['task cockpit progress', './components/task-cockpit/TaskProgressPanel.svelte'],
    ['todo tool result', './components/ToolCallBlock.svelte'],
    ['full task step todos', '../routes/(app)/tasks/[taskId]/+page.svelte']
  ])('%s uses the canonical status dot', (_label, path) => {
    expect(source(path)).toContain('TodoStatusDot');
  });
});
