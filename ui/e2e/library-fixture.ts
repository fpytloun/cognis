import type { Page } from '@playwright/test';
import type { LibraryItem } from '../src/lib/library';
import { installTaskCockpitFixture } from './task-cockpit-fixture';

const OWNER = 'owner@cognis.test';
function document(id: string, title: string, project: string | null, conversation: string | null, revision = 1): LibraryItem {
  return {
    id, title, kind: 'deliverable', format: 'markdown', owner_email: OWNER,
    agent_id: 'agent-stage39', agent_name: 'LaForge',
    project_id: project, project_name: project ? ({ cognis: 'Cognis', home: 'House renovation', museum: 'Museum demo' }[project] ?? project) : null,
    conversation_id: conversation, conversation_title: conversation,
    task_id: null, task_title: null, lineage_id: id, revision,
    created_at: '2026-10-13T14:30:00Z', updated_at: '2026-10-17T06:20:00Z',
    size_bytes: 4200, status: 'delivered', origin: 'agent', is_readonly: false,
  };
}
export const libraryCatalog: LibraryItem[] = [
  document('dlv_release_review', 'Release readiness review — October', 'cognis', 'Release planning', 3),
  document('dlv_library_design', 'Library: a home for finished work', 'cognis', 'Product design', 2),
  document('dlv_access_review', 'Content access and sharing security review', 'cognis', 'Release planning'),
  { ...document('dlv_station_health', 'Exhibit health · Weekly demonstration report', 'museum', 'Exhibit reliability'), agent_name: 'Riker' },
  { ...document('dlv_renovation', 'Kitchen renovation — materials and budget', 'home', 'Kitchen renovation'), agent_name: 'Riker' },
  document('dlv_research', 'Research notes: local inference options', null, null),
  { ...document('dlv_shared_report', 'Exhibit maintenance — September summary', 'museum', 'Demonstration review'), owner_email: 'colleague@cognis.test', is_readonly: true },
  { ...document('att_floor_plan', 'Ground floor — architectural plan.pdf', 'home', 'Kitchen renovation'), kind: 'artifact', format: 'application/pdf', origin: 'user', agent_name: null, size_bytes: 284000 },
  { ...document('att_requirements', 'Library requirements and acceptance notes.txt', 'cognis', 'Product design'), kind: 'artifact', format: 'text/plain', origin: 'user', agent_name: null, size_bytes: 1800 },
  { ...document('art_budget', 'Kitchen materials — cost breakdown.csv', 'home', 'Kitchen renovation'), kind: 'artifact', format: 'text/csv', size_bytes: 8200 },
  { ...document('art_report', 'Release validation results.json', 'cognis', 'Release planning'), kind: 'artifact', format: 'application/json', size_bytes: 12300 },
];

