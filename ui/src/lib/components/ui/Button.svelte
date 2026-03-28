<script lang="ts">
  import type { Snippet } from 'svelte';

  import { cva, type VariantProps } from 'class-variance-authority';

  import { cn } from '$lib/utils';

  const buttonVariants = cva(
    'inline-flex items-center justify-center rounded-xl text-sm font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400 disabled:pointer-events-none disabled:opacity-60',
    {
      variants: {
        variant: {
          primary: 'bg-sky-500 px-4 py-2 text-slate-950 hover:bg-sky-400',
          secondary: 'border border-slate-700 bg-slate-900 px-4 py-2 text-slate-100 hover:border-slate-500 hover:bg-slate-800',
          ghost: 'px-3 py-2 text-slate-300 hover:bg-slate-800 hover:text-white',
          danger: 'bg-rose-500 px-4 py-2 text-white hover:bg-rose-400'
        },
        size: {
          default: '',
          sm: 'px-3 py-1.5 text-xs',
          lg: 'px-5 py-2.5 text-base'
        }
      },
      defaultVariants: {
        variant: 'primary',
        size: 'default'
      }
    }
  );

  type Props = VariantProps<typeof buttonVariants> & {
    type?: 'button' | 'submit' | 'reset';
    class?: string;
    disabled?: boolean;
    onclick?: ((event: MouseEvent) => void) | null;
    children?: Snippet;
  };

  let {
    variant,
    size,
    type = 'button',
    class: className = '',
    disabled = false,
    onclick = null,
    children
  }: Props = $props();
</script>

<button class={cn(buttonVariants({ variant, size }), className)} {type} {disabled} {onclick}>
  {@render children?.()}
</button>
