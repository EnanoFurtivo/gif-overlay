"""Claude agents -> sprites: hook event tracking, GIF picking, layout, hook installation.

Pure (no GTK); unit-tested in tests/test_agents.py.
"""
import copy, os, re
from collections import namedtuple
from dataclasses import asdict, dataclass, field

# ---------------------------------------------------------------- hooks
# Claude Code runs gif-overlay-hook (installed next to the launcher) on each event; it
# hands the JSON payload to the running overlay over D-Bus, fire-and-forget.
HOOK_MARKER = "gif-overlay-hook"
OLD_MARKERS = ["local.GifOverlay AgentEvent"]  # inline commands of the first version
HOOK_CMD = '"$HOME/.local/bin/gif-overlay-hook"'
HOOK_EVENTS = {  # event -> matcher (None = every occurrence)
    "SessionStart": None,
    "SessionEnd": None,
    "UserPromptSubmit": None,
    "Stop": None,
    "SubagentStart": None,
    "SubagentStop": None,
    "Notification": None,
    "PermissionRequest": None,
    "PreToolUse": "AskUserQuestion|ExitPlanMode",
    "PostToolUse": "*",
}


def ours(hook):
    command = hook.get("command", "") if isinstance(hook, dict) else ""
    return any(marker in command for marker in [HOOK_MARKER] + OLD_MARKERS)


def remove_hooks(settings):
    """A copy of Claude Code `settings` without our hook entries (others untouched)."""
    settings = copy.deepcopy(settings)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if not ours(h)]
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    if not hooks:
        del settings["hooks"]
    return settings


def merge_hooks(settings):
    """A copy of `settings` with our hook entries (replacing any older version of them)."""
    settings = remove_hooks(settings)
    hooks = settings.setdefault("hooks", {})
    for event, matcher in HOOK_EVENTS.items():
        group = {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 5}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    return settings


# ---------------------------------------------------------------- tracker
BLOCKING_NOTIFICATIONS = {"permission_prompt", "elicitation_dialog", None}
WAITING_TOOLS = {"AskUserQuestion": "has a question for you",
                 "ExitPlanMode": "wants you to approve a plan"}


@dataclass
class SessionState:
    id: str
    first_seen: float
    last_event: float
    working: bool = False
    waiting: str | None = None       # why it needs the user, or None
    stopped_at: float | None = None
    agents: dict = field(default_factory=dict)   # running subagent id -> depth (1 = direct)
    parents: dict = field(default_factory=dict)  # running subagent id -> its spawner's id
    agent_started: dict = field(default_factory=dict)  # running subagent id -> start time
    cwd: str = ""