export async function installLibraryRoutes(page: Page, catalog = libraryCatalog) {
  const favorites = new Set<string>();
  const key = (item: LibraryItem) => `${item.kind}:${item.lineage_id || item.id}`;
  const metadata = (item: LibraryItem) => ({ ...item, is_favorite: favorites.has(key(item)) });
  await page.route('**/api/v1/library/**', async (route) => {
    const url = new URL(route.request().url());
    const params = url.searchParams;
    const content = url.pathname.match(/\/deliverables\/([^/]+)\/content$/);
    const exact = url.pathname.match(/\/items\/(deliverable|artifact)\/([^/]+)$/);
    const favorite = url.pathname.match(/\/items\/(deliverable|artifact)\/([^/]+)\/favorite$/);
    if (favorite) {
      const item = catalog.find(row => row.kind === favorite[1] && row.id === favorite[2]);
      if (!item) return route.fulfill({ status: 404, json: { detail: 'Item unavailable' } });
      if (route.request().postDataJSON().favorite) favorites.add(key(item));
      else favorites.delete(key(item));
      return route.fulfill({ json: metadata(item) });
    }
    if (url.pathname.endsWith('/sharing')) {
      const id = url.pathname.split('/').at(-2);
      const item = catalog.find(row => row.id === id);
      return route.fulfill({ json: {
        owner_agent_id: item?.agent_id ?? null, project_id: item?.project_id ?? null,
        agents: item?.agent_id ? [{ agent_id: item.agent_id, name: item.agent_name || 'Agent', inherited: true, explicit: false }] : [],
      } });
    }
    if (content) {
      const row = catalog.find((item) => item.id === content[1]);
      return route.fulfill({ json: { deliverable_id: row?.id, title: row?.title, format: 'markdown', status: 'delivered',
        lineage_id: row?.lineage_id, revision: row?.revision, version: 1,
        latest_deliverable_id: row?.id, latest_revision: row?.revision, revision_count: row?.revision, is_latest: true,
        content: '# Release readiness\n\nThe October release is ready for final acceptance. The review covers content ownership, revision history, and shared project access.\n\n## Validation\n\n- Content access checks passed.\n- Revision history is linear and stable.\n- Shared documents retain their privacy boundaries.\n\n## Next steps\n\nComplete human evaluation of the Library on desktop and mobile before release.' } });
    }
    if (exact) {
      const row = catalog.find((item) => item.id === exact[2]);
      return row ? route.fulfill({ json: metadata(row) }) : route.fulfill({ status: 404, json: { detail: 'Item unavailable' } });
    }
    let items = catalog.filter((item) => item.kind === (params.get('kind') ?? 'deliverable'));
    if (params.get('favorites') === 'true') items = items.filter(item => favorites.has(key(item)));
    if (params.has('source_id')) items = items.filter((item) => (item.source_id ?? (item.task_id ? `task:${item.task_id}` : item.conversation_id) ?? '') === params.get('source_id'));
    for (const field of ['project_id', 'conversation_id', 'agent_id', 'format'] as const) {
      if (params.has(field)) items = items.filter((item) => (item[field] ?? '') === params.get(field));
    }
    if (params.get('origin') && params.get('origin') !== 'all') items = items.filter((item) => item.origin === params.get('origin'));
    if (params.get('ownership') === 'mine') items = items.filter((item) => item.owner_email === OWNER);
    if (params.get('ownership') === 'shared') items = items.filter((item) => item.owner_email !== OWNER);
    if (params.get('q')) items = items.filter((item) => item.title?.toLowerCase().includes(params.get('q')!.toLowerCase()));
    if (url.pathname.endsWith('/groups')) {
      const by = params.get('group_by') ?? 'project';
      const groups = new Map<string, { id: string; label: string; count: number }>();
      for (const item of items) {
        const id = by === 'conversation' ? item.source_id ?? (item.task_id ? `task:${item.task_id}` : item.conversation_id) ?? '' : by === 'format' ? item.format : item[`${by}_id` as 'project_id' | 'agent_id'] ?? '';
        const label = by === 'format' ? item.format : by === 'project' ? item.project_name : by === 'agent' ? item.agent_name : item.source_title ?? (item.task_title ? `Task: ${item.task_title}` : item.conversation_title);
        const group = groups.get(id) ?? { id, label: label ?? `No ${by}`, count: 0 };
        group.count++;
        groups.set(id, group);
      }
      return route.fulfill({ json: { groups: [...groups.values()], next_cursor: null } });
    }
    const offset = Number(params.get('cursor') ?? 0);
    const limit = 4;
    return route.fulfill({ json: { items: items.slice(offset, offset + limit).map(metadata), next_cursor: items.length > offset + limit ? String(offset + limit) : null } });
  });
  await page.route('**/api/v1/deliverables/*/revisions', (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2);
    const row = catalog.find((item) => item.id === id);
    return route.fulfill({ json: { lineage_id: id, latest_deliverable_id: id, revisions: [{ deliverable_id: id, title: row?.title, revision: row?.revision ?? 1, format: 'markdown', status: 'delivered', is_latest: true }] } });
  });
  await page.route('**/api/v1/artifacts/*/signed-url**', (route) => route.fulfill({ json: { url: 'data:text/plain,Library%20requirements%20and%20acceptance%20notes', expires_at: null } }));
  await page.route('**/api/v1/artifacts/*/revisions', (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2);
    return route.fulfill({ json: { lineage_id: id, latest_artifact_id: id, latest_revision: 1, revisions: [{ artifact_id: id, revision: 1, is_latest: true }] } });
  });
  await page.route('**/api/v1/artifacts/*/text-preview', (route) => route.fulfill({ json: { content: 'Library requirements\n\nKeep finished work easy to find.\nDistinguish user uploads from agent outputs.\nPreserve revision history and source permissions.', truncated: false } }));
}

export async function installLibraryFixture(page: Page) {
  await installTaskCockpitFixture(page);
  await page.route('**/api/auth/me', (route) => route.fulfill({ json: { email: OWNER, name: 'Filip', role: 'user' } }));
  await page.routeWebSocket('**/api/ws', (socket) => {
    socket.send(JSON.stringify({ type: 'authenticated' }));
    socket.onMessage((message) => { if (JSON.parse(String(message)).type === 'ping') socket.send(JSON.stringify({ type: 'pong' })); });
  });
  await installLibraryRoutes(page);
}
