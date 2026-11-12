# Search keyboard shortcut

## Global command palette

Press **Command-K** on Mac or **Ctrl-K** elsewhere, or use the sidebar search
button pinned immediately above Notifications, including in the collapsed
desktop sidebar. The Spotlight-style palette searches accessible conversation
and task titles, schedule names, current Library document/file names, main
agents, projects, app pages, actions, and available settings destinations.
It does not search message or document content.

On mobile, open the main navigation menu and tap the **magnifier beside Close**.
The menu closes before Spotlight opens. This also works from full-screen chat.
Tap outside the open palette to dismiss it without activating a result.

Use **All types** to select any combination of conversations, tasks, schedules,
documents, files, main agents, projects, app pages, actions and settings.
The menu is multi-select; choose **All
types** inside it to reset. Type selection applies before server pagination.

Conversations and main agents use the agent's avatar as their primary icon;
tasks retain the agent's name and inline avatar. Projects use their configured
avatar, or their selected icon and color when no avatar is available. Stored results
show their last-updated time (hover for the full date and time). Files distinguish
agent production from user uploads; ambiguous legacy files say **Source unknown**.
Agent-produced files rank above user uploads at the same name-match tier.
Recurring tasks show only the latest visible execution of each schedule.
Independent tasks are not collapsed merely because their titles match.

Type to filter; matching is word-prefix based with bounded typo refinement.
Strong matches anywhere in the name precede typo candidates. Operational
destinations and a small head of matching files/documents share the first screen,
so a file flood does not bury a task such as **Smart coffee replenishment**.
Remaining matching files stay reachable by scrolling or by selecting Files only.

Main agents search both their internal and display names. Selecting one opens
its main conversation, not a previously visited topic; it is resolved only after
selection. Project destinations always open Overview, even if another project
tab was last selected. App pages use the same destinations as the sidebar.

Use **Up/Down** to select, **Enter** to open, or **Control-Shift-1 through
Control-Shift-9** to open a numbered match immediately on any platform.
On Mac, use **Control (⌃), not Command (⌘)**: Command-number selects browser
tabs, and several Command-Shift-number combinations take screenshots.
**Escape** closes the palette and restores focus.

Press **Up** in an empty search box to recall the most recent search, then use
**Up/Down** to browse earlier searches and return to your draft. **Alt-Up/Down**
(Option on Mac) also browses history without discarding typed text. Typing or
pressing **Left/Right** exits history browsing; Up/Down then selects results again.
Closing the palette remembers the query, not the results. History is stored
locally in this browser, separately for each account, with at most 20 searches.
Use **Clear history** in the palette footer to remove it.

On the main dashboard, opening a result opens or focuses its workspace window.
On any tab of an open project, only results belonging to that project open in its
workspace (or its modal fallback on phones or when windows are disabled);
other results navigate to their canonical page. Documents and files use the
Library reader, including items outside the project's recent activity. Elsewhere, results
navigate normally. **Command-Enter** / **Ctrl-Enter**, or the small redirect
arrow, always navigates to Chat, Tasks, Schedules, Library, or Settings.
Existing window capacity and access checks still apply.

Results load in keyset pages as you scroll. Rendering is virtualized and the
client retains at most 400 results per query; refine the search at that limit.
Blank-query results remain a stable snapshot until you interact or reopen the
palette; they do not automatically paginate while idle. Results from a different
query are never painted as a temporary replacement.
Small raster images can show thumbnails. Large originals and SVG files use
icons instead of downloading or decoding them for a search result.

The server reads metadata only, using native name indexes. Searches have
ten-second database deadlines, a fifteen-second operation limit, and per-user
and controller-wide admission/rate limits. The client allows twenty seconds per
request and automatically waits through brief admission contention for up to
thirty seconds. Changing the query or closing the palette retires that retry.
Client query caches are short-lived, memory-only, bounded, and cleared on account
changes. Persistent contention or a timed-out search leaves local actions
available and offers an explicit retry.
Routine background activity does not clear visible matches. Access changes
retire cached metadata immediately and refresh the current search automatically.

## Search the current view

Press **Command-F** on Mac or **Ctrl-F** on other platforms to focus the current
view's search field and select its existing text. The shortcut does not submit
a search or change a draft.

- In a full conversation, it opens conversation search when collapsed.
- In an opened deliverable or artifact, it opens **Find in document**. You can
  also tap the magnifier in the document action bar or **Search in document**
  in its ⋯ menu. Chat and documents use the same compact field inside their
  existing toolbar, with a count, arrows and Close. On phones it replaces the
  header contents at the same height; closing restores them and focus. No
  content is covered or shifted. Matching is literal and case-insensitive;
  **Enter** and **Shift-Enter** navigate matches and wrap at either end.
  **Escape** closes find without closing the document. Rich/Markdown and text
  previews search displayed text, not comments, controls or Library metadata.
  HTML search stays inside its sandbox. PDF search reads the embedded text
  across all pages and opens the selected page; scanned images need OCR first.
  Truncated text previews search only the displayed preview.
  Standalone Rich/text readers have the same find bar. External signed HTML
  pages keep browser-native Find inside their original opaque iframe; their
  strict trusted-shell CSP is unchanged.
- When focus is in the conversation sidebar or the Work file explorer, its
  existing search field takes priority.
- In the dashboard, an active workspace window's search takes priority.
  A window without search uses the visible dashboard search instead.
- A modal or sheet is a hard boundary: its search is used, or the shortcut is
  left to the browser. It never focuses a search behind that modal.
- Only existing, visible search controls participate. Hidden tabs, minimized
  windows and disabled fields do not. No search UI is added to compact chats.
- Shift/Alt variants, composition and already handled keyboard events are
  unchanged. Escape retains each view's existing behavior.

If the current view has no eligible search field, the app leaves the shortcut
to the browser or native host.
