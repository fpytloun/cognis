import { describe, expect, it } from 'vitest';
import { agentFormFromTemplate, AGENT_TEMPLATES } from '$lib/agent-templates';
import { formStateToPayload } from '$lib/agents';
import { configureInstanceManagement, INSTANCE_MANAGEMENT_TOOLS, INSTANCE_MANAGEMENT_SKILLS } from '$lib/instance-management';
import { executorInstallCommands, executorReady, executorWebsocketUrl, recommendedRoutes } from '$lib/onboarding';
import type { ExecutorConfig, LLMProvider, ModelEntry, ModelRouting, Skill } from '$lib/types/api';

describe('first-run resources', () => {
  it('uses the public Homebrew tap, persistent configure, doctor, and user service', () => {
    const script = executorInstallCommands('macos', 'https://cognis.localhost');
    expect(script).toContain('brew tap fpytloun/tap');
    expect(script).toContain("configure --controller-url 'wss://cognis.localhost/api/executor/ws'");
    expect(script).toContain('doctor --test-connection');
    expect(script).not.toContain('--token');
    expect(script).not.toContain('sudo');
  });
  it('keeps tokens out of scripts and container command arguments', () => {
    const docker = executorInstallCommands('docker', 'https://cognis.localhost');
    expect(docker).toContain('-e COGNIS_EXECUTOR_TOKEN');
    expect(docker).toContain('ghcr.io/fpytloun/cognis-executor:latest');
    const kubernetes = executorInstallCommands('kubernetes', 'https://cognis.localhost');
    expect(kubernetes).toContain('PersistentVolumeClaim');
    expect(kubernetes).toContain('automountServiceAccountToken: false');
    expect(kubernetes).toContain('--from-env-file=');
    expect(kubernetes).not.toContain('--from-literal=');
  });
  it('does not generate unusable insecure remote installs', () => {
    expect(() => executorInstallCommands('kubernetes', 'http://localhost:8080')).toThrow();
    expect(() => executorInstallCommands('docker', 'http://host.docker.internal:8080')).toThrow();
    expect(executorInstallCommands('docker', 'http://localhost:8080')).toContain('--network host');
    expect(() => executorWebsocketUrl('https://user:password@localhost')).toThrow();
    expect(executorWebsocketUrl('https://localhost/?token=private')).not.toContain('private');
  });
  it('does not treat an offline or stale executor record as connected', () => {
    const executor = {
      status: 'active', available: true, runtime_state: 'active', desired_config_version: 2,
      applied_config_version: 2, observed_tools: [{ name: 'read' }]
    } as unknown as ExecutorConfig;
    expect(executorReady(executor)).toBe(true);
    expect(executorReady({ ...executor, runtime_state: 'offline' })).toBe(false);
    expect(executorReady({ ...executor, applied_config_version: 1 })).toBe(false);
    expect(executorReady({ ...executor, observed_tools: [] })).toBe(false);
  });
});

describe('agent templates', () => {
  it('produces independent profiles, no deployment references, and explicit XO management', () => {
    const skill = { skill_id: 'cognis-agent-manager' } as Skill;
    const first = agentFormFromTemplate(AGENT_TEMPLATES[0], [skill]);
    const second = agentFormFromTemplate(AGENT_TEMPLATES[0], [skill]);
    first.agentProfiles[0].model = 'local-change';
    expect(second.agentProfiles[0].model).toBe('');
    expect(second.executorId).toBe('');
    expect(second.providerId).toBe('');
    const payload = formStateToPayload(second);
    expect(payload.tools).toMatchObject({ opt_in_builtin_tools: INSTANCE_MANAGEMENT_TOOLS.map((tool) => tool.id) });
    expect(payload.capabilities).toMatchObject({ guardrails_backend: 'intaris', intaris_evaluation: 'balanced', approval_mode: 'ask' });
    expect(payload.default_agent_profile_id).toBe('default');
    expect(payload.skills).toMatchObject({ items: [{ skill_id: 'cognis-agent-manager' }] });
  });
  it('keeps coding templates out of agent-management access', () => {
    const form = agentFormFromTemplate(AGENT_TEMPLATES[1], []);
    expect(form.optInBuiltinTools).toEqual([]);
    expect(form.agentProfiles.map((profile) => profile.profileId)).toEqual(['developer', 'reviewer']);
  });
  it('gives QuickStart full management independently of the selected template and preserves opt-out', () => {
    const skills = INSTANCE_MANAGEMENT_SKILLS.map((skill_id) => ({ skill_id }) as Skill);
    const form = agentFormFromTemplate(AGENT_TEMPLATES[1], skills);
    configureInstanceManagement(form, skills, true);
    const payload = formStateToPayload(form);
    expect(form.optInBuiltinTools).toEqual(INSTANCE_MANAGEMENT_TOOLS.map((tool) => tool.id));
    expect(form.selectedSkillIds).toEqual(INSTANCE_MANAGEMENT_SKILLS);
    expect(form.autoLoadSkillIds).toEqual(INSTANCE_MANAGEMENT_SKILLS);
    expect(payload.capabilities).toMatchObject({ guardrails_backend: 'intaris', approval_mode: 'ask' });
    configureInstanceManagement(form, skills, false);
    expect(form.optInBuiltinTools).toEqual([]);
    expect(form.selectedSkillIds).toEqual([]);
  });
});

describe('recommended model routes', () => {
  const models = [
    { model_id: 'openai/gpt-oss-20b', supports_tools: true },
    { model_id: 'openai/gpt-oss-120b', supports_tools: true },
    { model_id: 'typesafe/jev-1.13', model_mode: 'systemone' },
    { model_id: 'openai/text-embedding-3-small', supports_embedding: true },
    { model_id: 'google/gemini-image', supports_image_generation: true },
  ] as unknown as ModelEntry[];
  const provider = { provider_id: 'openrouter', models } as LLMProvider;
  it('uses discovered literal IDs and includes optional image generation', () => {
    const routes = recommendedRoutes(provider, {} as ModelRouting);
    expect(routes.companion_lite.model).toBe('openrouter/openai/gpt-oss-20b');
    expect(routes.mnemory_extraction.model).toBe('openrouter/openai/gpt-oss-120b');
    expect(routes.intaris_evaluation.model).toBe('openrouter/typesafe/jev-1.13');
    expect(routes.companion_embedding.model).toBe('openrouter/openai/text-embedding-3-small');
    expect(routes.image_generation.model).toBe('openrouter/google/gemini-image');
  });
  it('preserves existing routes and never invents unsupported model choices', () => {
    const routes = recommendedRoutes(provider, { companion_lite: { model: 'groq/openai/gpt-oss-20b', reasoning_effort: null } } as ModelRouting);
    expect(routes.companion_lite).toBeUndefined();
    expect(recommendedRoutes({ ...provider, models: [] }, {} as ModelRouting)).toEqual({});
  });
});
