"""Top-left corner: one sprite per active Claude agent, plus "needs you" bubbles."""
import glob, json, os, sys, time

from gif_overlay.agents import AgentTracker, gif_for_level, layout, level_gifs
from gif_overlay.bubble import STROKE, TAIL_H, bubble_box, bubble_layouts, paint_bubble
from gif_overlay.gtkbits import (Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkLayerShell, Pango,
                                 PangoCairo, Tween, cairo, clear_bg)

SESSIONS_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                            "Claude", "claude-code-sessions")
BUBBLE_MAX_W = 300
CAPTION_MAX_W = 160
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
        self.levels = {}           # level -> GIF path (level 1 = Claude sessions)
        self.gifs = {}             # path -> Gif, loaded on first use
        self.chats = {}            # Claude Code session id -> (desktop local id, title)
        self.chats_read_at = 0.0
        self.placements, self.bubbles, self.captions = [], {}, {}
        self.fade = Tween(1.0, lambda _v: self.area.queue_draw(), duration=0.2)
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
    def scan_levels(self):
        folder = os.path.expanduser(self.app.cfg["gifs_dir"])
        return level_gifs(glob.glob(os.path.join(folder, "*")))

    def path_for(self, depth):
        return gif_for_level(self.levels, depth + 1, self.app.cfg["gif"])

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

    def session_name(self, session):
        return (self.chat(session.id)[1] or os.path.basename(session.cwd.rstrip("/"))
                or "Claude")

    def caption_layout(self, session):
        layout = self.area.create_pango_layout(self.session_name(session))
        layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 8"))
        layout.set_width(CAPTION_MAX_W * Pango.SCALE)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        return layout

    def refresh(self):
        if self.app.cfg["hidden"] or not self.app.cfg["agents"]:
            self.hide()
            return
        sessions = self.tracker.active(time.monotonic())
        self.shown_key = self.rows_key()
        if not sessions:
            self.hide()
            return
        self.levels = self.scan_levels()  # cheap; lets new levelN.gif files show up live
        self.bubbles = {s.id: bubble_layouts(self.area, self.session_name(s), "", s.waiting,
                                             max_w=BUBBLE_MAX_W)
                        for s in sessions if s.waiting}
        self.captions = {s.id: self.caption_layout(s) for s in sessions}
        size = self.app.cfg["agent_size"]

        def aspect(depth):
            return self.gif(self.path_for(depth)).aspect()

        def caption_size(sid):
            w, h = self.captions[sid].get_pixel_size()
            return w + 4, h + 2  # room for the outline

        def bubble_size(sid):
            bw, bh = bubble_box(*self.bubbles[sid], True, max_w=BUBBLE_MAX_W)
            return bw + TAIL_H + 2 * STROKE, bh + 2 * STROKE

        rows = [{"id": s.id, "agents": [d for _a, d in self.tracker.tree(s)],
                 "waiting": bool(s.waiting)} for s in sessions]
        self.placements, w, h = layout(rows, size, aspect, bubble_size, caption_size)
        used = {self.path_for(p.depth) for p in self.placements if p.kind == "sprite"}
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

    # -- hover: the whole column (gaps included) is one area, everything fades together
    def rects(self):
        """['agents:x,y,w,h'] — the column of sprites and names, in KWin's coordinates."""
        boxes = [p for p in self.placements if p.kind != "bubble"]
        if not boxes:
            return []
        mx, my, _, _ = self.app.monitor_geometry()
        m = self.app.cfg["margin"]
        w = max(p.x + p.w for p in boxes)
        h = max(p.y + p.h for p in boxes)
        return [f"agents:{mx + m},{my + m},{w},{h}"]

    def hover(self, _name, on):
        self.fade.to(self.app.cfg["hover_opacity"] if on else 1.0)

    # -- drawing and clicks
    def draw(self, _w, cr):
        for p in self.placements:
            if p.kind == "sprite":
                surf = self.gif(self.path_for(p.depth)).surface(p.w, p.h)
                cr.set_source_surface(surf, p.x, p.y)
                cr.paint_with_alpha(self.fade.value)
            elif p.kind == "caption":  # name tag: white text with a dark outline
                cr.push_group()
                cr.move_to(p.x + 2, p.y + 1)
                PangoCairo.layout_path(cr, self.captions[p.session_id])
                cr.set_source_rgba(0.08, 0.08, 0.08, 0.9)
                cr.set_line_width(3)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
                cr.stroke_preserve()
                cr.set_source_rgba(1, 1, 1, 1)
                cr.fill()
                cr.pop_group_to_source()
                cr.paint_with_alpha(self.fade.value)
            elif p.kind == "more":
                layout = self.area.create_pango_layout(f"+{p.index}")
                layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 10"))
                tw, th = layout.get_pixel_size()
                cr.set_source_rgba(0.1, 0.1, 0.1, 0.7 * self.fade.value)
                cr.rectangle(p.x, p.y, p.w, p.h)
                cr.fill()
                cr.set_source_rgba(1, 1, 1, self.fade.value)
                cr.move_to(p.x + (p.w - tw) / 2, p.y + (p.h - th) / 2)
                PangoCairo.show_layout(cr, layout)
            elif p.kind == "bubble":
                header, speech = self.bubbles[p.session_id]
                x0, y0 = p.x + STROKE + TAIL_H, p.y + STROKE
                bw, bh = p.w - TAIL_H - 2 * STROKE, p.h - 2 * STROKE
                session_sprite = next(q for q in self.placements
                                      if q.kind == "sprite" and q.session_id == p.session_id
                                      and q.index == -1)
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
