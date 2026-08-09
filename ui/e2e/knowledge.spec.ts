/**
 * L3 browser e2e for the Knowledge feature: list -> create -> add a folder
 * -> browse -> search -> ask, plus disabled-capability, failure/retry, a
 * mobile viewport pass, and citation navigation.
 *
 * Requires the e2e compose stack, like the other specs in this directory:
 *   make e2e-up && make e2e-seed
 *   npx playwright test e2e/knowledge.spec.ts
 *
 * All knowledgebase backend calls are mocked via page.route so the spec is
 * independent of external retrieval services; fixtures mirror the serialized
 * backend product contract.
 */
import { expect, test, type Page } from '@playwright/test';
import path from 'node:path';
import process from 'node:process';
import { mkdir } from 'node:fs/promises';
import { login } from './helpers';

const KB: Record<string, unknown> = {
  knowledgebase_id: 'kb_e2e_1',
  owner_email: 'owner@example.com',
  access_level: 'owner',
  name: 'Product docs',
  description: 'Everything about the product',
  status: 'active',
  metadata_schema: { fields: { category: { type: 'keyword', filterable: true } } },
  settings: {},
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  archived_at: null
};

const SHARED_KB = {
  ...KB,
  knowledgebase_id: 'kb_shared',
  name: 'Shared research',
  owner_email: 'alice@example.com',
  access_level: 'shared'
};

async function reviewScreenshot(page: Page, name: string): Promise<void> {
  const directory = process.env.KNOWLEDGE_REVIEW_SCREENSHOTS_DIR;
  if (!directory) return;
  await expect(
    page.getByText(/Loading knowledgebase|Loading knowledgebases|Loading document/)
  ).toHaveCount(0);
  await expect(page.locator('[role="alert"]')).toHaveCount(0);
  await mkdir(directory, { recursive: true });
  await page.screenshot({ path: path.join(directory, `${name}.png`), fullPage: true });
}

const HEALTH = { enabled: true, vector_backend: 'qdrant', embedding_route_configured: true, healthy: true, notes: [] };
const CAPABILITIES = {
  enabled: true, vector_backend: 'qdrant', backend_ready: true, embedding_ready: true,
  indexer_ready: true, ask_ready: true, supported_mime_types: ['text/markdown'],
  supported_extensions: ['.md'], limits: {
    max_upload_bytes: 52_428_800, max_batch_files: 100, max_batch_upload_bytes: 104_857_600
  }, notes: []
};

function artifact(overrides: Record<string, unknown> = {}) {
  return {
    kb_artifact_id: 'kba_1',
    knowledgebase_id: 'kb_e2e_1',
    source_path: 'guides/intro.md',
    artifact_id: 'art_1',
    pending_artifact_id: null,
    pending_source_hash: null,
    active_generation: 1,
    desired_generation: 1,
    status: 'indexed',
    source_hash: 'h1',
    source_filename: 'intro.md',
    source_mime_type: 'text/markdown',
    source_size_bytes: 100,
    metadata: { category: 'guide', count: 0 },
    chunk_count: 3,
    last_job_id: null,
    last_error: null,
    last_diagnostics: {},
    attached_at: '2026-01-01T00:00:00Z',
    indexed_at: '2026-01-01T00:00:00Z',
    stale_at: null,
    removed_at: null,
    ...overrides
  };
}

