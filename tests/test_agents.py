"""Tests for the agent tracker, sprite picking, layout and hook merging. Run: python -I tests/test_agents.py"""
import os, sys, traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from gif_overlay.agents import (AgentTracker, HOOK_MARKER, layout, merge_hooks, pick_gif,
                                remove_hooks, session_gifs)


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


# ---------------------------------------------------------------- picking
def test_pick_gif_stable_and_excludes():
    gifs = [f"g{i}" for i in range(9)]
    a = pick_gif(gifs, "sess", 0, {"g0"})
    assert a == pick_gif(gifs, "sess", 0, {"g0"}) and a != "g0"
    picks = session_gifs(gifs, "sess", 3, "g0")
    assert len(picks) == 4 and "g0" not in picks
    assert all(picks[d] != picks[d - 1] for d in range(1, 4))

def test_pick_gif_small_folders():
    assert session_gifs([], "s", 2, "main") == ["main", "main", "main"]
    assert session_gifs(["main"], "s", 1, "main") == ["main", "main"]
    two = session_gifs(["main", "b"], "s", 1, "main")
    assert two[0] == "b" and two[1] in ("main", "b")


# ---------------------------------------------------------------- layout
def square(_sid, _depth):
    return 1.0

def no_bubble(_sid):
    return (0, 0)

def sprites(placements):
    return [(p.kind, p.session_id, p.index, p.x, p.y, p.w, p.h, p.depth) for p in placements]

def test_layout_rows_and_depth_sizes():
    rows = [{"id": "a", "agents": [1, 2, 1], "waiting": False},
            {"id": "b", "agents": [], "waiting": False}]
    placements, w, h = layout(rows, 100, square, no_bubble)
    assert sprites(placements) == [
        ("sprite", "a", -1, 0, 0, 100, 100, 0),
        ("sprite", "a", 0, 106, 35, 65, 65, 1),
        ("sprite", "a", 2, 177, 35, 65, 65, 1),
        ("sprite", "a", 1, 248, 58, 42, 42, 2),
        ("sprite", "b", -1, 0, 110, 100, 100, 0),
    ]
    assert (w, h) == (290, 210)

def test_layout_overflow_label():
    placements, _, _ = layout([{"id": "a", "agents": [1] * 30, "waiting": False}], 100, square,
                              no_bubble)
    kinds = [p.kind for p in placements]
    assert kinds.count("sprite") == 13 and kinds[-1] == "more"
    assert placements[-1].index == 18

def test_layout_bubble_right_of_row():
    placements, w, h = layout([{"id": "a", "agents": [], "waiting": True}], 100, square,
                              lambda s: (200, 60))
    assert sprites(placements)[1] == ("bubble", "a", -1, 106, 20, 200, 60, 0)
    assert (w, h) == (306, 100)
    placements, _, h = layout([{"id": "a", "agents": [], "waiting": True}], 100, square,
                              lambda s: (200, 150))
    assert sprites(placements)[0][4] == 25 and sprites(placements)[1][4] == 0 and h == 150


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
