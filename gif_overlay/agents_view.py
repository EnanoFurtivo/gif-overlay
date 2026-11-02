"""Top-left corner: one sprite per active Claude agent, plus "needs you" bubbles."""
import glob, json, os, sys, time

from gif_overlay.agents import AgentTracker, layout, session_gifs
from gif_overlay.bubble import STROKE, TAIL_H, bubble_box, bubble_layouts, paint_bubble
from gif_overlay.gtkbits import (Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkLayerShell, Pango,
                                 PangoCairo, Tween, cairo, clear_bg)

SESSIONS_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                            "Claude", "claude-code-sessions")
BUBBLE_MAX_W = 300
CHAT_URL = "claude://code/needs-input?session={}"


class Gif:
    """One GIF file: a shared animation clock and frames scaled once per size."""
    def __init__(self, path, on_frame):
        self.anim = GdkPixbuf.PixbufAnimation.new_from_file(path)
        self.w, self.h = self.anim.get_width(), self.anim.get_height()
        self.frames = self.anim.get_iter(None)
        self.on_frame, self.src, self.cache = on_frame, None, {}

    def aspect(self):
        return self.w / self.h

    def start(self):
        if self.src is None and not self.anim.is_static_image():
            self.src = GLib.timeout_add(max(self.frames.get_delay_time(), 20), self.tick)

    def stop(self):
        if self.src:
            GLib.source_remove(self.src)
            self.src = None

    def tick(self):
        self.frames.advance(None)
        self.on_frame()
        delay = self.frames.get_delay_time()
        self.src = GLib.timeout_add(max(delay, 20), self.tick) if delay >= 0 else None
        return False

    def surface(self, w, h):
        pixbuf = self.frames.get_pixbuf()
        key = (pixbuf, w, h)
        if key not in self.cache:
            if len(self.cache) > 400:
                self.cache.clear()
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
            cr = cairo.Context(surf)
            cr.scale(w / self.w, h / self.h)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
            self.cache[key] = surf
        return self.cache[key]


