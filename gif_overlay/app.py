"""
gif-overlay — animated GIF pinned to a screen corner, above every window (KDE Wayland).

  * transparent background (GIF/WebP alpha is respected)
  * hover -> fades out and lets clicks pass through to whatever is underneath
  * desktop notifications become speech bubbles coming from the GIF
  * sits on top of the taskbar and rides up and down with an auto-hide panel
  * resizable / hideable from outside (bind these to KDE global shortcuts)

usage:   gif-overlay ~/Pictures/thing.gif [--scale 0.3] [--margin 20] ...
         gif-overlay                       (uses the settings saved last time)
control: gif-overlay --bigger | --smaller | --reset-size | --toggle | --stop
config:  ~/.config/gif-overlay/config.json
"""
import argparse, json, os, signal, sys, time, warnings

from gif_overlay.helpers import make_note, panel_offset, parse_panels

warnings.filterwarnings("ignore", category=DeprecationWarning)

CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                      "gif-overlay", "config.json")
PIDFILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "gif-overlay.pid")
DEFAULTS = dict(gif=None, scale=0.3, step=0.05, margin=20, hover_opacity=0.25, bubbles=True,
                monitor=None, pixel=False, hidden=False)
# GLib.unix_signal_add only supports HUP, INT, TERM, USR1, USR2 and WINCH
COMMANDS = {"bigger": signal.SIGUSR1, "smaller": signal.SIGUSR2, "reset_size": signal.SIGWINCH,
            "toggle": signal.SIGHUP, "stop": signal.SIGTERM}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: cfg[k] for k in DEFAULTS}, f, indent=2)
    os.replace(tmp, CONFIG)


def running_pid():
    try:
        pid = int(open(PIDFILE).read())
        if b"gif-overlay" in open(f"/proc/{pid}/cmdline", "rb").read():
            return pid
    except (OSError, ValueError):
        pass
    return None


# ---------------------------------------------------------------- options
cfg = load_config()
p = argparse.ArgumentParser(description="Animated GIF pinned above all windows.")
p.add_argument("gif", nargs="?", help="GIF/WebP to show (saved; omit to reuse the last one)")
p.add_argument("--scale", type=float, help="size relative to the GIF's own size (default 0.3)")
p.add_argument("--step", type=float, help="how much --bigger/--smaller change the scale")
p.add_argument("--margin", type=int, help="distance from the corner (px)")
p.add_argument("--hover-opacity", type=float, help="opacity while hovered (0-1)")
p.add_argument("--bubbles", action=argparse.BooleanOptionalAction,
               help="show notifications as speech bubbles instead of Plasma popups")
p.add_argument("--monitor", help="monitor index (0, 1, 2...) or part of its make/model, "
                                  "e.g. ASUS (-1 = let KWin pick)")
p.add_argument("--pixel", action=argparse.BooleanOptionalAction,
               help="nearest-neighbour scaling (for pixel art)")
g = p.add_mutually_exclusive_group()
for name in COMMANDS:
    g.add_argument("--" + name.replace("_", "-"), action="store_true",
                   help="tell the running instance to " + name.replace("_", " "))
args = p.parse_args()

for name, sig in COMMANDS.items():
    if getattr(args, name):
        pid = running_pid()
        if not pid:
            sys.exit("gif-overlay is not running")
        os.kill(pid, sig)
        sys.exit(0)

if running_pid():
    sys.exit(f"gif-overlay is already running (pid {running_pid()}); use --stop first")

# command-line flags override the saved config and get saved
for k in DEFAULTS:
    v = getattr(args, k, None)
    if v is not None:
        cfg[k] = os.path.abspath(os.path.expanduser(v)) if k == "gif" else v
if cfg["monitor"] is not None and str(cfg["monitor"]).lstrip("-").isdigit():
    cfg["monitor"] = int(cfg["monitor"])
if cfg["monitor"] == -1:
    cfg["monitor"] = None
if not cfg["gif"]:
    p.error("no GIF given and none saved in " + CONFIG)
