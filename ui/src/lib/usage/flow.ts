/**
 * Cost flow between attribution levels (for example model → agent → conversation).
 * Each link is the priced cost of the requests matching the whole path, read with
 * ancestor filters; the long tail of each level is folded into "Other".
 */
import type { UsageDataSource } from './api';
import { emptyGroupLabel, type UsageGroup, type UsageQuery } from './model';
import type * as W from './wire';

export type FlowLevel = 'provider' | 'model' | 'agent' | 'project' | 'conversation' | 'schedule';
export interface FlowNode { key: string; depth: number; id: string | null; label: string; value: number; other?: boolean }
export interface FlowLink { source: string; target: string; value: number }
export interface Flow { levels: FlowLevel[]; nodes: FlowNode[]; links: FlowLink[]; currency: string | null }

const FILTER: Record<FlowLevel, keyof W.Filters> = {
  provider: 'provider_ids', model: 'model_ids', agent: 'agent_ids', project: 'project_ids', conversation: 'conversation_ids', schedule: 'schedule_ids'
};
/** Nodes shown per parent, and parents expanded per level: bounded reads (at most 1 + 5 + 3 calls for three levels). */
const ROOTS = 5, CHILDREN = 4, EXPAND = [5, 3];

const amountOf = (group: UsageGroup) => {
  const cost = [...(group.costs ?? []), group.cost].find((item) => item.amount !== null && item.currency);
  const value = Number(cost?.amount ?? 0);
  return { value: Number.isFinite(value) ? value : 0, currency: cost?.currency ?? null };
};

export async function loadFlow(source: UsageDataSource, query: UsageQuery, levels: FlowLevel[], signal: AbortSignal): Promise<Flow> {
  if (!source.ranking || levels.length < 2) return { levels, nodes: [], links: [], currency: null };
  const ranking = source.ranking.bind(source);
  const nodes = new Map<string, FlowNode>();
  const links = new Map<string, FlowLink>();
  let currency: string | null = null;
  const add = (node: FlowNode) => {
    const current = nodes.get(node.key);
    if (current) current.value += node.value; else nodes.set(node.key, { ...node });
    return node.key;
  };
  const link = (sourceKey: string, target: string, value: number) => {
    if (value <= 0) return;
    const key = `${sourceKey}\u0001${target}`;
    const current = links.get(key);
    if (current) current.value += value; else links.set(key, { source: sourceKey, target, value });
  };
  /** Top groups plus an Other bucket holding the rest of the parent's cost. */
  function fold(groups: UsageGroup[], depth: number, limit: number) {
    const priced = groups.map((group) => ({ group, ...amountOf(group) })).filter((item) => item.value > 0).sort((a, b) => b.value - a.value);
    currency ??= priced.find((item) => item.currency)?.currency ?? null;
    const head = priced.slice(0, limit).map(({ group, value }) => ({
      key: `${depth}:${group.id ?? '\u0000'}`, depth, id: group.id, label: group.label || group.id || emptyGroupLabel(levels[depth]), value
    }));
    const rest = priced.slice(limit).reduce((acc, item) => acc + item.value, 0);
    return { head, rest };
  }

  // Level 0.
  const roots = fold(await ranking(query, levels[0], {}, {}, signal), 0, ROOTS);
  roots.head.forEach(add);
  if (roots.rest > 0) add({ key: '0:other', depth: 0, id: null, label: 'Other', value: roots.rest, other: true });

  // Deeper levels: expand the largest identified parents with their full ancestor path.
  let frontier = roots.head.filter((node) => node.id !== null).map((node) => ({ node, path: { [FILTER[levels[0]]]: [node.id!] } as W.Filters }));
  for (let depth = 1; depth < levels.length; depth++) {
    const parents = frontier.slice(0, EXPAND[depth - 1] ?? 3);
    const next: typeof frontier = [];
    const results = await Promise.all(parents.map(({ path }) => ranking(query, levels[depth], path, {}, signal).catch((error: unknown) => {
      if (signal.aborted) throw error;
      return [] as UsageGroup[];
    })));
    parents.forEach(({ node, path }, index) => {
      const children = fold(results[index], depth, CHILDREN);
      for (const child of children.head) {
        link(node.key, add(child), child.value);
        if (child.id !== null) next.push({ node: child, path: { ...path, [FILTER[levels[depth]]]: [child.id] } });
      }
      if (children.rest > 0) link(node.key, add({ key: `${depth}:other`, depth, id: null, label: 'Other', value: children.rest, other: true }), children.rest);
    });
    frontier = next.sort((a, b) => b.node.value - a.node.value);
  }
  // Level-0 nodes keep their own totals; deeper node totals are the sum of their incoming links.
  for (const node of nodes.values()) if (node.depth > 0) node.value = [...links.values()].filter((item) => item.target === node.key).reduce((acc, item) => acc + item.value, 0);
  return { levels, nodes: [...nodes.values()], links: [...links.values()], currency };
}

