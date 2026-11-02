# gif-overlay: agent sprites (top-left)

Date: 2026-11-02 · Status: approved

## Goal

Fill the empty top-left corner with one animated sprite per running Claude agent:

- every **active** Claude Code session (desktop app or terminal) gets a sprite;
- its **subagents** appear next to it, smaller with each level of depth;
- when a session **needs the user's input** it shows a speech bubble; clicking the bubble
  opens that chat in the Claude desktop app;
- same click-through + fade-on-hover behaviour as the main GIF.

Out of scope here (next, separate change): custom notification sound, water reminder.

## Decisions (from the user)

| Question | Choice |
|---|---|
| Screen | Middle (ASUS / HDMI-A-1), same as the main GIF; `monitor` setting is shared |
| Which sessions | Active only: working, waiting for the user, or with running subagents |
| Terminal (CLI) sessions | Shown; their bubble has no click target |
| Layout | One row per session from the top-left corner, subagents to the right |
| GIF choice | Picked from `~/gifs` per **(session, depth)**: the session itself gets any GIF; all its depth-1 subagents share one GIF; all depth-2 share another, etc. A level never reuses its parent level's GIF or the main GIF (when the folder has enough GIFs) |

## Feasibility findings (spike)

- Claude Code 2.1.299 (used by the desktop app) has hook events `SessionStart`,
  `SessionEnd`, `UserPromptSubmit`, `Stop`, `SubagentStart`, `SubagentStop` (payload has
  `agent_id`, `agent_type`, `parent_agent_id`), `Notification` (`notification_type`,
  `message`), `PermissionRequest`, `PreToolUse`, `PostToolUse`.
