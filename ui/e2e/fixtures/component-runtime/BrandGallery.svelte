<script lang="ts">
  import CognisLogo from '$lib/components/CognisLogo.svelte';

  const palettes = [
    { background: '#ffffff', foreground: '#0f172a' },
    { background: '#0b1420', foreground: '#f8fafc' },
  ];
  const variants = ['color', 'mono'] as const;
  const sizes = [16, 18, 22, 32, 48, 96];
</script>

<section id="brand-gallery">
  {#each palettes as palette}
    <div class="gallery-row" style:background={palette.background} style:color={palette.foreground}>
      {#each variants as variant}
        {#each sizes as size}
          <span style:font-size={`${size}px`}>
            <CognisLogo {variant} label={`${variant} ${size}`} />
          </span>
        {/each}
      {/each}
    </div>
  {/each}
</section>

<style>
  #brand-gallery { position: fixed; inset: 0; z-index: 10000; overflow: auto; padding: 24px; background: #64748b; }
  .gallery-row { padding: 32px; display: flex; align-items: center; gap: 32px; min-height: 220px; }
</style>
