/**
 * Docs registry: metadata-only.
 *
 * This module intentionally does NOT import any markdown `?raw` content.
 * Content is loaded lazily via `loadDocContent()` / `loadDocsOverviewContent()`.
 *
 * Previously, `src/lib/docs.ts` eagerly imported all guide `.md?raw` files at module
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
    relatedSlugs: [
      'local-compose',
      'architecture',
      'configuring-providers',
      'creating-agents',
      'using-chat'
    ]
  },
  {
    slug: 'local-compose',
    title: 'Local Compose Deployment',
    description: 'Run a complete local Cognis, Mnemory, Intaris, Qdrant, and executor stack.',
    category: 'getting-started',
    sourcePath: 'docs/guide/local-compose.md',
    relatedSlugs: ['getting-started', 'deployment', 'executors', 'configuring-providers']
  },
  {
    slug: 'architecture',
    title: 'Architecture',
    description: 'See how Cognis works with Mnemory, Intaris, and executors.',
    category: 'getting-started',
    sourcePath: 'docs/guide/architecture.md',
    relatedSlugs: ['executors', 'deployment', 'security-and-privacy']
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
    slug: 'security-and-privacy',
    title: 'Security and Privacy',
    description: 'Understand secrets, value refs, guardrails, executor boundaries, and inference privacy.',
    category: 'getting-started',
    sourcePath: 'docs/guide/security-and-privacy.md',
    relatedSlugs: ['architecture', 'executors', 'deployment']
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
    relatedSlugs: ['workflows', 'projects', 'using-chat']
  },
  {
    slug: 'projects',
    title: 'Projects',
    description: 'Group sources, workflow bindings, grants, tasks, schedules, and conversations.',
    category: 'workspace',
    sourcePath: 'docs/guide/projects.md',
    relatedSlugs: ['managing-tasks', 'workflows', 'creating-agents']
  },
  {
    slug: 'schedules',
    title: 'Schedules',
    description: 'Create recurring task factories that run workflows on a schedule.',
    category: 'workspace',
    sourcePath: 'docs/guide/schedules.md',
    relatedSlugs: ['projects', 'workflows', 'managing-tasks']
  },
  {
    slug: 'workflows',
    title: 'Workflows',
    description: 'Create reusable execution templates with steps, gates, and revision loops.',
    category: 'workspace',
    sourcePath: 'docs/guide/workflows.md',
    relatedSlugs: ['managing-tasks', 'projects', 'creating-agents']
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
    slug: 'content-and-sharing',
    title: 'Content and Temporary Sharing',
    description: 'Understand artifact retention, temporary signed links, documents, HTML viewing, and expired-link recovery.',
    category: 'workspace',
    sourcePath: 'docs/guide/content-and-sharing.md',
    relatedSlugs: ['rich-deliverables', 'tools-and-skills', 'using-chat']
  },
  {
    slug: 'rich-deliverables',
    title: 'Rich Deliverables',
    description: 'Create structured, durable Cognis outputs with interactive and fallback renderers.',
    category: 'workspace',
    sourcePath: 'docs/guide/rich-deliverables.md',
    relatedSlugs: ['content-and-sharing', 'rich-deliverable-composition', 'tools-and-skills']
  },
  {
    slug: 'rich-deliverable-composition',
    title: 'Rich Deliverable Composition',
    description: 'Compose renderer-neutral rich deliverables with blocks, evidence, assets, and fallbacks.',
    category: 'workspace',
    sourcePath: 'docs/guide/rich-deliverable-composition.md',
    relatedSlugs: ['rich-deliverables', 'rich-deliverable-blocks-layout', 'rich-deliverable-blocks-data']
  },
  {
    slug: 'rich-deliverable-blocks-layout',
    title: 'Rich Deliverable Layout Blocks',
    description: 'Reference layout, narrative, status, and action blocks with generated examples.',
    category: 'workspace',
    sourcePath: 'docs/guide/rich-deliverable-blocks-layout.md',
    relatedSlugs: ['rich-deliverable-composition', 'rich-deliverable-blocks-data']
  },
  {
    slug: 'rich-deliverable-blocks-data',
    title: 'Rich Deliverable Data Blocks',
    description: 'Reference data, evidence, media, and utility blocks with generated examples.',
    category: 'workspace',
    sourcePath: 'docs/guide/rich-deliverable-blocks-data.md',
    relatedSlugs: ['rich-deliverable-composition', 'rich-deliverable-blocks-layout']
  },
  {
    slug: 'channels',
    title: 'Channels',
    description: 'Connect agents to external platforms and understand pairing and trust.',
    category: 'operations',
    sourcePath: 'docs/guide/channels.md',
    relatedSlugs: ['executors', 'security-and-privacy', 'troubleshooting']
  },
  {
    slug: 'executors',
    title: 'Executors',
    description: 'Choose where tools run and how remote executor placement affects agents.',
    category: 'operations',
    sourcePath: 'docs/guide/executors.md',
    relatedSlugs: ['deployment', 'channels', 'configuring-providers', 'creating-agents']
  },
  {
    slug: 'deployment',
    title: 'Deployment',
    description: 'Run Cognis with Docker or systemd, remote executors, TLS, backups, and hardening.',
    category: 'operations',
    sourcePath: 'docs/guide/deployment.md',
    relatedSlugs: ['high-availability', 'executors', 'security-and-privacy', 'troubleshooting']
  },
  {
    slug: 'high-availability',
    title: 'High availability',
    description: 'Deploy and operate multiple Cognis controllers with durable failover.',
    category: 'operations',
    sourcePath: 'docs/guide/high-availability.md',
    relatedSlugs: ['deployment', 'ha-e2e', 'executors', 'architecture']
  },
  {
    slug: 'ha-e2e',
    title: 'HA E2E Compose',
    description: 'Qualify a two-controller Cognis deployment with the Docker Compose HA stack.',
    category: 'operations',
    sourcePath: 'docs/guide/ha-e2e.md',
    relatedSlugs: ['high-availability', 'deployment', 'troubleshooting']
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
  'local-compose',
  'architecture',
  'configuring-providers',
  'creating-agents',
  'security-and-privacy',
  'settings',
  'using-chat',
  'projects',
  'managing-tasks',
  'schedules',
  'workflows',
  'channels',
  'executors',
  'deployment',
  'high-availability',
  'ha-e2e',
  'tools-and-skills',
  'content-and-sharing',
  'rich-deliverables',
  'rich-deliverable-composition',
  'rich-deliverable-blocks-layout',
  'rich-deliverable-blocks-data',
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
