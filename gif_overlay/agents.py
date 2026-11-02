"""Claude agents -> sprites: hook event tracking, GIF picking, layout, hook installation.

Pure (no GTK); unit-tested in tests/test_agents.py.
"""
import copy, os, re
from collections import namedtuple
from dataclasses import dataclass, field

# ---------------------------------------------------------------- hooks
# Claude Code runs these on each event; busctl hands the JSON payload to the running
# overlay and is a quick no-op when it isn't running.
HOOK_MARKER = "local.GifOverlay AgentEvent"
_BUSCTL = ("busctl --user --timeout=1 call local.GifOverlay /local/GifOverlay "
           "local.GifOverlay AgentEvent s")
EVENT_CMD = f'{_BUSCTL} "$(cat)" >/dev/null 2>&1 || true'
# PostToolUse payloads carry whole tool outputs (too big for one argument): only forward
# which session finished a tool, which is all the tracker needs.
TOOL_DONE_CMD = (
    "p=$(cat); sid=$(printf %s \"$p\" | head -c 600 | grep -o '\"session_id\": *\"[^\"]*\"' "
    f"| head -n1); {_BUSCTL} \"{{$sid,\\\"hook_event_name\\\":\\\"PostToolUse\\\"}}\" "
    ">/dev/null 2>&1 || true")
HOOK_EVENTS = {  # event -> (matcher or None, command)
    "SessionStart": (None, EVENT_CMD),
    "SessionEnd": (None, EVENT_CMD),
    "UserPromptSubmit": (None, EVENT_CMD),
    "Stop": (None, EVENT_CMD),
    "SubagentStart": (None, EVENT_CMD),
    "SubagentStop": (None, EVENT_CMD),
    "Notification": (None, EVENT_CMD),
    "PermissionRequest": (None, EVENT_CMD),
    "PreToolUse": ("AskUserQuestion|ExitPlanMode", EVENT_CMD),
    "PostToolUse": ("*", TOOL_DONE_CMD),
}


def remove_hooks(settings):
    """A copy of Claude Code `settings` without our hook entries (others untouched)."""
    settings = copy.deepcopy(settings)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", []) if HOOK_MARKER not in h.get("command", "")]
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
    for event, (matcher, command) in HOOK_EVENTS.items():
        group = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    return settings


# ---------------------------------------------------------------- tracker
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
        if name == "Notification" and payload.get("notification_type") == "idle_prompt":
            return False  # "still waiting after 60s idle": every finished chat would light up
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
        elif name == "Stop":
            s.working, s.stopped_at = False, now
        elif name == "SubagentStart":
            parent = payload.get("parent_agent_id")
            s.agents[agent] = s.agents[parent] + 1 if parent in s.agents else 1
            s.parents[agent] = parent
        elif name == "SubagentStop":
            s.agents.pop(agent, None)
            s.parents.pop(agent, None)
        elif name == "Notification":
            s.waiting = str(payload.get("message") or "needs your input")
        elif name == "PermissionRequest":
            tool = payload.get("tool_name")
            s.waiting = f"wants to use {tool}" if tool else "needs your permission"
        elif name == "PreToolUse":
            s.waiting = WAITING_TOOLS[payload["tool_name"]]
        elif name == "PostToolUse":
            s.working, s.waiting = True, None
        return True

    def active(self, now):
        """Sessions to show, oldest first: working, waiting, with subagents, or just stopped."""
        for sid in [sid for sid, s in self.sessions.items() if now - s.last_event > self.stale]:
            del self.sessions[sid]
        return sorted((s for s in self.sessions.values()
                       if s.working or s.waiting or s.agents
                       or (s.stopped_at is not None and now - s.stopped_at < self.idle_grace)),
                      key=lambda s: s.first_seen)

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
Placement = namedtuple("Placement", "kind session_id index x y w h depth")
DEPTH_RATIO, INDENT, V_GAP, SESSION_GAP, CAPTION_GAP, BUBBLE_GAP = 0.65, 18, 4, 12, 2, 8
MAX_AGENTS, MORE_W, MORE_H = 12, 40, 20


def layout(rows, agent_size, aspect, bubble_size, caption_size):
    """One column from the top-left: each session, its name, then its subagents below it.

    rows: [{"id", "agents": [depth, ...] in tree order, "waiting": bool}]; aspect(depth) is
    that level's GIF width/height, bubble_size / caption_size(session_id) give (w, h).
    Returns (placements, width, height). Sprite index = position in "agents" (-1 = the
    session); a "more" placement's index is how many subagents didn't fit.
    """
    placements, width, y = [], 0, 0
    for row in rows:
        sid, h0 = row["id"], agent_size
        w0 = round(h0 * aspect(0))
        placements.append(Placement("sprite", sid, -1, 0, y, w0, h0, 0))
        cw, ch = caption_size(sid)
        placements.append(Placement("caption", sid, -1, max(0, (w0 - cw) // 2),
                                    y + h0 + CAPTION_GAP, cw, ch, 0))
        bottom = y + h0 + CAPTION_GAP + ch
        if row["waiting"]:  # beside the session sprite, tail pointing at it
            bw, bh = bubble_size(sid)
            bx, by = max(w0, cw) + BUBBLE_GAP, y + max(0, (h0 - bh) // 2)
            placements.append(Placement("bubble", sid, -1, bx, by, bw, bh, 0))
            bubble_bottom = by + bh
        else:
            bubble_bottom = y
        cursor = bottom + V_GAP
        for index, depth in enumerate(row["agents"][:MAX_AGENTS]):
            h = round(agent_size * DEPTH_RATIO ** depth)
            w = round(h * aspect(depth))
            placements.append(Placement("sprite", sid, index, INDENT * depth, cursor, w, h, depth))
            cursor += h + V_GAP
        if len(row["agents"]) > MAX_AGENTS:
            placements.append(Placement("more", sid, len(row["agents"]) - MAX_AGENTS, INDENT,
                                        cursor, MORE_W, MORE_H, 1))
            cursor += MORE_H + V_GAP
        y = max(cursor - V_GAP, bubble_bottom) + SESSION_GAP
    for p in placements:
        width = max(width, p.x + p.w)
    return placements, width, (y - SESSION_GAP if rows else 0)
