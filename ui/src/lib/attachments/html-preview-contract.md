# HTML renderer boundary

`html-preview.ts` is standalone-safe. It has no private API, auth-store, account
coordination, or dynamic private-module import. The one `mountHtmlPreview`
implementation and annotation bridge serve owner and public views.

Public/standalone callers pass:

```ts
import type { HtmlPreviewSource, HtmlPreviewGuard } from '$lib/attachments/html-preview';
const guard: HtmlPreviewGuard = { signal, assertCurrent, validate };
const htmlSource: HtmlPreviewSource = { text: boundedHtml, guard };
// <ArtifactPreviewContent kind="html" {htmlSource} ... />
```

The guard is a trusted caller's view lifetime, with no owner/epoch/credential
fields. `validate` may return void or a Promise. Guards are revalidated before
and after asynchronous work, and abort removes the iframe before revocation.
Inline text remains bounded to `HTML_PREVIEW_MAX_BYTES`, sandboxed with
`allow-scripts` only. Image/PDF media URLs remain caller-supplied object URLs.

**Private-only import move:** `resolveHtmlPreviewUrl` and `signedHtmlUrl` now
come from `$lib/attachments/html-preview-private`. `SignedHtmlPreview` is still
exported as a type from the safe module and re-exported from the private module.
The resolver returns the existing `{url, lease}` fields plus a bounded `read`
closure. The safe mounting module never resolves or fetches a private URL.
`privateHtmlPreviewSource(url, DocumentLease)` adapts an already authorized
private URL when necessary. Manually constructing a URL source without a reader
fails closed; there is no private API fallback for anonymous views.

Shell owns these caller updates:
- `components/library/ResourceReader.svelte` (static resolver import).
- `components/projects/ProjectAssetModal.svelte` (static resolver import).
- `components/MessageAttachments.svelte` (dynamic resolver import).
- Any new private resolver import in `AttachmentPreviewModal`.

Only the **type** `SignedHtmlPreview` may be imported from the safe module in
either graph. Never re-export the private resolver from the safe module, even
via a dynamic import: standalone policy inspects every emitted chunk.
