# Stage 41 — Task Cockpit UX wireframes

Annotated grayscale + single-accent wireframes for the Stage 41 Task Cockpit
UX review. Source of truth is the `.svg` file; the `png/` copies are 2×
rasterizations for quick viewing and for embedding in reviews. Do not treat
these as production visuals — they express information architecture, hierarchy,
state, and interaction intent, not final styling.

| # | File | Screen | State |
|---|------|--------|-------|
| 00 | `00-information-architecture.svg` | IA region model (desktop + mobile + drawer) | reference |
| 01 | `01-cockpit-running-desktop.svg` | Task Cockpit, desktop | running, multi-workstream |
| 02 | `02-cockpit-action-required-desktop.svg` | Task Cockpit, desktop | paused at gate (action required) |
| 03 | `03-completed-result-work-desktop.svg` | Task Cockpit, desktop | completed result + inline Work |
| 04 | `04-task-control-chat-drawer.svg` | Task Control Chat, desktop | docked drawer with inline decision (early sketch; superseded by 08) |
| 05 | `05-mobile-cockpit-and-control-chat.svg` | Cockpit + control chat, mobile | action required |
| 06 | `06-workflow-builder.svg` | Workflow builder | phase/step canvas + inspector |
| 07 | `07-agent-dock-minimized-task-detail.svg` | Task Agent Dock (task-detail), desktop | minimized FAB on the task page; route-unmount + persistent-conversation reopen |
| 08 | `08-agent-dock-open-over-cockpit.svg` | Task Agent Dock, desktop | open/docked over Cockpit, gate resolvable, task title (scope = route) |
| 09 | `09-agent-dock-fullscreen.svg` | Task Agent Dock, desktop | full-screen Chat v2 (window mode), task-scoped, Return-to-task |
| 10 | `10-agent-dock-mobile.svg` | Task Agent Dock, mobile | minimized FAB → bottom sheet → full-screen |
| 11 | `11-work-file-tree-desktop.svg` | Work view, desktop | hierarchical file tree + diff (resizable, sync scroll) |
| 12 | `12-work-file-tree-mobile.svg` | Work view, mobile | files list/drawer → full-width diff |

Wireframes 07–12 cover the two user-approved refinements: the **Task Agent
Dock** (§9) and the **Work file explorer** (§10) in the review spec. The dock is
**task-detail-scoped only**: it is mounted by the task-detail layout, is
available across all task-detail surfaces (Cockpit, tabs, inspector, Work
modal), and **unmounts** when the user leaves task detail; the persistent
server-side conversation reopens on return. It is not an app-shell singleton and
does not follow the user across routes. Frame 04 is retained as the original
drawer sketch; the dock (07–10) is the final direction and treats that drawer as
its docked-open state.

## Rendering PNGs

PNGs are produced from the SVGs with headless Chrome:

```bash
cd docs/wireframes/stage-41
for f in *.svg; do
  W=$(grep -o 'width="[0-9]*"' "$f" | head -1 | grep -o '[0-9]*')
  H=$(grep -o 'height="[0-9]*"' "$f" | head -1 | grep -o '[0-9]*')
  printf '<!doctype html><meta charset="utf-8"><style>*{margin:0}html,body{background:#0b1220}svg{display:block}</style>' > /tmp/w.html
  cat "$f" >> /tmp/w.html
  google-chrome --headless=new --disable-gpu --hide-scrollbars \
    --force-device-scale-factor=2 --window-size=${W},$((H+120)) \
    --screenshot="png/${f%.svg}.png" "file:///tmp/w.html"
done
```

## Shared visual vocabulary

- Accent `#38bdf8` = interactive / active / primary.
- Amber `#f5b544` = attention / waiting / requires the user.
- Green `#4fd1a5` = completed / success / canonical result.
- Rose `#f2657f` = destructive (Stop / Cancel / Remove).
- Step glyphs (runtime and builder share them): `✷` Agent · `◆` Tool ·
  `⋔` Condition · `⛉/▽` Gate · `◼` Complete.
- Numbered circles are annotation callouts referenced in
  `docs/specs/implementation/stage-41-cockpit-ux-review.md`.