if args.gif:
    cfg["hidden"] = False
save_config(cfg)

from gif_overlay.gtkbits import (Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkLayerShell, Tween, cairo,
                                 clear_bg)
from gif_overlay.bubble import Bubble


# ---------------------------------------------------------------- window
win = Gtk.Window()
GtkLayerShell.init_for_window(win)
GtkLayerShell.set_namespace(win, "gif-overlay")
GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.BOTTOM, True)
GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.RIGHT, True)
GtkLayerShell.set_margin(win, GtkLayerShell.Edge.BOTTOM, cfg["margin"])
GtkLayerShell.set_margin(win, GtkLayerShell.Edge.RIGHT, cfg["margin"])
GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.NONE)
def find_monitor(want):
    display = Gdk.Display.get_default()
    if isinstance(want, int):
        return display.get_monitor(want)
    for i in range(display.get_n_monitors()):
        mon = display.get_monitor(i)
        if want.lower() in f"{mon.get_manufacturer()} {mon.get_model()}".lower():
            return mon
    return None

pinned_monitor = find_monitor(cfg["monitor"]) if cfg["monitor"] is not None else None
if cfg["monitor"] is not None:
    if pinned_monitor:
        GtkLayerShell.set_monitor(win, pinned_monitor)
    else:
        print(f"gif-overlay: monitor {cfg['monitor']!r} not found, letting KWin pick",
              file=sys.stderr)

# transparency: RGBA visual + transparent CSS + clear the background on every frame
screen = win.get_screen()
visual = screen.get_rgba_visual()
if visual:
    win.set_visual(visual)
win.set_app_paintable(True)
css = Gtk.CssProvider()
css.load_from_data(b"window, drawingarea { background: transparent; }")
Gtk.StyleContext.add_provider_for_screen(screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

win.connect("draw", clear_bg)

# the GIF is drawn by hand (instead of Gtk.Image) so it can be scaled
anim = GdkPixbuf.PixbufAnimation.new_from_file(cfg["gif"])
W, H = anim.get_width(), anim.get_height()
frames = anim.get_iter(None)
area = Gtk.DrawingArea()
win.add(area)

# the loader hands back the same pixbuf object for each frame, so every frame is
# scaled once into a surface and then just blitted (scaling per frame costs ~15% CPU)
cache = {}

def scaled(pixbuf):
    if pixbuf not in cache:
        if len(cache) > 300:  # a loader that makes a new pixbuf per frame: don't leak
            cache.clear()
        w, h = size()
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        cr.scale(w / W, h / H)
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_NEAREST if cfg["pixel"] else cairo.FILTER_GOOD)
        cr.paint()
        cache[pixbuf] = surf
    return cache[pixbuf]

def draw_gif(_w, cr):
    cr.set_source_surface(scaled(frames.get_pixbuf()), 0, 0)
    cr.paint()
    return True
area.connect("draw", draw_gif)

def next_frame():
    frames.advance(None)
    area.queue_draw()
    schedule_frame()
    return False

def schedule_frame():
    delay = frames.get_delay_time()
    if delay >= 0 and not anim.is_static_image():
        GLib.timeout_add(max(delay, 20), next_frame)

schedule_frame()


# ---------------------------------------------------------------- size
def size():
    return max(1, round(W * cfg["scale"])), max(1, round(H * cfg["scale"]))

def apply_size():
    cache.clear()
    w, h = size()
    area.set_size_request(w, h)
    win.resize(w, h)  # lets the layer surface shrink, not only grow
    moved()

def rescale(delta=None):
    cfg["scale"] = DEFAULTS["scale"] if delta is None else \
        min(5.0, max(0.05, round(cfg["scale"] + delta, 3)))
    apply_size()
    save_config(cfg)
    return True

def set_clickthrough():
    win.get_window().input_shape_combine_region(cairo.Region(), 0, 0)


