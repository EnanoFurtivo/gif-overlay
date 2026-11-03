# CI runs overlay — design

## Goal

See every GitHub Actions run of `EnanoFurtivo/ever-panel` that is queued or running (CI on
`dev`, Deploy staging, Promote production… every workflow, every branch), how long it has been
going and how long it usually takes, without opening GitHub.

## What the user sees

- A column in the **bottom-right corner of the top monitor (LG 23MP55)**, stacking **upwards**,
  newest run at the bottom.
- One row per run: a dark rounded neon tile (same look as the agent sprites) playing
  `~/gifs/run.gif`, and on its left a pill with
  - line 1: `<workflow> · <branch>` (e.g. `CI · dev`); with more than one repo, prefixed by the
    repo name;
  - line 2: the time: `3:12 / ~9:40` (elapsed / estimate), `3:12` when there's no estimate,
    `12:05 / +2:25` when past the estimate, `queued 0:20`, `waiting 0:20`;
  - a thin progress bar under it, elapsed ÷ estimate (none without an estimate).
- Colours: running = neon cyan; past the estimate = amber; success = green; failure = red;
  cancelled / skipped / anything else = grey. The colour is used for the tile and pill borders
  and the bar.
- When a run finishes the tile stops showing the GIF and shows ✓ / ✗ / – instead, the pill
  shows the final duration (`✓ 9:12`, `✗ failed 9:12`, `cancelled 2:03`), and the notification
  sound plays (with music ducking). A success leaves after **60 s**; anything else after
  **10 min**, or as soon as a new run of the same workflow on the same branch shows up.
- Click-through; hovering anywhere on the column fades all of it to `hover_opacity`.
- Hidden together with everything else by the toggle shortcut.

## Estimate

Median of `updated_at − run_started_at` of the last 10 **successful** runs of the same workflow
on the same branch; if that branch has none, the same workflow on any branch; none → no
estimate. History is fetched per workflow the first time one of its runs is seen and again after
one of its runs finishes.

## Data

- GitHub REST API, token from `gh auth token` (re-read after a 401), requests on a worker thread,
  results handed to the GTK thread with `GLib.idle_add`.
- Every poll: `GET /repos/{repo}/actions/runs?status=S&per_page=30` for S in `in_progress`,
  `queued`, `waiting`, with `If-None-Match` (a 304 is tiny and free of rate limit).
- A run that was shown as active and is no longer in those lists is fetched by id
  (`/actions/runs/{id}`) to get its conclusion.
- History: `GET /repos/{repo}/actions/workflows/{id}/runs?status=success&per_page=30&exclude_pull_requests=true`.
- Poll every **10 s** while something is active, **30 s** when idle. A failure (offline, gh not
  logged in) is logged once per streak; the tiles stay, timers keep counting, polling retries.
- Timers use the run's own timestamps (`run_started_at`, `created_at` for queued), so a restart
  shows correct times. Finished-result tiles are not persisted.

## Settings

`ci` (bool, `--ci/--no-ci`, default on), `ci_repos` (list, default `["EnanoFurtivo/ever-panel"]`,
`--ci-repo` repeatable), `ci_monitor` (default `"23MP55"`; not found → the main GIF's monitor),
`ci_gif` (default `~/gifs/run.gif`; unreadable → the main GIF), `ci_size` (tile height, default
44).

## Code

- `gif_overlay/ci.py` (pure, tested): `parse_time`, `median_duration`, `CiTracker` (active runs,
  finishing, expiry, replacement, which runs need fetching), `describe(tile, estimate, now)` →
  caption / time text / fraction / state.
- `gif_overlay/ci_view.py`: the layer-shell window, drawing, worker-thread fetches, hover rect
  `ci:x,y,w,h` for the KWin script.
- `gtkbits.neon_stroke` gains a `color` argument.
- `app.py`: options, creating the view, `Rects()` includes it, `Hover("ci")`, hide/show, panels
  re-place it.

## Not doing

Per-job / per-step progress, clicking a run, persisting finished results, GitLab or other CI.
