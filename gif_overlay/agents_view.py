"""Top-left corner: one sprite per active Claude agent, plus "needs you" bubbles."""
import glob, json, math, os, re, sys, time

from gif_overlay.agents import (INDENT, AgentTracker, format_elapsed, gif_for_level, layout,
                                level_gifs)
from gif_overlay.bubble import EDGE, TAIL_H, bubble_box, bubble_layouts, paint_bubble
from gif_overlay.gtkbits import (Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkLayerShell, Pango,
                                 PangoCairo, Tween, cairo, clear_bg, neon_stroke)

SESSIONS_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                            "Claude", "claude-code-sessions")
BUBBLE_MAX_W = 300
CAPTION_MAX_W = 160
CHAT_URL = "claude://code/needs-input?session={}"
CAPTION_PAD_X, CAPTION_PAD_Y = 6, 2
BADGE_OVERHANG = 8  # timer badges stick out of the tile corner by this much
SMALL_TILE = 36     # tiles smaller than this get their badge beside them
BADGE_SIDE_W = 44   # room for a badge beside a small tile
LOCAL_ID = re.compile(r"local_[0-9a-f-]+")



def pill(cr, x, y, w, h):
    """Rounded-end rectangle path."""
    r = h / 2
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, math.pi / 2)
    cr.arc(x + r, y + r, r, math.pi / 2, 3 * math.pi / 2)
    cr.close_path()


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
        self.placements, self.bubbles, self.captions, self.started = [], {}, {}, {}
        self.fade = Tween(1.0, self.invalidate, duration=0.2)
        self.layers = None         # (size, under, over) cached drawing; GIF frames go between
        self.frame_pending = False
        self.was_waiting = set()
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
        elif self.placements:
            self.invalidate()  # the timer badges
        return True

    def chat(self, sid):
        """(desktop local id, title) of a Claude Code session, or (None, "") for CLI ones."""
        known = self.chats.get(sid)
        # re-read now and then: a new chat only gets its title after the first prompt
        if (known is None or not known[1]) and time.monotonic() - self.chats_read_at > 5:
            self.chats_read_at = time.monotonic()
            for path in glob.glob(os.path.join(SESSIONS_DIR, "*", "*", "local_*.json")):
                try:
                    with open(path) as f:
                        d = json.load(f)
                    if LOCAL_ID.fullmatch(d["sessionId"]):  # it goes into a claude:// link
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
                self.gifs[path] = Gif(path, self.on_gif_frame)
            except GLib.Error:
                self.gifs[path] = Gif(self.app.cfg["gif"], self.on_gif_frame)
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
        waiting = {s.id for s in sessions if s.waiting}
        if waiting - self.was_waiting:  # someone new needs you: say "a"
            self.app.sound.play()
        self.was_waiting = waiting
        self.bubbles = {s.id: bubble_layouts(self.area, self.session_name(s), "", s.waiting,
                                             max_w=BUBBLE_MAX_W)
                        for s in sessions if s.waiting}
        self.captions = {s.id: self.caption_layout(s) for s in sessions}
        size = self.app.cfg["agent_size"]

        def aspect(depth):
            return self.gif(self.path_for(depth)).aspect()

        def caption_size(sid):
            w, h = self.captions[sid].get_pixel_size()
            return w + 2 * CAPTION_PAD_X, h + 2 * CAPTION_PAD_Y  # room for the pill

        def bubble_size(sid):
            bw, bh = bubble_box(*self.bubbles[sid], True, max_w=BUBBLE_MAX_W)
            return bw + TAIL_H + 2 * EDGE, bh + 2 * EDGE

        trees = {s.id: self.tracker.tree(s) for s in sessions}
        rows = [{"id": s.id, "agents": [d for _a, d in trees[s.id]], "waiting": bool(s.waiting)}
                for s in sessions]
        # when each tile's agent appeared, for its timer badge (index -1 = the session)
        self.started = {(s.id, -1): s.first_seen for s in sessions}
        self.started.update({(s.id, i): s.agent_started.get(agent, s.first_seen)
                             for s in sessions for i, (agent, _d) in enumerate(trees[s.id])})
        self.placements, w, h = layout(rows, size, aspect, bubble_size, caption_size)
        used = {self.path_for(p.depth) for p in self.placements if p.kind == "sprite"}
        for path, g in self.gifs.items():
            g.start() if path in used else g.stop()
        for path in used:
            self.gif(path).start()

        # everything is drawn EDGE in, so glows fit; timer badges overhang tile corners
        side = max([p.x + p.w + BADGE_SIDE_W for p in self.placements
                    if p.kind == "sprite" and p.h < SMALL_TILE] + [0])
        w, h = max(w + BADGE_OVERHANG, side) + 2 * EDGE, h + 2 * EDGE + BADGE_OVERHANG
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
                region.union(cairo.RectangleInt(EDGE + p.x, EDGE + p.y, p.w, p.h))
        self.win.get_window().input_shape_combine_region(region, 0, 0)
        self.invalidate()

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
        return [f"agents:{mx + m + EDGE},{my + m + EDGE},{w},{h}"]

    def hover(self, _name, on):
        self.fade.to(self.app.cfg["hover_opacity"] if on else 1.0)

    # -- drawing and clicks
    def draw_tree_lines(self, cr):
        """File-tree elbows from each sprite down to the subagents it spawned."""
        cr.push_group()
        for i, p in enumerate(self.placements):
            if p.kind != "sprite" or p.index < 0:
                continue
            parent = next(q for q in reversed(self.placements[:i])
                          if q.kind == "sprite" and q.session_id == p.session_id
                          and q.depth == p.depth - 1)
            top = parent.y + parent.h
            if parent.index < 0:  # a session: start under its name tag
                top = max(q.y + q.h for q in self.placements[:i]
                          if q.kind == "caption" and q.session_id == p.session_id)
            trunk, mid = parent.x + INDENT // 2, p.y + p.h / 2
            cr.move_to(trunk, top + 2)
            cr.line_to(trunk, mid)
            cr.line_to(p.x - 2, mid)
        neon_stroke(cr)
        cr.pop_group_to_source()
        cr.paint_with_alpha(self.fade.value)

    def draw_tile(self, cr, p):
        """Dark rounded tile with a neon border (the GIF is painted into it per frame)."""
        a, r = self.fade.value, min(10, p.h * 0.22)
        cr.new_sub_path()
        cr.arc(p.x + p.w - r, p.y + r, r, -math.pi / 2, 0)
        cr.arc(p.x + p.w - r, p.y + p.h - r, r, 0, math.pi / 2)
        cr.arc(p.x + r, p.y + p.h - r, r, math.pi / 2, math.pi)
        cr.arc(p.x + r, p.y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(0.043, 0.055, 0.078, a)
        cr.fill_preserve()
        neon_stroke(cr, a)

    def draw_badge(self, cr, p):
        """Neon pill on the tile's bottom-right corner: how long this agent has been around."""
        started = self.started.get((p.session_id, p.index))
        if started is None:
            return
        a = self.fade.value
        layout = self.area.create_pango_layout(format_elapsed(time.monotonic() - started))
        layout.set_font_description(Pango.FontDescription.from_string(
            "Sans Bold 7" if p.depth == 0 else "Sans Bold 6"))
        tw, th = layout.get_pixel_size()
        bw, bh = tw + 8, th + 2
        if p.h < SMALL_TILE:  # beside small tiles, so it doesn't cover them
            x, y = p.x + p.w + 4, p.y + (p.h - bh) / 2
        else:                  # on the corner of bigger ones
            x, y = p.x + p.w - bw + BADGE_OVERHANG, p.y + p.h - bh + BADGE_OVERHANG // 2
        pill(cr, x, y, bw, bh)
        cr.set_source_rgba(0.043, 0.055, 0.078, a)
        cr.fill_preserve()
        neon_stroke(cr, a, width=1, glow=0.45)
        cr.set_source_rgba(0.72, 1.0, 1.0, a)
        cr.move_to(x + 4, y + 1)
        PangoCairo.show_layout(cr, layout)

    # -- drawing: the neon tiles, lines and text are drawn once into two cached layers;
    # each GIF frame only paints the frames between them (glow passes are expensive)
    def invalidate(self, *_):
        self.layers = None
        self.area.queue_draw()

    def on_gif_frame(self):
        if not self.frame_pending:  # at most ~30 redraws a second, however many GIFs tick
            self.frame_pending = True
            GLib.timeout_add(33, self.flush_frame)

    def flush_frame(self):
        self.frame_pending = False
        self.area.queue_draw()
        return False

    def draw(self, _w, cr):
        size = (self.area.get_allocated_width(), self.area.get_allocated_height())
        if self.layers is None or self.layers[0] != size:
            self.layers = (size, self.render(size, self.draw_under), self.render(size, self.draw_over))
        _size, under, over = self.layers
        cr.set_source_surface(under, 0, 0)
        cr.paint()
        for p in self.placements:
            if p.kind == "sprite":
                pad = max(2, round(p.h * 0.08))
                surf = self.gif(self.path_for(p.depth)).surface(max(1, p.w - 2 * pad),
                                                                max(1, p.h - 2 * pad))
                cr.set_source_surface(surf, EDGE + p.x + pad, EDGE + p.y + pad)
                cr.paint_with_alpha(self.fade.value)
        cr.set_source_surface(over, 0, 0)
        cr.paint()
        return True

    def render(self, size, painter):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, size[0]), max(1, size[1]))
        cr = cairo.Context(surf)
        cr.translate(EDGE, EDGE)
        painter(cr)
        return surf

    def draw_under(self, cr):
        self.draw_tree_lines(cr)
        for p in self.placements:
            if p.kind == "sprite":
                self.draw_tile(cr, p)

    def draw_over(self, cr):
        for p in self.placements:
            if p.kind == "sprite":
                self.draw_badge(cr, p)
            elif p.kind == "caption":  # name tag: white text on a dark neon pill
                a = self.fade.value
                pill(cr, p.x, p.y, p.w, p.h)
                cr.set_source_rgba(0.043, 0.055, 0.078, a)
                cr.fill_preserve()
                neon_stroke(cr, a, width=1, glow=0.5)
                cr.set_source_rgba(1, 1, 1, a)
                cr.move_to(p.x + CAPTION_PAD_X, p.y + CAPTION_PAD_Y)
                PangoCairo.show_layout(cr, self.captions[p.session_id])
            elif p.kind == "more":
                layout = self.area.create_pango_layout(f"+{p.index}")
                layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 10"))
                tw, th = layout.get_pixel_size()
                cr.set_source_rgba(0.043, 0.055, 0.078, self.fade.value)
                cr.rectangle(p.x, p.y, p.w, p.h)
                cr.fill_preserve()
                neon_stroke(cr, self.fade.value)
                cr.set_source_rgba(1, 1, 1, self.fade.value)
                cr.move_to(p.x + (p.w - tw) / 2, p.y + (p.h - th) / 2)
                PangoCairo.show_layout(cr, layout)
            elif p.kind == "bubble":
                header, speech = self.bubbles[p.session_id]
                x0, y0 = p.x + EDGE + TAIL_H, p.y + EDGE
                bw, bh = p.w - TAIL_H - 2 * EDGE, p.h - 2 * EDGE
                session_sprite = next(q for q in self.placements
                                      if q.kind == "sprite" and q.session_id == p.session_id
                                      and q.index == -1)
                tail_y = min(max(session_sprite.y + session_sprite.h / 2, y0 + 16), y0 + bh - 16)
                paint_bubble(cr, x0, y0, bw, bh, ("left", tail_y), header, speech, True)
        return True

    def on_click(self, _w, event):
        for p in self.placements:
            x, y = event.x - EDGE, event.y - EDGE
            if p.kind == "bubble" and p.x <= x < p.x + p.w and p.y <= y < p.y + p.h:
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
