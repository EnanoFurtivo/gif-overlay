"""Tests for the agent tracker, sprite picking, layout and hook merging. Run: python -I tests/test_agents.py"""
import os, sys, traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from gif_overlay.agents import (AgentTracker, HOOK_MARKER, forest, format_elapsed, gif_for_level, layout,
                                level_gifs, merge_hooks, remove_hooks)


def ev(name, sid="s1", **kw):
    return dict(session_id=sid, hook_event_name=name, **kw)


def active_ids(t, now):
    return [s.id for s in t.active(now)]


# ---------------------------------------------------------------- tracker
def test_prompt_makes_active_and_stop_expires_after_grace():
    t = AgentTracker(idle_grace=4.0)
    t.event(ev("SessionStart"), 0)
    assert active_ids(t, 0) == []
    t.event(ev("UserPromptSubmit"), 1)
    assert active_ids(t, 1) == ["s1"]
    t.event(ev("Stop"), 10)
    assert active_ids(t, 13) == ["s1"]
    assert active_ids(t, 14.5) == []

def test_subagent_depths():
    t = AgentTracker()
    t.event(ev("UserPromptSubmit"), 0)
    t.event(ev("SubagentStart", agent_id="a"), 1)
    t.event(ev("SubagentStart", agent_id="b", parent_agent_id="a"), 2)
    t.event(ev("SubagentStart", agent_id="c"), 3)
    assert t.active(3)[0].agents == {"a": 1, "b": 2, "c": 1}
    t.event(ev("SubagentStop", agent_id="b"), 4)
    assert t.active(4)[0].agents == {"a": 1, "c": 1}

def test_unknown_parent_is_depth_one():
    t = AgentTracker()
    t.event(ev("SubagentStart", agent_id="x", parent_agent_id="ghost"), 0)
    assert t.active(0)[0].agents == {"x": 1}

def test_running_subagents_keep_session_active_after_stop():
    t = AgentTracker(idle_grace=4.0)
    t.event(ev("UserPromptSubmit"), 0)
    t.event(ev("SubagentStart", agent_id="bg"), 1)
    t.event(ev("Stop"), 2)
    assert active_ids(t, 60) == ["s1"]
    t.event(ev("SubagentStop", agent_id="bg"), 61)
    assert active_ids(t, 70) == []

def test_waiting_from_notification_permission_and_tools():
    t = AgentTracker()
    t.event(ev("UserPromptSubmit"), 0)
    t.event(ev("PreToolUse", tool_name="AskUserQuestion"), 1)
    assert t.active(1)[0].waiting == "has a question for you"
    t.event(ev("PostToolUse"), 2)
    assert t.active(2)[0].waiting is None
    t.event(ev("PermissionRequest", tool_name="Bash"), 3)
    assert t.active(3)[0].waiting == "wants to use Bash"
    t.event(ev("PostToolUse"), 4)
    t.event(ev("Notification", notification_type="permission_prompt",
               message="Claude needs your permission"), 5)
    assert t.active(5)[0].waiting == "Claude needs your permission"
    t.event(ev("UserPromptSubmit"), 6)
    assert t.active(6)[0].waiting is None
    t.event(ev("PreToolUse", tool_name="ExitPlanMode"), 7)
    assert t.active(7)[0].waiting == "wants you to approve a plan"
    assert t.any_waiting(7)

def test_waiting_session_stays_after_stop():
    t = AgentTracker(idle_grace=4.0)
    t.event(ev("Stop"), 0)
    t.event(ev("Notification", notification_type="permission_prompt", message="hi"), 1)
    assert active_ids(t, 100) == ["s1"]

def test_subagent_tools_dont_touch_main_state():
    t = AgentTracker(idle_grace=4.0)
    t.event(ev("UserPromptSubmit"), 0)
    t.event(ev("SubagentStart", agent_id="bg"), 1)
    t.event(ev("PreToolUse", tool_name="AskUserQuestion"), 2)
    t.event(ev("PostToolUse", agent_id="bg"), 3)
    assert t.active(3)[0].waiting == "has a question for you"
    t.event(ev("PostToolUse"), 4)
    t.event(ev("SubagentStop", agent_id="bg"), 5)
    t.event(ev("Stop"), 6)
    assert active_ids(t, 11) == []

