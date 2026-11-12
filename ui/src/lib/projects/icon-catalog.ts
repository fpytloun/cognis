import type Briefcase from 'lucide-svelte/icons/briefcase';
import { projectIcons } from './icons';

// Preserve persisted IDs and familiar labels from the original palette.
const existing = new Set<unknown>(projectIcons.map((icon) => icon.component));
// The pinned package's canonical icon entrypoints, without the barrel's thousands
// of distinct component types. This module itself is loaded only on demand.
const modules = import.meta.glob<typeof Briefcase>([
  '/node_modules/lucide-svelte/dist/icons/*.js',
  '!/node_modules/lucide-svelte/dist/icons/index.js',
], { eager: true, import: 'default' });
const lucideIcons = Object.entries(modules).map(([path, component]) => {
  const name = path.slice(path.lastIndexOf('/') + 1, -3);
  const label = name.split('-').map((word) => word[0].toUpperCase() + word.slice(1)).join(' ');
  return { id: `lucide:${name}`, label, component };
});
export const projectIconCatalog = [
  ...projectIcons.map((icon) => ({
    ...icon,
    search: lucideIcons.find((item) => item.component === icon.component)?.label ?? '',
  })),
  ...lucideIcons
    .filter(({ component }) => !existing.has(component))
    .map((icon) => ({ ...icon, search: '' }))
    .sort((a, b) => a.label.localeCompare(b.label)),
];

export function filterProjectIcons(query: string) {
  const terms = query.toLowerCase().trim().split(/[\s-]+/).filter(Boolean);
  return projectIconCatalog.filter((icon) => {
    const text = `${icon.id} ${icon.label} ${icon.search}`.toLowerCase();
    return terms.every((term) => text.includes(term));
  });
}
