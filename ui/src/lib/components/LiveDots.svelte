<script lang="ts">
  let {
    label = '',
    tone = 'amber',
    size = 'md',
    inline = false,
    class: className = ''
  } = $props<{
    label?: string;
    tone?: 'sky' | 'slate' | 'amber';
    size?: 'sm' | 'md';
    inline?: boolean;
    class?: string;
  }>();

  function dotClass(): string {
    const palette = {
      sky: 'bg-amber-400',
      slate: 'bg-slate-400',
      amber: 'bg-amber-300'
    } as const;
    const safeTone: keyof typeof palette = tone === 'slate' || tone === 'sky' ? tone : 'amber';
    const dimensions = size === 'sm' ? 'h-1.5 w-1.5' : 'h-2 w-2';
    return `${dimensions} rounded-full ${palette[safeTone]}`;
  }
</script>

<span class={`inline-flex items-center gap-2 ${inline ? '' : 'rounded-full border border-slate-800 bg-slate-900/80 px-3 py-1.5'} ${className}`}>
  <span class="inline-flex items-center gap-1.5" aria-hidden="true">
    <span class={`${dotClass()} animate-bounce [animation-delay:0ms]`}></span>
    <span class={`${dotClass()} animate-bounce [animation-delay:150ms]`}></span>
    <span class={`${dotClass()} animate-bounce [animation-delay:300ms]`}></span>
  </span>
  {#if label}
    <span class="text-xs font-medium text-slate-300">{label}</span>
  {/if}
</span>
