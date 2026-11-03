"""A corner of a chosen monitor: one tile per running GitHub Actions run."""
import math, os, sys, threading, time

from gif_overlay.agents_view import Gif
from gif_overlay.bubble import EDGE
from gif_overlay.ci import CiTracker, GitHub, describe, median_duration, poll
from gif_overlay.gtkbits import (NEON, Gdk, GLib, Gtk, GtkLayerShell, Pango, PangoCairo, Tween,
                                 cairo, clear_bg, neon_stroke)
from gif_overlay.helpers import panel_offset

POLL_ACTIVE, POLL_IDLE = 10, 30  # seconds between polls
MAX_ROWS = 8
V_GAP, H_GAP, PAD = 6, 6, 8
CAPTION_MAX_W = 220  # long branch names get "…"
STACK_GAP = 10       # between this column and the agents column below it
BAR_H = 3
FILL = (0.043, 0.055, 0.078)
COLORS = {"running": NEON, "queued": NEON, "waiting": NEON, "over": (1.0, 0.66, 0.1),
          "success": (0.2, 1.0, 0.45), "failure": (1.0, 0.24, 0.32), "cancelled": (0.6, 0.65, 0.7)}
GLYPHS = {"success": "✓", "failure": "✗", "cancelled": "–"}


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
        self.stacked = None        # (shown, height) the agents column was last moved for
        self.fade = Tween(1.0, self.invalidate, duration=0.2)
        self.layer = None
        self.frame_pending = False
        try:
            self.gif = Gif(os.path.expanduser(app.cfg["ci_gif"]), self.on_gif_frame)
        except GLib.Error as e:
            print("gif-overlay: can't load the CI GIF, using the main one:", e.message,
                  file=sys.stderr)
            self.gif = Gif(app.cfg["gif"], self.on_gif_frame)
        self.monitor = None        # the monitor it's shown on (set when it shows)
        self.warned_monitor = False
        display = Gdk.Display.get_default()
        display.connect("monitor-added", self.on_monitors_changed)
        display.connect("monitor-removed", self.on_monitors_changed)

        self.win = Gtk.Window()
        GtkLayerShell.init_for_window(self.win)
        GtkLayerShell.set_namespace(self.win, "gif-overlay-ci")
        GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)
        # the corner: newest run nearest to it, the tile on the screen-edge side of its pill
        corner = app.cfg["ci_corner"]
        self.top, self.left = corner.startswith("top"), corner.endswith("left")
        self.v_edge = GtkLayerShell.Edge.TOP if self.top else GtkLayerShell.Edge.BOTTOM
        self.h_edge = GtkLayerShell.Edge.LEFT if self.left else GtkLayerShell.Edge.RIGHT
        GtkLayerShell.set_anchor(self.win, self.v_edge, True)
        GtkLayerShell.set_anchor(self.win, self.h_edge, True)
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.NONE)
        if app.visual:
            self.win.set_visual(app.visual)
        self.win.set_app_paintable(True)
        self.win.connect("draw", clear_bg)
        # its monitor going away closes a layer surface; keep the window, just hide it
        self.win.connect("delete-event", lambda *_: self.hide() or True)
        self.area = Gtk.DrawingArea()
        self.area.connect("draw", self.draw)
        self.win.add(self.area)
        app.place_listeners.append(self.place)  # stacked above the GIF when sharing its monitor
        if app.cfg["ci"] and app.cfg["ci_repos"]:
            GLib.idle_add(self.start_poll)
            GLib.timeout_add_seconds(1, self.tick)  # timers count, finished tiles expire

    # -- polling (network on a worker thread, results back on the GTK thread)
    def start_poll(self):
        if self.busy:
            return False
        tracked = [(t.run["repo"], t.run["id"]) for t in self.tracker.runs.values()
                   if t.finished_at is None]
        args = (self.gh, list(self.app.cfg["ci_repos"]), tracked, sorted(self.stale))
        self.busy = True

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
        delay = POLL_IDLE
        try:
            delay = self.apply(result)
        except Exception as e:  # never let one bad answer stop the polling
            print("gif-overlay: CI runs:", repr(e), file=sys.stderr)
        finally:
            GLib.timeout_add_seconds(delay, self.start_poll)
        return False

    def apply(self, result):
        """Take in one poll's results; returns the seconds until the next poll."""
        errors = [str(result)] if isinstance(result, Exception) else result[4]
        if errors and not self.failing:
            print("gif-overlay: couldn't read some CI runs (will keep trying):",
                  "; ".join(errors), file=sys.stderr)
        elif not errors and self.failing:
            print("gif-overlay: reading the CI runs again", file=sys.stderr)
        self.failing = bool(errors)
        if isinstance(result, Exception):
            return POLL_IDLE
        active, ended, gone, found, _errors = result
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
        return POLL_ACTIVE if self.tracker.is_active() or self.stale else POLL_IDLE

    def tick(self):
        try:
            if self.rows or self.tracker.runs:
                self.refresh()
        except Exception as e:  # keep ticking
            print("gif-overlay: CI runs:", repr(e), file=sys.stderr)
        return True

    # -- which monitor: the configured one, else the GIF's
    def pick_monitor(self):
        want = self.app.cfg["ci_monitor"]
        mon = self.app.find_monitor(want) if want is not None else None
        if want is not None and not mon and not self.warned_monitor:
            print(f"gif-overlay: CI monitor {want!r} not found, using the GIF's",
                  file=sys.stderr)
            self.warned_monitor = True
        if mon:
            self.warned_monitor = False
            return mon
        gdkwin = self.app.win.get_window()
        return self.app.pinned_monitor or (Gdk.Display.get_default().get_monitor_at_window(gdkwin)
                                           if gdkwin else None)

    def on_monitors_changed(self, *_):
        if self.win.get_visible():  # show it again on the right monitor
            self.hide()
            GLib.idle_add(lambda: self.refresh() and False)

    # -- layout
    def estimate(self, run):
        history = self.history.get((run["repo"], run["workflow_id"]))
        return median_duration(history, run.get("head_branch")) if history else None

    def text(self, s, font, max_w=None):
        layout = self.area.create_pango_layout(s)
        layout.set_font_description(Pango.FontDescription.from_string(font))
        if max_w:
            layout.set_width(max_w * Pango.SCALE)
            layout.set_ellipsize(Pango.EllipsizeMode.END)
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
        if self.top:  # newest at the top, nearest the corner
            tiles.reverse()
        self.rows = []
        for t in tiles:
            caption, timing, fraction, state = describe(t, self.estimate(t.run), now, multi)
            self.rows.append((self.text(caption, "Sans Bold 8", CAPTION_MAX_W),
                              self.text(timing, "Sans 8"),
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
            self.monitor = self.pick_monitor()
            if self.monitor:
                GtkLayerShell.set_monitor(self.win, self.monitor)
            self.place()
            self.win.show_all()
            gdkwin = self.win.get_window()
            if gdkwin:
                gdkwin.input_shape_combine_region(cairo.Region(), 0, 0)
        self.invalidate()
        self.restack()

    def hide(self):
        self.rows = []
        self.gif.stop()
        self.win.hide()
        self.restack()

    # -- sharing a corner with the agents column: it goes right below this one
    def stack_height(self, corner, geometry):
        """How far a column in that corner of that monitor must move down to clear this one."""
        if not self.rows or self.app.cfg["ci_corner"] != corner or self.geometry() != geometry:
            return 0
        return self.win_h - 2 * EDGE + STACK_GAP

    def restack(self):
        shown = (bool(self.rows), self.win_h)
        if shown != self.stacked:
            self.stacked = shown
            self.app.agents.restack()

    def geometry(self):
        if self.monitor:
            g = self.monitor.get_geometry()
            return g.x, g.y, g.width, g.height
        return self.app.monitor_geometry()

    def vertical_margin(self):
        """Distance from the top, or from the bottom: above any panel there (riding with an
        auto-hide one), and above the GIF when sharing its corner."""
        if self.top:
            return self.app.cfg["margin"]
        if not self.left and (self.monitor is None
                              or self.geometry() == self.app.monitor_geometry()):
            return self.app.bottom_margin() + self.app.size()[1] + V_GAP
        return self.app.cfg["margin"] + panel_offset(self.app.panels, self.geometry())

    def place(self):
        GtkLayerShell.set_margin(self.win, self.h_edge, self.app.cfg["margin"])
        GtkLayerShell.set_margin(self.win, self.v_edge, self.vertical_margin())

    # -- hover: the whole column fades together
    def rects(self):
        if not self.rows:
            return []
        mx, my, mw, mh = self.geometry()
        m, v = self.app.cfg["margin"], self.vertical_margin()
        x = (mx + m if self.left else mx + mw - m - self.win_w) + EDGE
        y = (my + v if self.top else my + mh - v - self.win_h) + EDGE
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

    def xs(self):
        """(pill x, tile x)"""
        if self.left:
            return EDGE + self.tile_w + H_GAP, EDGE
        return EDGE, EDGE + self.pill_w + H_GAP

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
            cr.set_source_surface(surf, self.xs()[1] + pad, self.row_y(i) + pad)
            # a queued run's runner hasn't picked it up yet: dimmer
            cr.paint_with_alpha(self.fade.value * (0.45 if state in ("queued", "waiting") else 1))
        return True

    def paint_layer(self, cr):
        a, h = self.fade.value, self.app.cfg["ci_size"]
        for i, (caption, timing, fraction, state) in enumerate(self.rows):
            color, y = COLORS[state], self.row_y(i)
            # the pill: name, time, progress bar
            px, tx = self.xs()
            rounded(cr, px, y, self.pill_w, h, 10)
            cr.set_source_rgba(*FILL, a)
            cr.fill_preserve()
            neon_stroke(cr, a, width=1, glow=0.5, color=color)
            ch, sh = caption.get_pixel_size()[1], timing.get_pixel_size()[1]
            top = y + (h - ch - sh - (BAR_H + 3 if fraction is not None else 0)) / 2
            cr.set_source_rgba(1, 1, 1, a)
            cr.move_to(px + PAD, top)
            PangoCairo.show_layout(cr, caption)
            cr.set_source_rgba(*(0.6 + 0.4 * c for c in color), a)
            cr.move_to(px + PAD, top + ch)
            PangoCairo.show_layout(cr, timing)
            if fraction is not None:
                bx, by, bw = px + PAD, top + ch + sh + 3, self.pill_w - 2 * PAD
                cr.rectangle(bx, by, bw, BAR_H)
                cr.set_source_rgba(1, 1, 1, 0.12 * a)
                cr.fill()
                cr.rectangle(bx, by, bw * max(0.0, min(1.0, fraction)), BAR_H)
                cr.set_source_rgba(*color, a)
                cr.fill()
            # the tile: the GIF goes in it (see draw), or how it ended
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
