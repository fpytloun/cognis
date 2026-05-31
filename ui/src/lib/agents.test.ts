import { describe, expect, it } from 'vitest';

import {
  agentToFormState,
  buildSystemPromptPreview,
  createEmptyAgentForm,
  formStateToEffectiveToolsPreviewPayload,
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

  it('round-trips default-off builtin tool opt-ins', () => {
    const form = agentToFormState({
      agent_id: 'agent-1',
      name: 'Agent',
      agent_type: 'primary',
      tools: { opt_in_builtin_tools: ['manage_agents'], custom_flag: true }
    } as never);

    expect(form.optInBuiltinTools).toEqual(['manage_agents']);

    const payload = formStateToPayload(form);
    expect(payload.tools).toMatchObject({
      custom_flag: true,
      opt_in_builtin_tools: ['manage_agents']
    });
  });

  it('round-trips per-agent skill auto-load metadata', () => {
    const form = agentToFormState({
      agent_id: 'agent-1',
      name: 'Agent',
      agent_type: 'primary',
      tools: {},
      skills: {
        items: [
          { skill_id: 'cognis-coding', enabled: true, auto_load_instructions: true },
          { skill_id: 'research', enabled: true },
          { skill_id: 'disabled', enabled: false, auto_load_instructions: true }
        ]
      }
    } as never);

    expect(form.selectedSkillIds).toEqual(['cognis-coding', 'research']);
    expect(form.autoLoadSkillIds).toEqual(['cognis-coding']);

    const payload = formStateToPayload(form);
    expect(payload.skills).toEqual({
      items: [
        { skill_id: 'cognis-coding', enabled: true, auto_load_instructions: true },
        { skill_id: 'research', enabled: true },
        { skill_id: 'disabled', enabled: false, auto_load_instructions: true }
      ]
    });
  });

  it('omits default-off builtin tool opt-ins for secondary agents', () => {
    const form = createEmptyAgentForm();
    form.agentType = 'secondary';
    form.optInBuiltinTools = ['manage_agents'];

    const payload = formStateToPayload(form);

    expect(payload.tools).not.toHaveProperty('opt_in_builtin_tools');
  });

  it('returns empty preview when no identity is configured', () => {
    const form = createEmptyAgentForm();
    expect(buildSystemPromptPreview(form)).toBe('');
  });

  it('builds effective-tools preview payload with intaris and local MCP selections', () => {
    const form = createEmptyAgentForm();
    form.agentId = 'agent-1';
    form.name = 'Agent';
    form.intarisMcpServers = ['remote-audit'];
    form.mcpServers = [
      {
        name: 'filesystem',
        command: 'npx',
        argsText: '@modelcontextprotocol/server-filesystem\n/tmp/project',
        envVars: [],
        timeoutSeconds: 30
      }
    ];
    form.executorSelector = 'region=eu\ngpu=true';

    const payload = formStateToEffectiveToolsPreviewPayload(form);

    expect(payload).toEqual({
      agent_id: 'agent-1',
      agent_type: 'primary',
      skills: {},
      tools: {
        delegation_tools: true,
        mcp_servers: [
          {
            name: 'filesystem',
            command: 'npx',
            args: ['@modelcontextprotocol/server-filesystem', '/tmp/project'],
            env: {},
            timeout_seconds: 30
          }
        ],
        intaris_mcp_servers: ['remote-audit']
      },
      permissions: {
        tool_permissions: {},
        allowed_secrets: [],
        allowed_credentials: [],
        allowed_knowledgebases: [],
        can_delegate: true,
        max_delegation_depth: 3
      },
      execution: {
        executor_id: undefined,
        executor_selector: {
          region: 'eu',
          gpu: 'true'
        },
        available_workflow_ids: [],
        default_workflow_id: 'system:direct',
        workflow_selection_mode: 'automatic',
        default_chat_mode: 'default',
        additional_executors: undefined,
        step_agent_overrides: {}
      }
    });
  });

  it('round-trips allowed credentials independently of allowed secrets', () => {
    const form = createEmptyAgentForm();
    form.agentId = 'agent-1';
    form.name = 'Agent';
    form.allowedSecrets = ['legacy_secret'];
    form.allowedCredentials = ['github_work'];

    const payload = formStateToPayload(form);
    expect(payload.permissions).toMatchObject({
      allowed_secrets: ['legacy_secret'],
      allowed_credentials: ['github_work']
    });

    const next = agentToFormState({
      agent_id: 'agent-1',
      name: 'Agent',
      agent_type: 'primary',
      tools: {},
      permissions: {
        allowed_secrets: ['legacy_secret'],
        allowed_credentials: ['github_work']
      }
    } as never);

    expect(next.allowedSecrets).toEqual(['legacy_secret']);
    expect(next.allowedCredentials).toEqual(['github_work']);
  });

  it('round-trips allowed knowledgebases', () => {
    const form = createEmptyAgentForm();
    form.agentId = 'agent-1';
    form.name = 'Agent';
    form.allowedKnowledgebases = ['kb_docs'];

    const payload = formStateToPayload(form);
    expect(payload.permissions).toMatchObject({
      allowed_knowledgebases: ['kb_docs']
    });

    const next = agentToFormState({
      agent_id: 'agent-1',
      name: 'Agent',
      agent_type: 'primary',
      tools: {},
      permissions: {
        allowed_knowledgebases: ['kb_docs', 42]
      }
    } as never);

    expect(next.allowedKnowledgebases).toEqual(['kb_docs']);
  });
});
