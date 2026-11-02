# gif-overlay

A little animated GIF that lives in the bottom-right corner of your KDE Plasma (Wayland)
desktop, above every window — and reads your notifications out loud as speech bubbles,
Clippy style.

- **Always on top**, including fullscreen windows; never takes focus or shows in the taskbar.
- **Transparent**: the GIF's alpha channel is respected.
- **Click-through**: clicks go to whatever is underneath; hovering fades it to 25%.
- **Speech bubbles**: while the GIF is visible, desktop notifications appear as bubbles
  coming from it (typed out, one at a time). Click a bubble to open the app that sent it;
  hover it to keep it on screen. Hide the GIF and Plasma's normal popups come back.
- **Sits on the taskbar**: it stays above the panel, and rides up and down with an
  auto-hide panel.
- **Resizable / hideable** from global shortcuts; remembers its settings.

## Requirements

KDE Plasma 6 on Wayland, Python 3, and:

```bash
sudo pacman -S gtk-layer-shell python-gobject python-cairo
```

(Other distros: GTK 3 layer-shell, PyGObject and pycairo packages.)

## Install

```bash
./install.sh                                    # package + launcher + systemd unit
gif-overlay ~/Pictures/your.gif --monitor 0     # first run: saves the settings, Ctrl+C to stop
systemctl --user enable --now gif-overlay.service
```

From then on it starts with your session and uses the saved settings
(`~/.config/gif-overlay/config.json`).

## Control

```bash
gif-overlay --bigger | --smaller | --reset-size   # resize
gif-overlay --toggle                              # hide / show (hidden = Plasma popups)
gif-overlay --stop
```

Bind them in **System Settings → Keyboard → Shortcuts → Add New → Command or Script**,
using the full path (`~/.local/bin/gif-overlay --bigger`, …). For example
Meta+Alt+PgUp / Meta+Alt+PgDown / Meta+Alt+H.

## Options

| Flag | Default | |
|---|---|---|
| `--scale` | 0.3 | size relative to the GIF's own size |
| `--step` | 0.05 | how much `--bigger` / `--smaller` change the scale |
| `--margin` | 20 | distance from the screen corner / taskbar (px) |
| `--hover-opacity` | 0.25 | opacity while the mouse is over it |
| `--monitor` | KWin picks | monitor index, or part of its make/model (e.g. `ASUS`); `-1` = let KWin pick |
| `--pixel` / `--no-pixel` | off | nearest-neighbour scaling for pixel art |
| `--bubbles` / `--no-bubbles` | on | notifications as speech bubbles, or keep Plasma's popups |

Flags given on the command line are saved and become the new defaults.

## How it works

- GTK 3 + gtk-layer-shell window on the OVERLAY layer, input region empty (click-through).
- Wayland doesn't let apps read the cursor position, so a small KWin script (loaded at
  runtime through `org.kde.KWin /Scripting`) watches the cursor and the panels and calls
  back over D-Bus (`local.GifOverlay`).
- Notifications: Plasma stays the notification server (history and the tray bell keep
  working). While the GIF is visible the app turns on Plasma's Do Not Disturb with
  `Inhibit()` and eavesdrops `Notify()` calls through a D-Bus monitor connection. Plasma
  drops the inhibition by itself if the app exits or crashes.

### Known trade-offs

- Notification buttons (Reply, Accept…) aren't available in bubbles; use the tray history.
- The tray shows Do Not Disturb as on while the GIF is visible, and when you hide the GIF
  Plasma summarises the notifications it "held back".
- Critical notifications (e.g. low battery) still get Plasma's popup too.

## Tests

```bash
python -I tests/test_helpers.py && python -I tests/test_agents.py
```

## License

MIT, see [LICENSE](LICENSE).
