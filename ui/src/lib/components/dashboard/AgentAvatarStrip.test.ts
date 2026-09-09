import { render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

import type { Agent } from '$lib/types/api';
import AgentAvatarStrip from './AgentAvatarStrip.svelte';

vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

function agent(agentId: string): Agent {
  return {
    agent_id: agentId,
    name: agentId,
    display_name: agentId,
    agent_type: 'primary',
    status: 'active',
    hidden: false,
    disabled: false,
    avatar_url: null
  } as unknown as Agent;
}

describe('AgentAvatarStrip', () => {
  it('renders one visible agent and removes the All-agents entry', async () => {
    const view = render(AgentAvatarStrip, {
      agents: [],
      onSelectAgent: vi.fn()
    });
    expect(screen.queryByTestId('dashboard-agent-strip')).not.toBeInTheDocument();

    await view.rerender({ agents: [agent('riker')], onSelectAgent: vi.fn() });
    expect(screen.getByTestId('dashboard-agent-strip')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-agent-avatar-riker')).toBeInTheDocument();
    expect(screen.queryByTestId('dashboard-agent-strip-all')).not.toBeInTheDocument();
  });

  it('shows all visible non-system agent types in a horizontal strip', () => {
    const worker = { ...agent('laforge'), agent_type: 'worker' } as Agent;
    const system = { ...agent('system'), agent_type: 'secondary', is_system: true } as Agent;
    render(AgentAvatarStrip, {
      agents: [agent('riker'), worker, system],
      onSelectAgent: vi.fn()
    });

    expect(screen.getByTestId('dashboard-agent-strip')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-agent-avatar-riker')).toBeInTheDocument();
    expect(screen.getByTestId('dashboard-agent-avatar-laforge')).toBeInTheDocument();
    expect(screen.queryByTestId('dashboard-agent-avatar-system')).not.toBeInTheDocument();
  });
});
