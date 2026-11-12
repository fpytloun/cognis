# Format annotation adapters

The shell owns source resolution, authorization, feedback persistence, capture
adoption and dispatch. These adapters use its lexical `FeedbackSurfaceBinding`;
they never infer a conversation from URLs or producer metadata.

- Text is LF-normalized, with UTF-16 offsets and a separate text digest.
  Rendered selections must map uniquely; ambiguous or extracted text uses the
  canonical source selector. Rich fields and PDF pages use `resolveTextScope`.
- Rich source paths are original JSON paths carried through media/publication
  copies by a private Symbol. Semantic IDs must be globally unique. Direct item
  keys are only used when unique; index-based fields remain revision-local.
- Image shapes use normalized oriented display coordinates. PDF geometry uses
  unrotated top-left page points, independently of zoom and device-pixel ratio.
- `WorkFileTree.feedbackBindingForDiff` receives each original event, not the
  concatenated history. Its ordinal is a history position, **not** canonical
  `file_ordinal`: obtain that from the event's `feedback_source`. Missing exact
  evidence means no annotation binding. `FileDiffViewer.annotationBindings`
  provides one exact binding per corresponding diff. Line selection requires a
  complete hunk and never crosses sides or hunks. Hunk digests include the full
  LF-normalized header and body without a terminal LF.
- HTML remains `sandbox="allow-scripts"` with an opaque origin. Its bridge
  reports bounded, untrusted observations only. The host binds messages to the
  current window and instance, rate limits them, and tears down with the lease.
  An instance token is not authentication against scripts in that same frame.
- Dynamic HTML markup requires an explicit locally previewed/cropped raster.
  Browser capture is user-mediated; denied/unsupported capture offers PNG/WebP
  file/paste. Both are untrusted user observations. Only the confirmed frozen
  Blob reaches the shell candidate callback; the shell performs source-bound
  adoption. No generic guest upload or silent fresh render is introduced.
- A saved capture must be reviewed as that capture, never reprojected onto live
  HTML. Current live-frame lookup returns unavailable for capture-backed anchors.

Native capture permission and integrated private/public capture adoption need
the parent's API-backed browser acceptance in addition to the focused tests.