def test_tools_from_an_unseen_subagent_reveal_it():
    # the overlay started listening after the turn (and its subagents) began: the only
    # sign of life is subagents' tool calls, which must still put the session on screen
    t = AgentTracker(idle_grace=4.0)
    t.event(ev("PostToolUse", agent_id="bg"), 100)
    s = t.active(100)[0]
    assert s.agents == {"bg": 1} and s.agent_started == {"bg": 100} and not s.working
    t.event(ev("PostToolUse", agent_id="bg"), 101)
    assert s.agent_started == {"bg": 100}
    t.event(ev("SubagentStop", agent_id="bg"), 200)
    assert active_ids(t, 210) == []

def test_stop_clears_waiting():
    t = AgentTracker(idle_grace=4.0)
    t.event(ev("PermissionRequest", tool_name="Bash"), 0)
    t.event(ev("Stop"), 1)
    assert active_ids(t, 6) == []

def test_only_blocking_notifications_wait():
    t = AgentTracker(idle_grace=4.0)
    t.event(ev("Notification", notification_type="auth_success", message="ok"), 0)
    assert active_ids(t, 0) == []
    t.event(ev("Notification", notification_type="elicitation_dialog", message="pick one"), 1)
    assert t.active(1)[0].waiting == "pick one"

def test_idle_prompt_ignored():
    t = AgentTracker(idle_grace=4.0)
    t.event(ev("Stop"), 0)
    t.event(ev("Notification", notification_type="idle_prompt", message="waiting"), 60)
    assert active_ids(t, 61) == []

def test_session_end_forgets():
    t = AgentTracker()
    t.event(ev("UserPromptSubmit"), 0)
    t.event(ev("SessionEnd"), 1)
    assert active_ids(t, 1) == []

def test_stale_sessions_expire():
    t = AgentTracker(stale=1800)
    t.event(ev("UserPromptSubmit", sid="old"), 0)
    t.event(ev("SubagentStart", sid="old", agent_id="a"), 1)
    t.event(ev("UserPromptSubmit", sid="new"), 1500)
    assert active_ids(t, 1500) == ["old", "new"]
    assert active_ids(t, 1802) == ["new"]

def test_malformed_payload_ignored():
    t = AgentTracker()
    for bad in [{}, {"session_id": 3, "hook_event_name": "Stop"}, {"session_id": "s"},
                ev("SubagentStart"), ev("Whatever")]:
        t.event(bad, 0)
    assert active_ids(t, 0) == []

def test_active_in_first_seen_order_and_cwd_kept():
    t = AgentTracker()
    t.event(ev("UserPromptSubmit", sid="b", cwd="/x/proj"), 0)
    t.event(ev("UserPromptSubmit", sid="a"), 1)
    assert active_ids(t, 2) == ["b", "a"]
    assert t.active(2)[0].cwd == "/x/proj"


# ---------------------------------------------------------------- tree order
def test_tree_order_puts_children_under_parents():
    t = AgentTracker()
    for aid, parent in [("a", None), ("b", None), ("c", "a"), ("d", "c"), ("e", "b")]:
        t.event(ev("SubagentStart", agent_id=aid, parent_agent_id=parent), 0)
    s = t.active(0)[0]
    assert t.tree(s) == [("a", 1), ("c", 2), ("d", 3), ("b", 1), ("e", 2)]
    t.event(ev("SubagentStop", agent_id="a"), 1)
    assert t.tree(s) == [("b", 1), ("e", 2), ("c", 2), ("d", 3)]


# ---------------------------------------------------------------- persistence
def test_tracker_survives_a_restart():
    import json
    t = AgentTracker()
    t.event(ev("UserPromptSubmit", cwd="/x/proj"), 10)
    t.event(ev("SubagentStart", agent_id="a"), 20)
    t.event(ev("SubagentStart", agent_id="b", parent_agent_id="a"), 30)
    t.event(ev("PreToolUse", sid="s2", tool_name="AskUserQuestion"), 40)
    saved = json.loads(json.dumps(t.dump()))  # through the file format
    t2 = AgentTracker()
    t2.load(saved)
    a, b = t2.active(50)
    assert (a.id, a.first_seen, a.cwd, a.working) == ("s1", 10, "/x/proj", True)
    assert a.agents == {"a": 1, "b": 2} and a.agent_started == {"a": 20, "b": 30}
    assert t2.tree(a) == [("a", 1), ("b", 2)]
    assert (b.id, b.waiting) == ("s2", "has a question for you")

def test_tracker_load_ignores_garbage():
    t = AgentTracker()
    for junk in [None, {}, {"sessions": "x"}, {"sessions": [{"id": "s"}]}, []]:
        t.load(junk)
        assert t.active(0) == []


# ---------------------------------------------------------------- timers
def test_format_elapsed():
    assert [format_elapsed(t) for t in (0, 59.9, 61, 3599, 3600, 3725)] == \
        ["0:00", "0:59", "1:01", "59:59", "1:00:00", "1:02:05"]