async function mockKnowledgeApi(
  page: Page,
  options: { healthy?: boolean; searchFails?: boolean } = {}
): Promise<{ sharedManagementCalls: string[] }> {
  const healthy = options.healthy ?? true;
  let remainingSearchFailures = options.searchFails ? 1 : 0;
  const sharedManagementCalls: string[] = [];

  await page.route('**/api/v1/knowledgebases/capabilities', (route) =>
    route.fulfill({ json: healthy ? CAPABILITIES : {
      ...CAPABILITIES, backend_ready: false, indexer_ready: false, ask_ready: false,
      notes: ['Vector backend unreachable']
    } })
  );
  await page.route('**/api/v1/knowledgebases/', (route) => {
    if (route.request().method() === 'POST') {
      route.fulfill({ json: { ...KB, knowledgebase_id: 'kb_e2e_new', name: 'New KB' } });
    } else {
      route.fulfill({ json: [KB, SHARED_KB] });
    }
  });
  await page.route('**/api/v1/knowledgebases/kb_e2e_1', (route) => route.fulfill({ json: KB }));
  await page.route('**/api/v1/knowledgebases/kb_shared', (route) => route.fulfill({ json: SHARED_KB }));
  await page.route('**/api/v1/knowledgebases/kb_e2e_new', (route) =>
    route.fulfill({ json: { ...KB, knowledgebase_id: 'kb_e2e_new', name: 'New KB' } })
  );
  await page.route('**/api/v1/knowledgebases/kb_e2e_1/diagnostics', (route) =>
    route.fulfill({
      json: {
        enabled: true,
        artifact_counts: { indexed: 1 },
        job_counts: {},
        chunk_count: 3,
        backend_health: {}
      }
    })
  );
  await page.route('**/api/v1/knowledgebases/kb_e2e_1/artifacts', (route) => route.fulfill({ json: [artifact()] }));
  await page.route('**/api/v1/knowledgebases/kb_e2e_1/jobs', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/v1/knowledgebases/kb_e2e_1/agents', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/v1/agents**', (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } })
  );
  let shares: Record<string, unknown>[] = [];
  await page.route('**/api/v1/knowledgebases/kb_e2e_1/shares**', (route) => {
    if (route.request().url().includes('/shares/candidates')) {
      route.fulfill({ json: [{ email: 'reader@example.com', name: 'Reader' }] });
    } else if (route.request().method() === 'PUT') {
      const share = {
        grant_id: 'kbg_1', user_email: 'reader@example.com', user_name: 'Reader',
        permission: 'view', granted_at: '2026-01-01T00:00:00Z', note: null
      };
      shares = [share];
      route.fulfill({ json: share });
    } else if (route.request().method() === 'DELETE') {
      shares = [];
      route.fulfill({ json: { revoked: true } });
    } else route.fulfill({ json: shares });
  });
  await page.route('**/api/v1/knowledgebases/kb_e2e_1/documents**', (route) =>
    route.fulfill({ json: { documents: [artifact()], next_cursor: null } })
  );
  await page.route('**/api/v1/knowledgebases/kb_shared/documents**', (route) =>
    route.fulfill({ json: { documents: [artifact({ knowledgebase_id: 'kb_shared' })], next_cursor: null } })
  );
  await page.route('**/api/v1/knowledgebases/kb_e2e_1/documents/kba_1/content**', (route) =>
    route.fulfill({ json: {
      kb_artifact_id: 'kba_1', artifact_id: 'art_1', source_path: 'guides/intro.md',
      content_mode: 'extracted', mime_type: 'text/markdown',
      text: '# Introduction\n\n[Resource](/knowledge/resources/assets/info.txt)\n\n[Missing](/settings)',
      size_bytes: 14, extraction_method: 'markdown', diagnostics: {}
    } })
  );
  await page.route('**/api/v1/knowledgebases/kb_e2e_1/documents/kba_1/resources/knowledge/resources/assets/info.txt', (route) =>
    route.fulfill({ body: 'resource content', contentType: 'text/plain' })
  );
  await page.route('**/api/v1/knowledgebases/*/facets', (route) => route.fulfill({ json: {
    fields: [{
      field: 'category',
      type: 'string',
      values: [{ value: 'guide', count: 3 }],
      cardinality: 1,
      truncated: false
    }],
    documents_scanned: 3
  } }));
  await page.route('**/api/v1/knowledgebases/kb_shared/documents/kba_1/content**', (route) =>
    route.fulfill({ json: {
      kb_artifact_id: 'kba_1', artifact_id: 'art_1', source_path: 'guides/intro.md',
      content_mode: 'extracted', mime_type: 'text/markdown', text: '# Shared introduction',
      size_bytes: 21, extraction_method: 'markdown', diagnostics: {}
    } })
  );
  await page.route('**/api/v1/knowledgebases/kb_e2e_new/diagnostics', (route) =>
    route.fulfill({ json: { enabled: true, artifact_counts: {}, job_counts: {}, chunk_count: 0, backend_health: {} } })
  );
  await page.route('**/api/v1/knowledgebases/kb_e2e_new/jobs', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/v1/knowledgebases/kb_e2e_new/agents', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/v1/knowledgebases/kb_e2e_new/artifacts', (route) => route.fulfill({ json: [] }));
  await page.route('**/api/v1/knowledgebases/kb_e2e_new/documents**', (route) => {
    if (route.request().method() === 'POST') {
      route.fulfill({
        json: {
          outcomes: [{
            source_path: 'todo.md', filename: 'todo.md', status: 'created', artifact_id: 'art_uploaded',
            kb_artifact_id: 'kba_uploaded', job_id: 'job_uploaded', error_code: null, message: null
          }]
        }
      });
    } else {
      route.fulfill({ json: { documents: [], next_cursor: null } });
    }
  });
  await page.route('**/api/v1/artifacts/upload', (route) =>
    route.fulfill({ json: { artifact_id: 'art_uploaded', filename: 'todo.md', mime_type: 'text/markdown', size_bytes: 12 } })
  );

  await page.route('**/api/v1/knowledgebases/kb_e2e_1/search', (route) => {
    if (remainingSearchFailures > 0) {
      remainingSearchFailures -= 1;
      route.fulfill({ status: 500, json: { error: { message: 'Search backend unavailable' } } });
      return;
    }
    route.fulfill({
      json: {
        matches: [
          {
            chunk_id: 'chunk_1',
            kb_artifact_id: 'kba_1',
            artifact_id: 'art_1',
            snippet: 'The onboarding flow starts with account creation.',
            score: 0.87,
            score_breakdown: { bm25: 0.4, vector: 0.47 },
            metadata: { category: 'guide' },
            citation: {
              artifact_id: 'art_1',
              filename: 'intro.md',
              mime_type: 'text/markdown',
              locator: {
                artifact_id: 'art_1',
                artifact_hash: 'h1',
                chunk_id: 'chunk_1',
                chunk_index: 0,
                char_start: 0,
                char_end: 120,
                byte_start: null,
                byte_end: null,
                line_start: 1,
                line_end: 4,
                page_start: null,
                page_end: null,
                paragraph_start: null,
                paragraph_end: null,
                timestamp_start_ms: null,
                timestamp_end_ms: null,
                extraction_method: 'text'
              }
            }
          }
        ],
        diagnostics: {}
      }
    });
  });

  await page.route('**/api/v1/knowledgebases/kb_e2e_1/ask', (route) =>
    route.fulfill({
      json: {
        status: 'answered',
        answer: 'Onboarding starts with account creation. [1]',
        cited_chunk_ids: ['chunk_1'],
        matches: [
          {
            chunk_id: 'chunk_1',
            kb_artifact_id: 'kba_1',
            artifact_id: 'art_1',
            snippet: 'The onboarding flow starts with account creation.',
            score: 0.87,
            score_breakdown: {},
            metadata: {},
            citation: {
              artifact_id: 'art_1',
              filename: 'intro.md',
              mime_type: 'text/markdown',
              locator: {
                artifact_id: 'art_1',
                artifact_hash: 'h1',
                chunk_id: 'chunk_1',
                chunk_index: 0,
                char_start: 0,
                char_end: 120,
                byte_start: null,
                byte_end: null,
                line_start: 1,
                line_end: 4,
                page_start: null,
                page_end: null,
                paragraph_start: null,
                paragraph_end: null,
                timestamp_start_ms: null,
                timestamp_end_ms: null,
                extraction_method: 'text'
              }
            }
          }
        ],
        error: null
      }
    })
  );
  await page.route('**/api/v1/knowledgebases/kb_shared/search', (route) =>
    route.fulfill({ json: {
      matches: [{
        chunk_id: 'shared_chunk', kb_artifact_id: 'kba_1', artifact_id: 'art_1', snippet: 'Shared evidence.',
        score: 0.91, score_breakdown: {}, metadata: {},
        citation: {
          artifact_id: 'art_1', filename: 'intro.md', mime_type: 'text/markdown',
          locator: {
            artifact_id: 'art_1', artifact_hash: 'h1', chunk_id: 'shared_chunk',
            chunk_index: 0, char_start: 0, char_end: 16, byte_start: null, byte_end: null,
            line_start: 1, line_end: 1, page_start: null, page_end: null,
            paragraph_start: null, paragraph_end: null, timestamp_start_ms: null,
            timestamp_end_ms: null, extraction_method: 'text'
          }
        }
      }],
      diagnostics: {}
    } })
  );
  await page.route('**/api/v1/knowledgebases/kb_shared/ask', (route) =>
    route.fulfill({ json: {
      status: 'answered', answer: 'Shared answer. [1]', cited_chunk_ids: ['shared_chunk'],
      matches: [{
        chunk_id: 'shared_chunk', kb_artifact_id: 'kba_1', artifact_id: 'art_1', snippet: 'Shared evidence.',
        score: 0.91, score_breakdown: {}, metadata: {},
        citation: {
          artifact_id: 'art_1', filename: 'intro.md', mime_type: 'text/markdown',
          locator: {
            artifact_id: 'art_1', artifact_hash: 'h1', chunk_id: 'shared_chunk',
            chunk_index: 0, char_start: 0, char_end: 16, byte_start: null, byte_end: null,
            line_start: 1, line_end: 1, page_start: null, page_end: null,
            paragraph_start: null, paragraph_end: null, timestamp_start_ms: null,
            timestamp_end_ms: null, extraction_method: 'text'
          }
        }
      }],
      error: null
    } })
  );
  for (const endpoint of ['jobs', 'diagnostics', 'shares', 'agents']) {
    await page.route(`**/api/v1/knowledgebases/kb_shared/${endpoint}**`, (route) => {
      sharedManagementCalls.push(route.request().url());
      route.fulfill({ status: 500, json: { error: { message: 'Management endpoint leaked' } } });
    });
  }
  return { sharedManagementCalls };
}

