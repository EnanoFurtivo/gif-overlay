# Agent sprites — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One animated sprite per active Claude agent in the top-left corner, smaller per depth, with a clickable "needs input" bubble that opens the chat.

**Architecture:** Claude Code hooks forward events with `busctl` to the overlay's D-Bus method `AgentEvent`; a pure `AgentTracker` keeps state; a layer-shell window draws sprites + bubbles; the KWin script reports hover per named rect.

**Tech Stack:** Python 3.14, PyGObject (Gtk 3, Gdk, Gio, GLib, Pango, PangoCairo), gtk-layer-shell, pycairo, KWin JS, busctl.

**Spec:** `docs/superpowers/specs/2026-11-02-agent-sprites-design.md`

## Global Constraints

- Package `gif_overlay/` + launcher `gif-overlay` + `install.sh` (copies package to `~/.local/share/gif-overlay/gif_overlay/`, launcher to `~/.local/bin/gif-overlay`); service unit unchanged.
- D-Bus: `local.GifOverlay` at `/local/GifOverlay`; new `AgentEvent(s)`, `Rects() -> s`, `Hover(s, b)` (old `Rect`/`Hover(b)` removed).
- Hook command exactly: `busctl --user --timeout=1 call local.GifOverlay /local/GifOverlay local.GifOverlay AgentEvent s "$(cat)" >/dev/null 2>&1 || true`
- Sizes: `agent_size` 90, depth ratio 0.65, row gap 10, sprite gap 6, max 12 subagent sprites per row, idle grace 4 s, stale 30 min.
- New settings: `agents` (True), `agent_size` (90), `gifs_dir` (`~/gifs`).
- Commits at the end of each task (repo exists now), ending with the Co-Authored-By line.

## Review Focus

1. A session that ends while waiting / with subagents still listed (crash, `kill`) → disappears after 30 min, never blocks others. Test: `test_stale_sessions_expire` (Task 2).
2. `parent_agent_id` pointing to an unknown agent → depth 1, no crash. Test: `test_unknown_parent_is_depth_one` (Task 2).
3. Folder with 0, 1 or 2 GIFs → picks still return a GIF (fallbacks). Test: `test_pick_gif_small_folders` (Task 2).
4. `settings.json` with existing user hooks → kept intact; install twice → no duplicates; uninstall removes only ours. Test: `test_hooks_merge_keeps_others_and_is_idempotent` (Task 2).
5. Burst of 30 subagents (parallel workflow) → row shows 12 + "+18". Test: `test_layout_overflow_label` (Task 2).

---

### Task 1: Split into a package (no behaviour change)

**Files:** Create `gif_overlay/__init__.py`, `gif_overlay/helpers.py` (the pure helpers + `hex_rgb`), `gif_overlay/app.py` (the rest, `main()` wraps the former top-level code), `install.sh`; rewrite `gif-overlay` as launcher; update `tests/test_helpers.py` to `from gif_overlay.helpers import ...`; README install section → `./install.sh`.

- [ ] Move code; launcher inserts the repo dir (if it contains `gif_overlay/`) else `~/.local/share/gif-overlay` into `sys.path`, then `from gif_overlay.app import main; main()`.
- [ ] `python -I tests/test_helpers.py` → 13/13; `./install.sh`; restart service; `Rect`-equivalent check: Inhibited true, bubble via `notify-send` (screenshot).
- [ ] Commit.

### Task 2: Pure agent logic + tests

**Files:** Create `gif_overlay/agents.py`, `tests/test_agents.py`.

**Interfaces — Produces:**
- `class AgentTracker(idle_grace=4.0, stale=1800.0)`: `event(payload: dict, now: float) -> bool` (True if anything visible changed); `active(now: float) -> list[SessionState]`; `any_waiting(now) -> bool`.
- `@dataclass SessionState`: `id: str, first_seen: float, last_event: float, working: bool, waiting: str|None, stopped_at: float|None, agents: dict[str, int]` (agent_id → depth, insertion-ordered).
- `pick_gif(gifs: list[str], session_id: str, depth: int, exclude: set[str]) -> str`; `session_gifs(gifs, session_id, max_depth, main_gif) -> list[str]` (index = depth).
- `layout(rows: list[dict], agent_size: int, aspect: callable, bubble_size: callable) -> (list[Placement], int, int)` where a row is `{"id", "agents": [depth,...], "waiting": bool}` and `Placement` = `(kind: "sprite"|"more"|"bubble", session_id, index, x, y, w, h, depth)`; returns placements and total width/height (relative to the window origin, no margin).
- `merge_hooks(settings: dict, command: str) -> dict`, `remove_hooks(settings: dict, command: str) -> dict`, `HOOK_EVENTS` (event → matcher or None).

- [ ] Write tests: `test_prompt_makes_active_and_stop_expires_after_grace`, `test_subagent_depths`, `test_unknown_parent_is_depth_one`, `test_waiting_from_notification_permission_and_tools`, `test_idle_prompt_ignored`, `test_session_end_forgets`, `test_stale_sessions_expire`, `test_malformed_payload_ignored`, `test_pick_gif_stable_and_excludes`, `test_pick_gif_small_folders`, `test_layout_rows_and_depth_sizes`, `test_layout_overflow_label`, `test_layout_bubble_right_of_row`, `test_hooks_merge_keeps_others_and_is_idempotent`, `test_hooks_remove_only_ours`.
- [ ] Run → fail (module missing). Implement. Run → all pass. Commit.

### Task 3: Agent window, bubbles, hooks command, hover per sprite

**Files:** Create `gif_overlay/bubble.py` (move `Bubble` + extract `draw_bubble(cr, box, tail, header_layout, text_layout, colors)` with `tail` = `("down", x)` or `("left", y)`), `gif_overlay/agents_view.py` (`AgentsView(app)`: window, GIF cache, draw, input region, click, `refresh()`); modify `gif_overlay/app.py` (D-Bus `AgentEvent`/`Rects`/`Hover(s,b)`, KWin JS multi-rect, `--install-hooks/--uninstall-hooks`, settings, toggle, Claude-notification skip, session-file index).

- [ ] Implement; `py_compile`; unit tests still pass.
- [ ] Live: `--install-hooks` (backup made, entries visible in settings.json); fake events with `busctl ... AgentEvent s '{...}'` for a session with 2 depth levels and a waiting state → screenshot top-left; click target URL logged; `--toggle` hides; per-sprite hover via user check.
- [ ] Commit.

### Task 4: Real-session verification + review + push

- [ ] Start a real desktop session (user), have it run a subagent and ask a question → sprites + bubble; click opens the chat.
- [ ] Fresh reviewer on the whole change; fix Critical/Important with tests; push to GitHub.