class AgentTracker:
    """Folds Claude Code hook payloads into the set of sessions worth a sprite."""
    def __init__(self, idle_grace=4.0, stale=1800.0):
        self.idle_grace, self.stale = idle_grace, stale
        self.sessions = {}

    def event(self, payload, now):
        """Apply one hook payload; returns False when it was ignored."""
        sid, name = payload.get("session_id"), payload.get("hook_event_name")
        if not isinstance(sid, str) or not sid or not isinstance(name, str):
            return False
        if name == "SessionEnd":
            return self.sessions.pop(sid, None) is not None
        agent = payload.get("agent_id")
        if name in ("SubagentStart", "SubagentStop") and not isinstance(agent, str):
            return False
        if name == "Notification" and payload.get("notification_type") not in BLOCKING_NOTIFICATIONS:
            return False  # e.g. idle_prompt ("still idle after 60s"): every finished chat would light up
        if name == "PreToolUse" and payload.get("tool_name") not in WAITING_TOOLS:
            return False
        if name not in ("SessionStart", "UserPromptSubmit", "Stop", "SubagentStart",
                        "SubagentStop", "Notification", "PermissionRequest", "PreToolUse",
                        "PostToolUse"):
            return False

        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = SessionState(sid, now, now)
        s.last_event = now
        if isinstance(payload.get("cwd"), str):
            s.cwd = payload["cwd"]

        if name == "UserPromptSubmit":
            s.working, s.waiting, s.stopped_at = True, None, None
        elif name == "Stop":  # a turn can't end while it's blocked on the user
            s.working, s.waiting, s.stopped_at = False, None, now
        elif name == "SubagentStart":
            parent = payload.get("parent_agent_id")
            s.agents[agent] = s.agents[parent] + 1 if parent in s.agents else 1
            s.parents[agent] = parent
            s.agent_started[agent] = now
        elif name == "SubagentStop":
            s.agents.pop(agent, None)
            s.parents.pop(agent, None)
            s.agent_started.pop(agent, None)
        elif name == "Notification":
            s.waiting = str(payload.get("message") or "needs your input")
        elif name == "PermissionRequest":
            tool = payload.get("tool_name")
            s.waiting = f"wants to use {tool}" if tool else "needs your permission"
        elif name == "PreToolUse":
            s.waiting = WAITING_TOOLS[payload["tool_name"]]
        elif name == "PostToolUse" and agent is None:  # subagents' tools say nothing about main
            s.working, s.waiting = True, None
        elif name == "PostToolUse" and agent not in s.agents:
            # a subagent we never saw start (we began listening after it did): it's running
            s.agents[agent], s.parents[agent], s.agent_started[agent] = 1, None, now
        return True

    def active(self, now):
        """Sessions to show, oldest first: working, waiting, with subagents, or just stopped."""
        for sid in [sid for sid, s in self.sessions.items() if now - s.last_event > self.stale]:
            del self.sessions[sid]
        return sorted((s for s in self.sessions.values()
                       if s.working or s.waiting or s.agents
                       or (s.stopped_at is not None and now - s.stopped_at < self.idle_grace)),
                      key=lambda s: s.first_seen)

    def dump(self):
        """JSON-able state (times are CLOCK_MONOTONIC: valid until the next reboot)."""
        return {"sessions": [asdict(s) for s in self.sessions.values()]}

    def load(self, data):
        """Restore dump() output; anything unreadable just starts empty."""
        try:
            sessions = {d["id"]: SessionState(**d) for d in data["sessions"]}
        except (KeyError, TypeError, AttributeError):
            sessions = {}
        self.sessions = sessions

    def tree(self, session):
        """[(agent id, depth)] with every subagent right under the one that spawned it."""
        children = {}
        for agent in session.agents:  # dicts keep start order
            parent = session.parents.get(agent)
            children.setdefault(parent if parent in session.agents else None, []).append(agent)
        out, stack = [], list(reversed(children.get(None, [])))
        while stack:
            agent = stack.pop()
            out.append((agent, session.agents[agent]))
            stack.extend(reversed(children.get(agent, [])))
        return out

    def any_waiting(self, now):
        return any(s.waiting for s in self.active(now))


def format_elapsed(seconds):
    """3:42, or 1:02:05 past an hour."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


# ---------------------------------------------------------------- level GIFs
LEVEL_FILE = re.compile(r"level(\d+)\.(gif|webp)$", re.IGNORECASE)


def level_gifs(paths):
    """{level: path} for files named level1.gif, level2.gif... (level 1 = Claude sessions)."""
    levels = {}
    for path in paths:
        m = LEVEL_FILE.fullmatch(os.path.basename(path))
        if m:
            levels[int(m.group(1))] = path
    return levels


def gif_for_level(levels, level, main_gif):
    """The level's own GIF, else the deepest one above it, else the nearest below, else main."""
    above = [n for n in levels if n <= level]
    below = [n for n in levels if n > level]
    if above:
        return levels[max(above)]
    return levels[min(below)] if below else main_gif


# ---------------------------------------------------------------- layout
# parent: for a forked session's own sprite, the session it was started from
MAX_FORK_DEPTH = 6  # sessions started from sessions started from... deeper ones aren't nested


