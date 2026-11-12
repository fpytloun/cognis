<script lang="ts">
  import FindBar from './FindBar.svelte';
  import type { ChatSearchResult } from '$lib/chat-search';

  let {
    query = $bindable(''),
    inputElement = $bindable<HTMLInputElement | null>(null),
    results = [],
    selectedIndex = 0,
    loading = false,
    disabled = false,
    placeholder = 'Search this conversation',
    onSubmit,
    onClose,
    onNext,
    onPrevious
  } = $props<{
    query: string;
    inputElement?: HTMLInputElement | null;
    results?: ChatSearchResult[];
    selectedIndex?: number;
    loading?: boolean;
    disabled?: boolean;
    placeholder?: string;
    onSubmit: () => void;
    onClose: () => void;
    onNext: () => void;
    onPrevious: () => void;
  }>();
</script>

<FindBar bind:query bind:inputElement label={placeholder} {disabled}
  count={loading ? 'Searching…' : results.length ? `${selectedIndex + 1} of ${results.length}` : 'No matches'}
  navigationDisabled={results.length === 0 || loading} {onSubmit} {onPrevious} {onNext} {onClose} />