# ---------------------------------------------------------------- taskbar -> sit on top of it
# The KWin script below reports the visible panels; we stay `margin` px above the ones
# at the bottom of our monitor, so an auto-hide panel carries the GIF up and down.
panels = []         # visible panel rects (x, y, w, h), as last reported by KWin
place_listeners = []  # called whenever the GIF's bottom margin changes

def bottom_margin():
    return cfg["margin"] + int(dock.value)

def moved():
    for listener in place_listeners:
        listener()

def apply_bottom(_value):
    GtkLayerShell.set_margin(win, GtkLayerShell.Edge.BOTTOM, bottom_margin())
    moved()

dock = Tween(0.0, apply_bottom, duration=0.25)

def monitor_geometry():
    display = Gdk.Display.get_default()
    gdkwin = win.get_window()
    mon = pinned_monitor or (display.get_monitor_at_window(gdkwin) if gdkwin
                             else display.get_monitor(0))
    g = mon.get_geometry()
    return g.x, g.y, g.width, g.height

def update_dock():
    dock.to(panel_offset(panels, monitor_geometry()))


# ---------------------------------------------------------------- hover -> fade (KWin script)
# Wayland doesn't let apps read the global cursor position, and a click-through
# window gets no pointer events at all. KWin knows where the cursor is, though:
# a tiny KWin script (loaded at runtime) watches it, asks us for our Rect() and
# calls Hover(true/false) on us over D-Bus. So the window is always click-through.
# The same script reports the visible panels with Panels("x,y,w,h;...").
BUS_NAME = BUS_IFACE = "local.GifOverlay"
BUS_PATH = "/local/GifOverlay"
KWIN_SCRIPT = "gif-overlay-kwin"
OLD_KWIN_SCRIPTS = ["gif-overlay-hover"]
KWIN_JS = """// loaded at runtime by gif-overlay: tells it when the cursor is over it
// and which panels are visible
const SVC = "@NAME@", PATH = "@PATH@", IFACE = "@IFACE@";
let rect = null, rectAt = 0, askedAt = 0, inside = false;
const timer = new QTimer();
timer.interval = 300;

function evaluate() {
    const p = workspace.cursorPos;
    const now = !!rect && p.x >= rect[0] && p.x < rect[0] + rect[2]
                       && p.y >= rect[1] && p.y < rect[1] + rect[3];
    if (now !== inside) {
        inside = now;
        callDBus(SVC, PATH, IFACE, "Hover", inside);
        if (inside) timer.start(); else timer.stop();
    }
}

// the rect changes when resized / hidden / docked, so refresh it every 250ms at most
function check() {
    const t = Date.now();
    if (t - rectAt < 250) return evaluate();
    if (t - askedAt < 1000) return;  // a Rect() call is already in flight
    askedAt = t;
    callDBus(SVC, PATH, IFACE, "Rect", function (r) {
        rect = String(r).split(",").map(Number);
        rectAt = Date.now();
        askedAt = 0;
        evaluate();
    });
}

timer.timeout.connect(check);  // keeps checking while hovered: the GIF may move away
workspace.cursorPosChanged.connect(check);

let lastPanels = null;
function sendPanels() {
    const rects = workspace.windowList().filter(w => w.dock && !w.hidden).map(w => {
        const g = w.frameGeometry;
        return [g.x, g.y, g.width, g.height].map(Math.round).join(",");
    }).join(";");
    if (rects === lastPanels) return;
    lastPanels = rects;
    callDBus(SVC, PATH, IFACE, "Panels", rects);
}

function watchPanel(w) {
    if (!w.dock) return;
    w.hiddenChanged.connect(sendPanels);  // auto-hide panels slide in / out
    w.frameGeometryChanged.connect(sendPanels);
}

workspace.windowList().forEach(watchPanel);
workspace.windowAdded.connect(w => { watchPanel(w); sendPanels(); });
workspace.windowRemoved.connect(sendPanels);
sendPanels();
"""
BUS_XML = f"""<node><interface name="{BUS_IFACE}">
  <method name="Hover"><arg type="b" direction="in"/></method>
  <method name="Rect"><arg type="s" direction="out"/></method>
  <method name="Panels"><arg type="s" direction="in"/></method>
</interface></node>"""
opacity = Tween(1.0, area.set_opacity, duration=0.20)