def forest(active, parent_of, max_depth=MAX_FORK_DEPTH):
    """Sessions in tree order as [(id, depth, ghost)]: every session right under the one it
    was started from (parent_of(id) -> id or None). An idle ancestor is included as a ghost,
    where its first active descendant would have been; cycles are ignored."""
    is_active, parent = set(active), {}

    def reaches(a, b):  # does following parents from a lead to b?
        seen = set()
        while a is not None and a not in seen:
            if a == b:
                return True
            seen.add(a)
            a = parent.get(a)
        return False

    order = []
    for sid in active:
        chain, cur = [], sid
        while len(chain) < max_depth:
            p = parent_of(cur)
            if p is None or p in parent or reaches(p, cur):
                if p is not None and p in parent and not reaches(p, cur):
                    parent[cur] = p  # joins a known tree
                    chain.append(p)
                break
            parent[cur] = p
            chain.append(p)
            if p in is_active:
                break  # its own ancestors come with it
            cur = p
        for node in reversed(chain):
            if node not in order:
                order.append(node)
        if sid not in order:
            order.append(sid)
    children = {}
    for node in order:
        children.setdefault(parent.get(node), []).append(node)
    rows = []

    def visit(node, depth):
        rows.append((node, depth, node not in is_active))
        for child in children.get(node, []):
            visit(child, depth + 1)
    for root in children.get(None, []):
        visit(root, 0)
    return rows


Placement = namedtuple("Placement", "kind session_id index x y w h depth parent",
                       defaults=(None,))
DEPTH_RATIO, MIN_SIZE = 0.55, 16  # each level is 55% of its parent, never under 16px
INDENT, V_GAP, SESSION_GAP, CAPTION_GAP, BUBBLE_GAP = 18, 4, 12, 2, 8
MAX_AGENTS, MORE_W, MORE_H = 12, 40, 20


def layout(rows, agent_size, aspect, bubble_size, caption_size):
    """One column from the top-left: each session, its name, then its subagents below it.

    rows: [{"id", "agents": [depth, ...] in tree order, "waiting": bool, "depth": the
    session's own level (a session started from another one sits under it, like a subagent;
    default 0), "parent": that session's id}] in tree order; aspect(depth) is that level's GIF
    width/height, bubble_size / caption_size(session_id) give (w, h).
    Returns (placements, width, height). Sprite index = position in "agents" (-1 = the
    session); a "more" placement's index is how many subagents didn't fit. Depths in the
    placements are absolute (session level + subagent level).
    """
    def sprite_size(depth):
        h = max(MIN_SIZE, round(agent_size * DEPTH_RATIO ** depth))
        return round(h * aspect(depth)), h

    placements, width, y = [], 0, 0
    for i, row in enumerate(rows):
        sid, d = row["id"], row.get("depth", 0)
        if i and d:  # still the same tree: as close as subagents
            y -= SESSION_GAP - V_GAP
        x0, (w0, h0) = INDENT * d, sprite_size(d)
        placements.append(Placement("sprite", sid, -1, x0, y, w0, h0, d, row.get("parent")))
        cw, ch = caption_size(sid)
        placements.append(Placement("caption", sid, -1, x0 + max(0, (w0 - cw) // 2),
                                    y + h0 + CAPTION_GAP, cw, ch, d))
        bottom = y + h0 + CAPTION_GAP + ch
        if row["waiting"]:  # beside the session sprite, tail pointing at it
            bw, bh = bubble_size(sid)
            bx, by = x0 + max(w0, cw) + BUBBLE_GAP, y + max(0, (h0 - bh) // 2)
            placements.append(Placement("bubble", sid, -1, bx, by, bw, bh, d))
            bubble_bottom = by + bh
        else:
            bubble_bottom = y
        cursor = bottom + V_GAP
        for index, depth in enumerate(row["agents"][:MAX_AGENTS]):
            w, h = sprite_size(d + depth)
            placements.append(Placement("sprite", sid, index, INDENT * (d + depth), cursor, w, h,
                                        d + depth))
            cursor += h + V_GAP
        if len(row["agents"]) > MAX_AGENTS:
            placements.append(Placement("more", sid, len(row["agents"]) - MAX_AGENTS,
                                        INDENT * (d + 1), cursor, MORE_W, MORE_H, d + 1))
            cursor += MORE_H + V_GAP
        y = max(cursor - V_GAP, bubble_bottom) + SESSION_GAP
    for p in placements:
        width = max(width, p.x + p.w)
    return placements, width, (y - SESSION_GAP if rows else 0)
