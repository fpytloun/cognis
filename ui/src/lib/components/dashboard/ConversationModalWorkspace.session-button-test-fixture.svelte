<script lang="ts">
  let {
    open,
    ariaControls,
    testId,
    onclick,
  }: {
    open: boolean;
    ariaControls: string;
    testId: string;
    onclick: () => void;
  } = $props();
</script>

<button type="button" aria-expanded={open} aria-controls={ariaControls} data-testid={testId} {onclick}>
  Inspector
</button>