- Desktop chats are stored as
  `~/.config/Claude/claude-code-sessions/<org>/<account>/local_<uuid>.json` with
  `sessionId` (`local_…`), `cliSessionId` (= the hooks' `session_id`) and `title`.
- The app handles `claude://code/needs-input?session=local_<uuid>` (its own dock menu uses
  it to open a specific chat).
- The app's `waiting-input/` folder is a queue of messages the user sent that are not yet in
  the transcript — **not** a "waiting for you" signal. Not used.

## Architecture

### Event transport: Claude Code hooks → D-Bus
- `gif-overlay --install-hooks` merges hook entries into `~/.claude/settings.json`
  (idempotent; `--uninstall-hooks` removes exactly those entries). Every entry runs:

  ```sh
  busctl --user --timeout=1 call local.GifOverlay /local/GifOverlay local.GifOverlay AgentEvent s "$(cat)" >/dev/null 2>&1 || true
  ```

  Fast, never fails the hook, a no-op when the overlay is not running.
- Events: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `Stop`, `SubagentStart`,
  `SubagentStop`, `Notification`, `PermissionRequest`, and `PreToolUse` / `PostToolUse` with
  matcher `AskUserQuestion|ExitPlanMode` (keeps payloads small and avoids a hook per tool call).
- New D-Bus method `AgentEvent(s json)` on `local.GifOverlay`.

### Agent tracker (pure, unit-tested)
Per session (`session_id`): `working`, `waiting` (text or none), `agents`
(`agent_id → depth`), `stopped_at`, `last_event`, `first_seen`.

| Event | Effect |
|---|---|
| `UserPromptSubmit` | working, clear waiting |
| `Stop` | not working, `stopped_at = now` |
| `SubagentStart` | add agent; depth = parent's depth + 1 if `parent_agent_id` is known, else 1 |
| `SubagentStop` | remove agent |
| `Notification` (type ≠ `idle_prompt`) | waiting = `message` or "needs your input" |
| `PermissionRequest` | waiting = "wants to use <tool_name>" |
| `PreToolUse` AskUserQuestion / ExitPlanMode | waiting = "has a question for you" / "wants you to approve a plan" |
| `PostToolUse` (same tools) | clear waiting, working |
| `SessionEnd` | forget the session |

A session is **active** when working, waiting, has agents, or stopped less than 4 s ago.
Sessions with no event for 30 min are forgotten (crash safety). Active sessions are listed
in order of first appearance. Malformed payloads are ignored.

### Sprite picking (pure)
`pick_gif(gifs, session_id, depth, exclude)` — deterministic (`random.Random(f"{session_id}:{depth}")`)
over the GIFs not in `exclude`; falls back to all GIFs if that leaves none. For depth d the
exclude set is {main GIF, the pick for depth d−1}. `gifs` = sorted `*.gif`/`*.webp` in
`gifs_dir` (default `~/gifs`), rescanned when the overlay starts.

### Layout (pure)
- Origin = monitor top-left + `margin` (20). Rows go down, 10 px apart.
- Row: session sprite at height `agent_size` (default 90), then one sprite per running
  subagent sorted by (depth, start order), height `agent_size × 0.65^depth`, 6 px apart,
  bottom-aligned to the session sprite. Width from each GIF's aspect ratio.
- At most 12 subagent sprites per row; the rest become a "+N" label.
- A waiting session's bubble sits to the right of its row, tail pointing left at the session
  sprite, vertically centred on it; row height = max(sprite, bubble).

### Agent window (GTK)
- One layer-shell window, OVERLAY, anchored TOP|LEFT, keyboard NONE, same monitor as the
  main GIF, sized to the layout's bounding box (hidden when there are no active sessions).
- Draws all sprites (one animation iterator per GIF file, frames scaled once per height and
  cached, like the main GIF) and the bubbles.
- Input region = bubble rectangles only (sprites are click-through).
- Bubble: same look as the main bubble (shared drawing code), no typing animation, stays
  while waiting. Header = chat title (desktop session `title`, else the cwd's folder name),
  text = waiting reason. Click (left) → `claude://code/needs-input?session=<local id>` via
  `Gio.AppInfo.launch_default_for_uri`; CLI sessions (no desktop file) are not clickable.
- `cliSessionId → (local id, title)` comes from scanning the desktop session files; the
  index is rebuilt when an unknown session id shows up (at most every 5 s).

### Hover (KWin script)
- `Rect()` becomes `Rects()` returning `"name:x,y,w,h;..."` — `main` for the main GIF and
  `agent:<session>:<n>` per sprite. `Hover(b)` becomes `Hover(s name, b on)`.
- The script tracks which named rect contains the cursor and reports enter/leave per name.
- Each sprite fades to `hover_opacity` on its own.

### Other changes
- `--toggle` hides/shows the agent window too.
- While any tracked session is waiting, notifications from the Claude app
  (`desktop-entry` `com.anthropic.Claude` or app name `Claude`) are not turned into main
  bubbles (the agent bubble already says it). Otherwise they still are.
- Settings: `agents` (default true, `--agents/--no-agents`), `agent_size` (90),
  `gifs_dir` (`~/gifs`).
- Code is split into a package `gif_overlay/` (the single file would pass 1,200 lines):
  `helpers.py` (pure), `agents.py` (pure tracker, picking, layout, hook-settings merge),
  `bubble.py` (shared bubble drawing + main bubble window), `agents_view.py` (agent window),
  `app.py` (everything else). `gif-overlay` stays a tiny launcher; installed to
  `~/.local/share/gif-overlay/` + `~/.local/bin/gif-overlay` by `install.sh`.

## Error handling
- Hooks can't reach the overlay → silently nothing (by design).
- Unparseable `AgentEvent` JSON → ignored, warning once.
- `~/gifs` missing or empty → agent sprites use the main GIF.
- No desktop session file for an id → CLI-style bubble (no click).
- `settings.json` unreadable / invalid JSON → `--install-hooks` refuses and says why; never
  overwrites it. Writes go through a temp file + rename, with a `.bak` copy first.

## Known limits (accepted)
- Agents started before the overlay (or before hooks were installed) appear on their next event.
- Depth relies on `parent_agent_id`; when absent a subagent counts as depth 1.

## Testing
- Unit: tracker transitions incl. depth, stale expiry, idle grace, `idle_prompt` ignored;
  `pick_gif` stability/exclusion/fallback; layout positions, bottom alignment, "+N" overflow;
  hook merge idempotent and removable without touching other hooks.
- Live: install hooks; a new desktop session shows a sprite while working and vanishes after;
  a subagent appears smaller next to it; `AskUserQuestion` shows a bubble; clicking it opens
  the chat; hover fades only that sprite; `--toggle` hides all.
