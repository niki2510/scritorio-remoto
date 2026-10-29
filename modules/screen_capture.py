"""
Captura de pantalla multiplataforma (Windows, Linux/X11, macOS) usando `mss`.

La instancia de `mss` se crea de forma perezosa (y por hilo) para que
importar este módulo no falle en entornos sin sesión gráfica y para
respetar que `mss` no es seguro para compartir entre hilos.
"""
import base64
import io
import threading

import mss
from PIL import Image

_thread_local = threading.local()


def _get_sct():
    sct = getattr(_thread_local, "sct", None)
    if sct is None:
        sct = mss.mss()
        _thread_local.sct = sct
    return sct


class ScreenCapture:
    """Captura el escritorio completo (todos los monitores combinados)."""

    def __init__(self):
        self._monitor = None

    @property
    def monitor(self):
        if self._monitor is None:
            # El índice 0 representa la unión de todos los monitores.
            self._monitor = _get_sct().monitors[0]
        return self._monitor

    @property
    def width(self) -> int:
        return self.monitor["width"]

    @property
    def height(self) -> int:
        return self.monitor["height"]

    @property
    def left(self) -> int:
        return self.monitor["left"]

    @property
    def top(self) -> int:
        return self.monitor["top"]

    def capture_jpeg(self, quality: int = 50, max_width: int = 1600):
        """Devuelve (base64_jpeg, ancho, alto) del frame actual."""
        shot = _get_sct().grab(self.monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

        if max_width and img.width > max_width:
            ratio = max_width / img.width
            new_size = (max_width, max(1, int(img.height * ratio)))
            img = img.resize(new_size, Image.BILINEAR)

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return encoded, img.width, img.height
