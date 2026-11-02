"""The bubble sound, with other apps' audio dimmed ("ducked") while it plays."""
import json, os, subprocess, sys, time, wave

from gif_overlay.gtkbits import Gio, GLib
from gif_overlay.helpers import duck_targets, scaled_volumes

DUCK_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "gif-overlay-duck.json")
FADE_DOWN_STEPS, FADE_DOWN_MS = 3, 50   # ~150ms down
FADE_UP_STEPS, FADE_UP_MS = 6, 70       # ~400ms back up


class Sound:
    def __init__(self, app):
        self.app = app             # the gif_overlay.app module: cfg
        self.saved = None          # other apps' volumes before we dimmed them, while dimmed
        self.hold_until = 0.0
        self.durations = {}
        self.restore_leftovers()

    # -- playing
    def path(self):
        path = os.path.expanduser(self.app.cfg["sound"] or "")
        return path if path and os.path.isfile(path) else None

    def duration(self, path):
        if path not in self.durations:
            try:
                with wave.open(path) as w:
                    self.durations[path] = w.getnframes() / w.getframerate()
            except (wave.Error, OSError, EOFError):
                self.durations[path] = 1.0  # not a WAV: dim for a sensible while
        return self.durations[path]

    def play(self):
        path = self.path()
        if not path:
            return
        try:
            Gio.Subprocess.new(["paplay", "--client-name=gif-overlay", path],
                               Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as e:
            print("gif-overlay: couldn't play the sound:", e.message, file=sys.stderr)
            return
        if self.app.cfg["duck"] < 1:
            self.duck(self.duration(path))

    # -- ducking
    def duck(self, seconds):
        self.hold_until = max(self.hold_until, time.monotonic() + seconds + 0.25)
        if self.saved is not None:
            return  # already dimmed: just stay down longer
        self.saved = duck_targets(self.sink_inputs())
        if not self.saved:
            self.saved = None
            return
        with open(DUCK_FILE, "w") as f:  # so a crash mid-dip can't leave the music quiet
            json.dump(self.saved, f)
        ratio = self.app.cfg["duck"]
        for step in range(1, FADE_DOWN_STEPS + 1):
            GLib.timeout_add(step * FADE_DOWN_MS, self.set_scaled,
                             1 - (1 - ratio) * step / FADE_DOWN_STEPS)
        GLib.timeout_add(100, self.wait_for_end)

    def wait_for_end(self):
        if time.monotonic() < self.hold_until:
            return True
        ratio = self.app.cfg["duck"]
        for step in range(1, FADE_UP_STEPS):
            GLib.timeout_add(step * FADE_UP_MS, self.set_scaled,
                             ratio + (1 - ratio) * step / FADE_UP_STEPS)
        GLib.timeout_add(FADE_UP_STEPS * FADE_UP_MS, self.restore)
        return False

    def set_scaled(self, ratio):
        if self.saved:
            self.set_volumes(scaled_volumes(self.saved, ratio))
        return False

    def restore(self):
        if self.saved:
            self.set_volumes(self.saved)  # exactly what they were, no rounding drift
        self.saved = None
        try:
            os.remove(DUCK_FILE)
        except OSError:
            pass
        return False

    def restore_leftovers(self):
        try:
            with open(DUCK_FILE) as f:
                saved = {int(k): v for k, v in json.load(f).items()}
        except (OSError, ValueError, AttributeError):
            return
        self.set_volumes(saved)
        os.remove(DUCK_FILE)

    # -- pactl
    def sink_inputs(self):
        try:
            out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True,
                                 timeout=1, check=True).stdout
            return json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError):
            return []

    def set_volumes(self, volumes):
        for index, channels in volumes.items():
            try:
                Gio.Subprocess.new(["pactl", "set-sink-input-volume", str(index)]
                                   + [str(v) for v in channels],
                                   Gio.SubprocessFlags.STDOUT_SILENCE
                                   | Gio.SubprocessFlags.STDERR_SILENCE)
            except GLib.Error:
                pass
