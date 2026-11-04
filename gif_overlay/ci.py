"""GitHub Actions runs for the CI column: which ones to show, how long they usually take,
and fetching them (no GTK here: the fetching runs on a worker thread)."""
import datetime, json, re, statistics, subprocess, urllib.error, urllib.request

from gif_overlay.agents import format_elapsed

SUCCESS_LINGER, OTHER_LINGER = 60, 600  # how long a finished run's tile stays (s)
FAILED = {"failure", "timed_out", "startup_failure"}
ESTIMATE_RUNS = 10
API = "https://api.github.com"
ACTIVE_STATUSES = ("in_progress", "queued", "waiting")
CACHE_MAX = 300  # conditional answers kept for their ETags
REPO = re.compile(r"[\w.-]+/[\w.-]+")
GENERIC_LABELS = {"self-hosted", "linux", "windows", "macos", "x64", "arm64", "arm"}


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


def normalize_repos(value):
    """The configured repos as 'owner/repo' strings (URLs and .git tolerated, junk dropped)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    repos = []
    for v in value:
        if not isinstance(v, str):
            continue
        v = re.sub(r"^(https?://)?(www\.)?github\.com/", "", v.strip()).removesuffix(".git")
        if REPO.fullmatch(v) and v not in repos:
            repos.append(v)
    return repos


def runner_tags(jobs, runners, repo):
    """What a run runs on, e.g. ['pc', 'casa', 'pesado']: the labels of the runners of its
    running jobs (else its waiting jobs, else all of them), where first (the runner's own
    labels), then the kind (what the jobs asked for). Labels every runner has, OS / arch and
    the repo's name are left out. `runners` = runner name -> labels, None when unknown."""
    for wanted in ({"in_progress"}, {"queued", "waiting", "pending", "requested"}, None):
        picked = [j for j in jobs if wanted is None or j.get("status") in wanted]
        if picked:
            break
    runners = runners or {}
    shared = set.intersection(*map(set, runners.values())) if len(runners) > 1 else set()
    skip = lambda l: (l.lower() in GENERIC_LABELS or l in shared
                      or l.lower() == repo.split("/")[-1].lower())
    where, kind = [], []
    for j in picked:
        asked = j.get("labels") or []
        has = runners.get(j.get("runner_name"))
        for l in has or []:
            if l not in asked and not skip(l) and l not in where:
                where.append(l)
        for l in (asked if has is None else [l for l in asked if l in has]):
            if not skip(l) and l not in kind:
                kind.append(l)
    return where + [l for l in kind if l not in where]


def job_duration(job):
    return max(0.0, parse_time(job["completed_at"]) - parse_time(job["started_at"]))


def job_estimate(history, jobs_by_run, name, n=ESTIMATE_RUNS):
    """Median duration of the job called `name` in the newest n successful runs of its
    workflow whose jobs are known (`jobs_by_run`: run id -> jobs)."""
    took = [job_duration(j) for r in history[:n] for j in jobs_by_run.get(r["id"], [])
            if j.get("name") == name and j.get("conclusion") == "success"
            and j.get("started_at") and j.get("completed_at")]
    return statistics.median(took) if took else None


def shown_jobs(jobs):
    return [j for j in jobs if j.get("conclusion") != "skipped"]


class Tile:
    def __init__(self, run):
        self.run, self.finished_at = run, None
        self.jobs = []  # its jobs, as last read

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
            if tile is not None and tile.finished_at is not None and not rerun(tile.run, run):
                continue  # a list that hasn't caught up with its ending yet
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
        if tile.finished_at is not None:
            return False
        if run.get("status") != "completed":
            tile.run = run
            return False
        if self.newer(tile):  # e.g. cancelled by the run that replaced it: just go
            del self.runs[run["id"]]
            return False
        tile.run, tile.finished_at = run, now
        return True

    def newer(self, tile):
        """Is there a newer run of the same workflow on the same branch?"""
        mine = order(tile)
        return any(t is not tile and t.key() == tile.key() and order(t) > mine
                   for t in self.runs.values())

    def forget(self, run_id):
        self.runs.pop(run_id, None)

    def is_active(self):
        return any(t.finished_at is None for t in self.runs.values())

    def tiles(self, now):
        for rid, t in list(self.runs.items()):
            if t.finished_at is not None:
                linger = (self.success_linger if t.run.get("conclusion") == "success"
                          else self.other_linger)
                if now - t.finished_at > linger or self.newer(t):
                    del self.runs[rid]
        return sorted(self.runs.values(), key=order)


def order(tile):
    return parse_time(tile.run.get("created_at")) or 0, tile.run["id"]


def rerun(old, new):
    """Has a finished run been started again (a new attempt)?"""
    return (new.get("run_attempt", 1) > old.get("run_attempt", 1)
            or new.get("run_started_at") != old.get("run_started_at"))


def describe(tile, estimate, now, multi_repo=False):
    """(caption, time text, progress 0-1 or None, state) for one tile."""
    run = tile.run
    caption = f"{run.get('name') or 'run'} · {run.get('head_branch') or '?'}"
    if multi_repo and run.get("repo"):
        caption = f"{run['repo'].split('/')[-1]} · {caption}"
    if tile.finished_at is not None:
        return (caption,) + ended(run.get("conclusion"), duration(run))
    if run.get("status") != "in_progress":  # queued, waiting (for an approval), pending...
        return (caption,) + waiting(run, now)
    start = parse_time(run.get("run_started_at")) or parse_time(run.get("created_at")) or now
    return (caption,) + going(start, estimate, now)