def test_subagent_start_times():
    t = AgentTracker()
    t.event(ev("UserPromptSubmit"), 5)
    t.event(ev("SubagentStart", agent_id="a"), 10)
    s = t.active(10)[0]
    assert s.first_seen == 5 and s.agent_started == {"a": 10}
    t.event(ev("SubagentStop", agent_id="a"), 20)
    assert s.agent_started == {}


# ---------------------------------------------------------------- level GIFs
def test_level_gifs_from_folder():
    paths = ["/g/level1.gif", "/g/level3.webp", "/g/rumba.gif", "/g/Level2.GIF", "/g/level.gif"]
    assert level_gifs(paths) == {1: "/g/level1.gif", 2: "/g/Level2.GIF", 3: "/g/level3.webp"}

def test_gif_for_level_falls_back_to_deepest():
    levels = {1: "a", 3: "c"}
    assert [gif_for_level(levels, n, "main") for n in (1, 2, 3, 5)] == ["a", "a", "c", "c"]
    assert gif_for_level({3: "c"}, 1, "main") == "c"
    assert gif_for_level({}, 2, "main") == "main"


# ---------------------------------------------------------------- layout
def square(_depth):
    return 1.0

def no_bubble(_sid):
    return (0, 0)

def caption(_sid):
    return (60, 14)

def boxes(placements):
    return [(p.kind, p.session_id, p.index, p.x, p.y, p.w, p.h, p.depth) for p in placements]

def test_layout_tree_column():
    rows = [{"id": "a", "agents": [1, 2, 1], "waiting": False},
            {"id": "b", "agents": [], "waiting": False}]
    placements, w, h = layout(rows, 100, square, no_bubble, caption)
    assert boxes(placements) == [
        ("sprite", "a", -1, 0, 0, 100, 100, 0),
        ("caption", "a", -1, 20, 102, 60, 14, 0),
        ("sprite", "a", 0, 18, 120, 55, 55, 1),
        ("sprite", "a", 1, 36, 179, 30, 30, 2),
        ("sprite", "a", 2, 18, 213, 55, 55, 1),
        ("sprite", "b", -1, 0, 280, 100, 100, 0),
        ("caption", "b", -1, 20, 382, 60, 14, 0),
    ]
    assert (w, h) == (100, 396)

def test_layout_deep_levels_have_a_minimum_size():
    placements, _, _ = layout([{"id": "a", "agents": [5], "waiting": False}], 100, square,
                              no_bubble, caption)
    assert placements[-1].h == 16

def test_layout_overflow_label():
    placements, _, _ = layout([{"id": "a", "agents": [1] * 30, "waiting": False}], 100, square,
                              no_bubble, caption)
    kinds = [p.kind for p in placements]
    assert kinds.count("sprite") == 13 and kinds[-1] == "more"
    assert placements[-1].index == 18

def test_layout_bubble_beside_session_sprite():
    placements, w, h = layout([{"id": "a", "agents": [], "waiting": True}], 100, square,
                              lambda s: (200, 60), caption)
    assert boxes(placements)[-1] == ("bubble", "a", -1, 108, 20, 200, 60, 0)
    assert (w, h) == (308, 116)
    placements, _, h = layout([{"id": "a", "agents": [], "waiting": True}], 100, square,
                              lambda s: (200, 150), caption)
    assert boxes(placements)[-1][4] == 0 and h == 150


def test_layout_forked_session_nests_like_a_subagent():
    rows = [{"id": "p", "agents": [], "waiting": False},
            {"id": "c", "agents": [1], "waiting": True, "depth": 1, "parent": "p"},
            {"id": "x", "agents": [], "waiting": False}]
    placements, _w, _h = layout(rows, 100, square, lambda s: (50, 20), caption)
    assert boxes(placements) == [
        ("sprite", "p", -1, 0, 0, 100, 100, 0),
        ("caption", "p", -1, 20, 102, 60, 14, 0),
        ("sprite", "c", -1, 18, 120, 55, 55, 1),       # indented, 55%, a V_GAP below
        ("caption", "c", -1, 18, 177, 60, 14, 1),      # never left of its sprite
        ("bubble", "c", -1, 18 + 60 + 8, 137, 50, 20, 1),
        ("sprite", "c", 0, 36, 195, 30, 30, 2),        # its subagents: deeper still
        ("sprite", "x", -1, 0, 237, 100, 100, 0),      # the next tree: SESSION_GAP
        ("caption", "x", -1, 20, 339, 60, 14, 0),
    ]
    assert placements[2].parent == "p" and placements[0].parent is None


