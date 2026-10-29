"""
Control remoto de mouse y teclado multiplataforma.

- Ratón: `pynput` en todos los sistemas (en X11 ya usa XTest).
- Teclado en Linux/X11: XTest (ver `xtest_keyboard.py`), para que funcione
  también en diálogos de GNOME Shell como "Se necesita autenticación" o la
  pantalla de bloqueo, que ignoran los eventos que envía `pynput`.
- Teclado en Windows/macOS: `pynput` (SendInput / Quartz).

Los controladores se crean de forma perezosa para que el módulo pueda
importarse en cualquier entorno (por ejemplo, sin sesión gráfica).
"""
import sys

from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

from .screen_capture import ScreenCapture

_mouse = None
_keyboard = None
_xtest_keyboard = None
_screen = ScreenCapture()


def _get_xtest_keyboard():
    """Devuelve el teclado XTest en Linux, o None si no está disponible."""
    global _xtest_keyboard
    if not sys.platform.startswith("linux"):
        return None
    if _xtest_keyboard is None:
        from .xtest_keyboard import XTestKeyboard
        _xtest_keyboard = XTestKeyboard()
    return _xtest_keyboard


def _get_mouse() -> MouseController:
    global _mouse
    if _mouse is None:
        _mouse = MouseController()
    return _mouse


def _get_keyboard() -> KeyboardController:
    global _keyboard
    if _keyboard is None:
        _keyboard = KeyboardController()
    return _keyboard


def get_screen_size():
    return _screen.width, _screen.height


BUTTON_MAP = {
    0: Button.left,
    1: Button.middle,
    2: Button.right,
}

# Traducción de `KeyboardEvent.key` (navegador) a teclas especiales de pynput.
SPECIAL_KEYS = {
    "Enter": Key.enter,
    "Backspace": Key.backspace,
    "Tab": Key.tab,
    "Shift": Key.shift,
    "Control": Key.ctrl,
    "Alt": Key.alt,
    "AltGraph": Key.alt_gr,
    "Meta": Key.cmd,
    "Escape": Key.esc,
    "Delete": Key.delete,
    "Insert": Key.insert,
    "Home": Key.home,
    "End": Key.end,
    "PageUp": Key.page_up,
    "PageDown": Key.page_down,
    "ArrowUp": Key.up,
    "ArrowDown": Key.down,
    "ArrowLeft": Key.left,
    "ArrowRight": Key.right,
    "CapsLock": Key.caps_lock,
    "ContextMenu": Key.menu,
    "PrintScreen": Key.print_screen,
    "ScrollLock": Key.scroll_lock,
    "Pause": Key.pause,
    " ": Key.space,
    "F1": Key.f1, "F2": Key.f2, "F3": Key.f3, "F4": Key.f4,
    "F5": Key.f5, "F6": Key.f6, "F7": Key.f7, "F8": Key.f8,
    "F9": Key.f9, "F10": Key.f10, "F11": Key.f11, "F12": Key.f12,
}


def resolve_key(key_name: str):
    """Convierte el nombre de tecla recibido del navegador a un valor pynput."""
    if key_name in SPECIAL_KEYS:
        return SPECIAL_KEYS[key_name]
    if len(key_name) == 1:
        return key_name
    return None


def move_mouse(rel_x: float, rel_y: float) -> None:
    """Mueve el mouse a una posición relativa (0.0 - 1.0) de la pantalla."""
    width, height = get_screen_size()
    x = _screen.left + int(max(0.0, min(1.0, rel_x)) * width)
    y = _screen.top + int(max(0.0, min(1.0, rel_y)) * height)
    _get_mouse().position = (x, y)


def mouse_button(action: str, button_index: int) -> None:
    button = BUTTON_MAP.get(button_index, Button.left)
    if action == "down":
        _get_mouse().press(button)
    else:
        _get_mouse().release(button)


def mouse_scroll(delta_x: float, delta_y: float) -> None:
    # pynput espera "clicks" de rueda; dividimos para suavizar el scroll de pixel.
    _get_mouse().scroll(delta_x / 50.0, -delta_y / 50.0)


def key_event(action: str, key_name: str) -> None:
    xtest = _get_xtest_keyboard()
    if xtest is not None:
        xtest.key_event(action, key_name)
        return

    key = resolve_key(key_name)
    if key is None:
        return
    keyboard = _get_keyboard()
    if action == "down":
        keyboard.press(key)
    else:
        keyboard.release(key)


def type_text(text: str) -> None:
    if not text:
        return
    xtest = _get_xtest_keyboard()
    if xtest is not None:
        xtest.type_text(text)
    else:
        _get_keyboard().type(text)


def release_all_keys() -> None:
    """Suelta los modificadores que hayan quedado pulsados (p. ej. si el
    navegador se desconecta con Shift o Ctrl mantenidos)."""
    xtest = _get_xtest_keyboard()
    if xtest is not None:
        xtest.release_all()
        return
    keyboard = _get_keyboard()
    for key in (Key.shift, Key.ctrl, Key.alt, Key.alt_gr, Key.cmd):
        try:
            keyboard.release(key)
        except Exception:
            pass
