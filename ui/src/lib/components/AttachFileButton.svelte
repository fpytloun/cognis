<script lang="ts">
  import Paperclip from 'lucide-svelte/icons/paperclip';

  import Button from '$lib/components/ui/Button.svelte';

  /**
   * Attach-file button with an overlaid native ``<input type="file">``.
   *
   * Calling ``input.click()`` from JavaScript after a hidden-by-``display:none``
   * file input is unreliable on iOS Safari: the file picker often does not
   * appear. Overlaying a real ``<input>`` at ``opacity: 0`` on top of the
   * styled button means the user's tap hits the input directly, so Safari
   * treats it as a native user-gesture activation and opens the picker.
   *
   * Keyboard users focus the input (focus ring bubbles up via focus-within).
   */

  interface Props {
    onchange: (files: File[]) => void | Promise<void>;
    disabled?: boolean;
    label?: string;
    accept?: string;
    multiple?: boolean;
    class?: string;
  }

  let {
    onchange,
    disabled = false,
    label = 'Attach',
    accept,
    multiple = true,
    class: className = '',
  }: Props = $props();

  let input = $state<HTMLInputElement | null>(null);

  async function handleChange(event: Event): Promise<void> {
    const target = event.currentTarget as HTMLInputElement;
    const files = target.files;
    if (!files || files.length === 0) return;
    try {
      await onchange(Array.from(files));
    } finally {
      // Always reset so the same file can be picked twice in a row.
      target.value = '';
    }
  }
</script>

<span class={`relative inline-flex focus-within:ring-2 focus-within:ring-amber-300 focus-within:rounded-xl ${className}`}>
  <Button size="sm" variant="secondary" type="button" tabindex={-1} aria-hidden="true" {disabled}>
    <Paperclip class="h-4 w-4 sm:mr-2" /> <span class="hidden sm:inline">{label}</span>
  </Button>
  <input
    bind:this={input}
    aria-label={label}
    class="absolute inset-0 cursor-pointer opacity-0 disabled:cursor-not-allowed"
    type="file"
    {multiple}
    {accept}
    {disabled}
    onchange={(event) => void handleChange(event)}
  />
</span>
