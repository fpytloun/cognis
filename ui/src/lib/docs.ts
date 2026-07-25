/**
 * Docs content loader.
 *
 * Metadata lives in `docs-registry.ts`; markdown content is loaded with
 * per-document dynamic imports so docs content does not become one large
 * inlined JavaScript chunk.
 */

import {
  type DocCategory,
  type DocMeta,
  DOC_CATEGORIES,
  embeddedDocsMeta,
  getCategoryLabel,
  getDocHref,
  getDocMeta,
  getDocsByCategoryMeta,
  getOnboardingDocsMeta,
  getRelatedDocsMeta
} from './docs-registry';

type RawMarkdownLoader = () => Promise<string>;

const markdownModules = import.meta.glob('../../../docs/guide/*.md', {
  query: '?raw',
  import: 'default'
}) as Record<string, RawMarkdownLoader>;

const overviewModules = import.meta.glob('../../../docs/README.md', {
  query: '?raw',
  import: 'default'
}) as Record<string, RawMarkdownLoader>;

const assetUrlModules = import.meta.glob('../../../docs/assets/**/*.{svg,png,jpg,jpeg}', {
  query: '?url',
  import: 'default',
  eager: true
}) as Record<string, string>;

export {
  DOC_CATEGORIES,
  type DocCategory,
  getCategoryLabel,
  getDocHref
} from './docs-registry';

export interface EmbeddedDoc extends DocMeta {
  content: string;
  rawContent?: string;
}

export interface DocsOverview {
  title: string;
  sourcePath: string;
  content: string;
  rawContent?: string;
}

const DOC_ROUTE_RE = /^\/docs\/([a-z0-9-]+)$/;
const ALLOWED_APP_ROUTE_RE = /^\/(docs(\/[a-z0-9-]+)?|settings(\?.*)?|agents(\/.*)?|chat(\/.*)?|projects(\/.*)?|tasks(\/.*)?|schedules(\/.*)?|workflows(\/.*)?|tools(\/.*)?|channels(\/.*)?)$/;
const MARKDOWN_LINK_RE = /!??\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;

const EMBEDDED_DOC_ROUTE_BY_SOURCE_PATH: Record<string, string> = {
  'docs/README.md': '/docs',
  'docs/guide/getting-started.md': '/docs/getting-started',
  'docs/guide/local-compose.md': '/docs/local-compose',
  'docs/guide/architecture.md': '/docs/architecture',
  'docs/guide/configuring-providers.md': '/docs/configuring-providers',
  'docs/guide/creating-agents.md': '/docs/creating-agents',
  'docs/guide/security-and-privacy.md': '/docs/security-and-privacy',
  'docs/guide/settings.md': '/docs/settings',
  'docs/guide/using-chat.md': '/docs/using-chat',
  'docs/guide/projects.md': '/docs/projects',
  'docs/guide/managing-tasks.md': '/docs/managing-tasks',
  'docs/guide/schedules.md': '/docs/schedules',
  'docs/guide/workflows.md': '/docs/workflows',
  'docs/guide/tools-and-skills.md': '/docs/tools-and-skills',
  'docs/guide/content-and-sharing.md': '/docs/content-and-sharing',
  'docs/guide/rich-deliverables.md': '/docs/rich-deliverables',
  'docs/guide/rich-deliverable-composition.md': '/docs/rich-deliverable-composition',
  'docs/guide/rich-deliverable-blocks-layout.md': '/docs/rich-deliverable-blocks-layout',
  'docs/guide/rich-deliverable-blocks-data.md': '/docs/rich-deliverable-blocks-data',
  'docs/guide/channels.md': '/docs/channels',
  'docs/guide/executors.md': '/docs/executors',
  'docs/guide/deployment.md': '/docs/deployment',
  'docs/guide/troubleshooting.md': '/docs/troubleshooting'
};

const GITHUB_REPO_URL = 'https://github.com/fpytloun/cognis';
const DOC_REPO_URLS = new Set(['compose.local.yml', 'deploy/systemd', 'deploy/systemd/README.md']);

