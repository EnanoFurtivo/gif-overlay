# Speech-bubble notifications + taskbar following — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show desktop notifications as speech bubbles from the GIF (Plasma DND while visible), and make the GIF sit on / ride up with the taskbar.

**Architecture:** Everything stays in the single `gif-overlay` script. Plasma remains the notification server; a Gio monitor connection eavesdrops `Notify`, `Inhibit` silences Plasma's popups while the GIF is visible. A second layer-shell window draws the bubble. The runtime KWin script reports hover and visible panel rects over D-Bus.

**Tech Stack:** Python 3.14, PyGObject (Gtk 3, Gdk 3, Gio, GLib, Pango/PangoCairo), gtk-layer-shell 0.1, pycairo, KWin JS scripting.

**Spec:** `docs/superpowers/specs/2026-11-02-speech-bubbles-design.md`

## Global Constraints

- Single file `gif-overlay`, installed with `install -Dm755 gif-overlay ~/.local/bin/gif-overlay`; service restarted with `systemctl --user restart gif-overlay`.
- `gi.require_version` pins: Gtk 3.0, Gdk 3.0, GdkPixbuf 2.0, GtkLayerShell 0.1, Pango 1.0, PangoCairo 1.0.
- D-Bus: name/interface `local.GifOverlay`, path `/local/GifOverlay`. KWin script plugin name `gif-overlay-kwin`.
- Settings removed: `raise_px`, `max_stack`, `notif_seconds`. Added: `bubbles` (default `True`, `--bubbles/--no-bubbles`).
- Bubble: max width 340 px, ~6 body lines, typing ~80 chars/s, display = clamp(3 + chars/15, 4, 12) s after typing, 400 ms gap, queue max 10.
- Never call `Inhibit` unless `BecomeMonitor` succeeded.
- Not a git repo: no commit steps.

## Review Focus

1. Notification with HTML markup / entities in the body (`<b>`, `&amp;`, `<a href>`) → shown as clean plain text. Test: `test_plain_text_*` (Task 1).
2. Empty summary or empty body → bubble still renders, no blank line gap. Test: live step in Task 4.
3. Burst of more than 10 notifications → oldest dropped, no unbounded growth. Test: `test_queue_drops_oldest` (Task 1).
4. Panels on other monitors or at the top edge → no offset for the GIF's monitor. Test: `test_panel_offset_ignores_other_monitor_and_top` (Task 1).
5. `--toggle` while a bubble is mid-typing → bubble gone, timers cancelled, DND released. Test: live step in Task 4.

---

### Task 1: Pure helpers + tests

**Files:**
- Modify: `gif-overlay` (new section `# ---- pure helpers` placed before `# ---- options`, ending with the marker line `# ---------------------------------------------------------------- options`)
- Create: `tests/test_helpers.py` (loads the source text up to the options marker with `exec`, so no GTK/argparse runs)

**Interfaces — Produces:**
- `plain_text(s: str) -> str` — strip tags, unescape HTML entities, collapse whitespace runs (keep `\n`).
- `display_seconds(text: str) -> float` — `min(12, max(4, 3 + len(text) / 15))`.
- `parse_panels(s: str) -> list[tuple[int, int, int, int]]` — `"x,y,w,h;..."` → tuples; `""` → `[]`.
- `panel_offset(panels, mon: tuple[int, int, int, int]) -> int` — for panels that overlap the monitor horizontally and whose bottom is within 2 px of the monitor bottom: `max(mon_bottom - panel_top)`; else 0.
- `class BubbleQueue(maxlen=10)`: `push(note: dict) -> str` returns `"queued"` or `"replaced"`; `pop() -> dict | None`; `clear()`; `__len__`. A note is `{"app": str, "summary": str, "body": str, "entry": str, "replaces": int}`. `push` with `replaces != 0` and a queued note of the same `app` replaces that note in place.