export interface FlowLayout {
  width: number; height: number;
  nodes: (FlowNode & { x: number; y: number; h: number; color: string })[];
  links: (FlowLink & { path: string; color: string; width: number })[];
}

/** Columns per level; node height and link width are proportional to cost on one shared scale. */
export function layoutFlow(flow: Flow, width: number, height: number, colors: readonly string[], nodeWidth = 12, gap = 10): FlowLayout {
  const columns = flow.levels.map((_, depth) => flow.nodes.filter((node) => node.depth === depth).sort((a, b) => Number(Boolean(a.other)) - Number(Boolean(b.other)) || b.value - a.value));
  const usable = (column: FlowNode[]) => height - gap * Math.max(0, column.length - 1);
  const scale = Math.min(...columns.filter((column) => column.length).map((column) => usable(column) / Math.max(1e-9, column.reduce((acc, node) => acc + node.value, 0))));
  const step = flow.levels.length > 1 ? (width - nodeWidth) / (flow.levels.length - 1) : 0;
  const placed = new Map<string, FlowLayout['nodes'][number]>();
  const rootColor = new Map<string, string>();
  columns.forEach((column, depth) => {
    let y = 0;
    column.forEach((node, index) => {
      const h = Math.max(2, node.value * scale);
      const color = depth === 0 ? (node.other ? 'rgb(var(--color-secondary))' : colors[index % colors.length]) : 'rgb(var(--color-secondary))';
      placed.set(node.key, { ...node, x: depth * step, y, h, color });
      if (depth === 0) rootColor.set(node.key, color);
      y += h + gap;
    });
  });
  // Links inherit the color of the root they descend from.
  const origin = new Map<string, string>([...rootColor.entries()]);
  for (let depth = 1; depth < flow.levels.length; depth++) {
    for (const node of columns[depth]) {
      const incoming = flow.links.filter((item) => item.target === node.key).sort((a, b) => b.value - a.value)[0];
      if (incoming) origin.set(node.key, origin.get(incoming.source) ?? 'rgb(var(--color-secondary))');
    }
  }
  for (const node of placed.values()) if (node.depth > 0) node.color = origin.get(node.key) ?? node.color;
  const outOffset = new Map<string, number>(), inOffset = new Map<string, number>();
  const sorted = [...flow.links].sort((a, b) => (placed.get(a.source)!.y - placed.get(b.source)!.y) || (placed.get(a.target)!.y - placed.get(b.target)!.y));
  const links = sorted.map((item) => {
    const from = placed.get(item.source)!, to = placed.get(item.target)!;
    const w = Math.max(1, item.value * scale);
    const y0 = from.y + (outOffset.get(item.source) ?? 0), y1 = to.y + (inOffset.get(item.target) ?? 0);
    outOffset.set(item.source, (outOffset.get(item.source) ?? 0) + w);
    inOffset.set(item.target, (inOffset.get(item.target) ?? 0) + w);
    const x0 = from.x + nodeWidth, x1 = to.x, mid = (x0 + x1) / 2;
    const path = `M${x0},${y0}C${mid},${y0} ${mid},${y1} ${x1},${y1}L${x1},${y1 + w}C${mid},${y1 + w} ${mid},${y0 + w} ${x0},${y0 + w}Z`;
    return { ...item, path, color: origin.get(item.source) ?? 'rgb(var(--color-secondary))', width: w };
  });
  return { width, height, nodes: [...placed.values()], links };
}
