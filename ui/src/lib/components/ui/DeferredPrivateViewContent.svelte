<script lang="ts">
  let { name }: { name: string } = $props();
</script>
<input aria-label="Loaded source" value={name} />
