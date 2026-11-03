"""Tests for the CI run tracker, estimates and texts. Run: python -I tests/test_ci.py"""
import os, sys, traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import urllib.error

from gif_overlay.ci import CiTracker, describe, median_duration, normalize_repos, parse_time, poll

T0 = parse_time("2026-11-03T12:00:00Z")


def iso(t):
    import datetime
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(i, status="in_progress", conclusion=None, start=T0, end=None, branch="dev", wf=1,
        name="CI", created=None, repo="o/r"):
    return {"id": i, "status": status, "conclusion": conclusion, "head_branch": branch,
            "workflow_id": wf, "name": name, "repo": repo,
            "created_at": iso(created if created is not None else start),
            "run_started_at": iso(start) if start is not None else None,
            "updated_at": iso(end if end is not None else start if start is not None
                              else created)}


def ok(i, secs, branch="dev"):
    return run(i, "completed", "success", T0, T0 + secs, branch)


# ---------------------------------------------------------------- estimate
def test_parse_time():
    assert parse_time("1970-01-01T00:01:00Z") == 60.0
    assert parse_time(None) is None and parse_time("") is None

def test_median_same_branch_then_any():
    hist = [ok(1, 100), ok(2, 300), ok(3, 200), ok(4, 900, "main")]
    assert median_duration(hist, "dev") == 200
    assert median_duration(hist, "staging") == 250  # no staging runs: every branch
    assert median_duration([ok(i, i) for i in range(1, 30)], "dev") == 5.5  # newest 10 only

def test_median_none():
    assert median_duration([], "dev") is None


# ---------------------------------------------------------------- tracker
def test_active_then_missing_returned_for_fetch():
    t = CiTracker()
    assert t.update([run(1), run(2)], T0) == []
    assert t.is_active()
    assert t.update([run(2)], T0 + 10) == [1]
    assert [x.run["id"] for x in t.tiles(T0 + 10)] == [1, 2]  # kept until it's fetched
    assert t.update([run(2)], T0 + 20) == [1]  # a failed fetch: asked again next time

def test_finish_success_lingers_60s():
    t = CiTracker()
    t.update([run(1)], T0)
    t.update([], T0 + 5)
    assert t.finish(run(1, "completed", "success", T0, T0 + 5), T0 + 5) is True
    assert t.finish(run(1, "completed", "success", T0, T0 + 5), T0 + 6) is False  # once
    assert not t.is_active()
    assert t.update([], T0 + 7) == []
    assert [x.run["id"] for x in t.tiles(T0 + 64)] == [1]
    assert t.tiles(T0 + 66) == []

def test_finish_still_running_stays_active():
    t = CiTracker()
    t.update([run(1)], T0)
    assert t.finish(run(1), T0 + 5) is False
    assert t.is_active() and t.tiles(T0 + 5)[0].finished_at is None

def test_failure_lingers_600s():
    t = CiTracker()
    t.update([run(1)], T0)
    t.finish(run(1, "completed", "failure", T0, T0 + 5), T0 + 5)
    assert len(t.tiles(T0 + 500)) == 1
    assert t.tiles(T0 + 606) == []

def test_newer_run_replaces_result():
    t = CiTracker()
    t.update([run(1)], T0)
    t.finish(run(1, "completed", "failure", T0, T0 + 5), T0 + 5)
    t.update([run(2, start=T0 + 30, branch="staging")], T0 + 30)  # another branch: stays
    assert [x.run["id"] for x in t.tiles(T0 + 30)] == [1, 2]
    t.update([run(2, start=T0 + 30, branch="staging"), run(3, start=T0 + 40)], T0 + 40)
    assert [x.run["id"] for x in t.tiles(T0 + 40)] == [2, 3]

def test_rerun_reactivates():
    t = CiTracker()
    t.update([run(1)], T0)
    t.finish(run(1, "completed", "failure", T0, T0 + 5), T0 + 5)
    t.update([run(1, start=T0 + 100)], T0 + 100)
    tile = t.tiles(T0 + 100)[0]
    assert tile.finished_at is None and parse_time(tile.run["run_started_at"]) == T0 + 100

def test_unseen_completed_run_not_shown():
    t = CiTracker()
    assert t.finish(run(9, "completed", "success", T0, T0 + 5), T0 + 5) is False
    assert t.tiles(T0 + 5) == []

def test_forget():
    t = CiTracker()
    t.update([run(1)], T0)
    t.forget(1)
    assert t.tiles(T0) == [] and t.update([], T0 + 1) == []


def test_cancelled_by_newer_run_goes_without_sound():
    t = CiTracker()
    t.update([run(1)], T0)
    t.update([run(1), run(2, start=T0 + 30)], T0 + 30)  # concurrency: 2 cancels 1
    t.update([run(2, start=T0 + 30)], T0 + 35)
    assert t.finish(run(1, "completed", "cancelled", T0, T0 + 34), T0 + 35) is False
    assert [x.run["id"] for x in t.tiles(T0 + 35)] == [2]

