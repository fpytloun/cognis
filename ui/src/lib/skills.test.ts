import { describe, expect, it } from 'vitest';

import {
  clearSkillWorkflowDraft,
  createEmptySkillForm,
  formStateToSkillPayload,
  loadSkillWorkflowDraft,
  SKILL_WORKFLOW_DRAFT_STORAGE_KEY,
  saveSkillWorkflowDraft,
  skillToFormState,
  skillToWorkflowDraft,
  validateSkillForm
} from '$lib/skills';
import type { Skill } from '$lib/types/api';

function makeSkill(overrides: Partial<Skill> = {}): Skill {
  return {
    skill_id: 'skill_release',
    name: 'Release Helper',
    description: 'Coordinates release steps',
    instructions: 'Do release work.',
    tools: null,
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
      instructions: 'Do release work.',
      tools: [
        {
          name: 'publish_release',
          description: 'Publish the prepared release.',
          read_only: false,
          non_bypassable: true,
          timeout_seconds: 90,
          max_result_size: 1000,
          parameters: {
            type: 'object',
            properties: {
              version: {
                type: 'string',
                description: 'Release version'
              }
            },
            required: ['version']
          },
          recipe: {
            mode: 'script',
            entry: 'scripts/release.sh',
            args: ['--publish'],
            env: { CHANNEL: 'stable' },
            required_assets: ['scripts/release.sh'],
            secret_placeholders: ['GITHUB_TOKEN'],
            working_dir: 'scripts'
          }
        }
      ],
      prompt_templates: {
        changelog: 'Summarize the release notes.'
      },
      secret_placeholders: ['GITHUB_TOKEN'],
      steps: [
        {
          name: 'plan_release',
          type: 'run',
          prompt: 'Plan the release.',
          require_deliverable: false
        },
        {
          name: 'publish_release',
          type: 'run',
          prompt: 'Publish the release.',
          require_deliverable: true
        }
      ],
      decomposition_source_hash: 'b'.repeat(64),
      decomposition_stale: false,
      source_url: null,
      resolved_url: null,
      commit_sha: null,
      import_checksum: null,
      imported_at: null,
      import_format: null,
      asset_manifest: [
        {
          filename: 'scripts/release.sh',
          asset_id: 'asset_1',
          artifact_namespace: 'skills',
          artifact_object_id: 'obj_1',
          content_hash: 'c'.repeat(64),
          size_bytes: 123,
          content_type: 'text/x-shellscript',
          url: null
        }
      ],
      created_at: '2026-04-20T10:00:00Z'
    },
    owner_email: 'user@example.com',
    created_at: '2026-04-20T09:00:00Z',
    updated_at: '2026-04-20T10:00:00Z',
    ...overrides
  };
}

describe('skill form helpers', () => {
  it('maps a skill into structured form state', () => {
    const form = skillToFormState(makeSkill());

    expect(form.promptTemplates).toEqual([
      expect.objectContaining({ key: 'changelog', value: 'Summarize the release notes.' })
    ]);
    expect(form.secretPlaceholders).toEqual(['GITHUB_TOKEN']);
    expect(form.tools[0]).toEqual(
      expect.objectContaining({
        name: 'publish_release',
        recipeMode: 'script',
        entry: 'scripts/release.sh',
        argsText: '--publish',
        requiredAssetsText: 'scripts/release.sh',
        secretPlaceholdersText: 'GITHUB_TOKEN'
      })
    );
    expect(form.tools[0].parameters[0]).toEqual(
      expect.objectContaining({ name: 'version', required: true, type: 'string' })
    );
  });

  it('serializes structured form state back into API payloads', () => {
    const form = skillToFormState(makeSkill());

    const payload = formStateToSkillPayload(form);

    expect(payload.prompt_templates).toEqual({ changelog: 'Summarize the release notes.' });
    expect(payload.secret_placeholders).toEqual(['GITHUB_TOKEN']);
    expect(payload.tools).toEqual([
      {
        name: 'publish_release',
        description: 'Publish the prepared release.',
        read_only: false,
        non_bypassable: true,
        timeout_seconds: 90,
        max_result_size: 1000,
        parameters: {
          type: 'object',
          properties: {
            version: {
              type: 'string',
              description: 'Release version'
            }
          },
          required: ['version']
        },
        recipe: {
          mode: 'script',
          entry: 'scripts/release.sh',
          args: ['--publish'],
          env: { CHANNEL: 'stable' },
          timeout_seconds: 90,
          required_assets: ['scripts/release.sh'],
          secret_placeholders: ['GITHUB_TOKEN'],
          working_dir: 'scripts'
        }
      }
    ]);
  });

  it('validates missing names and required recipe entries', () => {
    const form = createEmptySkillForm();
    form.instructions = 'hello';
    form.tools = [
      {
        id: 'tool_1',
        name: '',
        description: '',
        readOnly: false,
        nonBypassable: true,
        timeoutSeconds: 60,
        maxResultSize: 1000,
        parameters: [],
        recipeMode: 'script',
        entry: '',
        argsText: '',
        env: [],
        requiredAssetsText: '',
        secretPlaceholdersText: '',
        workingDir: ''
      }
    ];

    expect(validateSkillForm(form)).toEqual(
      expect.arrayContaining([
        'Skill name is required.',
        'Each tool needs a name.',
        'Tool (unnamed) needs a description.',
        'Tool (unnamed) needs a recipe entry path or command.'
      ])
    );
  });
});

describe('skill workflow drafts', () => {
  it('creates a persistent workflow draft from saved skill steps', () => {
    const form = skillToWorkflowDraft(makeSkill());

    expect(form.name).toBe('Release Helper');
    expect(form.lifecycle).toBe('persistent');
    expect(form.steps.map((step) => step.name)).toEqual(['plan_release', 'publish_release']);
    expect(form.lineage).toEqual({
      source_skill_ids: ['skill_release'],
      composition_source: 'manual'
    });
  });

  it('round-trips workflow handoff drafts through session storage', () => {
    const form = skillToWorkflowDraft(makeSkill());

    clearSkillWorkflowDraft();
    saveSkillWorkflowDraft('skill_release', form, 'hash_123');

    expect(loadSkillWorkflowDraft('skill_release')).toEqual(
      expect.objectContaining({
        skillId: 'skill_release',
        decompositionSourceHash: 'hash_123',
        form: expect.objectContaining({
          name: 'Release Helper',
          steps: expect.any(Array)
        })
      })
    );

    clearSkillWorkflowDraft();
    expect(loadSkillWorkflowDraft('skill_release')).toBeNull();
  });

  it('ignores legacy stored drafts without decomposition hash', () => {
    const form = skillToWorkflowDraft(makeSkill());

    sessionStorage.setItem(
      SKILL_WORKFLOW_DRAFT_STORAGE_KEY,
      JSON.stringify({ skillId: 'skill_release', generatedAt: new Date().toISOString(), form })
    );

    expect(loadSkillWorkflowDraft('skill_release')).toBeNull();
  });
});
