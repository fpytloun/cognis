import docsOverviewMarkdown from '../../../docs/README.md?raw';
import architectureMarkdown from '../../../docs/guide/architecture.md?raw';
import channelsMarkdown from '../../../docs/guide/channels.md?raw';
import providersMarkdown from '../../../docs/guide/configuring-providers.md?raw';
import agentsMarkdown from '../../../docs/guide/creating-agents.md?raw';
import executorsMarkdown from '../../../docs/guide/executors.md?raw';
import gettingStartedMarkdown from '../../../docs/guide/getting-started.md?raw';
import tasksMarkdown from '../../../docs/guide/managing-tasks.md?raw';
import settingsMarkdown from '../../../docs/guide/settings.md?raw';
import troubleshootingMarkdown from '../../../docs/guide/troubleshooting.md?raw';
import toolsAndSkillsMarkdown from '../../../docs/guide/tools-and-skills.md?raw';
import chatMarkdown from '../../../docs/guide/using-chat.md?raw';
import workflowsMarkdown from '../../../docs/guide/workflows.md?raw';
import agentToolInheritanceSvg from '../../../docs/assets/images/cognis-agent-tool-inheritance.svg?url';
import channelPairingFlowSvg from '../../../docs/assets/images/cognis-channel-pairing-flow.svg?url';
import controllerExecutorSplitSvg from '../../../docs/assets/images/cognis-controller-executor-split.svg?url';
import ecosystemOverviewSvg from '../../../docs/assets/images/cognis-ecosystem-overview.svg?url';
import workflowLifecycleSvg from '../../../docs/assets/images/cognis-workflow-task-lifecycle.svg?url';

export const DOC_CATEGORIES = ['getting-started', 'workspace', 'operations'] as const;

export type DocCategory = (typeof DOC_CATEGORIES)[number];

export interface EmbeddedDoc {
  slug: string;
  title: string;
  description: string;
  category: DocCategory;
  sourcePath: string;
  content: string;
  rawContent?: string;
  relatedSlugs?: string[];
}

export interface DocsOverview {
  title: string;
  sourcePath: string;
  content: string;
  rawContent?: string;
}

const ONBOARDING_DOC_SLUGS = [
  'getting-started',
  'architecture',
  'configuring-providers',
  'creating-agents',
  'settings',
  'using-chat',
  'managing-tasks',
  'workflows',
  'channels',
  'executors',
  'tools-and-skills',
  'troubleshooting'
] as const;

const DOC_ROUTE_RE = /^\/docs\/([a-z0-9-]+)$/;
const ALLOWED_APP_ROUTE_RE = /^\/(docs(\/[a-z0-9-]+)?|settings(\?.*)?|agents(\/.*)?|chat(\/.*)?|tasks(\/.*)?|workflows(\/.*)?|tools(\/.*)?|channels(\/.*)?)$/;
const MARKDOWN_LINK_RE = /!??\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;

const DOC_ASSET_URLS: Record<string, string> = {
  'docs/assets/images/cognis-agent-tool-inheritance.svg': agentToolInheritanceSvg,
  'docs/assets/images/cognis-channel-pairing-flow.svg': channelPairingFlowSvg,
  'docs/assets/images/cognis-controller-executor-split.svg': controllerExecutorSplitSvg,
  'docs/assets/images/cognis-ecosystem-overview.svg': ecosystemOverviewSvg,
  'docs/assets/images/cognis-workflow-task-lifecycle.svg': workflowLifecycleSvg
};

function normalizeDocPath(path: string): string {
  const parts = path.split('/');
  const normalized: string[] = [];

  for (const part of parts) {
    if (!part || part === '.') {
      continue;
    }
    if (part === '..') {
      normalized.pop();
      continue;
    }
    normalized.push(part);
  }

  return normalized.join('/');
}

function dirname(path: string): string {
  const parts = path.split('/');
  parts.pop();
  return parts.join('/');
}

function resolveDocRelativePath(sourcePath: string, target: string): string {
  return normalizeDocPath(`${dirname(sourcePath)}/${target}`);
}

