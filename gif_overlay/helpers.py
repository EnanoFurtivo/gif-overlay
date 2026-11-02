"""Pure helpers: no GTK, unit-tested in tests/test_helpers.py."""
import html, re


def plain_text(s):
    """Notification bodies may carry a little HTML: show them as plain text."""
    s = html.unescape(re.sub(r"</?[A-Za-z][^<>]*>", "", s))  # tags only: keeps "<3", "->"
    return re.sub(r"[ \t]+", " ", s).strip()


NOTE_MAX_CHARS = 600  # a bubble shows ~8 lines anyway; more would only keep typing unseen

def make_note(app, replaces, summary, body, hints):
    """The bits of a Notify() call a bubble needs, as plain, bounded text."""
    return dict(app=app, replaces=replaces, summary=plain_text(summary)[:NOTE_MAX_CHARS],
                body=plain_text(body)[:NOTE_MAX_CHARS], entry=str(hints.get("desktop-entry", "")))


def display_seconds(text):
    """How long a bubble stays up once typed: longer for more text, 4-12s."""
    return min(12, max(4, 3 + len(text) / 15))


def parse_panels(s):
    """'x,y,w,h;x,y,w,h' from the KWin script -> [(x, y, w, h), ...]"""
    return [tuple(round(float(v)) for v in r.split(",")) for r in s.split(";") if r]


def panel_offset(panels, mon):
    """How far visible panels at the bottom of monitor `mon` reach up into it."""
    mx, my, mw, mh = mon
    bottom = my + mh
    return max([bottom - y for x, y, w, h in panels
                if w > h  # horizontal panels only: a side panel also touches the bottom
                and x < mx + mw and x + w > mx and abs(y + h - bottom) <= 2 and y < bottom]
               + [0])


class BubbleQueue:
    """FIFO of notes waiting for their bubble; an update replaces its app's queued note."""
    def __init__(self, maxlen=10):
        self.notes, self.maxlen = [], maxlen

    def push(self, note):
        if note["replaces"]:
            for i, queued in enumerate(self.notes):
                if queued["app"] == note["app"]:
                    self.notes[i] = note
                    return "replaced"
        self.notes.append(note)
        del self.notes[:-self.maxlen]
        return "queued"

    def pop(self):
        return self.notes.pop(0) if self.notes else None

    def clear(self):
        self.notes.clear()

    def __len__(self):
        return len(self.notes)