def screen_rect():
    """'x,y,w,h' of the GIF in KWin's (logical, global) coordinates."""
    gdkwin = win.get_window()
    if cfg["hidden"] or not gdkwin:
        return "0,0,0,0"
    g = Gdk.Display.get_default().get_monitor_at_window(gdkwin).get_geometry()
    w, h = size()
    return f"{g.x + g.width - cfg['margin'] - w},{g.y + g.height - bottom_margin() - h},{w},{h}"

def on_bus_call(_conn, _sender, _path, _iface, method, params, invocation):
    if method == "Hover":
        opacity.to(cfg["hover_opacity"] if params.unpack()[0] else 1.0)
        invocation.return_value(None)
    elif method == "Panels":
        panels[:] = parse_panels(params.unpack()[0])
        update_dock()
        invocation.return_value(None)
    else:
        invocation.return_value(GLib.Variant("(s)", (screen_rect(),)))

bus = Gio.bus_get_sync(Gio.BusType.SESSION)
bus.register_object(BUS_PATH, Gio.DBusNodeInfo.new_for_xml(BUS_XML).interfaces[0],
                    on_bus_call, None, None)
Gio.bus_own_name_on_connection(bus, BUS_NAME, Gio.BusNameOwnerFlags.NONE, None, None)

def kwin_scripting(path, iface, method, sig=None, values=None):
    return bus.call_sync("org.kde.KWin", path, iface, method,
                         GLib.Variant(sig, values) if sig else None, None,
                         Gio.DBusCallFlags.NONE, -1, None)

def load_kwin_script():
    path = os.path.join(os.path.dirname(PIDFILE), KWIN_SCRIPT + ".js")
    with open(path, "w") as f:
        f.write(KWIN_JS.replace("@NAME@", BUS_NAME).replace("@PATH@", BUS_PATH)
                       .replace("@IFACE@", BUS_IFACE))
    try:
        unload_kwin_script()
        sid = kwin_scripting("/Scripting", "org.kde.kwin.Scripting", "loadScript",
                             "(ss)", (path, KWIN_SCRIPT)).unpack()[0]
        kwin_scripting(f"/Scripting/Script{sid}", "org.kde.kwin.Script", "run")
    except GLib.Error as e:
        print("gif-overlay: couldn't load the KWin script, no hover fade or taskbar "
              "following:", e.message, file=sys.stderr)

def unload_kwin_script():
    for name in [KWIN_SCRIPT] + OLD_KWIN_SCRIPTS:
        kwin_scripting("/Scripting", "org.kde.kwin.Scripting", "unloadScript", "(s)", (name,))


# ---------------------------------------------------------------- notifications -> speech bubbles
# Plasma stays the notification server (history, tray bell). While the GIF is visible we
# switch on its Do Not Disturb with Inhibit() and eavesdrop every Notify() call through a
# D-Bus monitor connection, showing it as a speech bubble instead. Plasma drops the
# inhibition by itself when we leave the bus, so even a crash brings its popups back.
NOTIFY_RULE = "type='method_call',interface='org.freedesktop.Notifications',member='Notify'"
NOTIFICATIONS = "org.freedesktop.Notifications"
listening = False  # True once the monitor connection is up; never Inhibit without it
dnd_cookie = None

def on_monitored(_conn, message, incoming):
    if not incoming or message.get_message_type() != Gio.DBusMessageType.METHOD_CALL:
        return message  # our own BecomeMonitor call and its reply
    try:
        if message.get_member() == "Notify":
            app, replaces, _icon, summary, body, _actions, hints, _timeout = \
                message.get_body().unpack()
            GLib.idle_add(on_notification, make_note(app, replaces, summary, body, hints))
    except Exception as e:  # runs on GDBus's worker thread: never let it raise
        print("gif-overlay: couldn't read a notification:", e, file=sys.stderr)
    return None  # monitored calls aren't ours to answer

