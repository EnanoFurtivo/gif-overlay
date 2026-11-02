"""The main GIF's speech bubble: notifications shown one at a time, typed out."""
import math, time

from gif_overlay.gtkbits import (Gdk, Gio, GLib, Gtk, GtkLayerShell, Pango, PangoCairo,
                                 Tween, cairo, clear_bg, hex_rgb, neon_stroke)
from gif_overlay.helpers import BubbleQueue, display_seconds

MAX_W, MIN_W, PAD, RADIUS, TAIL_H = 340, 140, 14, 14, 16
EDGE = 6  # room around a shape for its neon glow
BUBBLE_GAP = 4  # between the main bubble's tail tip and the GIF
FILL, TEXT, MUTED = "#0b0e14", "#e8fbff", "#5fe9f5"


def launch_app(note):
    """Bring up the app that sent a notification (single-instance apps just raise)."""
    ids = [note["entry"]] if note["entry"] else []
    if note["app"]:
        ids += [hit[0] for hit in Gio.DesktopAppInfo.search(note["app"])[:1] if hit]
    for desktop_id in ids:
        try:
            info = Gio.DesktopAppInfo.new(
                desktop_id if desktop_id.endswith(".desktop") else desktop_id + ".desktop")
        except TypeError:
            info = None
        if info:
            info.launch([], Gdk.Display.get_default().get_app_launch_context())
            return


