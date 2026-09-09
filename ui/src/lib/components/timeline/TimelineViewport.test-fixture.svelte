<script lang="ts">
  import TimelineViewport from './TimelineViewport.svelte';

  let userScrolledUp = $state(false);
  let viewport = $state<{
    setProgrammaticScrollTop(scrollTop: number): void;
  } | null>(null);
</script>

<TimelineViewport
  bind:this={viewport}
  items={[]}
  bind:userScrolledUp
  autoScrollOnResize={false}
  testId="timeline-intent-viewport"
>
  {#snippet children()}
    <div style="height:1000px">Timeline content</div>
  {/snippet}
</TimelineViewport>
<output data-testid="timeline-intent-state">{userScrolledUp ? 'paused' : 'following'}</output>
<button type="button" onclick={() => viewport?.setProgrammaticScrollTop(800)}>
  Programmatic bottom
</button>
