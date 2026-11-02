# gif-overlay: speech-bubble notifications + sitting on the taskbar

Date: 2026-11-02 · Status: approved

## Goal

1. Desktop notifications appear as **speech bubbles coming from the GIF** (Clippy style)
   instead of Plasma's popups, while the GIF is visible.
2. Hiding the GIF (`--toggle`) **falls back to Plasma's normal popups**.
3. The GIF **sits on top of the taskbar**. With an auto-hide panel it rides up with the
   panel when it slides in and settles back down when it hides.
4. The old "slide up when a notification arrives" behaviour is removed.

What the user stated: items 1–4, plus the decisions below.
Assumptions that were presented and not objected to: the bubble sits above the GIF with
its tail pointing down at it; it shows app name, title and up to ~6 lines of body; the
text "types out" as if the GIF is speaking.

## Decisions

| Question | Choice |
|---|---|
| How bubbles get notifications | Plasma stays the notification server; we use its Do Not Disturb (`Inhibit`) and eavesdrop `Notify` calls |
| Several notifications at once | One bubble at a time, queued |
| Click on a bubble | Opens the sending app (`desktop-entry` hint) and closes the bubble |

## Architecture (all inside the single `gif-overlay` script, separate sections)

### 1. Notification listener (replaces the `dbus-monitor` subprocess)
- A second session-bus connection (`Gio.DBusConnection.new_for_address_sync`) calls
  `org.freedesktop.DBus.Monitoring.BecomeMonitor` with the rule
  `type='method_call',interface='org.freedesktop.Notifications',member='Notify'`.
- A message filter receives each `Notify` with its typed body
  `(app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout)` and
  hands it to the main loop with `GLib.idle_add` (filters run on a worker thread).
- Monitored messages are always consumed; the monitor connection never sends replies.

### 2. Do Not Disturb switch
- While the GIF is visible **and** bubbles are enabled: `Inhibit("gif-overlay",
  "Showing notifications as speech bubbles", {})` on `org.freedesktop.Notifications`,
  keep the returned cookie.
- On hide, `--no-bubbles`, or quit: `UnInhibit(cookie)`.
- Plasma releases an inhibition when its owner leaves the bus, so a crash or `kill -9`
  restores normal popups by itself.

### 3. Bubble window
- A second GTK3 layer-shell window: OVERLAY layer, anchored BOTTOM|RIGHT, keyboard mode
  NONE, same monitor as the GIF, RGBA visual, transparent background.
- Positioned so its bottom edge (the tail tip) sits just above the GIF's top edge, right
  edges aligned: bottom margin = GIF bottom margin + GIF height, right margin = GIF right
  margin. It moves whenever the GIF moves (resize, taskbar offset).
- Drawn with cairo + PangoCairo: rounded rectangle with a tail on the bottom-right
  pointing down at the GIF. Light "comic" style: near-white fill, dark outline, dark text.
  Content: app name (small, bold, muted), summary (bold), body (wrapped, ellipsized after
  ~6 lines). Body markup is stripped to plain text. Max width 340 px.
- Animation: pops in (opacity 0→1 with a small upward slide), text is revealed like typing
  (~80 chars/s), fades out when done.
- Input: the input region is the bubble shape only; the GIF stays click-through.
  - Click: if the notification has a `desktop-entry` hint, launch that app with
    `Gio.DesktopAppInfo` (single-instance apps just come to the front); then dismiss.
  - Hover: pauses the display timer; leaving resumes it.

### 4. Queue
- FIFO, one bubble at a time, 400 ms gap between bubbles, max 10 queued (oldest dropped).
- Display time = clamp(3 s + characters / 15 per second, 4 s, 12 s), counted after the
  typing finishes.
- `replaces_id != 0`: if a bubble from the same `app_name` is showing or queued, update it
  in place instead of adding a new one.
- Hiding the GIF clears the queue and closes the current bubble (Plasma already keeps them
  in its history).

### 5. Taskbar following (KWin script)
- The runtime KWin script is renamed `gif-overlay-kwin` and gains a second job: watch
  panel windows (`window.dock`), including ones added later (`workspace.windowAdded`).
- On load and on each panel's `hiddenChanged` / `frameGeometryChanged`, it sends the
  rectangles of all currently visible panels as `Panels(s "x,y,w,h;x,y,w,h")` (empty
  string when none are visible). The script does not need to know which monitor the GIF
  is on.
- The app caches the last list and computes offset = monitor bottom − panel top for the
  visible panels that touch the bottom edge of its own monitor (GDK monitor geometry,
  same logical coordinates as KWin), 0 if none. It recomputes on every `Panels` call and
  when shown again after a hide.
- The app tweens its bottom margin to `margin + offset` (the old lift Tween, repurposed).
  `Rect()` includes the offset so hover keeps matching.

### 6. Removed / changed settings
- Removed: `raise_px`, `max_stack`, `notif_seconds` (and their flags). Stale keys in
  `config.json` are ignored by the loader already.
- Added: `bubbles` (default true), flag `--bubbles/--no-bubbles`.

## Error handling
- KWin script fails to load → warning on stderr; no hover fade and no taskbar following,
  everything else works.
- `BecomeMonitor` fails → warning; do **not** call `Inhibit` (otherwise notifications would
  vanish entirely).
- `Inhibit` fails → bubbles still shown alongside Plasma's popups; warning on stderr.
- Malformed notification bodies → shown as plain text, never crash the main loop
  (exceptions inside handlers are caught and logged).

## Known trade-offs (accepted)
- Notification buttons (Reply, Accept…) are not available in bubbles; they remain in the
  tray history.
- The tray shows Do Not Disturb as on while the GIF is visible.
- Plasma still shows critical-urgency notifications in DND, so those appear twice.

## Testing (live, on the Plasma session)
1. `notify-send` short text → one bubble above the GIF, typed out, disappears; no Plasma popup.
2. Long body → wraps and ellipsizes, stays longer.
3. Three `notify-send` at once → shown one after another.
4. `notify-send -h string:desktop-entry:org.kde.dolphin …` → click opens Dolphin, bubble closes.
5. Hover a bubble → it stays until the mouse leaves.
6. `--toggle` while bubbles are queued → bubbles gone, next `notify-send` shows a Plasma popup;
   `--toggle` again → bubbles back.
7. `kill -9` the app → `Inhibited` property back to false, Plasma popups return.
8. Show the auto-hide panel → GIF and any bubble slide up; panel hides → slide back.
9. Hover fade still works with the panel shown and hidden.
10. CPU stays low (about 3% at idle, as now).
