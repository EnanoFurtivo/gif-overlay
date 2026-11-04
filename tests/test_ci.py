"""Tests for the CI run tracker, estimates and texts. Run: python -I tests/test_ci.py"""
import os, sys, traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import urllib.error

from gif_overlay.ci import (GitHub, CiTracker, describe, describe_job, job_estimate, median_duration,
                            normalize_repos, parse_time, poll, poll_jobs, runner_tags, shown_jobs)

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


# ---------------------------------------------------------------- where it runs
RUNNERS = {"pc-lucho-1": ["self-hosted", "Linux", "X64", "ever-panel", "pesado", "pc"],
           "truenas-casa-3": ["self-hosted", "Linux", "X64", "ever-panel", "pesado", "casa"],
           "truenas-ever-panel": ["self-hosted", "Linux", "X64", "ever-panel", "oficina", "liviano"]}

def job(status, runner=None, labels=("self-hosted", "ever-panel", "pesado")):
    return {"status": status, "runner_name": runner, "labels": list(labels)}

def test_tags_where_then_kind_from_running_jobs():
    jobs = [job("in_progress", "pc-lucho-1"), job("in_progress", "truenas-casa-3"),
            job("completed", "truenas-ever-panel", ("self-hosted", "liviano")), job("queued")]
    assert runner_tags(jobs, RUNNERS, "me/ever-panel") == ["pc", "casa", "pesado"]

def test_tags_queued_only_shows_the_requested_kind():
    assert runner_tags([job("queued")], RUNNERS, "me/ever-panel") == ["pesado"]

def test_tags_finished_run_keeps_where_it_ran():
    jobs = [job("completed", "truenas-ever-panel", ("self-hosted", "liviano"))]
    assert runner_tags(jobs, RUNNERS, "me/ever-panel") == ["oficina", "liviano"]

def test_tags_without_runner_list_use_requested_labels():
    jobs = [job("in_progress", "pc-lucho-1", ("self-hosted", "Linux", "ever-panel", "casa", "liviano"))]
    assert runner_tags(jobs, None, "me/ever-panel") == ["casa", "liviano"]  # repo-named label dropped
    hosted = [job("in_progress", "GitHub Actions 3", ("ubuntu-latest",))]
    assert runner_tags(hosted, RUNNERS, "me/ever-panel") == ["ubuntu-latest"]
    assert runner_tags([], RUNNERS, "me/ever-panel") == []

class JobsGitHub:
    def __init__(self, runners_code=None):
        self.runners_code, self.calls = runners_code, []

    def get(self, path, conditional=False):
        self.calls.append(path)
        if "/runners" in path:
            if self.runners_code:
                raise urllib.error.HTTPError(path, self.runners_code, "no", {}, None)
            return {"runners": [{"name": n, "labels": [{"name": l} for l in ls]}
                                for n, ls in RUNNERS.items()]}
        if "/runs/5/" in path:
            raise urllib.error.HTTPError(path, 500, "boom", {}, None)
        return {"jobs": [job("in_progress", "pc-lucho-1")]}

def test_poll_jobs_fetches_runners_once():
    gh = JobsGitHub()
    jobs, runners, errors, _h = poll_jobs(gh, [("o/r", 1), ("o/r", 5)], {})
    assert list(jobs) == [1] and len(errors) == 1  # run 5's jobs failed: only its part lost
    assert runners == {"o/r": RUNNERS}
    gh.calls.clear()
    poll_jobs(gh, [("o/r", 1)], runners)
    assert not any("/runners" in c for c in gh.calls)  # every runner known: not fetched again

def test_poll_jobs_no_access_to_runners_is_not_an_error():
    gh = JobsGitHub(runners_code=403)
    jobs, runners, errors, _h = poll_jobs(gh, [("o/r", 1)], {})
    assert runners == {"o/r": None} and errors == [] and list(jobs) == [1]
    gh.calls.clear()
    poll_jobs(gh, [("o/r", 1)], runners)
    assert not any("/runners" in c for c in gh.calls)

def test_poll_jobs_history_runs_fetched_unconditionally():
    gh = JobsGitHub()
    gh.get = lambda path, conditional=False, _g=gh.get: (gh.calls.append(("cond", conditional))
                                                         or _g(path, conditional))
    _j, _k, errors, hist = poll_jobs(gh, [], {}, history=[("o/r", 1), ("o/r", 5)])
    assert list(hist) == [1] and len(errors) == 1 and ("cond", False) in gh.calls

def test_conditional_cache_is_bounded():
    gh = GitHub()
    for i in range(400):
        gh.etags[f"/p{i}"], gh.cache[f"/p{i}"] = "e", {}
    gh.remember("/new", "e2", {"x": 1})
    assert len(gh.cache) <= 300 and len(gh.etags) <= 300 and gh.cache["/new"] == {"x": 1}


# ---------------------------------------------------------------- jobs
def jb(name, status="in_progress", conclusion=None, start=T0, end=None, created=None):
    return {"name": name, "status": status, "conclusion": conclusion,
            "created_at": iso(created if created is not None else start),
            "started_at": iso(start) if start is not None else None,
            "completed_at": iso(end) if end is not None else None}

def test_job_estimate_median_of_successful_runs():
    hist = [{"id": i} for i in range(1, 13)]
    by_run = {i: [jb("Web tests", "completed", "success", T0, T0 + 100 * i),
                  jb("Lint", "completed", "failure", T0, T0 + 5)] for i in range(1, 13)}
    del by_run[2]  # its jobs couldn't be read: just fewer samples
    assert job_estimate(hist, by_run, "Web tests") == 600  # runs 1, 3..10
    assert job_estimate(hist, by_run, "Lint") is None       # never succeeded
    assert job_estimate(hist, {}, "Web tests") is None

def test_describe_job():
    assert describe_job(jb("Web tests"), 300, T0 + 90) == ("Web tests", "1:30 / ~5:00", 0.3, "running")
    assert describe_job(jb("Web tests"), 60, T0 + 90)[1:] == ("1:30 / +0:30", 1.0, "over")
    assert describe_job(jb("Web tests"), None, T0 + 90)[1:] == ("1:30", None, "running")
    q = jb("Smoke", "queued", start=None, created=T0)
    assert describe_job(q, 300, T0 + 20)[1:] == ("queued 0:20", None, "queued")
    done = jb("Lint", "completed", "success", T0, T0 + 75)
    assert describe_job(done, 300, T0 + 500)[1:] == ("✓ 1:15", 1.0, "success")
    bad = jb("Lint", "completed", "failure", T0, T0 + 75)
    assert describe_job(bad, 300, T0 + 500)[1:] == ("✗ failed 1:15", 1.0, "failure")

def test_skipped_jobs_hidden():
    jobs = [jb("a"), jb("b", "completed", "skipped", None, None, created=T0), jb("c", "queued")]
    assert [j["name"] for j in shown_jobs(jobs)] == ["a", "c"]


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
