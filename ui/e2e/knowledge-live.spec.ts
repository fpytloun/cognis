/**
 * Real vertical Knowledge smoke test.
 *
 * Unlike knowledge.spec.ts, this test does not mock Knowledge API calls. It
 * exercises the built UI against Cognis, SQLite, the index worker, Qdrant, and
 * the deterministic mock embedding endpoint.
 */
import { expect, test } from '@playwright/test';

import { login } from './helpers';

test.describe('Knowledge live stack', () => {
  test.skip(
    process.env.COGNIS_KNOWLEDGE_LIVE_E2E !== '1',
    'Set COGNIS_KNOWLEDGE_LIVE_E2E=1 and seed the deterministic e2e stack.'
  );

  test('creates, ingests, indexes, browses, and searches a real document', async ({ page }) => {
    await login(page);

    const knowledgebaseId = await page.evaluate(async () => {
      const createResponse = await fetch('/api/v1/knowledgebases/', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: `Live E2E ${Date.now()}`,
          description: 'Real browser-to-Qdrant Knowledge smoke test'
        })
      });
      if (!createResponse.ok) throw new Error(await createResponse.text());
      const knowledgebase = await createResponse.json();

      const form = new FormData();
      form.append(
        'files[]',
        new File(
          ['# Live Knowledge\n\nThe uniquely searchable phrase is forge-lantern-739.'],
          'live-guide.md',
          { type: 'text/markdown' }
        )
      );
      form.append('paths[]', 'guides/live-guide.md');
      form.set('conflict_policy', 'replace');
      const uploadResponse = await fetch(
        `/api/v1/knowledgebases/${knowledgebase.knowledgebase_id}/documents`,
        { method: 'POST', credentials: 'include', body: form }
      );
      if (!uploadResponse.ok) throw new Error(await uploadResponse.text());
      return knowledgebase.knowledgebase_id as string;
    });

    try {
      await expect
        .poll(
          () =>
            page.evaluate(async (id) => {
              const response = await fetch(
                `/api/v1/knowledgebases/${id}/documents?limit=50`,
                { credentials: 'include' }
              );
              if (!response.ok) return `http-${response.status}`;
              const payload = await response.json();
              return payload.documents?.[0]?.status ?? 'missing';
            }, knowledgebaseId),
          { timeout: 60_000 }
        )
        .toBe('indexed');

      await page.goto(`/knowledge/${knowledgebaseId}?tab=browse`);
      await page.getByTestId('knowledge-tree-file').first().click();
      await expect(
        page.locator('[data-testid="knowledge-document-reader"]:visible')
      ).toContainText('forge-lantern-739');

      await page.getByTestId('knowledge-tab-search').click();
      await page.getByTestId('knowledge-search-query-input').fill('forge-lantern-739');
      await page.getByTestId('knowledge-search-submit').click();
      await expect(page.getByTestId('knowledge-raw-result')).toContainText('forge-lantern-739');
    } finally {
      await page.evaluate(async (id) => {
        await fetch(`/api/v1/knowledgebases/${id}`, {
          method: 'DELETE',
          credentials: 'include'
        });
      }, knowledgebaseId);
    }
  });
});
