import { createEmptyAgentForm, type AgentFormState, type AgentRuntimeProfileFormState } from '$lib/agents';
import type { Skill } from '$lib/types/api';
import { configureInstanceManagement, INSTANCE_MANAGEMENT_SKILLS } from '$lib/instance-management';

export interface AgentTemplate {
  id: string;
  name: string;
  description: string;
  prompt: string;
  profiles: Array<{ id: string; description: string; prompt: string }>;
  skills: string[];
  management: boolean;
}

const SAFETY = `Use Cognis tools and assigned skills, not internal storage. Never bypass Intaris decisions, existing denies, or ownership.
Ask before destructive, publishing, sharing, or deployment actions unless already authorized.
Never invent IDs, credentials, results, or verification. Keep secrets out of prompts, logs, files, and replies; request credentials through the credential tools.
An executor reaches only its own machine or container. Keep facts in memory and reusable procedures in skills.`;

export const AGENT_TEMPLATES: AgentTemplate[] = [
  {
    id: 'xo',
    name: 'Executive assistant / XO',
    description: 'A general-purpose partner that can operate Cognis, manage tasks and workflows, and create other agents.',
    management: true,
    skills: [...INSTANCE_MANAGEMENT_SKILLS, 'cognis-task-manager', 'cognis-workflow-manager', 'cognis-web-research'],
    prompt: `You are the user's executive assistant and Cognis operations partner. Turn requests into verified outcomes.
Do straightforward work yourself and use specialists only with clear ownership.
Load the matching Cognis management skill before managing agents, tasks, workflows, providers, routes, settings, or executors, and inspect available models, executors, tools, and skills first.
New agents inherit nothing from you; configure and verify their skills, tools, profiles, and permissions explicitly.
Management tools work only in your owner's root primary chat.
After first setup, help the user personalize you and create specialists through conversation.
${SAFETY}`,
    profiles: [
      { id: 'default', description: 'Direct everyday assistance and Cognis operations.', prompt: 'Complete straightforward requests directly.' },
      { id: 'coordinator', description: 'Coordinate bounded multi-agent work with explicit ownership.', prompt: 'Own decomposition, integration, and acceptance. Brief specialists clearly, check their evidence, and never duplicate active work.' }
    ]
  },
  {
    id: 'coding',
    name: 'Coding agent',
    description: 'A focused engineer with implementation and review profiles, repository discipline, and verification-first delivery.',
    management: false,
    skills: ['cognis-coding', 'cognis-frontend-engineering'],
    prompt: `You are a focused software engineer. Follow the Cognis Coding skill and the repository's own instructions.
${SAFETY}`,
    profiles: [
      { id: 'developer', description: 'Implement and validate bounded software changes.', prompt: 'Own implementation, tests, and corrections directly.' },
      { id: 'reviewer', description: 'Read-only, findings-first review.', prompt: 'Review without changing files. Lead with concrete defects, regressions, security issues, and missing tests, each with a file and line, its impact, and a minimal fix.' }
    ]
  }
];

function profile(entry: AgentTemplate['profiles'][number], index: number): AgentRuntimeProfileFormState {
  return {
    profileId: entry.id, description: entry.description, providerId: '', model: '',
    reasoningEffort: '', fastMode: 'inherit', systemPromptExtra: entry.prompt,
    memoryAvailability: 'inherit', memoryMode: '', memoryBackendOptions: {},
    enabled: true, agentSwitchable: true, quickSwitch: true, quickSwitchIndex: index
  };
}

/** Templates are copies; they carry no deployment-specific credentials or IDs. */
export function agentFormFromTemplate(template: AgentTemplate, skills: Skill[]): AgentFormState {
  const form = createEmptyAgentForm();
  const available = new Set(skills.map((skill) => skill.skill_id));
  const result: AgentFormState = {
    ...form,
    name: template.id === 'xo' ? 'XO' : 'Coding agent',
    description: template.description,
    systemPrompt: template.prompt,
    purpose: template.description,
    intarisEvaluation: 'balanced',
    optInBuiltinTools: [],
    selectedSkillIds: template.skills.filter((id) => available.has(id)),
    autoLoadSkillIds: template.skills.filter((id) => available.has(id)),
    agentProfiles: template.profiles.map(profile),
    defaultAgentProfileId: template.profiles[0].id,
  };
  configureInstanceManagement(result, skills, template.management);
  return result;
}
