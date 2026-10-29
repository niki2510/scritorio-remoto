import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.input_control import resolve_key  # noqa: E402
from pynput.keyboard import Key  # noqa: E402


def test_resolve_key_special_keys():
    assert resolve_key("Enter") is Key.enter
    assert resolve_key("ArrowUp") is Key.up
    assert resolve_key("Shift") is Key.shift
    assert resolve_key("F5") is Key.f5


def test_resolve_key_printable_character():
    assert resolve_key("a") == "a"
    assert resolve_key("Z") == "Z"
    assert resolve_key("5") == "5"


def test_resolve_key_space():
    assert resolve_key(" ") is Key.space


def test_resolve_key_unknown_multi_char_returns_none():
    assert resolve_key("Unidentified") is None
    assert resolve_key("Dead") is None
