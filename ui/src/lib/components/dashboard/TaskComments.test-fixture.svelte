<script lang="ts">
  import type { TaskComment, TaskDetail } from '$lib/types/api';

  let {
    task: _task,
    initialIntent: _initialIntent = 'record_only',
    onSubmitted: _onSubmitted = undefined
  } = $props<{
    task: TaskDetail | null;
    initialIntent?: string;
    onSubmitted?: (comment: TaskComment) => void | Promise<void>;
  }>();
</script>

<div data-testid="task-comments-fixture">Task context</div>
