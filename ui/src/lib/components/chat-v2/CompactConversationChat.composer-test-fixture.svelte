<script lang="ts">
  let { ariaLabel = 'Composer' }: { ariaLabel?: string } = $props();
</script>

<button type="button" data-testid="compact-composer-fixture">{ariaLabel}</button>
