"""Tests for gif-overlay's pure helpers. Run: python -I tests/test_helpers.py

Loads only the source above the options section, so no GTK window or argparse runs.
"""
import os, sys, traceback

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gif-overlay")
MARKER = "# ---------------------------------------------------------------- options"
ns = {"__name__": "gif_overlay_helpers"}
exec(open(SRC).read().split(MARKER)[0], ns)
plain_text, display_seconds = ns["plain_text"], ns["display_seconds"]
parse_panels, panel_offset, BubbleQueue = ns["parse_panels"], ns["panel_offset"], ns["BubbleQueue"]
make_note = ns.get("make_note")


def note(app, n=0, replaces=0):
    return {"app": app, "summary": f"s{n}", "body": "", "entry": "", "replaces": replaces}


def test_plain_text_strips_tags_and_entities():
    assert plain_text("<b>Hi</b> &amp; <a href='x'>you</a>") == "Hi & you"

def test_plain_text_keeps_newlines():
    assert plain_text("a\n\nb  c") == "a\n\nb c"

def test_plain_text_keeps_angle_brackets_that_arent_tags():
    assert plain_text("love you <3 see you -> there") == "love you <3 see you -> there"
    assert plain_text("if a < b and c > d") == "if a < b and c > d"

def test_make_note_clips_long_text():
    n = make_note("App", 0, "<b>Title</b>", "y" * 5000, {"desktop-entry": "org.kde.kate"})
    assert n["summary"] == "Title" and len(n["body"]) == 600
    assert n["entry"] == "org.kde.kate" and n["app"] == "App" and n["replaces"] == 0

def test_display_seconds_clamps():
    assert display_seconds("") == 4
    assert display_seconds("x" * 60) == 7
    assert display_seconds("x" * 1000) == 12

def test_parse_panels():
    assert parse_panels("") == []
    assert parse_panels("1920,1018,1920,62") == [(1920, 1018, 1920, 62)]
    assert parse_panels("0,0,10,10;1.6,2,3,4") == [(0, 0, 10, 10), (2, 2, 3, 4)]

def test_panel_offset_bottom_panel():
    assert panel_offset([(1920, 1018, 1920, 62)], (1920, 0, 1920, 1080)) == 62

def test_panel_offset_ignores_other_monitor_and_top():
    assert panel_offset([(0, 1018, 1920, 62), (1920, 0, 1920, 40)], (1920, 0, 1920, 1080)) == 0

def test_panel_offset_ignores_vertical_panels():
    mon = (1920, 0, 1920, 1080)
    assert panel_offset([(1920, 0, 44, 1080), (3796, 0, 44, 1080)], mon) == 0

def test_queue_fifo():
    q = BubbleQueue()
    q.push(note("a", 1)); q.push(note("b", 2))
    assert [q.pop()["summary"], q.pop()["summary"], q.pop()] == ["s1", "s2", None]

def test_queue_drops_oldest():
    q = BubbleQueue(maxlen=10)
    for i in range(1, 13):
        q.push(note("a", i))
    assert len(q) == 10
    assert q.pop()["summary"] == "s3"

def test_queue_replaces_same_app():
    q = BubbleQueue()
    assert q.push(note("A", 1)) == "queued"
    q.push(note("B", 2))
    assert q.push(note("A", 3, replaces=5)) == "replaced"
    assert [q.pop()["summary"], q.pop()["summary"], q.pop()] == ["s3", "s2", None]

def test_queue_clear():
    q = BubbleQueue()
    q.push(note("a")); q.clear()
    assert len(q) == 0 and q.pop() is None


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
