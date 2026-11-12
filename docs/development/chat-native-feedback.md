# Native chat feedback

Completed assistant message bodies activate feedback from native text selection.
Inline deliverables activate their own exact document revision, not the surrounding
transcript. Table hover/focus and first touch reveal existing annotation handles;
only a subsequent handle gesture selects a structural range.

The canonical Markdown pipeline produces on-demand, ephemeral UTF-16 provenance.
The adapter checks the canonical text digest and mounted body before using that
map in either direction: selection to raw source, or persisted anchor to target.
Formatting delimiters can occur inside the minimal raw interval. Repeated text is
resolved by parser position, never quote guessing. Default rendering and streaming
do not request provenance or load annotation implementations.

Generated content and uncharacterized transformations fail closed. In particular,
transcript tables, fenced code, indentation-stripped containers and raw HTML are
not precise text targets in this initial adapter. Selections cannot cross those
regions or images. Rich deliverable tables use their separate authored adapter.
New assistant sources use `chat-item-as-of-event-v1` and the last proven assistant
contributor in canonical projection order. The server resolves the immutable item
as of that event; the client never assembles a second message. Legacy event-only
sources remain unchanged. This requires the matching backend representation
support before integrated runtime acceptance.

Native copy, links and scrolling remain browser-owned. Lazy loading captures the
account lease and selection; source/context changes, account invalidation and
unmount discard pending intent. No canonical event, DTO or backend state is added.
