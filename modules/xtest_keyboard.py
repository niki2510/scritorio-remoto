"""
Inyección de teclado en Linux/X11 mediante la extensión XTest.

`pynput` envía los caracteres imprimibles con `XSendEvent` directamente a la
ventana con foco. Esos eventos llegan marcados como "sintéticos" y los
ignoran los diálogos que capturan el teclado a nivel de compositor, como el
cuadro "Se necesita autenticación" (contraseña de administrador/polkit) o
la pantalla de bloqueo de GNOME Shell. XTest, en cambio, inyecta las teclas
en el propio servidor X, igual que un teclado físico, por lo que funcionan
en cualquier ventana o diálogo.
"""
import threading
import time

import Xlib.display
import Xlib.X
import Xlib.XK
import Xlib.ext.xtest
from pynput._util.xorg_keysyms import CHARS, SYMBOLS

# Nombre de tecla del navegador (KeyboardEvent.key) -> nombre de keysym X11.
SPECIAL_KEYSYMS = {
    "Enter": "Return", "Backspace": "BackSpace", "Tab": "Tab",
    "Escape": "Escape", "Delete": "Delete", "Insert": "Insert",
    "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
    "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
    "CapsLock": "Caps_Lock", "ContextMenu": "Menu", "PrintScreen": "Print",
    "ScrollLock": "Scroll_Lock", "Pause": "Pause", "NumLock": "Num_Lock",
    **{f"F{i}": f"F{i}" for i in range(1, 13)},
}

# Modificadores: nombre del navegador -> keysym X11 que se pulsa.
MODIFIER_KEYSYMS = {
    "Shift": "Shift_L",
    "Control": "Control_L",
    "Alt": "Alt_L",
    "AltGraph": "ISO_Level3_Shift",
    "Meta": "Super_L",
}

# Índice de columna del mapa de teclado X -> modificadores necesarios.
_INDEX_MODIFIERS = {
    0: (),
    1: ("Shift",),
    4: ("AltGraph",),
    5: ("AltGraph", "Shift"),
}

_SHORTCUT_MODIFIERS = {"Control", "Alt", "Meta"}


def char_to_keysym(char: str):
    if char == "\n":
        return Xlib.XK.string_to_keysym("Return")
    if char == "\t":
        return Xlib.XK.string_to_keysym("Tab")
    name = CHARS.get(char)
    if name and name in SYMBOLS:
        return SYMBOLS[name][0]
    code = ord(char)
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 | code


class XTestKeyboard:
    def __init__(self, display_name=None):
        self._display = Xlib.display.Display(display_name)
        self._lock = threading.Lock()
        self._held = set()

    # ------------------------------------------------------------------ util
    def _fake(self, keycode: int, press: bool) -> None:
        Xlib.ext.xtest.fake_input(
            self._display, Xlib.X.KeyPress if press else Xlib.X.KeyRelease, keycode
        )

    def _keycode_for_name(self, keysym_name: str) -> int:
        return self._display.keysym_to_keycode(Xlib.XK.string_to_keysym(keysym_name))

    def _set_modifier(self, name: str, press: bool) -> None:
        keycode = self._keycode_for_name(MODIFIER_KEYSYMS[name])
        if keycode:
            self._fake(keycode, press)

    def _find_keycode(self, keysym: int):
        """Devuelve (keycode, modificadores) usando columnas que sabemos
        reproducir (normal, Shift, AltGr, AltGr+Shift)."""
        for keycode, index in self._display.keysym_to_keycodes(keysym):
            if index in _INDEX_MODIFIERS:
                return keycode, _INDEX_MODIFIERS[index]
        return None, None

    def _tap_with_temporary_mapping(self, keysym: int) -> None:
        """Para caracteres que no existen en la distribución de teclado
        actual: asigna temporalmente el keysym a un keycode libre (igual que
        hace xdotool), lo pulsa y restaura el mapa."""
        first = self._display.display.info.min_keycode
        count = self._display.display.info.max_keycode - first + 1
        mapping = self._display.get_keyboard_mapping(first, count)
        spare = None
        for offset, syms in enumerate(mapping):
            if not any(syms):
                spare = first + offset
                break
        if spare is None:
            return
        width = len(mapping[0])
        self._display.change_keyboard_mapping(spare, [(keysym,) * width])
        self._display.sync()
        time.sleep(0.03)
        released = [m for m in self._held if m in ("Shift", "AltGraph")]
        for m in released:
            self._set_modifier(m, False)
        self._fake(spare, True)
        self._fake(spare, False)
        for m in released:
            self._set_modifier(m, True)
        self._display.sync()
        time.sleep(0.03)
        self._display.change_keyboard_mapping(spare, [(0,) * width])
        self._display.sync()

    def _tap_char(self, char: str) -> None:
        keysym = char_to_keysym(char)
        keycode, required = self._find_keycode(keysym)
        if keycode is None:
            self._tap_with_temporary_mapping(keysym)
            return

        required = set(required)
        shortcut = bool(self._held & _SHORTCUT_MODIFIERS) and "AltGraph" not in self._held
        if shortcut:
            # Atajo (Ctrl+C, Alt+F4...): se respetan los modificadores que
            # tiene pulsados el usuario y solo se añaden los que falten.
            to_release = set()
        else:
            # Escritura normal: el navegador ya nos manda el carácter final,
            # así que se ajustan los modificadores exactamente a los que hacen
            # falta en la distribución del servidor.
            to_release = self._held - required
        to_press = required - self._held

        for m in to_release:
            self._set_modifier(m, False)
        for m in to_press:
            self._set_modifier(m, True)
        self._fake(keycode, True)
        self._fake(keycode, False)
        for m in to_press:
            self._set_modifier(m, False)
        for m in to_release:
            self._set_modifier(m, True)

    # ------------------------------------------------------------- pública
    def key_event(self, action: str, key_name: str) -> None:
        press = action == "down"
        with self._lock:
            if key_name in MODIFIER_KEYSYMS:
                if press and key_name not in self._held:
                    self._set_modifier(key_name, True)
                    self._held.add(key_name)
                elif not press and key_name in self._held:
                    self._set_modifier(key_name, False)
                    self._held.discard(key_name)
            elif key_name in SPECIAL_KEYSYMS:
                keycode = self._keycode_for_name(SPECIAL_KEYSYMS[key_name])
                if keycode:
                    self._fake(keycode, press)
            elif len(key_name) == 1:
                # Los caracteres se "teclean" completos al pulsar; la
                # repetición automática del navegador envía más "down".
                if press:
                    self._tap_char(key_name)
            self._display.sync()

    def type_text(self, text: str) -> None:
        with self._lock:
            for char in text:
                self._tap_char(char)
            self._display.sync()

    def release_all(self) -> None:
        with self._lock:
            for m in list(self._held):
                self._set_modifier(m, False)
            self._held.clear()
            self._display.sync()
