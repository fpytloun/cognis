import { describe, expect, it } from 'vitest';

import {
  agentToFormState,
  buildSystemPromptPreview,
  createEmptyAgentForm,
  formStateToPayload
} from '$lib/agents';

describe('agent payload mapping', () => {
  it('preserves existing tool configuration when MCP settings are updated', () => {
    const form = createEmptyAgentForm();
    form.agentId = 'agent-1';
    form.name = 'Agent';
    form.originalTools = { intaris_mcp_servers: ['remote-audit'] };
    form.intarisMcpServers = ['remote-audit'];
    form.mcpServers = [
      {
        name: 'filesystem',
        command: 'npx',
        argsText: '@modelcontextprotocol/server-filesystem\n/tmp/project',
        envVars: [{ key: 'TOKEN', value: 'secret_name', type: 'literal' }],
        timeoutSeconds: 45
      }
    ];

    const payload = formStateToPayload(form);
    expect(payload.tools).toEqual({
      intaris_mcp_servers: ['remote-audit'],
      delegation_tools: true,
      mcp_servers: [
        {
          name: 'filesystem',
          command: 'npx',
          args: ['@modelcontextprotocol/server-filesystem', '/tmp/project'],
          env: { TOKEN: 'secret_name' },
          timeout_seconds: 45
        }
      ]
    });
  });

  it('round-trips executor_selector as newline-separated entries', () => {
    const form = agentToFormState({
      agent_id: 'agent-1',
      name: 'Agent',
      agent_type: 'primary',
      tools: {},
      execution: {
        executor_selector: {
          region: 'eu',
          gpu: 'true'
        }
      }
    } as never);

    expect(form.executorSelector).toBe('region=eu\ngpu=true');

    const payload = formStateToPayload(form);
    expect(payload.execution).toMatchObject({
      executor_selector: {
        region: 'eu',
        gpu: 'true'
      }
    });
  });

  it('clears previously configured disabled and intaris MCP tool fields', () => {
    const form = createEmptyAgentForm();
    form.agentId = 'agent-1';
    form.name = 'Agent';
    form.originalTools = {
      disabled_categories: ['filesystem'],
      disabled_tools: ['bash'],
      intaris_mcp_servers: ['remote-audit'],
      custom_flag: true
    };

    const payload = formStateToPayload(form);
    expect(payload.tools).toEqual({
      custom_flag: true,
      delegation_tools: true,
      mcp_servers: []
    });
  });

  it('returns empty preview when no identity is configured', () => {
    const form = createEmptyAgentForm();
    expect(buildSystemPromptPreview(form)).toBe('');
  });
});