class AgentsView:
    def __init__(self, app):
        self.app = app             # the gif_overlay.app module: cfg, monitor_geometry()...
        self.tracker = AgentTracker()
        self.gif_paths = self.scan_gifs()
        self.gifs = {}             # path -> Gif, loaded on first use
        self.chats = {}            # Claude Code session id -> (desktop local id, title)
        self.chats_read_at = 0.0
        self.placements, self.picks, self.bubbles = [], {}, {}
        self.fades = {}            # sprite name -> Tween of its opacity
        self.warned = False
        self.shown_key = None

        self.win = Gtk.Window()
        GtkLayerShell.init_for_window(self.win)
        GtkLayerShell.set_namespace(self.win, "gif-overlay-agents")
        GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.NONE)
        if app.visual:
            self.win.set_visual(app.visual)
        self.win.set_app_paintable(True)
        self.win.connect("draw", clear_bg)
        self.area = Gtk.DrawingArea()
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.area.connect("draw", self.draw)
        self.area.connect("button-press-event", self.on_click)
        self.win.add(self.area)
        GLib.timeout_add_seconds(1, self.tick)  # idle grace and stale sessions expire

    # -- inputs
    def scan_gifs(self):
        folder = os.path.expanduser(self.app.cfg["gifs_dir"])
        return sorted(p for ext in ("gif", "webp") for p in glob.glob(os.path.join(folder, "*." + ext)))

    def event(self, text):
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except ValueError as e:
            if not self.warned:
                print("gif-overlay: ignoring an unreadable agent event:", e, file=sys.stderr)
                self.warned = True
            return
        if self.tracker.event(payload, time.monotonic()):
            self.refresh()

    def tick(self):
        if self.placements and self.rows_key() != self.shown_key:
            self.refresh()
        return True

    def chat(self, sid):
        """(desktop local id, title) of a Claude Code session, or (None, "") for CLI ones."""
        if sid not in self.chats and time.monotonic() - self.chats_read_at > 5:
            self.chats_read_at = time.monotonic()
            for path in glob.glob(os.path.join(SESSIONS_DIR, "*", "*", "local_*.json")):
                try:
                    with open(path) as f:
                        d = json.load(f)
                    self.chats[d["cliSessionId"]] = (d["sessionId"], d.get("title") or "")
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return self.chats.get(sid, (None, ""))

    # -- layout
    def rows_key(self):
        return [(s.id, tuple(s.agents.values()), s.waiting) for s in
                self.tracker.active(time.monotonic())]

    def gif(self, path):
        if path not in self.gifs:
            try:
                self.gifs[path] = Gif(path, self.area.queue_draw)
            except GLib.Error:
                self.gifs[path] = Gif(self.app.cfg["gif"], self.area.queue_draw)
        return self.gifs[path]

    def bubble_layouts(self, session):
        local, title = self.chat(session.id)
        header = title or os.path.basename(session.cwd.rstrip("/")) or "Claude"
        return bubble_layouts(self.area, header, "", session.waiting or "", max_w=BUBBLE_MAX_W)

    def refresh(self):
        if self.app.cfg["hidden"] or not self.app.cfg["agents"]:
            self.hide()
            return
        sessions = self.tracker.active(time.monotonic())
        self.shown_key = self.rows_key()
        if not sessions:
            self.hide()
            return
        main = self.app.cfg["gif"]
        self.picks = {s.id: session_gifs(self.gif_paths, s.id, max(s.agents.values(), default=0),
                                         main) for s in sessions}
        self.bubbles = {s.id: self.bubble_layouts(s) for s in sessions if s.waiting}
        size = self.app.cfg["agent_size"]

        def aspect(sid, depth):
            return self.gif(self.picks[sid][depth]).aspect()

        def bubble_size(sid):
            bw, bh = bubble_box(*self.bubbles[sid], True, max_w=BUBBLE_MAX_W)
            return bw + TAIL_H + 2 * STROKE, bh + 2 * STROKE

        rows = [{"id": s.id, "agents": list(s.agents.values()), "waiting": bool(s.waiting)}
                for s in sessions]
        self.placements, w, h = layout(rows, size, aspect, bubble_size)
        used = {p for picks in self.picks.values() for p in picks}
        for path, g in self.gifs.items():
            g.start() if path in used else g.stop()
        for path in used:
            self.gif(path).start()

        self.area.set_size_request(w, h)
        self.win.resize(w, h)
        if not self.win.get_visible():
            display = Gdk.Display.get_default()
            gdkwin = self.app.win.get_window()
            mon = self.app.pinned_monitor or (display.get_monitor_at_window(gdkwin) if gdkwin
                                              else None)
            if mon:
                GtkLayerShell.set_monitor(self.win, mon)
            m = self.app.cfg["margin"]
            GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.TOP, m)
            GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.LEFT, m)
            self.win.show_all()
        region = cairo.Region()
        for p in self.placements:
            if p.kind == "bubble":  # only bubbles take clicks; sprites are click-through
                region.union(cairo.RectangleInt(p.x, p.y, p.w, p.h))
        self.win.get_window().input_shape_combine_region(region, 0, 0)
        self.area.queue_draw()

    def hide(self):
        self.placements = []
        self.shown_key = None
        for g in self.gifs.values():
            g.stop()
        self.win.hide()

    # -- hover (names come from rects())
    def name(self, p):
        return f"agent:{p.session_id}:{p.index}"

    def rects(self):
        """'name:x,y,w,h' of every sprite in KWin's global coordinates."""
        if not self.placements:
            return []
        mx, my, _, _ = self.app.monitor_geometry()
        m = self.app.cfg["margin"]
        return [f"{self.name(p)}:{mx + m + p.x},{my + m + p.y},{p.w},{p.h}"
                for p in self.placements if p.kind == "sprite"]

    def hover(self, name, on):
        fade = self.fades.get(name)
        if fade is None:
            fade = self.fades[name] = Tween(1.0, lambda _v: self.area.queue_draw(), duration=0.2)
        fade.to(self.app.cfg["hover_opacity"] if on else 1.0)

    # -- drawing and clicks
    def draw(self, _w, cr):
        for p in self.placements:
            if p.kind == "sprite":
                surf = self.gif(self.picks[p.session_id][p.depth]).surface(p.w, p.h)
                cr.set_source_surface(surf, p.x, p.y)
                fade = self.fades.get(self.name(p))
                cr.paint_with_alpha(fade.value if fade else 1.0)
            elif p.kind == "more":
                layout = self.area.create_pango_layout(f"+{p.index}")
                layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 10"))
                tw, th = layout.get_pixel_size()
                cr.set_source_rgba(0.1, 0.1, 0.1, 0.7)
                cr.rectangle(p.x, p.y, p.w, p.h)
                cr.fill()
                cr.set_source_rgba(1, 1, 1, 1)
                cr.move_to(p.x + (p.w - tw) / 2, p.y + (p.h - th) / 2)
                PangoCairo.show_layout(cr, layout)
            elif p.kind == "bubble":
                header, speech = self.bubbles[p.session_id]
                x0, y0 = p.x + STROKE + TAIL_H, p.y + STROKE
                bw, bh = p.w - TAIL_H - 2 * STROKE, p.h - 2 * STROKE
                session_sprite = next(q for q in self.placements
                                      if q.session_id == p.session_id and q.index == -1)
                tail_y = min(max(session_sprite.y + session_sprite.h / 2, y0 + 16), y0 + bh - 16)
                paint_bubble(cr, x0, y0, bw, bh, ("left", tail_y), header, speech, True)
        return True

    def on_click(self, _w, event):
        for p in self.placements:
            if p.kind == "bubble" and p.x <= event.x < p.x + p.w and p.y <= event.y < p.y + p.h:
                local, _title = self.chat(p.session_id)
                if local and event.button == 1:
                    try:
                        Gio.AppInfo.launch_default_for_uri(
                            CHAT_URL.format(local),
                            Gdk.Display.get_default().get_app_launch_context())
                    except GLib.Error as e:
                        print("gif-overlay: couldn't open the chat:", e.message, file=sys.stderr)
                return True
        return True