function rewriteMarkdownAssets(sourcePath: string, markdown: string): string {
  return markdown.replace(MARKDOWN_LINK_RE, (fullMatch, rawTarget: string) => {
    if (!(rawTarget.startsWith('./') || rawTarget.startsWith('../'))) {
      return fullMatch;
    }

    const resolvedTarget = resolveDocRelativePath(sourcePath, rawTarget);
    const rewrittenTarget = DOC_ASSET_URLS[resolvedTarget];
    if (!rewrittenTarget) {
      return fullMatch;
    }

    return fullMatch.replace(rawTarget, rewrittenTarget);
  });
}

export const docsOverview: DocsOverview = {
  title: 'Documentation',
  sourcePath: 'docs/README.md',
  rawContent: docsOverviewMarkdown,
  content: rewriteMarkdownAssets('docs/README.md', docsOverviewMarkdown)
};

export const embeddedDocs: EmbeddedDoc[] = [
  {
    slug: 'getting-started',
    title: 'Getting Started',
    description: 'Set up Mnemory, Intaris, Cognis, a provider, and your first agent.',
    category: 'getting-started',
    sourcePath: 'docs/guide/getting-started.md',
    rawContent: gettingStartedMarkdown,
    content: rewriteMarkdownAssets('docs/guide/getting-started.md', gettingStartedMarkdown),
    relatedSlugs: ['architecture', 'configuring-providers', 'creating-agents', 'using-chat']
  },
  {
    slug: 'architecture',
    title: 'Architecture',
    description: 'See how Cognis works with Mnemory, Intaris, and executors.',
    category: 'getting-started',
    sourcePath: 'docs/guide/architecture.md',
    rawContent: architectureMarkdown,
    content: rewriteMarkdownAssets('docs/guide/architecture.md', architectureMarkdown),
    relatedSlugs: ['executors', 'channels', 'workflows']
  },
  {
    slug: 'configuring-providers',
    title: 'Configuring Providers',
    description: 'Add LLM providers, test connectivity, and tune routing decisions.',
    category: 'getting-started',
    sourcePath: 'docs/guide/configuring-providers.md',
    rawContent: providersMarkdown,
    content: rewriteMarkdownAssets('docs/guide/configuring-providers.md', providersMarkdown),
    relatedSlugs: ['getting-started', 'settings', 'executors', 'creating-agents']
  },
  {
    slug: 'creating-agents',
    title: 'Creating Agents',
    description: 'Define identity, personality, tools, executors, and workflow options.',
    category: 'workspace',
    sourcePath: 'docs/guide/creating-agents.md',
    rawContent: agentsMarkdown,
    content: rewriteMarkdownAssets('docs/guide/creating-agents.md', agentsMarkdown),
    relatedSlugs: ['configuring-providers', 'using-chat', 'executors', 'tools-and-skills']
  },
  {
    slug: 'settings',
    title: 'Settings',
    description: 'Configure providers, routing, secrets, executors, diagnostics, and users.',
    category: 'workspace',
    sourcePath: 'docs/guide/settings.md',
    rawContent: settingsMarkdown,
    content: rewriteMarkdownAssets('docs/guide/settings.md', settingsMarkdown),
    relatedSlugs: ['configuring-providers', 'executors', 'tools-and-skills', 'troubleshooting']
  },
  {
    slug: 'using-chat',
    title: 'Using Chat',
    description: 'Understand streaming replies, tool activity, approvals, and delegation.',
    category: 'workspace',
    sourcePath: 'docs/guide/using-chat.md',
    rawContent: chatMarkdown,
    content: rewriteMarkdownAssets('docs/guide/using-chat.md', chatMarkdown),
    relatedSlugs: ['getting-started', 'managing-tasks', 'creating-agents']
  },
  {
    slug: 'managing-tasks',
    title: 'Managing Tasks',
    description: 'Track queued or running work, workflow progress, and delivery back to chat.',
    category: 'workspace',
    sourcePath: 'docs/guide/managing-tasks.md',
    rawContent: tasksMarkdown,
    content: rewriteMarkdownAssets('docs/guide/managing-tasks.md', tasksMarkdown),
    relatedSlugs: ['workflows', 'using-chat']
  },
  {
    slug: 'workflows',
    title: 'Workflows',
    description: 'Create reusable execution templates with steps, gates, and revision loops.',
    category: 'workspace',
    sourcePath: 'docs/guide/workflows.md',
    rawContent: workflowsMarkdown,
    content: rewriteMarkdownAssets('docs/guide/workflows.md', workflowsMarkdown),
    relatedSlugs: ['managing-tasks', 'creating-agents']
  },
  {
    slug: 'tools-and-skills',
    title: 'Tools and Skills',
    description: 'Inspect the tool registry, MCP-backed capabilities, and reusable skills.',
    category: 'workspace',
    sourcePath: 'docs/guide/tools-and-skills.md',
    rawContent: toolsAndSkillsMarkdown,
    content: rewriteMarkdownAssets('docs/guide/tools-and-skills.md', toolsAndSkillsMarkdown),
    relatedSlugs: ['settings', 'creating-agents', 'executors']
  },
  {
    slug: 'channels',
    title: 'Channels',
    description: 'Connect agents to external platforms and understand pairing and trust.',
    category: 'operations',
    sourcePath: 'docs/guide/channels.md',
    rawContent: channelsMarkdown,
    content: rewriteMarkdownAssets('docs/guide/channels.md', channelsMarkdown),
    relatedSlugs: ['executors', 'troubleshooting']
  },
  {
    slug: 'executors',
    title: 'Executors',
    description: 'Choose where tools run and how remote executor placement affects agents.',
    category: 'operations',
    sourcePath: 'docs/guide/executors.md',
    rawContent: executorsMarkdown,
    content: rewriteMarkdownAssets('docs/guide/executors.md', executorsMarkdown),
    relatedSlugs: ['channels', 'configuring-providers', 'creating-agents']
  },
  {
    slug: 'troubleshooting',
    title: 'Troubleshooting',
    description: 'Resolve common setup, provider, executor, and UI problems.',
    category: 'operations',
    sourcePath: 'docs/guide/troubleshooting.md',
    rawContent: troubleshootingMarkdown,
    content: rewriteMarkdownAssets('docs/guide/troubleshooting.md', troubleshootingMarkdown),
    relatedSlugs: ['getting-started', 'configuring-providers', 'executors']
  }
];

