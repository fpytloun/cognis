<script lang="ts">
  import ViewerIcon from './ViewerIcon.svelte';
  import PublicLinkDialog from './PublicLinkDialog.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import type { ShareLinkOptions } from './share-options';
  import { getFeedbackSurface } from '$lib/feedback/context';
  import { captureSharingLease, getDocumentSharing } from '$lib/document-sharing-context';
  const sharing = getDocumentSharing();
  const feedback = getFeedbackSurface();
  import './document-actions.css';
  let { createLink, internalUrl = '', allowModeChoice = false, defaultMode = 'view', label = '' }: {
    createLink?: ((options: ShareLinkOptions) => Promise<string>) | null;
    internalUrl?: string;
    allowModeChoice?: boolean;
    defaultMode?: 'view' | 'download';
    label?: string;
  } = $props();
  let open = $state(false);
  let copied = $state(false);
  let permissionsError = $state('');
  const feedbackSource = $derived(feedback?.binding?.source ?? null);
  function show() {
    open = true; copied = false; permissionsError = '';
    if (feedback?.prepare) void Promise.resolve(feedback.prepare()).catch(() => { permissionsError = 'Comment permissions could not be loaded. Reopen sharing to retry.'; });
  }
  async function createSharedLink(options: ShareLinkOptions): Promise<string> {
    const { allow_comments, ...linkOptions } = options;
    const priorRef = sharing?.contentRef ?? (feedback?.binding?.source.source.kind === 'document' ? feedback.binding.source.source.content_ref : null);
    function assertDocumentCurrent() {
      const currentRef = sharing?.contentRef ?? (feedback?.binding?.source.source.kind === 'document' ? feedback.binding.source.source.content_ref : null);
      if (!open || currentRef !== priorRef) throw new Error('The document changed. Reopen sharing.');
    }
    await feedback?.prepare?.();
    assertDocumentCurrent();
    const source = feedback?.binding?.source;
    if (!sharing?.canManage || !source?.capabilities.disclose || source.source.kind !== 'document') {
      if (!allow_comments && createLink) {
        const url = await createLink(linkOptions);
        assertDocumentCurrent();
        return url;
      }
      throw new Error('Comment sharing is unavailable for this viewer.');
    }
    if (priorRef && source.source.content_ref !== priorRef) throw new Error('The document changed. Reopen sharing.');
    const guard = captureSharingLease(sharing);
    await guard.validate();
    const { feedbackApi } = await import('$lib/feedback/api');
    guard.assertCurrent();
    assertDocumentCurrent();
    const share = await feedbackApi.share(source.subject_id, {
      source: source.source, expected_content_digest: source.content_digest,
      allow_comments: !!allow_comments, discussion: 'own_only', link_mode: options.mode ?? defaultMode,
      expires_at: options.expires_at ?? new Date(Date.now() + (options.ttl_seconds ?? 604800) * 1000).toISOString(),
    });
    await guard.validate();
    assertDocumentCurrent();
    if (feedback?.binding?.source.source.kind !== 'document' || feedback.binding.source.source.content_ref !== source.source.content_ref) {
      throw new Error('The document changed. Check existing links before creating another.');
    }
    if (!share.share_token) throw new Error('The public link could not be confirmed. Check existing links before creating another.');
    return `${location.origin}/feedback#${encodeURIComponent(share.share_token)}`;
  }
</script>

<Button variant="ghost" size="icon" class="document-icon-action" aria-label="Share link" title="Share link" aria-haspopup="dialog" aria-expanded={open} onclick={show}>
  <span data-testid={copied ? 'rich-share-copied-icon' : 'rich-share-icon'}><ViewerIcon name={copied ? 'check' : 'share'} /></span>
  {#if label}<span>{label}</span>{/if}
</Button>
{#if open}
  <PublicLinkDialog {internalUrl} createLink={createLink ? createSharedLink : null} {allowModeChoice} {defaultMode}
    {feedbackSource} {permissionsError}
    onClose={() => { open = false; }} onCopied={() => { copied = true; }} />
{/if}
