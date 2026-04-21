import { createId, isRecord } from '$lib/utils';
import { createEmptyWorkflowForm, workflowToFormState, type WorkflowFormState } from '$lib/workflows';
import type { Skill, SkillCreate, SkillUpdate, Workflow, WorkflowStep } from '$lib/types/api';

export const SKILL_WORKFLOW_DRAFT_STORAGE_KEY = 'cognis.skillWorkflowDraft';

export interface SkillPromptTemplateFormItem {
  id: string;
  key: string;
  value: string;
}

export interface SkillKeyValueFormItem {
  id: string;
  key: string;
  value: string;
}

export interface SkillToolParameterFormItem {
  id: string;
  name: string;
  type: string;
  description: string;
  required: boolean;
  enumText: string;
}

export interface SkillToolFormItem {
  id: string;
  name: string;
  description: string;
  readOnly: boolean;
  nonBypassable: boolean;
  timeoutSeconds: number;
  maxResultSize: number;
  parameters: SkillToolParameterFormItem[];
  recipeMode: 'none' | 'script' | 'command';
  entry: string;
  argsText: string;
  env: SkillKeyValueFormItem[];
  requiredAssetsText: string;
  secretPlaceholdersText: string;
  workingDir: string;
}

export interface SkillAssetFormItem {
  filename: string;
  existing_asset_id?: string;
  source_artifact_id?: string;
  content_type?: string;
  size_bytes?: number;
}

export interface SkillFormState {
  name: string;
  description: string;
  instructions: string;
  tagsText: string;
  attachToAllAgents: boolean;
  promptTemplates: SkillPromptTemplateFormItem[];
  secretPlaceholders: string[];
  tools: SkillToolFormItem[];
  assets: SkillAssetFormItem[];
}

export interface SkillWorkflowDraftStorage {
  skillId: string;
  generatedAt: string;
  form: WorkflowFormState;
}

