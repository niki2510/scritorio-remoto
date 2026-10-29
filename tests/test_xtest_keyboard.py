import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import Xlib.X  # noqa: E402
import Xlib.XK  # noqa: E402

from modules import xtest_keyboard as xk  # noqa: E402

KC_SHIFT, KC_CTRL, KC_ALTGR = 50, 37, 108
KC_A, KC_2, KC_E = 38, 11, 26


class FakeDisplay:
    """Mapa de teclado mínimo similar al español."""

    def __init__(self):
        self.mapping = {
            ord("a"): [(KC_A, 0)],
            ord("A"): [(KC_A, 1)],
            ord("@"): [(KC_2, 4)],
            0x20AC: [(KC_E, 4)],  # €
        }
        self.names = {
            "Shift_L": KC_SHIFT,
            "Control_L": KC_CTRL,
            "ISO_Level3_Shift": KC_ALTGR,
            "Return": 36,
        }

    def keysym_to_keycodes(self, keysym):
        return iter(self.mapping.get(keysym, []))

    def keysym_to_keycode(self, keysym):
        for name, code in self.names.items():
            if Xlib.XK.string_to_keysym(name) == keysym:
                return code
        return 0

    def sync(self):
        pass


def make_keyboard(monkeypatch):
    events = []
    monkeypatch.setattr(
        xk.Xlib.ext.xtest,
        "fake_input",
        lambda dpy, kind, code: events.append(("down" if kind == Xlib.X.KeyPress else "up", code)),
    )
    kb = xk.XTestKeyboard.__new__(xk.XTestKeyboard)
    kb._display = FakeDisplay()
    kb._lock = threading.Lock()
    kb._held = set()
    return kb, events


def test_char_to_keysym():
    assert xk.char_to_keysym("a") == ord("a")
    assert xk.char_to_keysym("ñ") == 0xF1
    assert xk.char_to_keysym("€") == 0x20AC


def test_plain_lowercase_char(monkeypatch):
    kb, events = make_keyboard(monkeypatch)
    kb.key_event("down", "a")
    kb.key_event("up", "a")
    assert events == [("down", KC_A), ("up", KC_A)]


def test_uppercase_with_browser_shift(monkeypatch):
    kb, events = make_keyboard(monkeypatch)
    for action, key in [("down", "Shift"), ("down", "A"), ("up", "A"), ("up", "Shift")]:
        kb.key_event(action, key)
    assert events == [("down", KC_SHIFT), ("down", KC_A), ("up", KC_A), ("up", KC_SHIFT)]


def test_uppercase_without_shift_adds_it(monkeypatch):
    kb, events = make_keyboard(monkeypatch)
    kb.type_text("A")
    assert events == [("down", KC_SHIFT), ("down", KC_A), ("up", KC_A), ("up", KC_SHIFT)]


def test_altgr_from_windows_releases_control(monkeypatch):
    # En Windows, AltGr llega como Control + AltGraph; para escribir "@" hay
    # que soltar temporalmente Control o se interpretaría como un atajo.
    kb, events = make_keyboard(monkeypatch)
    kb.key_event("down", "Control")
    kb.key_event("down", "AltGraph")
    events.clear()
    kb.key_event("down", "@")
    assert ("up", KC_CTRL) in events
    assert ("down", KC_2) in events
    assert events.index(("up", KC_CTRL)) < events.index(("down", KC_2))
    assert events[-1] == ("down", KC_CTRL)


def test_ctrl_shortcut_keeps_control(monkeypatch):
    kb, events = make_keyboard(monkeypatch)
    kb.key_event("down", "Control")
    events.clear()
    kb.key_event("down", "a")
    assert events == [("down", KC_A), ("up", KC_A)]


def test_release_all(monkeypatch):
    kb, events = make_keyboard(monkeypatch)
    kb.key_event("down", "Shift")
    events.clear()
    kb.release_all()
    assert events == [("up", KC_SHIFT)]
    assert kb._held == set()
