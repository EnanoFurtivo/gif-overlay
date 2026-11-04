# gif-overlay

A little animated GIF that lives in the bottom-right corner of your KDE Plasma (Wayland)
desktop, above every window — and reads your notifications out loud as speech bubbles,
Clippy style.

- **Always on top**, including fullscreen windows; never takes focus or shows in the taskbar.
- **Transparent**: the GIF's alpha channel is respected.
- **Click-through**: clicks go to whatever is underneath; hovering fades it to 25%.
- **Speech bubbles**: while the GIF is visible, desktop notifications appear as bubbles
  coming from it (typed out, one at a time). Click a bubble to dismiss it;
  hover it to see through it and keep it on screen. Hide the GIF and Plasma's normal popups come back.
- **Sits on the taskbar**: it stays above the panel, and rides up and down with an
  auto-hide panel.
- **Resizable / hideable** from global shortcuts; remembers its settings.
- **Sound + music ducking**: every bubble plays a sound of your choice, and other apps'
  audio dips to 30% while it plays so you actually hear it.
- **Water reminder**: every 20 minutes the GIF tells you to drink water; it stays until you
  click it and skips while the screen is locked.
- **Claude agent sprites** (top-right): one sprite per Claude Code session that is working
  or waiting for you, with its subagents stacked below it, smaller per level. When a
  session needs you it says so in a bubble; click it to jump to that chat in the Claude
  desktop app.
- **CI runs** (a corner of a monitor you pick): a tile per running GitHub Actions run,
  with its elapsed time, an estimate from past runs and a progress bar; it turns green / red
  when it ends.

## Claude agent sprites

```bash
gif-overlay --install-hooks      # adds hooks to ~/.claude/settings.json (backup kept)
gif-overlay --uninstall-hooks    # removes exactly those again
```

Sprites come from `~/gifs` (`--gifs-dir`): `level1.gif` for Claude sessions, `level2.gif`
for their subagents, `level3.gif` for theirs, and so on. A level without its own file
reuses the deepest one above it. Each session's sprite carries its chat name; hovering
anywhere over the column fades it. New hooks apply to sessions started after installing.
`--agent-size` sets the session sprite height (default 54), `--no-agents` turns it off.

Sessions started from another session in the desktop app (its side sessions) sit under that
session like subagents: indented, smaller, with that level's sprite, keeping their own name,
timer and "needs you" bubble. While any of them is active, the session they came from shows
as active too, even if it's idle itself.

Agents started from a terminal show up too, but their bubble can't open a chat.
With no `levelN` files at all, every level uses the main GIF. The hooks need `busctl`
(part of systemd).

## CI runs

```bash
gif-overlay --ci-repo owner/repo --ci-monitor 23MP55   # saved; repeat --ci-repo for more repos
```

Every queued or running GitHub Actions run of those repos (any workflow, any branch) gets a
tile in the bottom-right corner of that monitor (`--ci-corner`), newest nearest the corner,
playing `~/gifs/run.gif`
(`--ci-gif`). Next to it: `workflow · branch`, the elapsed time and `~estimate`, the median of
the last 10 successful runs of that workflow on that branch (or on any branch). Past the
estimate it turns amber and counts `+overtime`. On the right of the time, tags say what it runs
on: the labels of the runners its jobs got (e.g. `pc` `casa`), then the kind the jobs asked for
(`pesado`); labels every runner has, OS / arch and the repo's name are left out. Listing the
runners needs admin access to the repo; without it the jobs' requested labels are shown. When the run ends the tile shows ✓ / ✗ and the
sound plays; successes leave after a minute, the rest after 10 minutes or when that workflow
runs again on that branch. It's click-through and fades on hover like the rest.

It uses your `gh` login (`gh auth token`) and polls every 10 s while something runs, every
30 s otherwise; unchanged answers are ETag hits, which don't count against the rate limit.
`--ci-size` sets the tile height (default 44), `--no-ci` turns it off.
In the agents' corner (`--ci-corner top-right`), the agents column moves down below it.

## Requirements

KDE Plasma 6 on Wayland, Python 3, and:

```bash
sudo pacman -S gtk-layer-shell python-gobject python-cairo
```

(Other distros: GTK 3 layer-shell, PyGObject and pycairo packages.) The CI runs also need
the GitHub CLI (`gh`), logged in.

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
| `--agents` / `--no-agents` | on | Claude agent sprites in the top-left corner |
| `--agents-corner` | top-right | `top-right` or `top-left` |
| `--agent-size` | 54 | height of a session's sprite (px) |
| `--gifs-dir` | `~/gifs` | where `level1.gif`, `level2.gif`… live |
| `--sound` | `~/.local/share/gif-overlay/sounds/notification.wav` | played with every bubble (`none` = silent) |
| `--duck` | 0.3 | other apps' volume while the sound plays (1 = don't dim) |
| `--water-minutes` | 20 | water reminder interval (0 = off) |
| `--ci-repo` | none | GitHub repo whose Actions runs are shown (repeatable) |
| `--ci-monitor` | the GIF's | monitor for the CI runs, like `--monitor` |
| `--ci-gif` | `~/gifs/run.gif` | GIF in each CI tile |
| `--ci-size` | 44 | CI tile height (px) |
| `--ci-corner` | bottom-right | corner the CI runs stack from (`top-left`, `bottom-left`…) |
| `--ci` / `--no-ci` | on | show the CI runs |

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
python -I tests/test_helpers.py && python -I tests/test_agents.py && python -I tests/test_ci.py
```

## License

MIT, see [LICENSE](LICENSE).