function normalizeDocPath(path: string): string {
  const parts = path.split('/');
  const normalized: string[] = [];
  for (const part of parts) {
    if (!part || part === '.') continue;
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

function getGitHubRepoUrl(path: string): string {
  const normalized = path.replace(/\/$/, '');
  const isDir = path.endsWith('/') || !normalized.includes('.');
  const segment = isDir ? 'tree' : 'blob';
  return `${GITHUB_REPO_URL}/${segment}/main/${normalized}`;
}

function lookupAssetUrl(repoPath: string): string | null {
  for (const [globKey, url] of Object.entries(assetUrlModules)) {
    const rel = globKey.replace(/^(\.\.\/)+/, '');
    if (rel === repoPath) return url;
  }
  return null;
}

async function getMarkdownForSourcePath(sourcePath: string): Promise<string> {
  if (sourcePath === 'docs/README.md') {
    const key = Object.keys(overviewModules)[0];
    return key ? await overviewModules[key]() : '';
  }
  for (const [globKey, load] of Object.entries(markdownModules)) {
    const rel = globKey.replace(/^(\.\.\/)+/, '');
    if (rel === sourcePath) return load();
  }
  return '';
}

function resolveRelativeMarkdownTarget(sourcePath: string, rawTarget: string): string | null {
  if (
    rawTarget.startsWith('#') ||
    rawTarget.startsWith('/') ||
    rawTarget.startsWith('http://') ||
    rawTarget.startsWith('https://') ||
    rawTarget.startsWith('mailto:') ||
    rawTarget.startsWith('data:')
  ) {
    return null;
  }
  const resolvedTarget = resolveDocRelativePath(sourcePath, rawTarget);
  const bundledAsset = lookupAssetUrl(resolvedTarget);
  if (bundledAsset) return bundledAsset;
  const embeddedDocRoute = EMBEDDED_DOC_ROUTE_BY_SOURCE_PATH[resolvedTarget];
  if (embeddedDocRoute) return embeddedDocRoute;
  const normalized = resolvedTarget.replace(/\/$/, '');
  if (normalized.startsWith('docs/specs/') || DOC_REPO_URLS.has(normalized)) {
    return getGitHubRepoUrl(resolvedTarget);
  }
  return null;
}

function rewriteMarkdownTargets(sourcePath: string, markdown: string): string {
  return markdown.replace(MARKDOWN_LINK_RE, (fullMatch, rawTarget: string) => {
    const rewrittenTarget = resolveRelativeMarkdownTarget(sourcePath, rawTarget);
    if (!rewrittenTarget) return fullMatch;
    return fullMatch.replace(rawTarget, rewrittenTarget);
  });
}

export async function loadDocsOverviewContent(): Promise<DocsOverview> {
  const sourcePath = 'docs/README.md';
  const raw = await getMarkdownForSourcePath(sourcePath);
  return {
    title: 'Documentation',
    sourcePath,
    rawContent: raw,
    content: rewriteMarkdownTargets(sourcePath, raw)
  };
}

export async function loadEmbeddedDoc(slug: string): Promise<EmbeddedDoc | null> {
  const meta = getDocMeta(slug);
  if (!meta) return null;
  const raw = await getMarkdownForSourcePath(meta.sourcePath);
  return {
    ...meta,
    rawContent: raw,
    content: rewriteMarkdownTargets(meta.sourcePath, raw)
  };
}

export async function loadEmbeddedDocs(): Promise<EmbeddedDoc[]> {
  const docs = await Promise.all(embeddedDocsMeta.map((meta) => loadEmbeddedDoc(meta.slug)));
  return docs.filter((doc): doc is EmbeddedDoc => doc !== null);
}

export async function getEmbeddedDoc(slug: string): Promise<EmbeddedDoc | null> {
  return loadEmbeddedDoc(slug);
}

export async function getEmbeddedDocs(): Promise<EmbeddedDoc[]> {
  return loadEmbeddedDocs();
}

export async function getOnboardingDocs(): Promise<EmbeddedDoc[]> {
  const docs = await Promise.all(getOnboardingDocsMeta().map((meta) => loadEmbeddedDoc(meta.slug)));
  return docs.filter((d): d is EmbeddedDoc => d !== null);
}

export async function getRelatedDocs(doc: EmbeddedDoc | DocMeta): Promise<EmbeddedDoc[]> {
  const docs = await Promise.all(getRelatedDocsMeta(doc).map((meta) => loadEmbeddedDoc(meta.slug)));
  return docs.filter((d): d is EmbeddedDoc => d !== null);
}

export function getDocsByCategory(): Array<{ category: DocCategory; docs: DocMeta[] }> {
  return getDocsByCategoryMeta();
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
  const allowedSlugs = new Set(embeddedDocsMeta.map((doc) => doc.slug));

  for (const doc of embeddedDocsMeta) {
    if (slugSet.has(doc.slug)) errors.push(`Duplicate doc slug: ${doc.slug}`);
    slugSet.add(doc.slug);

    if (sourcePathSet.has(doc.sourcePath)) errors.push(`Duplicate doc source path: ${doc.sourcePath}`);
    sourcePathSet.add(doc.sourcePath);

    if (!DOC_CATEGORIES.includes(doc.category)) {
      errors.push(`Invalid doc category for ${doc.slug}: ${doc.category}`);
    }
    if (doc.sourcePath.includes('docs/specs/')) {
      errors.push(`Forbidden docs/specs source path: ${doc.sourcePath}`);
    }
    for (const relatedSlug of doc.relatedSlugs ?? []) {
      if (!allowedSlugs.has(relatedSlug)) {
        errors.push(`Doc ${doc.slug} references missing related slug: ${relatedSlug}`);
      }
    }
  }

  return errors;
}

export async function validateEmbeddedDocsContent(): Promise<string[]> {
  const errors = validateEmbeddedDocs();
  const overview = await loadDocsOverviewContent();
  const docs = await loadEmbeddedDocs();
  const allowedSlugs = new Set(docs.map((doc) => doc.slug));
  const allMarkdownSources: Array<{ title: string; sourcePath: string; content: string }> = [
    { title: overview.title, sourcePath: overview.sourcePath, content: overview.rawContent ?? overview.content },
    ...docs.map((doc) => ({ title: doc.title, sourcePath: doc.sourcePath, content: doc.rawContent ?? doc.content }))
  ];

  for (const doc of docs) {
    if (!doc.content.trim()) {
      errors.push(`Doc content is empty: ${doc.slug}`);
    }
    const markdownTitle = extractMarkdownTitle(doc.content);
    if (markdownTitle !== doc.title) {
      errors.push(`Doc title mismatch for ${doc.slug}: expected "${doc.title}" but found "${markdownTitle ?? 'missing'}"`);
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
      if (target.startsWith('http://') || target.startsWith('https://')) continue;
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
      const rewrittenRelativeTarget = resolveRelativeMarkdownTarget(doc.sourcePath, target);
      if (rewrittenRelativeTarget) continue;
      errors.push(`Relative links or assets are not allowed in ${doc.sourcePath}: ${target}`);
    }
  }

  return errors;
}

const registryErrors = validateEmbeddedDocs();
if (registryErrors.length > 0) {
  throw new Error(`Embedded docs validation failed:\n${registryErrors.join('\n')}`);
}