# ---------------------------------------------------------------- forked sessions
def test_forest_nests_children_under_parents():
    parents = {"c": "p", "g": "c"}
    assert forest(["p", "c", "x", "g"], parents.get) == [
        ("p", 0, False), ("c", 1, False), ("g", 2, False), ("x", 0, False)]

def test_forest_idle_parent_is_a_ghost_where_its_first_child_was():
    parents = {"c": "p", "d": "p", "q": "gp", "gp": None}
    assert forest(["x", "c", "y", "d"], parents.get) == [
        ("x", 0, False), ("p", 0, True), ("c", 1, False), ("d", 1, False), ("y", 0, False)]
    parents = {"c": "p", "p": "gp"}  # an idle grandparent too
    assert forest(["c"], parents.get) == [("gp", 0, True), ("p", 1, True), ("c", 2, False)]

def test_forest_cycles_and_self_parents_terminate():
    assert forest(["a", "b"], {"a": "b", "b": "a"}.get) in (
        [("b", 0, False), ("a", 1, False)], [("a", 0, False), ("b", 1, False)])
    assert forest(["a"], {"a": "a"}.get) == [("a", 0, False)]
    chain = {str(i): str(i + 1) for i in range(100)}
    rows = forest(["0"], chain.get)
    assert rows[-1] == ("0", len(rows) - 1, False) and len(rows) <= 7


# ---------------------------------------------------------------- hooks

def test_hooks_merge_keeps_others_and_is_idempotent():
    mine = {"type": "command", "command": "notify-send hi"}
    settings = {"autoMode": True, "hooks": {"Stop": [{"hooks": [mine]}]}}
    once = merge_hooks(settings)
    twice = merge_hooks(once)
    assert once == twice
    assert once["autoMode"] is True
    stop_cmds = [h["command"] for g in once["hooks"]["Stop"] for h in g["hooks"]]
    assert "notify-send hi" in stop_cmds and sum(HOOK_MARKER in c for c in stop_cmds) == 1
    pre = once["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == "AskUserQuestion|ExitPlanMode"
    for event in ("SessionStart", "SessionEnd", "UserPromptSubmit", "SubagentStart",
                  "SubagentStop", "Notification", "PermissionRequest", "PostToolUse"):
        assert any(HOOK_MARKER in h["command"] for g in once["hooks"][event] for h in g["hooks"])
    assert settings == {"autoMode": True, "hooks": {"Stop": [{"hooks": [mine]}]}}  # not mutated

def test_hooks_remove_only_ours():
    mine = {"type": "command", "command": "notify-send hi"}
    settings = {"hooks": {"Stop": [{"hooks": [mine]}]}, "x": 1}
    assert remove_hooks(merge_hooks(settings)) == settings
    assert remove_hooks({"x": 1}) == {"x": 1}


# ---------------------------------------------------------------- the hook script
import subprocess, tempfile

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gif-overlay-hook")

def run_hook(payload):
    """Run gif-overlay-hook with a fake busctl that records what it would send."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "sent")
        with open(os.path.join(d, "busctl"), "w") as f:
            f.write(f'#!/bin/sh\nfor a; do last="$a"; done; printf %s "$last" > {out}\n')
        os.chmod(os.path.join(d, "busctl"), 0o755)
        r = subprocess.run(["sh", HOOK], input=payload.encode(), env={"PATH": d + ":/usr/bin:/bin"},
                           timeout=10)
        assert r.returncode == 0
        return open(out).read() if os.path.exists(out) else None

def test_hook_forwards_small_payloads_verbatim():
    p = '{"session_id": "s1", "hook_event_name": "Stop", "cwd": "/x"}'
    assert run_hook(p) == p

def test_hook_shrinks_post_tool_use_and_keeps_agent_id():
    import json
    p = json.dumps({"session_id": "s1", "transcript_path": "/t", "agent_id": "ag-7",
                    "hook_event_name": "PostToolUse", "tool_name": "Read",
                    "tool_input": {"agent_id": "fake"}, "tool_response": "x" * 300000})
    assert json.loads(run_hook(p)) == {"session_id": "s1", "agent_id": "ag-7",
                                       "hook_event_name": "PostToolUse"}

def test_hook_shrinks_huge_prompts():
    import json
    p = json.dumps({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
                    "prompt": "y" * 200000})
    assert json.loads(run_hook(p)) == {"session_id": "s1", "hook_event_name": "UserPromptSubmit"}


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
