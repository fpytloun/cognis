<script lang="ts">
  let { src, alt = 'Image', onClose } = $props<{
    src: string;
    alt?: string;
    onClose: () => void;
  }>();

  function handleKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') onClose();
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="fixed inset-0 z-[80] flex items-center justify-center bg-black/80 backdrop-blur-sm" onclick={onClose}>
  <img
    {src}
    {alt}
    class="max-h-[60vh] max-w-[400px] rounded-2xl object-contain shadow-2xl"
    onclick={(e) => e.stopPropagation()}
  />
</div>
