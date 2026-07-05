import { describe, expect, it } from 'vitest';

import {
  agentToFormState,
  buildSystemPromptPreview,
  createEmptyAgentForm,
  formStateToEffectiveToolsPreviewPayload,
  formStateToPayload,
  formStateToSystemOverridePayload,
  normalizeSelectedAgentProfileId,
  profileOptionsForAgent
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
      disabled_mcp_servers: ['local_mcp:srv-github'],
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

  it('drops legacy hidden MCP category disables on load and save', () => {
    const form = agentToFormState({
      agent_id: 'agent-1',
      name: 'Agent',
      agent_type: 'primary',
      tools: {
        disabled_categories: ['mcp', 'filesystem'],
        disabled_mcp_servers: ['local_mcp:srv-github']
      }
    } as never);

    expect(form.disabledCategories).toEqual(['filesystem']);

    form.disabledCategories = ['mcp', ...form.disabledCategories];
    const payload = formStateToPayload(form);

    expect(payload.tools).toMatchObject({
      disabled_categories: ['filesystem'],
      disabled_mcp_servers: ['local_mcp:srv-github']
    });
  });

  it('round-trips disabled MCP server groups', () => {
    const form = agentToFormState({
      agent_id: 'agent-1',
      name: 'Agent',
      agent_type: 'primary',
      tools: {
        disabled_mcp_servers: ['local_mcp:srv-github', 'intaris_mcp:slack']
      }
    } as never);

    expect(form.disabledMcpServers).toEqual(['local_mcp:srv-github', 'intaris_mcp:slack']);

    const payload = formStateToPayload(form);
    expect(payload.tools).toMatchObject({
      disabled_mcp_servers: ['local_mcp:srv-github', 'intaris_mcp:slack']
    });
  });

  it('includes tool and permission overrides in system agent payloads', () => {
    const form = createEmptyAgentForm();
    form.disabledMcpServers = ['local_mcp:mcp-arr'];
    form.disabledTools = ['mcp:mcp-arr:arr_status'];
    form.toolPermissions = {
      'mcp:mcp-arr:arr_search_all': 'allow'
    };

    const payload = formStateToSystemOverridePayload(form);

    expect(payload.tools).toMatchObject({
      disabled_mcp_servers: ['local_mcp:mcp-arr'],
      disabled_tools: ['mcp:mcp-arr:arr_status']
    });
    expect(payload.permissions).toMatchObject({
      tool_permissions: {
        'mcp:mcp-arr:arr_search_all': 'allow'
      }
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

  it('round-trips per-agent backend capabilities', () => {
    const form = agentToFormState({
      agent_id: 'agent-1',
      name: 'Agent',
      agent_type: 'primary',
      capabilities: {
        memory_backend: 'none',
        guardrails_backend: 'none'
      }
    } as never);

    expect(form.memoryBackend).toBe('none');
    expect(form.guardrailsBackend).toBe('none');

    const payload = formStateToPayload(form);
    expect(payload.capabilities).toEqual({
      memory_backend: 'none',
      guardrails_backend: 'none'
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

  it('round-trips agent runtime profiles independently of base llm config', () => {
    const form = agentToFormState({
      agent_id: 'agent-1',
      name: 'Agent',
      agent_type: 'primary',
      llm_config: {
        provider_id: 'openai',
        model: 'gpt-default',
        reasoning_effort: 'medium'
      },
      agent_profiles: {
        fast: {
          profile_id: 'fast',
          description: 'Low latency routing profile',
          provider_id: 'openai',
          model: 'gpt-fast',
          reasoning_effort: 'low',
          system_prompt_extra: 'Be concise.',
          enabled: true,
          metadata: { tier: 'cheap' }
        }
      },
      default_agent_profile_id: 'fast'
    } as never);

    expect(form.providerId).toBe('openai');
    expect(form.model).toBe('gpt-default');
    expect(form.reasoningEffort).toBe('medium');
    expect(form.agentProfiles).toEqual([
      {
        profileId: 'fast',
        description: 'Low latency routing profile',
        providerId: 'openai',
        model: 'gpt-fast',
        reasoningEffort: 'low',
        systemPromptExtra: 'Be concise.',
        enabled: true
      }
    ]);
    expect(form.defaultAgentProfileId).toBe('fast');

    const payload = formStateToPayload(form);
    expect(payload.llm_config).toMatchObject({
      provider_id: 'openai',
      model: 'gpt-default',
      reasoning_effort: 'medium'
    });
    expect(payload.agent_profiles).toEqual({
      fast: {
        profile_id: 'fast',
        description: 'Low latency routing profile',
        provider_id: 'openai',
        model: 'gpt-fast',
        reasoning_effort: 'low',
        system_prompt_extra: 'Be concise.',
        enabled: true
      }
    });
    expect(payload.default_agent_profile_id).toBe('fast');
  });

  it('selects the first runtime profile as default when the selected default is invalid', () => {
    const form = createEmptyAgentForm();
    form.agentProfiles = [
      {
        profileId: 'quality',
        description: '',
        providerId: '',
        model: '',
        reasoningEffort: '',
        systemPromptExtra: '',
        enabled: true
      }
    ];
    form.defaultAgentProfileId = 'missing';

    const payload = formStateToPayload(form);

    expect(payload.default_agent_profile_id).toBe('quality');
  });
});

describe('agent runtime profile helpers', () => {
  const agent = {
    agent_id: 'agent-1',
    name: 'Agent',
    agent_type: 'primary',
    default_agent_profile_id: 'fast',
    agent_profiles: {
      fast: {
        profile_id: 'fast',
        description: 'Fast responses',
        enabled: true
      },
      disabled: {
        profile_id: 'disabled',
        description: 'Disabled profile',
        enabled: false
      }
    }
  } as never;

  it('builds enabled profile options and marks the agent default', () => {
    expect(profileOptionsForAgent(agent)).toEqual([
      {
        profileId: 'fast',
        label: 'fast (default)',
        description: 'Fast responses',
        isDefault: true
      }
    ]);
  });

  it('clears profile selections that do not belong to the selected agent', () => {
    expect(normalizeSelectedAgentProfileId(agent, 'fast')).toBe('fast');
    expect(normalizeSelectedAgentProfileId(agent, 'missing')).toBe('');
    expect(normalizeSelectedAgentProfileId(null, 'fast')).toBe('');
  });
});
