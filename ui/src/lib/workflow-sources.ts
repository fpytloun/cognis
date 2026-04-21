import { isRecord } from '$lib/utils';
import type { Agent, Skill, Workflow } from '$lib/types/api';

export type WorkflowSourceKind = 'workflow' | 'skill';

export interface WorkflowSourceSelection {
  workflow_id: string | null;
  skill_id: string | null;
}

export interface WorkflowSourceOption {
  value: string;
  id: string;
  kind: WorkflowSourceKind;
  label: string;
  description: string;
}

const WORKFLOW_SOURCE_PREFIX = 'workflow:';
const SKILL_SOURCE_PREFIX = 'skill:';

export function skillHasWorkflow(skill: Skill): boolean {
  return (
    (Array.isArray(skill.current_version?.steps) && skill.current_version.steps.length > 0) ||
    (Array.isArray(skill.steps) && skill.steps.length > 0)
  );
}

export function skillIsAttachedToAgent(skill: Skill, agent: Agent | null | undefined): boolean {
  if (!agent) {
    return false;
  }
  if (skill.attach_to_all_agents ?? skill.auto_load) {
    return true;
  }
  const items = isRecord(agent.skills) && Array.isArray(agent.skills.items) ? agent.skills.items : [];
  return items.some(
    (item) => isRecord(item) && item.skill_id === skill.skill_id && item.enabled !== false
  );
}

export function encodeWorkflowSourceValue(kind: WorkflowSourceKind, id: string): string {
  return `${kind === 'workflow' ? WORKFLOW_SOURCE_PREFIX : SKILL_SOURCE_PREFIX}${id}`;
}

export function decodeWorkflowSourceValue(value: string): WorkflowSourceSelection {
  if (!value) {
    return { workflow_id: null, skill_id: null };
  }
  if (value.startsWith(WORKFLOW_SOURCE_PREFIX)) {
    return { workflow_id: value.slice(WORKFLOW_SOURCE_PREFIX.length), skill_id: null };
  }
  if (value.startsWith(SKILL_SOURCE_PREFIX)) {
    return { workflow_id: null, skill_id: value.slice(SKILL_SOURCE_PREFIX.length) };
  }
  return { workflow_id: value, skill_id: null };
}

export function workflowSourceValueForWorkflow(workflowId: string | null | undefined): string {
  return workflowId ? encodeWorkflowSourceValue('workflow', workflowId) : '';
}

export function buildWorkflowSourceOptions(
  workflows: Workflow[],
  skills: Skill[],
  agent?: Agent | null
): WorkflowSourceOption[] {
  const workflowOptions = workflows
    .slice()
    .sort((left, right) => left.name.localeCompare(right.name))
    .map((workflow) => ({
      value: encodeWorkflowSourceValue('workflow', workflow.workflow_id),
      id: workflow.workflow_id,
      kind: 'workflow' as const,
      label: workflow.name,
      description: workflow.description || workflow.workflow_id
    }));

  const skillOptions = skills
    .filter((skill) => skillHasWorkflow(skill) && skillIsAttachedToAgent(skill, agent))
    .slice()
    .sort((left, right) => left.name.localeCompare(right.name))
    .map((skill) => ({
      value: encodeWorkflowSourceValue('skill', skill.skill_id),
      id: skill.skill_id,
      kind: 'skill' as const,
      label: `Skill: ${skill.name}`,
      description: skill.description || skill.skill_id
    }));

  return [...workflowOptions, ...skillOptions];
}
