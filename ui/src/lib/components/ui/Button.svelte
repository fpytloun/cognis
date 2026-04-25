<script lang="ts">
  import type { HTMLButtonAttributes } from 'svelte/elements';
  import type { Snippet } from 'svelte';

  import { cva, type VariantProps } from 'class-variance-authority';

  import { cn } from '$lib/utils';

  /**
   * Button primitive. Mobile-first sizing: every variant meets the 40px minimum
   * tap target on touch devices and tightens on `md+`. Adds `icon-mobile` size
   * for 44x44 icon-only buttons where tap area is critical.
   */
  const buttonVariants = cva(
    'inline-flex items-center justify-center rounded-xl text-sm font-medium transition disabled:pointer-events-none disabled:opacity-60 select-none',
    {
      variants: {
        variant: {
          primary: 'bg-[color:var(--theme-accent-strong)] text-slate-950 hover:bg-[color:var(--theme-accent)]',
          secondary: 'border border-[color:var(--theme-border)] bg-[color:var(--theme-panel-muted)] text-[color:var(--theme-text)] hover:border-[color:var(--theme-accent)] hover:bg-[color:var(--theme-panel)]',
          ghost: 'text-[color:var(--theme-text-muted)] hover:bg-[color:var(--theme-panel-muted)] hover:text-[color:var(--theme-text)]',
          danger: 'bg-rose-500 text-white hover:bg-rose-400'
        },
        size: {
          // default: 40px min-height mobile; 36px at md+ (preserves desktop density)
          default: 'min-h-[40px] px-4 py-2 md:min-h-[36px] md:py-1.5',
          // sm: 36px min-height mobile; 30px at md+
          sm: 'min-h-[36px] px-3 py-1.5 text-xs md:min-h-[30px] md:py-1',
          // lg: 44px everywhere
          lg: 'min-h-[44px] px-5 py-2.5 text-base',
          // icon: 40x40 mobile, 36x36 at md+
          icon: 'h-10 w-10 md:h-9 md:w-9',
          // icon-mobile: 44x44 on every device (use for critical icon-only taps)
          'icon-mobile': 'h-11 w-11'
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
  } & HTMLButtonAttributes;

  let {
    variant,
    size,
    type = 'button',
    class: className = '',
    disabled = false,
    onclick = null,
    children,
    ...rest
  }: Props = $props();
</script>

<button class={cn(buttonVariants({ variant, size }), className)} {type} {disabled} onclick={onclick ?? undefined} {...rest}>
  {@render children?.()}
</button>