def start_listening():
    global listening, monitor_conn
    try:
        monitor_conn = Gio.DBusConnection.new_for_address_sync(
            Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        monitor_conn.add_filter(on_monitored)
        monitor_conn.connect("closed", on_monitor_closed)
        monitor_conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                               "org.freedesktop.DBus.Monitoring", "BecomeMonitor",
                               GLib.Variant("(asu)", ([NOTIFY_RULE], 0)), None,
                               Gio.DBusCallFlags.NONE, -1, None)
        listening = True
    except GLib.Error as e:
        print("gif-overlay: can't watch notifications, keeping Plasma's popups:", e.message,
              file=sys.stderr)

def on_monitor_closed(*_):
    """Lost the eavesdropping connection: give the notifications back to Plasma."""
    global listening
    def give_back():
        global listening
        listening = False
        print("gif-overlay: notification monitor disconnected, back to Plasma's popups",
              file=sys.stderr)
        bubble.clear()
        set_dnd(False)
        return False
    GLib.idle_add(give_back)  # "closed" may be emitted off the main thread

def on_server_appeared(*_):
    """Plasma's notification server (re)started: our old cookie died with it."""
    if not cfg["hidden"]:
        set_dnd(True)

def on_server_vanished(*_):
    global dnd_cookie
    dnd_cookie = None

def set_dnd(on):
    """Silence Plasma's popups while we show bubbles (and only then)."""
    global dnd_cookie
    try:
        if on and dnd_cookie is None and listening and cfg["bubbles"]:
            dnd_cookie = bus.call_sync(
                NOTIFICATIONS, "/org/freedesktop/Notifications", NOTIFICATIONS, "Inhibit",
                GLib.Variant("(ssa{sv})", ("gif-overlay", "Showing notifications as speech "
                                           "bubbles", {})),
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
        elif not on and dnd_cookie is not None:
            bus.call_sync(NOTIFICATIONS, "/org/freedesktop/Notifications", NOTIFICATIONS,
                          "UnInhibit", GLib.Variant("(u)", (dnd_cookie,)), None,
                          Gio.DBusCallFlags.NONE, -1, None)
            dnd_cookie = None
    except GLib.Error as e:
        print("gif-overlay: couldn't switch Plasma's Do Not Disturb:", e.message,
              file=sys.stderr)

def on_notification(note):
    if not cfg["hidden"] and cfg["bubbles"] and listening:
        bubble.enqueue(note)
    return False


bubble = Bubble(sys.modules[__name__])


# ---------------------------------------------------------------- show / hide / quit
def show():
    win.show_all()
    set_clickthrough()
    update_dock()
    set_dnd(True)

def hide():
    win.hide()
    bubble.clear()
    set_dnd(False)  # hidden: Plasma shows its own popups again

def toggle():
    cfg["hidden"] = not cfg["hidden"]
    hide() if cfg["hidden"] else show()
    save_config(cfg)
    return True

def quit_(*_):
    Gtk.main_quit()
    return False

GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, rescale, cfg["step"])
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, rescale, -cfg["step"])
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGWINCH, lambda: rescale())
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, toggle)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, quit_)
GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, quit_)
win.connect("destroy", quit_)

start_listening()
Gio.bus_watch_name_on_connection(bus, NOTIFICATIONS, Gio.BusNameWatcherFlags.NONE,
                                 on_server_appeared, on_server_vanished)
apply_size()
if not cfg["hidden"]:
    show()
with open(PIDFILE, "w") as f:
    f.write(str(os.getpid()))
load_kwin_script()
try:
    Gtk.main()
finally:
    set_dnd(False)
    try:
        unload_kwin_script()
    except GLib.Error:
        pass
    if running_pid() == os.getpid():
        os.remove(PIDFILE)
