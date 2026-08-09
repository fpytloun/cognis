<script lang="ts">
  import ChevronDown from 'lucide-svelte/icons/chevron-down';
  import ChevronUp from 'lucide-svelte/icons/chevron-up';
  import FileCode2 from 'lucide-svelte/icons/file-code-2';
  import hljs from 'highlight.js/lib/common';
  import 'highlight.js/styles/github-dark.css';

  import { parseFileDiff, type FileDiff, type ParsedDiffLine } from '$lib/diff';

  let {
    diffs,
    collapsible = true,
    collapsedByDefault = false,
    onExpand,
  } = $props<{
    diffs: FileDiff[];
    collapsible?: boolean;
    collapsedByDefault?: boolean;
    onExpand?: (() => void) | undefined;
  }>();

  let collapsed = $state<Record<string, boolean>>({});

  const parsedDiffs = $derived(diffs.map(parseFileDiff));

  function keyFor(path: string, index: number): string {
    return `${path || 'omitted'}:${index}`;
  }

  function toggle(path: string, index: number): void {
    const key = keyFor(path, index);
    collapsed = { ...collapsed, [key]: !(collapsed[key] ?? collapsedByDefault) };
  }

  function escapeHtml(value: string): string {
    return value
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function highlighted(line: ParsedDiffLine, language: string | null): string {
    if (line.type === 'hunk' || line.type === 'file' || line.type === 'meta') {
      return escapeHtml(line.content);
    }
    if (!line.content) return '';
    try {
      if (language && hljs.getLanguage(language)) {
        return hljs.highlight(line.content, { language, ignoreIllegals: true }).value;
      }
      return hljs.highlightAuto(line.content).value;
    } catch {
      return escapeHtml(line.content);
    }
  }

  function rowClass(type: string): string {
    if (type === 'add') return 'bg-lime-400/10 text-lime-50';
    if (type === 'remove') return 'bg-rose-500/10 text-rose-50';
    if (type === 'hunk') return 'bg-sky-500/10 text-sky-200';
    if (type === 'file' || type === 'meta') return 'bg-slate-800/50 text-slate-400';
    return 'text-slate-300';
  }

  function marker(type: string): string {
    if (type === 'add') return '+';
    if (type === 'remove') return '-';
    return ' ';
  }
</script>

<div class="space-y-3">
  {#each parsedDiffs as diff, index (keyFor(diff.path, index))}
    {@const key = keyFor(diff.path, index)}
    {@const isCollapsed = collapsed[key] ?? collapsedByDefault}
    {@const hasContent = Boolean(diffs[index]?.diff?.trim())}
    {@const contentId = `file-diff-content-${index}-${key.replace(/[^a-zA-Z0-9_-]/g, '-')}`}
    {#if diff.omittedCount > 0 && !diff.path}
      <div class="rounded-xl border border-yellow-500/30 bg-yellow-500/10 px-3 py-2 text-xs text-yellow-100">
        {diff.omittedCount} additional file diff{diff.omittedCount === 1 ? '' : 's'} omitted from this preview.
      </div>
    {:else}
      <section class="overflow-hidden rounded-xl border border-slate-700/80 bg-[#0b0f14] shadow-inner">
        <button
          class="flex w-full min-w-0 items-center gap-3 border-b border-slate-700/80 bg-slate-950/80 px-3 py-2 text-left transition hover:bg-slate-900"
          type="button"
          onclick={() => hasContent && collapsible ? toggle(diff.path, index) : onExpand?.()}
          aria-expanded={hasContent && collapsible ? !isCollapsed : undefined}
          aria-controls={hasContent && collapsible ? contentId : undefined}
        >
          <span class="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-slate-700 bg-slate-900 text-[10px] font-semibold text-cyan-200" title={diff.languageLabel}>
            {#if diff.iconLabel}
              {diff.iconLabel}
            {:else}
              <FileCode2 class="h-4 w-4" />
            {/if}
          </span>
          <span class="min-w-0 flex-1">
            <span class="block truncate font-mono text-xs text-slate-100">{diff.path}</span>
            <span class="mt-0.5 block text-[11px] text-slate-500">{diff.languageLabel}</span>
          </span>
          <span class="flex shrink-0 items-center gap-2 font-mono text-xs">
            {#if diff.additions > 0}<span class="text-lime-300">+{diff.additions}</span>{/if}
            {#if diff.deletions > 0}<span class="text-rose-300">-{diff.deletions}</span>{/if}
            {#if hasContent && collapsible}
              {#if isCollapsed}<ChevronDown class="h-4 w-4 text-slate-500" />{:else}<ChevronUp class="h-4 w-4 text-slate-500" />{/if}
            {:else if onExpand}
              <span class="hidden rounded border border-slate-700 px-2 py-1 text-[10px] text-sky-200 sm:inline-flex">{hasContent ? 'Expand diff' : 'View diff'}</span>
            {/if}
          </span>
        </button>

        {#if hasContent && (!collapsible || !isCollapsed)}
          <div class="max-h-[58vh] overflow-auto text-xs leading-5" id={contentId}>
            <table class="w-full border-collapse font-mono">
              <tbody>
                {#each diff.lines as line, lineIndex (`${key}:${lineIndex}`)}
                  <tr class={rowClass(line.type)}>
                    <td class="select-none border-r border-slate-800/80 px-2 text-right text-slate-600">{line.oldLine ?? ''}</td>
                    <td class="select-none border-r border-slate-800/80 px-2 text-right text-slate-600">{line.newLine ?? ''}</td>
                    <td class="select-none px-2 text-center text-slate-500">{marker(line.type)}</td>
                    <td class="min-w-[28rem] whitespace-pre px-2 [tab-size:2]">
                      {@html highlighted(line, diff.language)}
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
    {/if}
  {/each}
</div>
