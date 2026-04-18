/**
 * Docs registry: metadata-only.
 *
 * This module intentionally does NOT import any markdown `?raw` content.
 * Content is loaded lazily via `loadDocContent()` / `loadDocsOverviewContent()`.
 *
 * Previously, `src/lib/docs.ts` eagerly imported 13 `.md?raw` files at module
 * load time, resulting in a ~71 KB docs chunk being shipped to every page
 * that imported `$lib/docs` — including `/getting-started`, which needed
 * only titles and hrefs.
 *
 * Now:
 *   - `embeddedDocsMeta` is cheap (titles, slugs, categories, related links).
 *   - Actual markdown for a given slug is fetched with a dynamic `import()`
 *     so each doc becomes its own chunk, loaded on demand.
 */

export const DOC_CATEGORIES = ['getting-started', 'workspace', 'operations'] as const;

export type DocCategory = (typeof DOC_CATEGORIES)[number];

export interface DocMeta {
  slug: string;
  title: string;
  description: string;
  category: DocCategory;
  sourcePath: string;
  relatedSlugs?: string[];
}

export const embeddedDocsMeta: DocMeta[] = [
  {
    slug: 'getting-started',
    title: 'Getting Started',
    description: 'Set up Mnemory, Intaris, Cognis, a provider, and your first agent.',
    category: 'getting-started',
    sourcePath: 'docs/guide/getting-started.md',
    relatedSlugs: ['architecture', 'configuring-providers', 'creating-agents', 'using-chat']
  },
  {
    slug: 'architecture',
    title: 'Architecture',
    description: 'See how Cognis works with Mnemory, Intaris, and executors.',
    category: 'getting-started',
    sourcePath: 'docs/guide/architecture.md',
    relatedSlugs: ['executors', 'channels', 'workflows']
  },
  {
    slug: 'configuring-providers',
    title: 'Configuring Providers',
    description: 'Add LLM providers, test connectivity, and tune routing decisions.',
    category: 'getting-started',
    sourcePath: 'docs/guide/configuring-providers.md',
    relatedSlugs: ['getting-started', 'settings', 'executors', 'creating-agents']
  },
  {
    slug: 'creating-agents',
    title: 'Creating Agents',
    description: 'Define identity, personality, tools, executors, and workflow options.',
    category: 'workspace',
    sourcePath: 'docs/guide/creating-agents.md',
    relatedSlugs: ['configuring-providers', 'using-chat', 'executors', 'tools-and-skills']
  },
  {
    slug: 'settings',
    title: 'Settings',
    description: 'Configure providers, routing, secrets, executors, diagnostics, and users.',
    category: 'workspace',
    sourcePath: 'docs/guide/settings.md',
    relatedSlugs: ['configuring-providers', 'executors', 'tools-and-skills', 'troubleshooting']
  },
  {
    slug: 'using-chat',
    title: 'Using Chat',
    description: 'Understand streaming replies, tool activity, approvals, and delegation.',
    category: 'workspace',
    sourcePath: 'docs/guide/using-chat.md',
    relatedSlugs: ['getting-started', 'managing-tasks', 'creating-agents']
  },
  {
    slug: 'managing-tasks',
    title: 'Managing Tasks',
    description: 'Track queued or running work, workflow progress, and delivery back to chat.',
    category: 'workspace',
    sourcePath: 'docs/guide/managing-tasks.md',
    relatedSlugs: ['workflows', 'using-chat']
  },
  {
    slug: 'workflows',
    title: 'Workflows',
    description: 'Create reusable execution templates with steps, gates, and revision loops.',
    category: 'workspace',
    sourcePath: 'docs/guide/workflows.md',
    relatedSlugs: ['managing-tasks', 'creating-agents']
  },
  {
    slug: 'tools-and-skills',
    title: 'Tools and Skills',
    description: 'Inspect the tool registry, MCP-backed capabilities, and reusable skills.',
    category: 'workspace',
    sourcePath: 'docs/guide/tools-and-skills.md',
    relatedSlugs: ['settings', 'creating-agents', 'executors']
  },
  {
    slug: 'channels',
    title: 'Channels',
    description: 'Connect agents to external platforms and understand pairing and trust.',
    category: 'operations',
    sourcePath: 'docs/guide/channels.md',
    relatedSlugs: ['executors', 'troubleshooting']
  },
  {
    slug: 'executors',
    title: 'Executors',
    description: 'Choose where tools run and how remote executor placement affects agents.',
    category: 'operations',
    sourcePath: 'docs/guide/executors.md',
    relatedSlugs: ['channels', 'configuring-providers', 'creating-agents']
  },
  {
    slug: 'troubleshooting',
    title: 'Troubleshooting',
    description: 'Resolve common setup, provider, executor, and UI problems.',
    category: 'operations',
    sourcePath: 'docs/guide/troubleshooting.md',
    relatedSlugs: ['getting-started', 'configuring-providers', 'executors']
  }
];

export const ONBOARDING_DOC_SLUGS = [
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

export function getDocMeta(slug: string): DocMeta | null {
  return embeddedDocsMeta.find((doc) => doc.slug === slug) ?? null;
}

export function getDocHref(slug: string): string {
  return `/docs/${slug}`;
}

export function getOnboardingDocsMeta(): DocMeta[] {
  return ONBOARDING_DOC_SLUGS.map((slug) => getDocMeta(slug)).filter(
    (doc): doc is DocMeta => doc !== null
  );
}

export function getRelatedDocsMeta(doc: DocMeta): DocMeta[] {
  return (doc.relatedSlugs ?? [])
    .map((slug) => getDocMeta(slug))
    .filter((d): d is DocMeta => d !== null);
}

export function getDocsByCategoryMeta(): Array<{ category: DocCategory; docs: DocMeta[] }> {
  return DOC_CATEGORIES.map((category) => ({
    category,
    docs: embeddedDocsMeta.filter((doc) => doc.category === category)
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