class Bubble:
    """The speech bubble above the GIF: shows queued notes one at a time, typed out."""
    TAIL_H, EDGE = TAIL_H, EDGE
    CPS = 80          # typing speed, characters per second
    GAP_MS = 400      # pause between two bubbles

    def __init__(self, app):
        self.app = app             # the gif_overlay.app module: cfg, win, size()...
        self.queue = BubbleQueue(maxlen=10)
        self.note = None           # the note on screen (also while fading out)
        self.typed = 0.0           # characters revealed so far
        self.step_src = None       # the one running timer: typing, display, fade or gap
        self.display_left = 0.0    # display seconds still owed (kept while hovered)
        self.display_from = None   # when the display timer started; None if not running
        self.hovered = False
        self.typing_done = False
        self.box = (1, 1)          # bubble body size, without tail and stroke room

        self.win = Gtk.Window()
        GtkLayerShell.init_for_window(self.win)
        GtkLayerShell.set_namespace(self.win, "gif-overlay-bubble")
        GtkLayerShell.set_layer(self.win, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_anchor(self.win, GtkLayerShell.Edge.RIGHT, True)
        GtkLayerShell.set_keyboard_mode(self.win, GtkLayerShell.KeyboardMode.NONE)
        if self.app.visual:
            self.win.set_visual(self.app.visual)
        self.win.set_app_paintable(True)
        self.win.connect("draw", clear_bg)
        self.area = Gtk.DrawingArea()
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK
                             | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.area.connect("draw", self.draw)
        self.area.connect("button-press-event", self.on_click)
        self.area.connect("enter-notify-event", self.on_enter)
        self.area.connect("leave-notify-event", self.on_leave)
        self.win.add(self.area)
        self.fade = Tween(0.0, self.area.set_opacity, duration=0.20)
        self.rise = Tween(0.0, lambda _v: self.place(), duration=0.25)
        self.app.place_listeners.append(self.place)

    # -- queue
    def enqueue(self, note):
        if self.note and note["replaces"] and note["app"] == self.note["app"]:
            self.show(note)  # an update of what's on screen: say it again
        elif self.note or self.step_src:
            self.queue.push(note)
        else:
            self.show(note)

    def clear(self):
        self.cancel_step()
        self.queue.clear()
        self.note, self.display_from, self.hovered = None, None, False
        self.win.hide()

    def cancel_step(self):
        if self.step_src:
            GLib.source_remove(self.step_src)
            self.step_src = None

    # -- text
    def layouts(self, typed=None):
        note = self.note
        return bubble_layouts(self.area, note["app"], note["summary"], note["body"], typed)

    # -- showing one note
    def show(self, note):
        self.cancel_step()
        self.note, self.typed, self.display_from = note, 0.0, None
        self.typing_done = False
        self.hovered = False  # a fresh window: no leave event will come for the old one
        self.box = bubble_box(*self.layouts(), bool(note["app"]))
        w, h = self.box[0] + self.TAIL_H + 2 * self.EDGE, self.box[1] + 2 * self.EDGE
        self.area.set_size_request(w, h)
        self.win.resize(w, h)
        display = Gdk.Display.get_default()
        mon = self.app.pinned_monitor or (display.get_monitor_at_window(self.app.win.get_window())
                                 if self.app.win.get_window() else None)
        if mon and not self.win.get_visible():
            GtkLayerShell.set_monitor(self.win, mon)
        self.rise.value = 12.0
        self.place()
        self.win.show_all()
        self.win.get_window().input_shape_combine_region(cairo.Region(cairo.RectangleInt(
            self.EDGE, self.EDGE, self.box[0], self.box[1])), 0, 0)
        self.fade.value = 0.0
        self.area.set_opacity(0.0)
        self.fade.to(1.0)
        self.rise.to(0.0)
        self.last_tick = time.monotonic()
        self.step_src = GLib.timeout_add(16, self.type_tick)

    def window_height(self):
        return self.box[1] + 2 * EDGE

    def window_bottom(self):
        """Bottom margin that centres the bubble on the GIF (or sits on its baseline if taller)."""
        return self.app.bottom_margin() + max(0, (self.app.size()[1] - self.window_height()) // 2)

    def place(self):
        """To the GIF's left, tail pointing right at it."""
        GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.RIGHT,
                                 self.app.cfg["margin"] + self.app.size()[0] + BUBBLE_GAP)
        GtkLayerShell.set_margin(self.win, GtkLayerShell.Edge.BOTTOM,
                                 self.window_bottom() + int(self.rise.value))

    def type_tick(self):
        now = time.monotonic()
        self.typed += self.CPS * (now - self.last_tick)
        self.last_tick = now
        self.area.queue_draw()
        total = len(self.note["summary"]) + len(self.note["body"])
        if self.typed < total:
            return True
        self.typed = total
        self.typing_done = True
        self.step_src = None
        self.display_left = display_seconds(self.note["summary"] + " " + self.note["body"])
        if not self.hovered:
            self.start_display()
        return False

    def start_display(self):
        self.display_from = time.monotonic()
        self.step_src = GLib.timeout_add(int(self.display_left * 1000), self.finish)

    def finish(self):
        self.cancel_step()
        self.display_from = None
        self.fade.to(0.0)
        self.step_src = GLib.timeout_add(220, self.after_fade)
        return False

    def after_fade(self):
        self.win.hide()
        self.note = None
        self.step_src = GLib.timeout_add(self.GAP_MS, self.next)
        return False

    def next(self):
        self.step_src = None
        note = self.queue.pop()
        if note:
            self.show(note)
        return False

    # -- mouse
    def on_click(self, _w, event):
        if not self.note:
            return True
        if event.button == 1:
            launch_app(self.note)
        self.finish()
        return True

    def on_enter(self, *_):
        self.hovered = True
        if self.display_from is not None:  # pause the display timer
            self.cancel_step()
            self.display_left -= time.monotonic() - self.display_from
            self.display_from = None
        return False

    def on_leave(self, *_):
        self.hovered = False
        if self.note and self.step_src is None and self.typing_done and self.display_from is None:
            self.display_left = max(self.display_left, 1.5)
            self.start_display()
        return False

    # -- drawing
    def draw(self, _w, cr):
        if not self.note:
            return True
        bw, bh = self.box
        # tail at the GIF's middle height, measured from the window's bottom edge
        gif_mid = self.app.bottom_margin() + self.app.size()[1] / 2 - self.window_bottom()
        ty = min(max(self.window_height() - gif_mid, EDGE + 18), EDGE + bh - 18)
        paint_bubble(cr, EDGE, EDGE, bw, bh, ("right", ty), *self.layouts(self.typed),
                     bool(self.note["app"]))
        return True


# ---------------------------------------------------------------- shared bubble drawing
def bubble_layouts(widget, header_text, summary, body, typed=None, max_w=MAX_W):
    """(header, speech) Pango layouts; `typed` limits how much of the speech is revealed."""
    esc = lambda s: GLib.markup_escape_text(s, -1)
    header = widget.create_pango_layout("")
    header.set_font_description(Pango.FontDescription.from_string("Sans Bold 8"))
    header.set_markup(f"<span foreground='{MUTED}'>{esc(header_text)}</span>", -1)
    header.set_width((max_w - 2 * PAD) * Pango.SCALE)
    header.set_ellipsize(Pango.EllipsizeMode.END)
    n = len(summary) + len(body) if typed is None else int(typed)
    parts = []
    if summary:
        parts.append(f"<b>{esc(summary[:n])}</b>")
    if body and n > len(summary):
        parts.append(esc(body[:n - len(summary)]))
    speech = widget.create_pango_layout("")
    speech.set_font_description(Pango.FontDescription.from_string("Sans 10"))
    speech.set_width((max_w - 2 * PAD) * Pango.SCALE)
    speech.set_wrap(Pango.WrapMode.WORD_CHAR)
    speech.set_height(-8)  # at most 8 lines, then "…"
    speech.set_ellipsize(Pango.EllipsizeMode.END)
    speech.set_markup("\n".join(parts), -1)
    return header, speech


def bubble_box(header, speech, has_header, max_w=MAX_W):
    """(w, h) of the bubble body for these layouts (no tail, no stroke room)."""
    (hw, hh), (sw, sh) = header.get_pixel_size(), speech.get_pixel_size()
    hh = hh + 2 if has_header else 0
    return min(max_w, max(MIN_W, max(hw, sw) + 2 * PAD)), hh + sh + 2 * PAD


def paint_bubble(cr, x0, y0, bw, bh, tail, header, speech, has_header):
    """Bubble body at (x0, y0) sized (bw, bh); tail = ("down", x), ("left", y) or ("right", y)."""
    r, (side, at) = RADIUS, tail
    x1, y1 = x0 + bw, y0 + bh
    cr.new_sub_path()
    cr.arc(x1 - r, y0 + r, r, -math.pi / 2, 0)
    if side == "right":
        cr.line_to(x1, at - 9)
        cr.line_to(x1 + TAIL_H, at - 2)
        cr.line_to(x1, at + 9)
    cr.arc(x1 - r, y1 - r, r, 0, math.pi / 2)
    if side == "down":
        cr.line_to(at + 9, y1)
        cr.line_to(at + 4, y1 + TAIL_H)
        cr.line_to(at - 9, y1)
    cr.arc(x0 + r, y1 - r, r, math.pi / 2, math.pi)
    if side == "left":
        cr.line_to(x0, at + 9)
        cr.line_to(x0 - TAIL_H, at + 2)
        cr.line_to(x0, at - 9)
    cr.arc(x0 + r, y0 + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.set_source_rgba(*hex_rgb(FILL))
    cr.fill_preserve()
    neon_stroke(cr)
    y = y0 + PAD
    cr.set_source_rgba(*hex_rgb(TEXT))
    if has_header:
        cr.move_to(x0 + PAD, y)
        PangoCairo.show_layout(cr, header)
        y += header.get_pixel_size()[1] + 2
    cr.move_to(x0 + PAD, y)
    PangoCairo.show_layout(cr, speech)
