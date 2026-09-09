<script lang="ts">
  let { testIdPrefix }: { testIdPrefix: string } = $props();
</script>

<div data-testid={`${testIdPrefix}-content`}>Inspector</div>
