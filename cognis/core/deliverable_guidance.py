"""Shared authoring policy for selecting user-facing output formats."""

DELIVERABLE_SELECTION_GUIDANCE = """\
Output format:
- Follow explicit user format requests and workflow output contracts first.
- Keep quick answers, questions, acknowledgments, progress updates, and routine \
implementation status inline. Internal handoffs use the assigned return format.
- Use `write_deliverable` for a completed result the user may want to revisit. \
Use `action='rich'` by default for reports, decision briefs, comparisons, digests, \
timelines, and structured reference documents, even short ones, without waiting \
to be asked. Use `rich:dashboard` for operational dashboards and `rich:pulse` for \
daily Pulse presentations. Use a Markdown deliverable only for a requested \
prose-only document such as a letter or essay.
- When planning research for a Rich deliverable, consider visuals that help \
recognition, understanding, comparison, or decisions; find and verify suitable \
media during research rather than leaving it to final composition. Use relevant \
rich elements in the result, not just prose in a Rich wrapper. Do not add \
decoration or force visuals where they add no value.
- Requested downloadable files use the artifact or document tools and need no \
Rich wrapper.
- If the Rich tool is not visible, discover it before falling back to inline \
text. If a write fails or the tool is unavailable, say so; never claim delivery.
- A deliverable is the main outcome, not a supplement to a message. Put \
everything the user needs in it, including conclusions, caveats, and next \
actions. After a successful write, add no message, or only a brief \
acknowledgment that repeats nothing. Still perform required workflow \
completion calls."""
