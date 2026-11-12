<script lang="ts">
  import type { Snippet } from 'svelte';
  import type { FeedbackDiffRegistry } from '$lib/feedback/diff-registry';
  import FeedbackSourceProvider from './FeedbackSourceProvider.svelte';
  import FeedbackWorkspace from './FeedbackWorkspace.svelte';
  import FeedbackIndicator from './FeedbackIndicator.svelte';
  import FeedbackChromeMount from './FeedbackChromeMount.svelte';
  import { panelChrome, inlineChrome } from './panel-chrome';
  import { getCommentWorkspace } from '$lib/feedback/comment-workspace';
  const workspace = getCommentWorkspace();
  let { registry, children, onSourceFocus }: { registry: FeedbackDiffRegistry | null; children: Snippet; onSourceFocus?: () => void } = $props();
  const active = $derived(registry?.active);
  const snapshot = $derived($active?.state);
  const presenter = $derived($active?.presenter.state);
  const binding = $derived.by(() => { void $presenter; return $active && $snapshot ? $active.binding($snapshot) : null; });
</script>
{#snippet panel()}{#if $active}<FeedbackChromeMount definition={panelChrome} viewProps={{ controller: $active }} />{/if}{/snippet}
<FeedbackSourceProvider {binding} {panel} workspaceOwned={!!workspace} layout={$presenter?.layout} onBoundsChange={$active ? $active.presenter.setBounds.bind($active.presenter) : undefined} available={!!$active} open={Boolean($presenter?.surface)}
  onOpen={() => $active?.presentation().openRail()} onClose={() => $active?.presentation().closeRail()}
  onCommenting={enabled => {
    const owner = $active;
    owner?.presenter.close(false); owner?.patch({ mode: enabled ? 'comment' : 'view' });
  }}>
  <FeedbackWorkspace>
    {#if $active && !workspace}<FeedbackIndicator />{/if}
    {@render children()}
  </FeedbackWorkspace>
</FeedbackSourceProvider>
{#if $active && !workspace}{#key $active}<FeedbackChromeMount definition={inlineChrome} viewProps={{ controller: $active, comments: panel }} />{/key}{/if}
