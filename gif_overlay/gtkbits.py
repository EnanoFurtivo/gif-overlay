"""GTK-side building blocks shared by the windows (import after argparse: loads GTK)."""
import time

import cairo
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkLayerShell, Pango, PangoCairo


class Tween:
    """Smoothly animates a number and calls apply(value) on every frame."""
    def __init__(self, value, apply, duration=0.30):
        self.value, self.apply, self.duration, self.src = value, apply, duration, None

    def to(self, target):
        if self.src:
            GLib.source_remove(self.src)
        start_v, start_t = self.value, time.monotonic()

        def tick():
            t = min(1.0, (time.monotonic() - start_t) / self.duration)
            ease = 1 - (1 - t) ** 3  # ease-out cubic
            self.value = start_v + (target - start_v) * ease
            self.apply(self.value)
            if t >= 1.0:
                self.src = None
                return False
            return True

        self.src = GLib.timeout_add(16, tick)


def clear_bg(_w, cr):
    cr.set_source_rgba(0, 0, 0, 0)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)
    return False


def hex_rgb(color):
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)) + (1.0,)