def test_stale_active_listing_does_not_reactivate():
    t = CiTracker()
    t.update([run(1)], T0)
    t.finish(run(1, "completed", "success", T0, T0 + 5), T0 + 5)
    t.update([run(1)], T0 + 10)  # an active list that hasn't caught up yet
    assert t.tiles(T0 + 10)[0].finished_at is not None
    assert t.finish(run(1, "completed", "success", T0, T0 + 5), T0 + 15) is False
    rerun = dict(run(1, start=T0 + 5), run_attempt=2)  # a real rerun (same start, newer attempt)
    t.update([rerun], T0 + 20)
    assert t.tiles(T0 + 20)[0].finished_at is None


# ---------------------------------------------------------------- fetching
class FakeGitHub:
    def __init__(self, fail=()):
        self.fail, self.calls = fail, []

    def get(self, path, conditional=False):
        self.calls.append(path)
        if any(f in path for f in self.fail):
            raise urllib.error.HTTPError(path, 404 if "/runs/7" in path else 403, "no", {}, None)
        if "/runs?status=in_progress" in path:
            return {"workflow_runs": [{"id": 1, "workflow_id": 5}]}
        if "/runs/" in path:
            return {"id": int(path.rsplit("/", 1)[1]), "status": "completed"}
        return {"workflow_runs": []}

def test_poll_one_bad_repo_or_history_doesnt_sink_the_rest():
    gh = FakeGitHub(fail=("bad/repo", "workflows/9"))
    active, ended, gone, found, errors = poll(gh, ["o/r", "bad/repo"], [("o/r", 3), ("o/r", 7)],
                                              [("o/r", 5), ("o/r", 9)])
    assert [(r["id"], r["repo"]) for r in active] == [(1, "o/r")]
    assert [r["id"] for r in ended] == [3, 7]
    assert found == {("o/r", 5): [], ("o/r", 9): []}  # a failing history: no estimate, not retried
    assert len(errors) == 4 and gone == []  # bad/repo's 3 lists + history 9

def test_poll_deleted_run_is_gone():
    gh = FakeGitHub(fail=("/runs/7",))
    _a, ended, gone, _f, errors = poll(gh, ["o/r"], [("o/r", 7)], [])
    assert ended == [] and gone == [7] and errors == []

def test_normalize_repos():
    assert normalize_repos("o/r") == ["o/r"]
    assert normalize_repos(["https://github.com/o/r.git", "o/r", " x/y ", "nope", 3]) == ["o/r", "x/y"]
    assert normalize_repos(None) == [] and normalize_repos(5) == []


# ---------------------------------------------------------------- texts
def tile_for(r, now, finish=None):
    t = CiTracker()
    t.update([r], T0)
    if finish:
        t.finish(finish, now)
    return t.tiles(now)[0]

def test_describe_running_over_queued_done():
    r = run(1)
    assert describe(tile_for(r, T0), 580, T0 + 192) == ("CI · dev", "3:12 / ~9:40", 192 / 580, "running")
    assert describe(tile_for(r, T0), None, T0 + 192)[1:] == ("3:12", None, "running")
    assert describe(tile_for(r, T0), 580, T0 + 725)[1:] == ("12:05 / +2:25", 1.0, "over")
    q = run(2, "queued", start=None, created=T0)
    assert describe(tile_for(q, T0), 580, T0 + 20)[1:] == ("queued 0:20", None, "queued")
    w = run(3, "waiting")
    assert describe(tile_for(w, T0), 580, T0 + 20)[3] == "waiting"
    done = run(1, "completed", "success", T0, T0 + 552)
    assert describe(tile_for(r, T0 + 553, done), 580, T0 + 560)[1:] == ("✓ 9:12", 1.0, "success")
    bad = run(1, "completed", "failure", T0, T0 + 552)
    assert describe(tile_for(r, T0 + 553, bad), 580, T0 + 560)[1:] == ("✗ failed 9:12", 1.0, "failure")
    stop = run(1, "completed", "cancelled", T0, T0 + 123)
    assert describe(tile_for(r, T0 + 124, stop), 580, T0 + 130)[1:] == ("cancelled 2:03", 1.0, "cancelled")

def test_describe_multi_repo_caption():
    assert describe(tile_for(run(1, repo="me/ever-panel"), T0), None, T0, multi_repo=True)[0] \
        == "ever-panel · CI · dev"

def test_elapsed_never_negative():
    assert describe(tile_for(run(1), T0), 580, T0 - 5)[1:3] == ("0:00 / ~9:40", 0.0)

def test_queued_uses_created_at():
    q = run(2, "queued", start=None, created=T0)
    assert describe(tile_for(q, T0), None, T0 + 65)[1] == "queued 1:05"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            failed += 1
            print("FAIL", name); traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
