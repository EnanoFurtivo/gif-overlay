"""Bottom-right corner of a chosen monitor: one tile per running GitHub Actions run."""
import json, math, os, subprocess, sys, threading, time, urllib.error, urllib.request

from gif_overlay.agents_view import Gif
from gif_overlay.bubble import EDGE
from gif_overlay.ci import CiTracker, describe, median_duration
from gif_overlay.gtkbits import (NEON, Gdk, GLib, Gtk, GtkLayerShell, Pango, PangoCairo, Tween,
                                 cairo, clear_bg, neon_stroke)
from gif_overlay.helpers import panel_offset

API = "https://api.github.com"
ACTIVE_STATUSES = ("in_progress", "queued", "waiting")
POLL_ACTIVE, POLL_IDLE = 10, 30  # seconds between polls
MAX_ROWS = 8
V_GAP, H_GAP, PAD = 6, 6, 8
BAR_H = 3
FILL = (0.043, 0.055, 0.078)
COLORS = {"running": NEON, "queued": NEON, "waiting": NEON, "over": (1.0, 0.66, 0.1),
          "success": (0.2, 1.0, 0.45), "failure": (1.0, 0.24, 0.32), "cancelled": (0.6, 0.65, 0.7)}
GLYPHS = {"success": "✓", "failure": "✗", "cancelled": "–"}


