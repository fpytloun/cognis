import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import ProjectIconPicker from './ProjectIconPicker.svelte';
import ProjectAvatar from './ProjectAvatar.svelte';
import { filterProjectIcons, projectIconCatalog } from '$lib/projects/icon-catalog';

afterEach(cleanup);

describe('project icon library', () => {
  it('offers the full Lucide pack without colliding with saved project IDs', () => {
    expect(projectIconCatalog.length).toBeGreaterThan(1600);
    expect(new Set(projectIconCatalog.map((icon) => icon.id)).size).toBe(projectIconCatalog.length);
    expect(filterProjectIcons('coffee').map((icon) => icon.id)).toContain('lucide:coffee');
    expect(filterProjectIcons('CLOCK alarm').map((icon) => icon.id)).toContain('lucide:alarm-clock');
    expect(filterProjectIcons('travel').map((icon) => icon.id)).toContain('suitcase');
    expect(filterProjectIcons('luggage').map((icon) => icon.id)).toContain('suitcase');
    expect(filterProjectIcons('graduation cap').map((icon) => icon.id)).toContain('graduation');
  });

  it('searches, selects, resets, and expands the library', async () => {
    render(ProjectIconPicker, { color: '#38bdf8' });
    await screen.findByRole('button', { name: 'Use Cognis icon' });
    expect(screen.getAllByRole('button', { name: /^Use .+ icon$/ })).toHaveLength(60);
    await fireEvent.click(screen.getByRole('button', { name: 'Show more icons' }));
    expect(screen.getAllByRole('button', { name: /^Use .+ icon$/ })).toHaveLength(120);
    await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'coffee' } });
    const coffee = screen.getByRole('button', { name: 'Use Coffee icon' });
    await fireEvent.click(coffee);
    expect(coffee).toHaveAttribute('aria-pressed', 'true');
    expect(await screen.findByRole('img', { name: 'Selected project icon' })).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Use default' }));
    expect(coffee).toHaveAttribute('aria-pressed', 'false');
    await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'no matching project symbol' } });
    expect(screen.getByText(/No icons match/)).toBeInTheDocument();
  });

  it('renders a newly selected icon as a colored glyph, not an image avatar', async () => {
    const { container } = render(ProjectAvatar, { name: 'Cafe', icon: 'lucide:coffee', color: '#ffffff' });
    await screen.findByTestId('project-library-icon');
    await screen.findByRole('img', { name: 'Cafe' });
    // Lucide gives its SVG the icon's name as a CSS class.
    await waitFor(() => {
      expect(container.querySelector('svg.lucide-coffee')).toBeInTheDocument();
    });
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByTestId('project-library-icon')).toHaveStyle({ color: '#0f172a' });
  });
});
