import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';
import FilterBuilder from './FilterBuilder.svelte';

const schema = { fields: {
  category: { type: 'keyword', filterable: true, enum: ['guide', 'reference'] },
  published: { type: 'boolean', filterable: true },
  priority: { type: 'number', filterable: true }
} };

describe('FilterBuilder', () => {
  it('keeps local rows stable until Apply and supports typed controls', async () => {
    const onChange = vi.fn();
    render(FilterBuilder, { knowledgebaseId: 'kb_1', metadataSchema: schema, filters: [], revision: 'r1', onChange });
    await fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    const field = screen.getByLabelText('Filter field');
    await fireEvent.change(field, { target: { value: 'published' } });
    expect(screen.getByLabelText('Filter value').tagName).toBe('SELECT');
    await fireEvent.change(screen.getByLabelText('Filter value'), { target: { value: 'false' } });
    expect(onChange).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    expect(onChange).toHaveBeenLastCalledWith([{ field: 'published', op: 'eq', value: false }]);
  });

  it('renders enum multi-select for in and clears explicitly', async () => {
    const onChange = vi.fn();
    render(FilterBuilder, { knowledgebaseId: 'kb_1', metadataSchema: schema, filters: [], revision: 'r1', onChange });
    await fireEvent.click(screen.getByRole('button', { name: 'Add filter' }));
    await fireEvent.change(screen.getByLabelText('Filter operator'), { target: { value: 'in' } });
    const values = screen.getByLabelText('Filter values');
    for (const option of Array.from((values as HTMLSelectElement).options)) option.selected = true;
    await fireEvent.change(values);
    await fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    expect(onChange).toHaveBeenLastCalledWith([{ field: 'category', op: 'in', value: ['guide', 'reference'] }]);
    await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it('keeps restored list filters editable and clears incompatible hidden values', async () => {
    const onChange = vi.fn();
    render(FilterBuilder, {
      knowledgebaseId: 'kb_1',
      metadataSchema: schema,
      filters: [{ field: 'category', op: 'in', value: ['guide', 'reference'] }],
      revision: 'r1',
      onChange
    });
    await fireEvent.change(screen.getByLabelText('Filter operator'), { target: { value: 'eq' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    expect(onChange).toHaveBeenLastCalledWith([{ field: 'category', op: 'eq', value: 'guide' }]);

    await fireEvent.change(screen.getByLabelText('Filter field'), { target: { value: 'published' } });
    await fireEvent.change(screen.getByLabelText('Filter value'), { target: { value: 'false' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    expect(onChange).toHaveBeenLastCalledWith([{ field: 'published', op: 'eq', value: false }]);
  });

  it('does not restore hidden values after all restored multi-select options are cleared', async () => {
    const onChange = vi.fn();
    render(FilterBuilder, {
      knowledgebaseId: 'kb_1',
      metadataSchema: schema,
      filters: [{ field: 'category', op: 'in', value: ['guide', 'reference'] }],
      revision: 'r1',
      onChange
    });
    const select = screen.getByLabelText('Filter values') as HTMLSelectElement;
    for (const option of Array.from(select.options)) option.selected = false;
    await fireEvent.change(select);
    await fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });
});