class GitHub:
    """Tiny REST client: token from `gh auth token`, ETags so unchanged lists cost nothing.
    Only ever used from the worker thread."""
    def __init__(self):
        self.token, self.etags, self.cache = None, {}, {}

    def get(self, path, conditional=False):
        if not self.token:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                                 timeout=15)
            self.token = out.stdout.strip()
            if not self.token:
                raise RuntimeError("`gh auth token` gave no token (is gh logged in?)")
        req = urllib.request.Request(API + path, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gif-overlay"})
        if conditional and path in self.etags:
            req.add_header("If-None-Match", self.etags[path])
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
                if conditional and resp.headers.get("ETag"):
                    self.etags[path], self.cache[path] = resp.headers["ETag"], data
                return data
        except urllib.error.HTTPError as e:
            if e.code == 304 and path in self.cache:
                return self.cache[path]
            if e.code == 401:
                self.token = None  # re-read it next time (gh may have refreshed it)
            raise


def poll(gh, repos, tracked, histories):
    """Worker thread: the active runs of every repo, the tracked runs that left those lists
    (fetched by id) and the requested histories. Plain data only, no GTK."""
    active = []
    for repo in repos:
        for status in ACTIVE_STATUSES:
            data = gh.get(f"/repos/{repo}/actions/runs?status={status}&per_page=30",
                          conditional=True)
            active += [dict(r, repo=repo) for r in data.get("workflow_runs", [])]
    ids = {r["id"] for r in active}
    ended, gone = [], []
    for repo, run_id in tracked:
        if run_id not in ids:
            try:
                ended.append(dict(gh.get(f"/repos/{repo}/actions/runs/{run_id}"), repo=repo))
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                gone.append(run_id)  # deleted
    found = {}
    for repo, workflow_id in histories:
        data = gh.get(f"/repos/{repo}/actions/workflows/{workflow_id}/runs"
                      "?status=success&per_page=20&exclude_pull_requests=true")
        found[(repo, workflow_id)] = data.get("workflow_runs", [])
    return active, ended, gone, found


class CiView:
    def __init__(self, app):
        self.app = app             # the gif_overlay.app module: cfg, sound, find_monitor()...
        self.tracker = CiTracker()
        self.gh = GitHub()
        self.history = {}          # (repo, workflow id) -> successful runs, newest first
        self.stale = set()         # histories to (re)fetch on the next poll
        self.busy = False          # a poll is running on the worker thread
        self.failing = False       # the last poll failed (logged once per streak)
        self.rows, self.win_w, self.win_h = [], 0, 0
        self.fade = Tween(1.0, self.invalidate, duration=0.2)
        self.layer = None
        self.frame_pending = False
        try:
            self.gif = Gif(os.path.expanduser(app.cfg["ci_gif"]), self.on_gif_frame)
        except GLib.Error as e:
            print("gif-overlay: can't load the CI GIF, using the main one:", e.message,
                  file=sys.stderr)
            self.gif = Gif(app.cfg["gif"], self.on_gif_frame)
        want = app.cfg["ci_monitor"]
        self.monitor = app.find_monitor(want) if want is not None else None
        if want is not None and not self.monitor:
            print(f"gif-overlay: CI monitor {want!r} not found, using the GIF's",
                  file=sys.stderr)

        self.win = Gtk.Window()
        GtkLayerShell.init_for_window(self.win)
        GtkLayerShell.set_namespace(self.win, "gif-overlay-ci")
        GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.NONE)
        if app.visual:
            self.win.set_visual(app.visual)
        self.win.set_app_paintable(True)
        self.win.connect("draw", clear_bg)
        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self.draw)
        self.win.add(self.area)
        if app.cfg["ci"] and app.cfg["ci_repos"]:
            GLib.idle_add(self.start_poll)
            GLib.timeout_add_seconds(1, self.tick)  # timers count, finished tiles expire

    # -- polling (network on a worker thread, results back on the GTK thread)
    def start_poll(self):
        if self.busy:
            return False
        self.busy = True
        tracked = [(t.run["repo"], t.run["id"]) for t in self.tracker.runs.values()
                   if t.finished_at is None]
        args = (self.gh, list(self.app.cfg["ci_repos"]), tracked, sorted(self.stale))

        def work():
            try:
                result = poll(*args)
            except Exception as e:  # offline, rate limited, gh logged out...
                result = e
            GLib.idle_add(self.polled, result)
        threading.Thread(target=work, daemon=True).start()
        return False

    def polled(self, result):
        self.busy = False
        if isinstance(result, Exception):
            if not self.failing:
                print("gif-overlay: couldn't read the CI runs (will keep trying):", result,
                      file=sys.stderr)
            self.failing = True
            GLib.timeout_add_seconds(POLL_IDLE, self.start_poll)
            return False
        if self.failing:
            print("gif-overlay: reading the CI runs again", file=sys.stderr)
        self.failing = False
        active, ended, gone, found = result
        self.history.update(found)
        self.stale -= set(found)
        now = time.time()
        self.tracker.update(active, now)
        finished = False
        for run in ended:
            if self.tracker.finish(run, now):
                finished = True
                self.stale.add((run["repo"], run["workflow_id"]))  # one more for the estimate
        for run_id in gone:
            self.tracker.forget(run_id)
        for run in active:
            if (run["repo"], run["workflow_id"]) not in self.history:
                self.stale.add((run["repo"], run["workflow_id"]))
        if finished and not self.app.cfg["hidden"]:
            self.app.sound.play()
        self.refresh()
        soon = self.tracker.is_active() or self.stale
        GLib.timeout_add_seconds(POLL_ACTIVE if soon else POLL_IDLE, self.start_poll)
        return False

    def tick(self):
        if self.rows or self.tracker.runs:
            self.refresh()
        return True

    # -- layout
    def estimate(self, run):
        history = self.history.get((run["repo"], run["workflow_id"]))
        return median_duration(history, run.get("head_branch")) if history else None

    def text(self, s, font):
        layout = self.area.create_pango_layout(s)
        layout.set_font_description(Pango.FontDescription.from_string(font))
        return layout

    def refresh(self):
        cfg = self.app.cfg
        if cfg["hidden"] or not cfg["ci"]:
            self.hide()
            return
        now = time.time()
        multi = len(cfg["ci_repos"]) > 1
        tiles = self.tracker.tiles(now)[-MAX_ROWS:]
        if not tiles:
            self.hide()
            return
        self.rows = []
        for t in tiles:
            caption, timing, fraction, state = describe(t, self.estimate(t.run), now, multi)
            self.rows.append((self.text(caption, "Sans Bold 8"), self.text(timing, "Sans 8"),
                              fraction, state))
        size = cfg["ci_size"]
        self.tile_w = round(size * min(1.8, max(0.6, self.gif.aspect())))
        self.pill_w = max([max(c.get_pixel_size()[0], s.get_pixel_size()[0])
                           for c, s, _f, _st in self.rows] + [110]) + 2 * PAD
        n = len(self.rows)
        content_w, content_h = self.pill_w + H_GAP + self.tile_w, n * size + (n - 1) * V_GAP
        self.win_w, self.win_h = content_w + 2 * EDGE, content_h + 2 * EDGE
        if any(st in ("running", "over", "queued", "waiting") for *_x, st in self.rows):
            self.gif.start()
        else:
            self.gif.stop()
        self.area.set_size_request(self.win_w, self.win_h)
        self.win.resize(self.win_w, self.win_h)
        if not self.win.get_visible():
            if self.monitor or self.app.pinned_monitor:
                GtkLayerShell.set_monitor(self.win, self.monitor or self.app.pinned_monitor)
            self.place()
            self.win.show_all()
            self.win.get_window().input_shape_combine_region(cairo.Region(), 0, 0)
        self.invalidate()

    def hide(self):
        self.rows = []
        self.gif.stop()
        self.win.hide()

    def geometry(self):
        if self.monitor:
            g = self.monitor.get_geometry()
            return g.x, g.y, g.width, g.height
        return self.app.monitor_geometry()

    def bottom(self):
        return self.app.cfg["margin"] + panel_offset(self.app.panels, self.geometry())

    def place(self):
        """Above any panel at the bottom of the monitor (and riding with an auto-hide one)."""
        GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.RIGHT, self.app.cfg["margin"])
        GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.BOTTOM, self.bottom())

    # -- hover: the whole column fades together
    def rects(self):
        if not self.rows:
            return []
        mx, my, mw, mh = self.geometry()
        x = mx + mw - self.app.cfg["margin"] - self.win_w + EDGE
        y = my + mh - self.bottom() - self.win_h + EDGE
        return [f"ci:{x},{y},{self.win_w - 2 * EDGE},{self.win_h - 2 * EDGE}"]

    def hover(self, _name, on):
        self.fade.to(self.app.cfg["hover_opacity"] if on else 1.0)

    # -- drawing: everything but the GIF frames is cached in one layer, redrawn each second
    def invalidate(self, *_):
        self.layer = None
        self.area.queue_draw()

    def on_gif_frame(self):
        if not self.frame_pending:  # at most ~30 redraws a second
            self.frame_pending = True
            GLib.timeout_add(33, self.flush_frame)

    def flush_frame(self):
        self.frame_pending = False
        self.area.queue_draw()
        return False

    def row_y(self, i):
        size = self.app.cfg["ci_size"]
        return EDGE + i * (size + V_GAP)

    def draw(self, _w, cr):
        if not self.rows:
            return True
        size = (self.area.get_allocated_width(), self.area.get_allocated_height())
        if self.layer is None or self.layer[0] != size:
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, size[0]), max(1, size[1]))
            self.paint_layer(cairo.Context(surf))
            self.layer = (size, surf)
        cr.set_source_surface(self.layer[1], 0, 0)
        cr.paint()
        h = self.app.cfg["ci_size"]
        for i, (_c, _s, _f, state) in enumerate(self.rows):
            if state in GLYPHS:
                continue
            pad = max(2, round(h * 0.08))
            surf = self.gif.surface(max(1, self.tile_w - 2 * pad), max(1, h - 2 * pad))
            cr.set_source_surface(surf, EDGE + self.pill_w + H_GAP + pad, self.row_y(i) + pad)
            # a queued run's runner hasn't picked it up yet: dimmer
            cr.paint_with_alpha(self.fade.value * (0.45 if state in ("queued", "waiting") else 1))
        return True

    def paint_layer(self, cr):
        a, h = self.fade.value, self.app.cfg["ci_size"]
        for i, (caption, timing, fraction, state) in enumerate(self.rows):
            color, y = COLORS[state], self.row_y(i)
            # the pill: name, time, progress bar
            rounded(cr, EDGE, y, self.pill_w, h, 10)
            cr.set_source_rgba(*FILL, a)
            cr.fill_preserve()
            neon_stroke(cr, a, width=1, glow=0.5, color=color)
            ch, sh = caption.get_pixel_size()[1], timing.get_pixel_size()[1]
            top = y + (h - ch - sh - (BAR_H + 3 if fraction is not None else 0)) / 2
            cr.set_source_rgba(1, 1, 1, a)
            cr.move_to(EDGE + PAD, top)
            PangoCairo.show_layout(cr, caption)
            cr.set_source_rgba(*(0.6 + 0.4 * c for c in color), a)
            cr.move_to(EDGE + PAD, top + ch)
            PangoCairo.show_layout(cr, timing)
            if fraction is not None:
                bx, by, bw = EDGE + PAD, top + ch + sh + 3, self.pill_w - 2 * PAD
                cr.rectangle(bx, by, bw, BAR_H)
                cr.set_source_rgba(1, 1, 1, 0.12 * a)
                cr.fill()
                cr.rectangle(bx, by, bw * max(0.0, min(1.0, fraction)), BAR_H)
                cr.set_source_rgba(*color, a)
                cr.fill()
            # the tile: the GIF goes in it (see draw), or how it ended
            tx = EDGE + self.pill_w + H_GAP
            rounded(cr, tx, y, self.tile_w, h, min(10, h * 0.22))
            cr.set_source_rgba(*FILL, a)
            cr.fill_preserve()
            neon_stroke(cr, a, color=color)
            if state in GLYPHS:
                glyph = self.text(GLYPHS[state], f"Sans Bold {max(8, round(h * 0.4))}")
                gw, gh = glyph.get_pixel_size()
                cr.set_source_rgba(*color, a)
                cr.move_to(tx + (self.tile_w - gw) / 2, y + (h - gh) / 2)
                PangoCairo.show_layout(cr, glyph)


def rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