test.describe('Knowledge', () => {
  test('list -> create -> add folder -> browse -> search -> ask', async ({ page }) => {
    await login(page);
    await mockKnowledgeApi(page);

    await page.goto('/knowledge');
    await expect(page.getByRole('heading', { name: 'Knowledge', exact: true })).toBeVisible();
    await expect(page.getByText('Product docs')).toBeVisible();
    await reviewScreenshot(page, 'knowledge-list-desktop');

    await page.getByTestId('knowledge-create-button').click();
    await page.getByTestId('kb-form-name').fill('New KB');
    await page.getByTestId('kb-form-submit').click();
    await page.waitForURL(/\/knowledge\/kb_e2e_new/);

    await page.getByTestId('knowledge-tab-documents').click();
    await page.getByTestId('knowledge-add-documents-button').click();
    await page
      .getByTestId('knowledge-upload-folder-input')
      .setInputFiles(path.resolve('e2e/fixtures/knowledge-folder'));
    await page.getByTestId('knowledge-upload-review-button').click();
    await page.getByTestId('knowledge-upload-start-button').click();
    await expect(page.getByText(/created/i)).toBeVisible();

    await page.goto('/knowledge/kb_e2e_1?tab=browse');
    await expect(page.getByTestId('knowledge-tree-folder')).toBeVisible();
    await page.getByTestId('knowledge-tree-file').first().click();
    await expect(page.locator('[data-testid="knowledge-document-reader"]:visible')).toBeVisible();
    await expect(page.locator('[data-testid="knowledge-document-metadata"]:visible')).toContainText('category');
    await expect(page.getByRole('link', { name: 'Resource' })).toHaveAttribute(
      'href',
      /\/resources\/knowledge\/resources\/assets\/info\.txt$/
    );
    await page.getByRole('link', { name: 'Missing' }).click();
    await expect(page.getByText('Resource unavailable')).toBeVisible();
    await reviewScreenshot(page, 'knowledge-browse-desktop');

    await page.getByTestId('knowledge-tab-search').click();
    await page.getByTestId('knowledge-search-query-input').fill('onboarding');
    await page.getByTestId('knowledge-search-submit').click();
    await expect(page.getByTestId('knowledge-raw-result')).toBeVisible();
    await expect(page.getByText('score 0.870')).toBeVisible();
    await page.getByRole('button', { name: 'Open' }).click();
    await expect(page).toHaveURL(/tab=browse.*document=kba_1/);
    await expect(page.locator('[data-testid="knowledge-document-reader"]:visible')).toBeVisible();
    await page.getByTestId('knowledge-tab-search').click();
    await page.getByTestId('knowledge-search-query-input').fill('onboarding');
    await page.getByTestId('knowledge-search-submit').click();
    await page.getByRole('button', { name: 'Open' }).click();
    await expect(page.locator('[data-testid="knowledge-document-reader"]:visible')).toBeVisible();
    await page.getByTestId('knowledge-tab-search').click();

    await page.getByRole('tab', { name: 'Ask' }).click();
    await page.getByTestId('knowledge-search-query-input').fill('How does onboarding work?');
    await page.getByTestId('knowledge-search-submit').click();
    await expect(page.getByTestId('knowledge-ask-answer-card')).toBeVisible();
    await expect(page.getByText(/Onboarding starts with account creation/)).toBeVisible();
    await reviewScreenshot(page, 'knowledge-ask-desktop');

    const citation = page.getByRole('button', { name: 'Citation 1: intro.md' });
    await citation.click();
    await expect(page.getByTestId('knowledge-raw-result')).toBeInViewport();
  });

  test('owner grants direct access and shared detail remains read-only', async ({ page }) => {
    await login(page);
    const { sharedManagementCalls } = await mockKnowledgeApi(page);
    await page.goto('/knowledge/kb_e2e_1?tab=access');
    await expect(page).toHaveURL(/tab=access/);
    await page.getByTestId('knowledge-share-search').fill('re');
    await expect(page.getByText('reader@example.com')).toBeVisible();
    await page.getByRole('button', { name: 'Grant access' }).click();
    await expect(page.getByText('reader@example.com · Read/query')).toBeVisible();
    await reviewScreenshot(page, 'knowledge-access-desktop');

    await page.goto('/knowledge/kb_shared?tab=settings');
    await expect(page).toHaveURL(/tab=browse/);
    await expect(page.getByTestId('knowledge-shared-banner')).toContainText('alice@example.com');
    await expect(page.getByTestId('knowledge-tab-browse')).toBeVisible();
    await expect(page.getByTestId('knowledge-tab-search')).toBeVisible();
    await expect(page.getByTestId('knowledge-tab-documents')).toBeVisible();
    await expect(page.getByTestId('knowledge-tab-access')).toHaveCount(0);
    await expect(page.getByTestId('knowledge-tab-settings')).toHaveCount(0);
    await page.getByTestId('knowledge-tab-documents').click();
    await expect(page.getByTestId('knowledge-add-documents-button')).toHaveCount(0);
    await page.getByTestId('knowledge-tab-browse').click();
    await page.getByTestId('knowledge-tree-file').first().click();
    await expect(page.locator('[data-testid="knowledge-document-reader"]:visible')).toContainText('Shared introduction');
    await reviewScreenshot(page, 'knowledge-shared-browse-desktop');
    await page.getByTestId('knowledge-tab-search').click();
    await page.getByTestId('knowledge-search-query-input').fill('shared');
    await page.getByTestId('knowledge-search-submit').click();
    await expect(page.getByText('Shared evidence.')).toBeVisible();
    await page.getByRole('tab', { name: 'Ask' }).click();
    await page.getByTestId('knowledge-search-query-input').fill('What is shared?');
    await page.getByTestId('knowledge-search-submit').click();
    await expect(page.getByText('Shared answer. [1]')).toBeVisible();
    expect(sharedManagementCalls).toEqual([]);
  });

  test('disabled capability shows a setup banner instead of crashing', async ({ page }) => {
    await login(page);
    await mockKnowledgeApi(page, { healthy: false });
    await page.goto('/knowledge');
    await expect(page.getByText(/remain available/i)).toBeVisible();
    await expect(page.getByText('Product docs')).toBeVisible();
  });

  test('search failure surfaces an error and retry succeeds', async ({ page }) => {
    await login(page);
    await mockKnowledgeApi(page, { searchFails: true });
    await page.goto('/knowledge/kb_e2e_1?tab=search');
    await page.getByTestId('knowledge-search-query-input').fill('onboarding');
    await page.getByTestId('knowledge-search-submit').click();
    await expect(page.getByRole('alert')).toContainText(/unavailable/i);
    await page.getByTestId('knowledge-search-submit').click();
    await expect(page.getByTestId('knowledge-raw-result')).toBeVisible();
  });

  test('mobile viewport renders the document reader as a bottom sheet', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);
    await mockKnowledgeApi(page);
    await page.goto('/knowledge/kb_e2e_1?tab=browse');
    await page.getByTestId('knowledge-tree-file').first().click();
    await expect(page.locator('[data-testid="knowledge-document-reader"]:visible')).toContainText('Introduction');
    await reviewScreenshot(page, 'knowledge-browse-mobile');
  });
});
