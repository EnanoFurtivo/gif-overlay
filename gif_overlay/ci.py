"""GitHub Actions runs for the CI column: which ones to show, how long they usually take."""
import datetime, statistics

from gif_overlay.agents import format_elapsed

SUCCESS_LINGER, OTHER_LINGER = 60, 600  # how long a finished run's tile stays (s)
FAILED = {"failure", "timed_out", "startup_failure"}
ESTIMATE_RUNS = 10


def parse_time(iso):
    """GitHub's '2026-11-03T17:03:51Z' -> epoch seconds (None for None / '')."""
    if not iso:
        return None
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def duration(run):
    start = parse_time(run.get("run_started_at")) or parse_time(run.get("created_at"))
    return max(0.0, parse_time(run["updated_at"]) - start)


def median_duration(history, branch, n=ESTIMATE_RUNS):
    """Median duration of the newest n successful runs of one workflow: on `branch` if it has
    any, else on every branch. `history` is newest first."""
    same = [r for r in history if r.get("head_branch") == branch]
    runs = (same or history)[:n]
    return statistics.median(duration(r) for r in runs) if runs else None


class Tile:
    def __init__(self, run):
        self.run, self.finished_at = run, None

    def key(self):  # a newer run of the same workflow on the same branch replaces this one
        return self.run.get("repo"), self.run["workflow_id"], self.run.get("head_branch")


class CiTracker:
    """The runs on screen: active ones, plus finished ones for a little while."""
    def __init__(self, success_linger=SUCCESS_LINGER, other_linger=OTHER_LINGER):
        self.success_linger, self.other_linger = success_linger, other_linger
        self.runs = {}  # run id -> Tile

    def update(self, active, now):
        """`active` = every queued / running run right now. Returns the ids of runs shown as
        active that aren't in it any more: fetch them to learn how they ended."""
        for run in active:
            tile = self.runs.get(run["id"])
            if tile is None or tile.finished_at is not None:  # new, or a rerun
                fresh = Tile(run)
                for rid, old in list(self.runs.items()):
                    if rid != run["id"] and old.finished_at is not None and old.key() == fresh.key():
                        del self.runs[rid]
                self.runs[run["id"]] = fresh
            else:
                tile.run = run
        ids = {run["id"] for run in active}
        return [rid for rid, t in self.runs.items() if t.finished_at is None and rid not in ids]

    def finish(self, run, now):
        """A run fetched by id. True when it has just finished (time for the sound)."""
        tile = self.runs.get(run["id"])
        if tile is None:
            return False
        tile.run = run
        if run.get("status") != "completed" or tile.finished_at is not None:
            return False
        tile.finished_at = now
        return True

    def forget(self, run_id):
        self.runs.pop(run_id, None)

    def is_active(self):
        return any(t.finished_at is None for t in self.runs.values())

    def tiles(self, now):
        for rid, t in list(self.runs.items()):
            if t.finished_at is not None:
                linger = (self.success_linger if t.run.get("conclusion") == "success"
                          else self.other_linger)
                if now - t.finished_at > linger:
                    del self.runs[rid]
        return sorted(self.runs.values(),
                      key=lambda t: (parse_time(t.run.get("created_at")) or 0, t.run["id"]))


def describe(tile, estimate, now, multi_repo=False):
    """(caption, time text, progress 0-1 or None, state) for one tile."""
    run = tile.run
    caption = f"{run.get('name') or 'run'} · {run.get('head_branch') or '?'}"
    if multi_repo and run.get("repo"):
        caption = f"{run['repo'].split('/')[-1]} · {caption}"
    if tile.finished_at is not None:
        took, conclusion = format_elapsed(duration(run)), run.get("conclusion")
        if conclusion == "success":
            return caption, f"✓ {took}", 1.0, "success"
        if conclusion in FAILED:
            return caption, f"✗ failed {took}", 1.0, "failure"
        return caption, f"{conclusion or 'done'} {took}", 1.0, "cancelled"
    status = run.get("status")
    if status != "in_progress":  # queued, waiting (for an approval), pending, requested
        waited = max(0.0, now - (parse_time(run.get("created_at")) or now))
        label = "waiting" if status == "waiting" else "queued"
        return caption, f"{label} {format_elapsed(waited)}", None, label
    start = parse_time(run.get("run_started_at")) or parse_time(run.get("created_at")) or now
    elapsed = max(0.0, now - start)
    if estimate is None:
        return caption, format_elapsed(elapsed), None, "running"
    if elapsed <= estimate:
        return (caption, f"{format_elapsed(elapsed)} / ~{format_elapsed(estimate)}",
                elapsed / estimate if estimate else 1.0, "running")
    return caption, f"{format_elapsed(elapsed)} / +{format_elapsed(elapsed - estimate)}", 1.0, "over"