def describe_job(job, estimate, now):
    """(name, time text, progress 0-1 or None, state) for one job of a run."""
    name = job.get("name") or "job"
    if job.get("status") == "completed":
        took = job_duration(job) if job.get("started_at") and job.get("completed_at") else 0
        return (name,) + ended(job.get("conclusion"), took)
    if job.get("status") != "in_progress":
        return (name,) + waiting(job, now)
    return (name,) + going(parse_time(job.get("started_at")) or now, estimate, now)


def ended(conclusion, took):
    took = format_elapsed(took)
    if conclusion == "success":
        return f"✓ {took}", 1.0, "success"
    if conclusion in FAILED:
        return f"✗ failed {took}", 1.0, "failure"
    return f"{conclusion or 'done'} {took}", 1.0, "cancelled"


def waiting(item, now):
    waited = max(0.0, now - (parse_time(item.get("created_at")) or now))
    label = "waiting" if item.get("status") == "waiting" else "queued"
    return f"{label} {format_elapsed(waited)}", None, label


def going(start, estimate, now):
    elapsed = max(0.0, now - start)
    if estimate is None:
        return format_elapsed(elapsed), None, "running"
    if elapsed <= estimate:
        return (f"{format_elapsed(elapsed)} / ~{format_elapsed(estimate)}",
                elapsed / estimate if estimate else 1.0, "running")
    return f"{format_elapsed(elapsed)} / +{format_elapsed(elapsed - estimate)}", 1.0, "over"


# ---------------------------------------------------------------- fetching (worker thread)
class GitHub:
    """Tiny REST client: token from `gh auth token`, ETags so unchanged lists cost nothing.
    Only ever used from the worker thread."""
    def __init__(self):
        self.token, self.etags, self.cache = None, {}, {}

    def get(self, path, conditional=False):
        if not self.token:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                                 timeout=15)
            self.token = out.stdout.strip()
            if not self.token:
                raise RuntimeError("`gh auth token` gave no token (is gh logged in?)")
        req = urllib.request.Request(API + path, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "gif-overlay"})
        if conditional and path in self.etags:
            req.add_header("If-None-Match", self.etags[path])
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
                if conditional and resp.headers.get("ETag"):
                    self.remember(path, resp.headers["ETag"], data)
                return data
        except urllib.error.HTTPError as e:
            if e.code == 304 and path in self.cache:
                return self.cache[path]
            if e.code == 401:
                self.token = None  # re-read it next time (gh may have refreshed it)
            raise


    def remember(self, path, etag, data):
        """Keep an answer for its ETag; the oldest go past CACHE_MAX (one per run's jobs...)."""
        self.etags.pop(path, None)
        self.cache.pop(path, None)
        while len(self.cache) >= CACHE_MAX:
            old = next(iter(self.cache))
            del self.cache[old]
            self.etags.pop(old, None)
        self.etags[path], self.cache[path] = etag, data


def poll(gh, repos, tracked, histories):
    """The active runs of every repo, the tracked runs that left those lists (fetched by id)
    and the requested histories: (active, ended, gone ids, histories, errors). One failing
    request only loses its own part."""
    active, ended, gone, found, errors = [], [], [], {}, []
    for repo in repos:
        for status in ACTIVE_STATUSES:
            try:
                data = gh.get(f"/repos/{repo}/actions/runs?status={status}&per_page=30",
                              conditional=True)
                active += [dict(r, repo=repo) for r in data.get("workflow_runs", [])]
            except Exception as e:
                errors.append(f"{repo} {status} runs: {e}")
    ids = {r["id"] for r in active}
    for repo, run_id in tracked:
        if run_id in ids:
            continue
        try:
            ended.append(dict(gh.get(f"/repos/{repo}/actions/runs/{run_id}"), repo=repo))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                gone.append(run_id)  # deleted
            else:
                errors.append(f"{repo} run {run_id}: {e}")
        except Exception as e:
            errors.append(f"{repo} run {run_id}: {e}")
    for repo, workflow_id in histories:
        try:
            data = gh.get(f"/repos/{repo}/actions/workflows/{workflow_id}/runs"
                          "?status=success&per_page=30&exclude_pull_requests=true")
            found[(repo, workflow_id)] = data.get("workflow_runs", [])
        except Exception as e:
            errors.append(f"{repo} workflow {workflow_id} history: {e}")
            found[(repo, workflow_id)] = []  # no estimate; tried again after its next run
    return active, ended, gone, found, errors


def poll_jobs(gh, runs, known, history=()):
    """The jobs of (repo, run id) pairs, and the runners of their repos when one of those jobs'
    runners isn't in `known` (repo -> runner name -> labels, None = not allowed to list them),
    and the jobs of `history` runs (finished, so not worth an ETag): (jobs by run id, the
    updated `known`, errors, history jobs by run id)."""
    jobs, known, errors, past = {}, dict(known), [], {}
    for (repo, run_id), conditional, into in ([(r, True, jobs) for r in runs]
                                             + [(r, False, past) for r in history]):
        try:
            into[run_id] = gh.get(f"/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
                                  conditional=conditional).get("jobs", [])
        except Exception as e:
            errors.append(f"{repo} run {run_id} jobs: {e}")
    for repo in {repo for repo, _ in runs}:
        names = {j.get("runner_name") for repo_, rid in runs if repo_ == repo
                 for j in jobs.get(rid, []) if j.get("runner_name")}
        if repo in known and (known[repo] is None or names <= set(known[repo])):
            continue
        try:
            data = gh.get(f"/repos/{repo}/actions/runners?per_page=100", conditional=True)
            known[repo] = {r["name"]: [l["name"] for l in r.get("labels", [])]
                           for r in data.get("runners", [])}
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                known[repo] = None  # listing runners needs admin: the jobs' own labels do
            else:
                errors.append(f"{repo} runners: {e}")
        except Exception as e:
            errors.append(f"{repo} runners: {e}")
    return jobs, known, errors, past