export function getEmbeddedDocs(): EmbeddedDoc[] {
  return embeddedDocs;
}

export function getEmbeddedDoc(slug: string): EmbeddedDoc | null {
  return embeddedDocs.find((doc) => doc.slug === slug) ?? null;
}

export function getDocHref(slug: string): string {
  return `/docs/${slug}`;
}

export function getOnboardingDocs(): EmbeddedDoc[] {
  return ONBOARDING_DOC_SLUGS.map((slug) => getEmbeddedDoc(slug)).filter((doc): doc is EmbeddedDoc => doc !== null);
}

export function getRelatedDocs(doc: EmbeddedDoc): EmbeddedDoc[] {
  return (doc.relatedSlugs ?? [])
    .map((slug) => getEmbeddedDoc(slug))
    .filter((candidate): candidate is EmbeddedDoc => candidate !== null);
}

export function getDocsByCategory(): Array<{ category: DocCategory; docs: EmbeddedDoc[] }> {
  return DOC_CATEGORIES.map((category) => ({
    category,
    docs: embeddedDocs.filter((doc) => doc.category === category)
  })).filter((group) => group.docs.length > 0);
}

export function getCategoryLabel(category: DocCategory): string {
  switch (category) {
    case 'getting-started':
      return 'Getting Started';
    case 'workspace':
      return 'Workspace';
    case 'operations':
      return 'Operations';
  }
}

