import { describe, expect, it } from 'vitest';

import {
  buildWorkflowSourceOptions,
  decodeWorkflowSourceValue,
  encodeWorkflowSourceValue,
  skillIsAttachedToAgent,
  skillHasWorkflow,
  workflowSourceValueForSelection,
  workflowSourceValueForWorkflow
} from '$lib/workflow-sources';
import type { Agent, Skill, Workflow } from '$lib/types/api';

const workflow: Workflow = {
  workflow_id: 'wf_release',
  name: 'Release Workflow',
  description: 'Handles releases',
  version: 1,
  criteria: 'Release tasks',
  tags: ['release'],
  interaction: { mode: 'explicit_gates' },
  defaults: { evaluate: true, max_attempts: 3, on_exhausted: 'gate', delivery: { completion_mode_family: 'default', allow_silent_completion: false } },
  steps: [{ name: 'publish', type: 'run', prompt: 'Publish' }],
  is_system: false,
  owner_email: 'user@example.com',
  lifecycle: 'persistent',
  archived_at: null,
  lineage: null,
  editable_fields: [],
  has_overrides: false,
  disabled: false,
  disableable: false,
  override_warnings: []
};

const decomposedSkill: Skill = {
  skill_id: 'skill_release',
  name: 'Release Helper',
  description: 'Coordinates release steps',
  instructions: 'Do release work',
  tools: null,
  linked_tool_ids: null,
  prompt_templates: null,
  steps: null,
  tags: ['release'],
  attach_to_all_agents: false,
  auto_load: false,
  is_system: false,
  source: 'db',
  current_version_id: 'sv_1',
  current_version: {
    version_id: 'sv_1',
    skill_id: 'skill_release',
    version_number: 1,
    content_hash: 'a'.repeat(64),
    schema_version: 1,
    instructions: 'Do release work',
    tools: null,
    linked_tool_ids: null,
    prompt_templates: null,
    secret_placeholders: null,
    steps: [{ name: 'publish', type: 'run', prompt: 'Publish release' }],
    decomposition_source_hash: 'b'.repeat(64),
    decomposition_stale: false,
    source_url: null,
    resolved_url: null,
    commit_sha: null,
    import_checksum: null,
    imported_at: null,
    import_format: null,
    asset_manifest: null,
    created_at: null
  },
  owner_email: 'user@example.com',
  created_at: null,
  updated_at: null
};

const agent: Agent = {
  agent_id: 'agent_release',
  owner_email: 'user@example.com',
  name: 'Release Agent',
  display_name: null,
  description: null,
  system_prompt: null,
  personality: null,
  skills: { items: [{ skill_id: 'skill_release', enabled: true }] },
  tools: null,
  permissions: null,
  llm_config: null,
  execution: null,
  personality_synced: false,
  personality_sync_error: null,
  personality_sync_checked_at: null,
  avatar_url: null,
  avatar_image_id: null,
  agent_type: 'primary',
  is_system: false,
  hidden: false,
  sync_metadata: null,
  is_shared_with_me: false,
  shared_by_email: null,
  granted_permission: null,
  executor_scope: null,
  is_readonly_for_caller: false,
  editable_fields: [],
  has_overrides: false,
  disabled: false,
  disableable: false,
  status: 'active',
  created_at: null,
  updated_at: null
};

describe('workflow source helpers', () => {
  it('identifies decomposed skills as workflow-capable', () => {
    expect(skillHasWorkflow(decomposedSkill)).toBe(true);
    expect(skillIsAttachedToAgent(decomposedSkill, agent)).toBe(true);
    expect(
      skillHasWorkflow({
        ...decomposedSkill,
        current_version: { ...decomposedSkill.current_version!, steps: [] }
      })
    ).toBe(false);
  });

  it('encodes and decodes workflow and skill selections', () => {
    expect(decodeWorkflowSourceValue(encodeWorkflowSourceValue('workflow', 'wf_release'))).toEqual({
      workflow_id: 'wf_release',
      skill_id: null
    });
    expect(decodeWorkflowSourceValue(encodeWorkflowSourceValue('skill', 'skill_release'))).toEqual({
      workflow_id: null,
      skill_id: 'skill_release'
    });
    expect(workflowSourceValueForWorkflow('wf_release')).toBe('workflow:wf_release');
    expect(workflowSourceValueForSelection(null, 'skill_release')).toBe('skill:skill_release');
  });

  it('builds combined workflow and skill options', () => {
    expect(buildWorkflowSourceOptions([workflow], [decomposedSkill], agent)).toEqual([
      {
        value: 'workflow:wf_release',
        id: 'wf_release',
        kind: 'workflow',
        label: 'Release Workflow',
        description: 'Handles releases'
      },
      {
        value: 'skill:skill_release',
        id: 'skill_release',
        kind: 'skill',
        label: 'Skill: Release Helper',
        description: 'Coordinates release steps'
      }
    ]);
  });
});
