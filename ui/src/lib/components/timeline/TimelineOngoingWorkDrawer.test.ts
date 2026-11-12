import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import TimelineOngoingWorkDrawer from './TimelineOngoingWorkDrawer.svelte';
import BackgroundCommandDetails from './BackgroundCommandDetails.svelte';
import { tick } from 'svelte';
import type { BackgroundWorkItem } from '$lib/types/api';

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
});

const mocks = vi.hoisted(() => ({ backgroundCommand: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: { conversations: mocks } }));

function work(kind: BackgroundWorkItem['kind'], fields: Partial<BackgroundWorkItem> = {}): BackgroundWorkItem {
  return { kind, work_id: kind, controller_conversation_id: 'conv-root', title: kind,
    agent_id: 'laforge', status: 'running', todos: [], ...fields };
}

describe('Ongoing work navigation and details', () => {
  afterEach(cleanup);
  beforeEach(() => {
    mocks.backgroundCommand.mockReset().mockResolvedValue({
      shell_id: 'shell-preview', executor_id: 'sample-desktop', command: 'npm run dev',
      cwd: '/workspace/ui', pid: 123, status: 'running', runtime_seconds: 65,
      idle_seconds: 10, exit_code: null, output_tail: '<script>alert(1)</script>',
      output_chars: 30, trimmed_chars: 0,
    });
  });

  it('opens tasks, managed conversations, and subsessions from their titles', async () => {
    const onViewSession = vi.fn();
    render(TimelineOngoingWorkDrawer, { open: true, todos: [], onViewSession, work: [
      work('task', { task_id: 'task-build', title: 'Build task' }),
      work('managed_conversation', { target_conversation_id: 'conv-child', title: 'Managed work' }),
      work('delegated_session', { session_id: 'sess-review', title: 'Review work' }),
    ] });
    expect(screen.getByRole('link', { name: 'Build task' })).toHaveAttribute('href', '/tasks/task-build');
    expect(screen.getByRole('link', { name: 'Managed work' })).toHaveAttribute('href', '/chat/conv-child');
    await fireEvent.click(screen.getByRole('button', { name: 'Review work' }));
    expect(onViewSession).toHaveBeenCalledWith('sess-review');
  });

  it('loads command details only on title click, refreshes output and opens its source', async () => {
    const onViewSession = vi.fn();
    render(TimelineOngoingWorkDrawer, { open: true, todos: [], onViewSession, work: [
      work('background_command', { work_id: 'shell-preview', session_id: 'sess-root', title: 'Browser preview' }),
    ] });
    expect(mocks.backgroundCommand).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('button', { name: 'Browser preview' }));
    await waitFor(() => expect(screen.getByLabelText('Command')).toHaveTextContent('npm run dev'));
    expect(mocks.backgroundCommand).toHaveBeenCalledWith('conv-root', 'shell-preview');
    expect(screen.getByLabelText('Latest command output')).toHaveTextContent('<script>alert(1)</script>');
    expect(screen.getByLabelText('Latest command output').querySelector('script')).toBeNull();
    await fireEvent.click(screen.getByRole('button', { name: 'Open source session' }));
    expect(onViewSession).toHaveBeenCalledWith('sess-root');
    await fireEvent.click(screen.getByRole('button', { name: 'Refresh details' }));
    await waitFor(() => expect(mocks.backgroundCommand).toHaveBeenCalledTimes(2));
  });

  it('shows unavailable command errors without removing source navigation', async () => {
    mocks.backgroundCommand.mockRejectedValue(new Error('Background command is no longer available'));
    render(TimelineOngoingWorkDrawer, { open: true, todos: [], onViewSession: vi.fn(), work: [
      work('background_command', { session_id: 'sess-root' }),
    ] });
    await fireEvent.click(screen.getByRole('button', { name: 'background_command' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('no longer available'));
    expect(screen.getByRole('button', { name: 'Open source session' })).toBeEnabled();
  });

  it('ignores a late response after collapsing and reopening a command', async () => {
    let resolveOld!: (value: unknown) => void;
    mocks.backgroundCommand.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    render(TimelineOngoingWorkDrawer, { open: true, todos: [], work: [
      work('background_command', { title: 'Delayed preview' }),
    ] });
    const title = screen.getByRole('button', { name: 'Delayed preview' });
    await fireEvent.click(title);
    await fireEvent.click(title);
    await fireEvent.click(title);
    await waitFor(() => expect(screen.getByLabelText('Command')).toHaveTextContent('npm run dev'));
    resolveOld({ command: 'stale command', output_chars: 0, runtime_seconds: 0, idle_seconds: 0 });
    await tick();
    await waitFor(() => expect(mocks.backgroundCommand).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText('Command')).toHaveTextContent('npm run dev');
    expect(screen.queryByText('stale command')).not.toBeInTheDocument();
  });

  it('does not apply the previous item response after switching commands', async () => {
    let resolveOld!: (value: unknown) => void;
    mocks.backgroundCommand.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
    const view = render(BackgroundCommandDetails, {
      item: work('background_command', { work_id: 'shell-old' }),
    });
    await waitFor(() => expect(mocks.backgroundCommand).toHaveBeenCalledWith('conv-root', 'shell-old'));
    await view.rerender({ item: work('background_command', { work_id: 'shell-new' }) });
    await waitFor(() => expect(screen.getByLabelText('Command')).toHaveTextContent('npm run dev'));
    resolveOld({ command: 'old result', output_chars: 0, runtime_seconds: 0, idle_seconds: 0 });
    await tick();
    expect(screen.getByLabelText('Command')).toHaveTextContent('npm run dev');
  });
});
