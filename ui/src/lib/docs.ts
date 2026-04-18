/**
 * Docs content loader (lazy).
 *
 * This module re-exports metadata-only helpers from `docs-registry` and adds
 * lazy loaders for the actual markdown. The heavy eager imports of
 * 13 `?raw` markdown files live exclusively in this file and are loaded
 * on demand via `import.meta.glob(..., { eager: true })` so that callers
 * who only need metadata (e.g. `/getting-started` page) can import from
 * `docs-registry` and avoid pulling the docs bundle.
 *
 * Legacy shape `embeddedDocs: EmbeddedDoc[]` is preserved for existing tests.
 */

import {
  type DocCategory,
  type DocMeta,
  DOC_CATEGORIES,
  ONBOARDING_DOC_SLUGS,
  embeddedDocsMeta,
  getCategoryLabel,
  getDocHref,
  getDocMeta,
  getDocsByCategoryMeta,
  getOnboardingDocsMeta,
  getRelatedDocsMeta
} from './docs-registry';

// Eager glob of the guide markdown. Vite will ship these as one shared chunk
// that is loaded by the `/docs` routes only. Pages that merely need DocMeta
// can import from `docs-registry.ts` and avoid this module.
const markdownModules = import.meta.glob('../../../docs/guide/*.md', {
  query: '?raw',
  import: 'default',
  eager: true
}) as Record<string, string>;

const overviewModules = import.meta.glob('../../../docs/README.md', {
  query: '?raw',
  import: 'default',
  eager: true
}) as Record<string, string>;

// SVG asset urls (diagrams referenced from guides).
const assetUrlModules = import.meta.glob('../../../docs/assets/images/*.svg', {
  query: '?url',
  import: 'default',
  eager: true
}) as Record<string, string>;

// Re-exports from the registry for convenience.
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
const ALLOWED_APP_ROUTE_RE = /^\/(docs(\/[a-z0-9-]+)?|settings(\?.*)?|agents(\/.*)?|chat(\/.*)?|tasks(\/.*)?|workflows(\/.*)?|tools(\/.*)?|channels(\/.*)?)$/;
const MARKDOWN_LINK_RE = /!??\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;

const EMBEDDED_DOC_ROUTE_BY_SOURCE_PATH: Record<string, string> = {
  'docs/README.md': '/docs',
  'docs/guide/getting-started.md': '/docs/getting-started',
  'docs/guide/architecture.md': '/docs/architecture',
  'docs/guide/configuring-providers.md': '/docs/configuring-providers',
  'docs/guide/creating-agents.md': '/docs/creating-agents',
  'docs/guide/settings.md': '/docs/settings',
  'docs/guide/using-chat.md': '/docs/using-chat',
  'docs/guide/managing-tasks.md': '/docs/managing-tasks',
  'docs/guide/workflows.md': '/docs/workflows',
  'docs/guide/tools-and-skills.md': '/docs/tools-and-skills',
  'docs/guide/channels.md': '/docs/channels',
  'docs/guide/executors.md': '/docs/executors',
  'docs/guide/troubleshooting.md': '/docs/troubleshooting'
};

const GITHUB_REPO_URL = 'https://github.com/fpytloun/cognis';
const DOC_REPO_URLS = new Set(['deploy/systemd', 'deploy/systemd/README.md']);

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
  // Glob keys look like "../../../docs/assets/images/xyz.svg"; map to repo-relative paths.
  for (const [globKey, url] of Object.entries(assetUrlModules)) {
    const rel = globKey.replace(/^(\.\.\/)+/, '');
    if (rel === repoPath) return url;
  }
  return null;
}

function getMarkdownForSourcePath(sourcePath: string): string {
  if (sourcePath === 'docs/README.md') {
    const key = Object.keys(overviewModules)[0];
    return overviewModules[key] ?? '';
  }
  for (const [globKey, content] of Object.entries(markdownModules)) {
    const rel = globKey.replace(/^(\.\.\/)+/, '');
    if (rel === sourcePath) return content;
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

export const docsOverview: DocsOverview = (() => {
  const sourcePath = 'docs/README.md';
  const raw = getMarkdownForSourcePath(sourcePath);
  return {
    title: 'Documentation',
    sourcePath,
    rawContent: raw,
    content: rewriteMarkdownTargets(sourcePath, raw)
  };
})();

export const embeddedDocs: EmbeddedDoc[] = embeddedDocsMeta.map((meta) => {
  const raw = getMarkdownForSourcePath(meta.sourcePath);
  return {
    ...meta,
    rawContent: raw,
    content: rewriteMarkdownTargets(meta.sourcePath, raw)
  };
});

export function getEmbeddedDocs(): EmbeddedDoc[] {
  return embeddedDocs;
}

export function getEmbeddedDoc(slug: string): EmbeddedDoc | null {
  return embeddedDocs.find((doc) => doc.slug === slug) ?? null;
}

export function getOnboardingDocs(): EmbeddedDoc[] {
  return getOnboardingDocsMeta()
    .map((meta) => getEmbeddedDoc(meta.slug))
    .filter((d): d is EmbeddedDoc => d !== null);
}

export function getRelatedDocs(doc: EmbeddedDoc | DocMeta): EmbeddedDoc[] {
  return getRelatedDocsMeta(doc)
    .map((meta) => getEmbeddedDoc(meta.slug))
    .filter((d): d is EmbeddedDoc => d !== null);
}

export function getDocsByCategory(): Array<{ category: DocCategory; docs: EmbeddedDoc[] }> {
  return getDocsByCategoryMeta().map(({ category, docs }) => ({
    category,
    docs: docs.map((meta) => getEmbeddedDoc(meta.slug)).filter((d): d is EmbeddedDoc => d !== null)
  }));
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