function splitCsvOrLines(value: string): string[] {
  return value
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function parsePromptTemplates(value: Record<string, unknown> | null | undefined): SkillPromptTemplateFormItem[] {
  return Object.entries(value ?? {}).map(([key, item]) => ({
    id: createId('skill_template'),
    key,
    value: String(item ?? '')
  }));
}

function parseKeyValueEntries(value: Record<string, unknown> | null | undefined): SkillKeyValueFormItem[] {
  return Object.entries(value ?? {}).map(([key, item]) => ({
    id: createId('skill_env'),
    key,
    value: String(item ?? '')
  }));
}

function parseParameters(value: Record<string, unknown> | null | undefined): SkillToolParameterFormItem[] {
  const properties = isRecord(value?.properties) ? value.properties : {};
  const required = Array.isArray(value?.required)
    ? value.required.filter((item): item is string => typeof item === 'string')
    : [];
  return Object.entries(properties).map(([name, schema]) => {
    const parameter = isRecord(schema) ? schema : {};
    const enumValues = Array.isArray(parameter.enum)
      ? parameter.enum.map((item) => String(item)).join(', ')
      : '';
    return {
      id: createId('skill_param'),
      name,
      type: typeof parameter.type === 'string' ? parameter.type : 'string',
      description: typeof parameter.description === 'string' ? parameter.description : '',
      required: required.includes(name),
      enumText: enumValues
    };
  });
}

function parseTool(tool: Record<string, unknown>): SkillToolFormItem {
  const recipe = isRecord(tool.recipe) ? tool.recipe : null;
  const mode = recipe?.mode === 'script' || recipe?.mode === 'command' ? recipe.mode : 'none';
  return {
    id: createId('skill_tool'),
    name: typeof tool.name === 'string' ? tool.name : '',
    description: typeof tool.description === 'string' ? tool.description : '',
    readOnly: tool.read_only === true,
    nonBypassable: tool.non_bypassable !== false,
    timeoutSeconds:
      typeof tool.timeout_seconds === 'number' && Number.isFinite(tool.timeout_seconds)
        ? tool.timeout_seconds
        : 60,
    maxResultSize:
      typeof tool.max_result_size === 'number' && Number.isFinite(tool.max_result_size)
        ? tool.max_result_size
        : 50_000,
    parameters: parseParameters(isRecord(tool.parameters) ? tool.parameters : null),
    recipeMode: mode,
    entry: typeof recipe?.entry === 'string' ? recipe.entry : '',
    argsText: Array.isArray(recipe?.args)
      ? recipe.args.filter((item): item is string => typeof item === 'string').join('\n')
      : '',
    env: parseKeyValueEntries(isRecord(recipe?.env) ? recipe.env : null),
    requiredAssetsText: Array.isArray(recipe?.required_assets)
      ? recipe.required_assets.filter((item): item is string => typeof item === 'string').join('\n')
      : '',
    secretPlaceholdersText: Array.isArray(recipe?.secret_placeholders)
      ? recipe.secret_placeholders.filter((item): item is string => typeof item === 'string').join('\n')
      : '',
    workingDir: typeof recipe?.working_dir === 'string' ? recipe.working_dir : ''
  };
}

function toolToPayload(tool: SkillToolFormItem): Record<string, unknown> {
  const properties = Object.fromEntries(
    tool.parameters
      .filter((parameter) => parameter.name.trim())
      .map((parameter) => {
        const enumValues = splitCsvOrLines(parameter.enumText);
        return [
          parameter.name.trim(),
          {
            type: parameter.type.trim() || 'string',
            ...(parameter.description.trim() ? { description: parameter.description.trim() } : {}),
            ...(enumValues.length > 0 ? { enum: enumValues } : {})
          }
        ];
      })
  );
  const required = tool.parameters
    .filter((parameter) => parameter.required && parameter.name.trim())
    .map((parameter) => parameter.name.trim());
  const payload: Record<string, unknown> = {
    name: tool.name.trim(),
    description: tool.description.trim(),
    read_only: tool.readOnly,
    non_bypassable: tool.nonBypassable,
    timeout_seconds: Number.isFinite(tool.timeoutSeconds) ? Number(tool.timeoutSeconds) : 60,
    max_result_size: Number.isFinite(tool.maxResultSize) ? Number(tool.maxResultSize) : 50_000,
    parameters: {
      type: 'object',
      properties,
      ...(required.length > 0 ? { required } : {})
    }
  };
  if (tool.recipeMode !== 'none') {
    payload.recipe = {
      mode: tool.recipeMode,
      entry: tool.entry.trim(),
      args: splitCsvOrLines(tool.argsText),
      env: Object.fromEntries(
        tool.env
          .filter((entry) => entry.key.trim())
          .map((entry) => [entry.key.trim(), entry.value])
      ),
      timeout_seconds: Number.isFinite(tool.timeoutSeconds) ? Number(tool.timeoutSeconds) : 60,
      required_assets: splitCsvOrLines(tool.requiredAssetsText),
      secret_placeholders: splitCsvOrLines(tool.secretPlaceholdersText),
      ...(tool.workingDir.trim() ? { working_dir: tool.workingDir.trim() } : {})
    };
  }
  return payload;
}

export function createEmptySkillTool(): SkillToolFormItem {
  return {
    id: createId('skill_tool'),
    name: '',
    description: '',
    readOnly: false,
    nonBypassable: true,
    timeoutSeconds: 60,
    maxResultSize: 50_000,
    parameters: [],
    recipeMode: 'none',
    entry: '',
    argsText: '',
    env: [],
    requiredAssetsText: '',
    secretPlaceholdersText: '',
    workingDir: ''
  };
}

export function createEmptySkillParameter(): SkillToolParameterFormItem {
  return {
    id: createId('skill_param'),
    name: '',
    type: 'string',
    description: '',
    required: false,
    enumText: ''
  };
}

export function createEmptyPromptTemplate(): SkillPromptTemplateFormItem {
  return {
    id: createId('skill_template'),
    key: '',
    value: ''
  };
}

export function createEmptyKeyValueEntry(prefix = 'skill_env'): SkillKeyValueFormItem {
  return {
    id: createId(prefix),
    key: '',
    value: ''
  };
}

export function createEmptySkillForm(): SkillFormState {
  return {
    name: '',
    description: '',
    instructions: '',
    tagsText: '',
    attachToAllAgents: false,
    promptTemplates: [],
    secretPlaceholders: [],
    tools: [],
    assets: []
  };
}

export function skillToFormState(skill: Skill): SkillFormState {
  const current = skill.current_version;
  return {
    name: skill.name,
    description: skill.description ?? '',
    instructions: current?.instructions ?? skill.instructions,
    tagsText: (skill.tags ?? []).join(', '),
    attachToAllAgents: Boolean(skill.attach_to_all_agents ?? skill.auto_load),
    promptTemplates: parsePromptTemplates(current?.prompt_templates ?? skill.prompt_templates),
    secretPlaceholders: Array.isArray(current?.secret_placeholders)
      ? current.secret_placeholders.filter((item): item is string => typeof item === 'string')
      : [],
    tools: Array.isArray(current?.tools)
      ? current.tools
          .filter((item): item is Record<string, unknown> => isRecord(item))
          .map((tool) => parseTool(tool))
      : [],
    assets: Array.isArray(current?.asset_manifest)
      ? current.asset_manifest.map((asset) => ({
          filename: asset.filename,
          existing_asset_id: asset.asset_id,
          content_type: asset.content_type,
          size_bytes: asset.size_bytes
        }))
      : []
  };
}

export function validateSkillForm(form: SkillFormState): string[] {
  const issues: string[] = [];
  if (!form.name.trim()) {
    issues.push('Skill name is required.');
  }
  if (!form.instructions.trim()) {
    issues.push('Instructions are required.');
  }
  const templateKeys = new Set<string>();
  for (const template of form.promptTemplates) {
    const key = template.key.trim();
    if (!key && !template.value.trim()) {
      continue;
    }
    if (!key) {
      issues.push('Prompt template keys cannot be empty.');
      continue;
    }
    if (templateKeys.has(key)) {
      issues.push(`Duplicate prompt template key: ${key}.`);
    }
    templateKeys.add(key);
  }
  const toolNames = new Set<string>();
  for (const tool of form.tools) {
    const name = tool.name.trim();
    if (!name) {
      issues.push('Each tool needs a name.');
    } else if (toolNames.has(name)) {
      issues.push(`Duplicate tool name: ${name}.`);
    } else {
      toolNames.add(name);
    }
    if (!tool.description.trim()) {
      issues.push(`Tool ${name || '(unnamed)'} needs a description.`);
    }
    if (tool.recipeMode !== 'none' && !tool.entry.trim()) {
      issues.push(`Tool ${name || '(unnamed)'} needs a recipe entry path or command.`);
    }
    const parameterNames = new Set<string>();
    for (const parameter of tool.parameters) {
      const parameterName = parameter.name.trim();
      if (!parameterName && !parameter.description.trim()) {
        continue;
      }
      if (!parameterName) {
        issues.push(`Tool ${name || '(unnamed)'} has a parameter without a name.`);
        continue;
      }
      if (parameterNames.has(parameterName)) {
        issues.push(`Tool ${name || '(unnamed)'} has a duplicate parameter: ${parameterName}.`);
      }
      parameterNames.add(parameterName);
    }
  }
  return issues;
}

export function formStateToSkillPayload(form: SkillFormState): SkillCreate | SkillUpdate {
  return {
    name: form.name.trim(),
    description: form.description.trim() || undefined,
    instructions: form.instructions,
    tags: splitCsvOrLines(form.tagsText),
    attach_to_all_agents: form.attachToAllAgents,
    prompt_templates: Object.fromEntries(
      form.promptTemplates
        .filter((template) => template.key.trim())
        .map((template) => [template.key.trim(), template.value])
    ),
    secret_placeholders: form.secretPlaceholders.filter((item) => item.trim()).map((item) => item.trim()),
    tools: form.tools.map((tool) => toolToPayload(tool)),
    assets: form.assets.map((asset) => ({
      filename: asset.filename,
      existing_asset_id: asset.existing_asset_id,
      source_artifact_id: asset.source_artifact_id,
      content_type: asset.content_type
    }))
  };
}

export function skillToWorkflowDraft(
  skill: Skill,
  steps?: Record<string, unknown>[] | null
): WorkflowFormState {
  const effectiveSteps = Array.isArray(steps) && steps.length > 0 ? steps : skill.current_version?.steps ?? skill.steps ?? [];
  if (!Array.isArray(effectiveSteps) || effectiveSteps.length === 0) {
    const form = createEmptyWorkflowForm();
    form.name = `${skill.name} Workflow`;
    form.description = skill.description ?? '';
    form.lineage = {
      source_skill_ids: [skill.skill_id],
      composition_source: 'manual'
    };
    return form;
  }
  const workflow: Workflow = {
    workflow_id: '',
    name: `${skill.name} Workflow`,
    description: skill.description ?? '',
    version: 1,
    criteria: '',
    tags: skill.tags ?? [],
    interaction: { mode: 'explicit_gates' },
    defaults: {
      evaluate: true,
      max_attempts: 3,
      on_exhausted: 'gate',
      delivery: {
        completion_mode_family: 'default',
        allow_silent_completion: false
      }
    },
    steps: effectiveSteps as unknown as WorkflowStep[],
    is_system: false,
    owner_email: null,
    lifecycle: 'persistent',
    archived_at: null,
    lineage: {
      source_skill_ids: [skill.skill_id],
      composition_source: 'manual'
    },
    editable_fields: [],
    has_overrides: false,
    disabled: false,
    disableable: false,
    override_warnings: []
  };
  return workflowToFormState(workflow);
}

export function saveSkillWorkflowDraft(skillId: string, form: WorkflowFormState): void {
  if (typeof sessionStorage === 'undefined') {
    return;
  }
  const payload: SkillWorkflowDraftStorage = {
    skillId,
    generatedAt: new Date().toISOString(),
    form
  };
  sessionStorage.setItem(SKILL_WORKFLOW_DRAFT_STORAGE_KEY, JSON.stringify(payload));
}

export function loadSkillWorkflowDraft(skillId: string): SkillWorkflowDraftStorage | null {
  if (typeof sessionStorage === 'undefined') {
    return null;
  }
  const raw = sessionStorage.getItem(SKILL_WORKFLOW_DRAFT_STORAGE_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as SkillWorkflowDraftStorage;
    if (parsed.skillId !== skillId || !parsed.form) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function clearSkillWorkflowDraft(): void {
  if (typeof sessionStorage === 'undefined') {
    return;
  }
  sessionStorage.removeItem(SKILL_WORKFLOW_DRAFT_STORAGE_KEY);
}