export function extractMarkdownTitle(markdown: string): string | null {
  const match = markdown.match(/^#\s+(.+)$/m);
  return match?.[1]?.trim() ?? null;
}

function collectMarkdownTargets(markdown: string): string[] {
  const matches = markdown.matchAll(MARKDOWN_LINK_RE);
  return Array.from(matches, (match) => match[1] ?? '').filter(Boolean);
}

export function validateEmbeddedDocs(): string[] {
  const errors: string[] = [];
  const slugSet = new Set<string>();
  const sourcePathSet = new Set<string>();
  const allowedSlugs = new Set(embeddedDocs.map((doc) => doc.slug));
  const allMarkdownSources: Array<{ title: string; sourcePath: string; content: string }> = [
    { title: docsOverview.title, sourcePath: docsOverview.sourcePath, content: docsOverview.rawContent ?? docsOverview.content },
    ...embeddedDocs.map((doc) => ({ title: doc.title, sourcePath: doc.sourcePath, content: doc.rawContent ?? doc.content }))
  ];

  for (const doc of embeddedDocs) {
    if (slugSet.has(doc.slug)) {
      errors.push(`Duplicate doc slug: ${doc.slug}`);
    }
    slugSet.add(doc.slug);

    if (sourcePathSet.has(doc.sourcePath)) {
      errors.push(`Duplicate doc source path: ${doc.sourcePath}`);
    }
    sourcePathSet.add(doc.sourcePath);

    if (!DOC_CATEGORIES.includes(doc.category)) {
      errors.push(`Invalid doc category for ${doc.slug}: ${doc.category}`);
    }

    if (doc.sourcePath.includes('docs/specs/')) {
      errors.push(`Forbidden docs/specs source path: ${doc.sourcePath}`);
    }

    if (!doc.content.trim()) {
      errors.push(`Doc content is empty: ${doc.slug}`);
    }

    const markdownTitle = extractMarkdownTitle(doc.content);
    if (markdownTitle !== doc.title) {
      errors.push(`Doc title mismatch for ${doc.slug}: expected "${doc.title}" but found "${markdownTitle ?? 'missing'}"`);
    }

    for (const relatedSlug of doc.relatedSlugs ?? []) {
      if (!allowedSlugs.has(relatedSlug)) {
        errors.push(`Doc ${doc.slug} references missing related slug: ${relatedSlug}`);
      }
    }
  }

  for (const doc of allMarkdownSources) {
    const targets = collectMarkdownTargets(doc.content);
    for (const target of targets) {
      if (target.includes('github.com') && target.includes('/blob/')) {
        errors.push(`GitHub blob links are not allowed in ${doc.sourcePath}: ${target}`);
        continue;
      }

      if (target.startsWith('#')) {
        errors.push(`Heading anchor links are not supported in ${doc.sourcePath}: ${target}`);
        continue;
      }

      if (target.startsWith('http://') || target.startsWith('https://')) {
        continue;
      }

      if (target.startsWith('/')) {
        const docsRouteMatch = target.match(DOC_ROUTE_RE);
        if (docsRouteMatch) {
          const targetSlug = docsRouteMatch[1];
          if (!allowedSlugs.has(targetSlug)) {
            errors.push(`Doc route points to missing slug in ${doc.sourcePath}: ${target}`);
          }
          continue;
        }

        if (!ALLOWED_APP_ROUTE_RE.test(target)) {
          errors.push(`Unsupported app route in ${doc.sourcePath}: ${target}`);
        }
        continue;
      }

      if (target.startsWith('./') || target.startsWith('../')) {
        const resolvedTarget = resolveDocRelativePath(doc.sourcePath, target);
        if (!DOC_ASSET_URLS[resolvedTarget]) {
          errors.push(`Relative link or asset is not allowlisted in ${doc.sourcePath}: ${target}`);
        }
        continue;
      }

      errors.push(`Relative links or assets are not allowed in ${doc.sourcePath}: ${target}`);
    }
  }

  return errors;
}

const registryErrors = validateEmbeddedDocs();

if (registryErrors.length > 0) {
  throw new Error(`Embedded docs validation failed:\n${registryErrors.join('\n')}`);
}
