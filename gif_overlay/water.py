"""Every few minutes the main GIF reminds you to drink some water (until you click it)."""
import random, time

from gif_overlay.gtkbits import Gio, GLib

LINES = [
    "Hydration check! Grab a glass of water 💧",
    "Water break! Your brain is 75% water, top it up 🧠",
    "Sip sip! Time for some water 🥤",
    "Have you had water lately? Go on, I'll wait 💦",
    "Shark tip: even sharks stay hydrated 🦈",
    "a. (drink water)",
    "Glug glug glug — water time! 🌊",
    "Stretch, breathe, drink some water ✨",
]


def water_note():
    # "own": not from an app, so clicking it doesn't launch anything; "sticky": stays put
    return {"app": "💧 Water break", "summary": random.choice(LINES), "body": "", "entry": "",
            "replaces": 0, "sticky": True, "own": True}


class WaterReminder:
    def __init__(self, app):
        self.app = app             # the gif_overlay.app module: cfg, bubble, bus
        self.last = time.monotonic()
        self.locked = False
        # KDE's screen locker: skip reminders while locked, restart the countdown on unlock
        app.bus.signal_subscribe(None, "org.freedesktop.ScreenSaver", "ActiveChanged", None, None,
                                 Gio.DBusSignalFlags.NONE, self.on_lock_changed)
        GLib.timeout_add_seconds(30, self.tick)

    def on_lock_changed(self, _conn, _sender, _path, _iface, _signal, params):
        self.locked = bool(params.unpack()[0])
        if not self.locked:
            self.last = time.monotonic()

    def tick(self):
        minutes, now = self.app.cfg["water_minutes"], time.monotonic()
        if minutes and not self.app.cfg["hidden"] and not self.locked \
                and now - self.last >= minutes * 60:
            self.last = now
            if not self.app.bubble.has_sticky():  # one reminder at a time is plenty
                self.app.bubble.enqueue(water_note())
        return True
