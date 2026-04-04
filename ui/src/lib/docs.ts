import docsOverviewMarkdown from '../../../docs/README.md?raw';
import channelsMarkdown from '../../../docs/guide/channels.md?raw';
import providersMarkdown from '../../../docs/guide/configuring-providers.md?raw';
import agentsMarkdown from '../../../docs/guide/creating-agents.md?raw';
import executorsMarkdown from '../../../docs/guide/executors.md?raw';
import gettingStartedMarkdown from '../../../docs/guide/getting-started.md?raw';
import tasksMarkdown from '../../../docs/guide/managing-tasks.md?raw';
import troubleshootingMarkdown from '../../../docs/guide/troubleshooting.md?raw';
import chatMarkdown from '../../../docs/guide/using-chat.md?raw';
import workflowsMarkdown from '../../../docs/guide/workflows.md?raw';

export const DOC_CATEGORIES = ['getting-started', 'workspace', 'operations'] as const;

export type DocCategory = (typeof DOC_CATEGORIES)[number];

export interface EmbeddedDoc {
  slug: string;
  title: string;
  description: string;
  category: DocCategory;
  sourcePath: string;
  content: string;
  relatedSlugs?: string[];
}

export interface DocsOverview {
  title: string;
  sourcePath: string;
  content: string;
}

const ONBOARDING_DOC_SLUGS = [
  'getting-started',
  'configuring-providers',
  'creating-agents',
  'using-chat',
  'managing-tasks',
  'workflows',
  'channels',
  'executors',
  'troubleshooting'
] as const;

const DOC_ROUTE_RE = /^\/docs\/([a-z0-9-]+)$/;
const ALLOWED_APP_ROUTE_RE = /^\/(docs(\/[a-z0-9-]+)?|settings(\?.*)?|agents(\/.*)?|chat(\/.*)?|tasks(\/.*)?|workflows(\/.*)?|tools(\/.*)?|channels(\/.*)?)$/;
const MARKDOWN_LINK_RE = /!??\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;

export const docsOverview: DocsOverview = {
  title: 'Documentation',
  sourcePath: 'docs/README.md',
  content: docsOverviewMarkdown
};

export const embeddedDocs: EmbeddedDoc[] = [
  {
    slug: 'getting-started',
    title: 'Getting Started',
    description: 'Set up Mnemory, Intaris, Cognis, a provider, and your first agent.',
    category: 'getting-started',
    sourcePath: 'docs/guide/getting-started.md',
    content: gettingStartedMarkdown,
    relatedSlugs: ['configuring-providers', 'creating-agents', 'using-chat']
  },
  {
    slug: 'configuring-providers',
    title: 'Configuring Providers',
    description: 'Add LLM providers, test connectivity, and tune routing decisions.',
    category: 'getting-started',
    sourcePath: 'docs/guide/configuring-providers.md',
    content: providersMarkdown,
    relatedSlugs: ['getting-started', 'executors', 'creating-agents']
  },
  {
    slug: 'creating-agents',
    title: 'Creating Agents',
    description: 'Define identity, personality, tools, executors, and workflow options.',
    category: 'workspace',
    sourcePath: 'docs/guide/creating-agents.md',
    content: agentsMarkdown,
    relatedSlugs: ['configuring-providers', 'using-chat', 'executors']
  },
  {
    slug: 'using-chat',
    title: 'Using Chat',
    description: 'Understand streaming replies, tool activity, approvals, and delegation.',
    category: 'workspace',
    sourcePath: 'docs/guide/using-chat.md',
    content: chatMarkdown,
    relatedSlugs: ['getting-started', 'managing-tasks', 'creating-agents']
  },
  {
    slug: 'managing-tasks',
    title: 'Managing Tasks',
    description: 'Track queued or running work, workflow progress, and delivery back to chat.',
    category: 'workspace',
    sourcePath: 'docs/guide/managing-tasks.md',
    content: tasksMarkdown,
    relatedSlugs: ['workflows', 'using-chat']
  },
  {
    slug: 'workflows',
    title: 'Workflows',
    description: 'Create reusable execution templates with steps, gates, and revision loops.',
    category: 'workspace',
    sourcePath: 'docs/guide/workflows.md',
    content: workflowsMarkdown,
    relatedSlugs: ['managing-tasks', 'creating-agents']
  },
  {
    slug: 'channels',
    title: 'Channels',
    description: 'Connect agents to external platforms and understand pairing and trust.',
    category: 'operations',
    sourcePath: 'docs/guide/channels.md',
    content: channelsMarkdown,
    relatedSlugs: ['executors', 'troubleshooting']
  },
  {
    slug: 'executors',
    title: 'Executors',
    description: 'Choose where tools run and how remote executor placement affects agents.',
    category: 'operations',
    sourcePath: 'docs/guide/executors.md',
    content: executorsMarkdown,
    relatedSlugs: ['channels', 'configuring-providers', 'creating-agents']
  },
  {
    slug: 'troubleshooting',
    title: 'Troubleshooting',
    description: 'Resolve common setup, provider, executor, and UI problems.',
    category: 'operations',
    sourcePath: 'docs/guide/troubleshooting.md',
    content: troubleshootingMarkdown,
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
  const allMarkdownSources: Array<{ title: string; sourcePath: string; content: string }> = [docsOverview, ...embeddedDocs];

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

      errors.push(`Relative links or assets are not allowed in ${doc.sourcePath}: ${target}`);
    }
  }

  return errors;
}

const registryErrors = validateEmbeddedDocs();

if (registryErrors.length > 0) {
  throw new Error(`Embedded docs validation failed:\n${registryErrors.join('\n')}`);
}