- [ ] **Step 1: Write the failing tests** in `tests/test_helpers.py`:
  - `test_plain_text_strips_tags_and_entities`: `plain_text("<b>Hi</b> &amp; <a href='x'>you</a>") == "Hi & you"`
  - `test_plain_text_keeps_newlines`: `plain_text("a\n\nb  c") == "a\n\nb c"`
  - `test_display_seconds_clamps`: `display_seconds("") == 4`, `display_seconds("x"*60) == 7`, `display_seconds("x"*1000) == 12`
  - `test_parse_panels`: `parse_panels("") == []`, `parse_panels("1920,1018,1920,62") == [(1920,1018,1920,62)]`
  - `test_panel_offset_bottom_panel`: `panel_offset([(1920,1018,1920,62)], (1920,0,1920,1080)) == 62`
  - `test_panel_offset_ignores_other_monitor_and_top`: `panel_offset([(0,1018,1920,62),(1920,0,1920,40)], (1920,0,1920,1080)) == 0`
  - `test_queue_fifo`, `test_queue_drops_oldest` (push 12 → `len == 10`, first pop is note #3), `test_queue_replaces_same_app` (push A1, B, then A2 with replaces=5 → returns `"replaced"`, pops A2 then B).
- [ ] **Step 2: Run** `python -I -m pytest tests -q` (or `python -I tests/test_helpers.py` if pytest is missing) → FAIL (names not defined).
- [ ] **Step 3: Implement** the helpers in `gif-overlay`. `plain_text`: `re.sub(r"<[^>]+>", "", s)` then `html.unescape`, then `re.sub(r"[ \t]+", " ", ...)`, strip.
- [ ] **Step 4: Run tests** → all pass; `python -m py_compile gif-overlay` → OK.

### Task 2: Taskbar following (replaces notification lift)

**Files:** Modify `gif-overlay` (hover section's `HOVER_JS`, notifications section, options).

**Interfaces:**
- Consumes: `parse_panels`, `panel_offset` (Task 1).
- Produces: `dock = Tween(0.0, apply_bottom)` replacing `lift`; `bottom_margin() -> int` = `cfg["margin"] + int(dock.value)`; `monitor_geometry() -> tuple` of the GIF's current GDK monitor (pinned monitor if set, else `get_monitor_at_window`); `place_listeners: list[callable]` — functions called with no args after every bottom-margin change (Task 4 appends the bubble re-placer).
- D-Bus method `Panels(in s rects)`.

- [ ] **Step 1:** Remove `raise_px`, `max_stack`, `notif_seconds` from `DEFAULTS`, the argparse flags, `recompute`, `active`, and the `dbus-monitor` subprocess + `on_dbus` watch (Task 3 replaces the listener).
- [ ] **Step 2:** Rename `KWIN_SCRIPT` to `"gif-overlay-kwin"`; on load also try `unloadScript("gif-overlay-hover")` (old name). Add to the JS: `sendPanels()` collects `frameGeometry` of `workspace.windowList()` items with `w.dock && !w.hidden`, joins `"x,y,w,h"` with `;` (rounded ints), calls `Panels` only when the string changed; hook every dock window's `hiddenChanged` and `frameGeometryChanged`, `workspace.windowAdded` (hook if dock, then send), `workspace.windowRemoved` (send); call once at load.
- [ ] **Step 3:** App: `Panels` handler stores `parse_panels(s)` in `panels`, then `update_dock()` = `dock.to(panel_offset(panels, monitor_geometry()))`. Call `update_dock()` in `show()` too. `screen_rect()` uses `bottom_margin()`.
- [ ] **Step 4: Verify live:** reinstall + restart; `gdbus call ... Rect` shows y = 1080 − (20 + 0) − h with the panel hidden. Ask the user to show the panel → GIF slides up 62 px and back; screenshot while shown.

### Task 3: Notification listener + DND switch

**Files:** Modify `gif-overlay` (new section `# ---- notifications -> bubbles`).

**Interfaces:**
- Consumes: `BubbleQueue`, `plain_text` (Task 1).
- Produces: `on_notification(note: dict)` (main-thread callback; Task 4 implements the bubble side as `bubble_enqueue(note)`); `set_dnd(on: bool)`; `listening: bool` (True once `BecomeMonitor` succeeded).

- [ ] **Step 1:** Monitor connection: `Gio.DBusConnection.new_for_address_sync(Gio.dbus_address_get_for_bus_sync(SESSION), AUTHENTICATION_CLIENT | MESSAGE_BUS_CONNECTION)`, call `org.freedesktop.DBus.Monitoring.BecomeMonitor` `(asu)` with the `Notify` rule and flags 0. Filter: for `METHOD_CALL` with member `Notify`, unpack body `(susssasa{sv}i)` into a note (`entry` = hints `desktop-entry` or `""`; summary/body through `plain_text`), `GLib.idle_add` to `on_notification`; return `None` (consume). On `GLib.Error`: stderr warning, `listening = False`.
- [ ] **Step 2:** `set_dnd(on)`: if on and not cookie and `listening` and `cfg["bubbles"]`: `Inhibit("gif-overlay", "Showing notifications as speech bubbles", {})` → cookie; if off and cookie: `UnInhibit(cookie)`. Errors → stderr warning. Call `set_dnd(True)` after `show()` at startup and in `toggle` when shown; `set_dnd(False)` on hide and in the `finally` block.
- [ ] **Step 3: Verify live:** after restart, `gdbus call ... org.freedesktop.DBus.Properties.Get org.freedesktop.Notifications Inhibited` → `true`; `--toggle` → `false`; `--toggle` → `true`; `kill -9 <pid>` → `false` (then `systemctl --user restart gif-overlay`).

### Task 4: Bubble window

**Files:** Modify `gif-overlay` (same section as Task 3).

**Interfaces:**
- Consumes: `on_notification`, `bottom_margin()`, `size()`, `place_listeners`, `display_seconds`, `BubbleQueue`, `Tween`.
- Produces: `bubble_enqueue(note)`, `bubble_clear()` (called on hide).

- [ ] **Step 1:** Second `Gtk.Window` with layer shell (OVERLAY, BOTTOM|RIGHT, keyboard NONE, same monitor, namespace `gif-overlay-bubble`), RGBA visual, transparent bg, `Gtk.DrawingArea`. `place_bubble()`: right margin = `cfg["margin"]`, bottom margin = `bottom_margin() + size()[1]`; appended to `place_listeners`.
- [ ] **Step 2:** Layout with PangoCairo: app name (9 pt bold, 55% grey), summary (11 pt bold), body (10 pt, `set_height(-6)` + ellipsize END); width 340 − 2×14 padding; skip empty parts. Bubble path: rounded rect radius 14, tail 16 px tall on the bottom edge 28 px from the right, pointing down. Fill `#fffef5`, stroke 2 px `#2b2b2b`, text `#1e1e1e`. Window size = bubble + tail + 4 px for the stroke; `win.resize` to shrink.
- [ ] **Step 3:** Typing: render only the first `n` characters of summary+body (layout set per frame, `n += 80 * dt`); when done start the display timer `display_seconds(summary + body)`; then fade out (opacity Tween 0.2 s), hide, 400 ms later `pop()` next. Pop-in: opacity 0→1 and a 12 px upward slide over 0.25 s.
- [ ] **Step 4:** Input region = bubble rect. `button-press-event`: `Gio.DesktopAppInfo.new(entry + ".desktop")` → `launch([], None)` if found; then dismiss. `enter-notify-event` pauses the display timer; `leave-notify-event` restarts it with the remaining time.
- [ ] **Step 5:** `bubble_enqueue`: if the shown bubble has the same `app` and `replaces != 0`, swap its content and restart typing; else `queue.push`; if idle, show next. `bubble_clear()`: cancel timers, `queue.clear()`, hide. Hook: `toggle` hide → `bubble_clear()`; `on_notification` → `bubble_enqueue` only if not hidden and `cfg["bubbles"]`.
- [ ] **Step 6: Verify live** (screenshots): spec testing items 1–6 and 8; Review Focus 2 (`notify-send "" "only body"`, `notify-send "only title"`) and 5 (toggle mid-typing).

### Task 5: Final verification

- [ ] `python -I -m pytest tests -q` passes; `py_compile` OK.
- [ ] Reinstall, restart service, `journalctl --user -u gif-overlay -n 20` has no tracebacks.
- [ ] Idle CPU over 10 s ≤ ~4%.
- [ ] Hover fade still works (user check) with panel shown and hidden.
