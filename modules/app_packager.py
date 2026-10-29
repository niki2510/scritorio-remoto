"""
Empaqueta el proyecto en un .zip descargable para poder instalarlo en otro
equipo (Windows o Ubuntu). Se excluyen deliberadamente los secretos
(instance/config.json), el entorno virtual, credenciales de Cloudflare y
cualquier artefacto de caché/control de versiones.
"""
import io
import os
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS_BUILD_DIR = os.path.join(BASE_DIR, "artifacts", "Escritorio-Remoto-Bravo-win32-x64")
WINDOWS_ZIP_PATH = os.path.join(BASE_DIR, "artifacts", "Escritorio-Remoto-Bravo-Windows-x64.zip")
LINUX_ZIP_PATH = os.path.join(BASE_DIR, "artifacts", "Escritorio-Remoto-Bravo-Linux-x64.zip")

INCLUDE_TOP_LEVEL = [
    "app.py",
    "config.py",
    "requirements.txt",
    "README.md",
    "run.sh",
    "run.bat",
    "device_agent.py",
    "agent-requirements.txt",
    "modules",
    "templates",
    "static",
    "deploy",
    "tests",
]

EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", "venv", ".venv", ".git", "instance"}
EXCLUDE_FILE_SUFFIXES = (".pyc",)


def _build_zip_buffer(include_top_level, root_name="escritorio-remoto") -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for top_name in include_top_level:
            full_top = os.path.join(BASE_DIR, top_name)
            if not os.path.exists(full_top):
                continue
            if os.path.isfile(full_top):
                zf.write(full_top, os.path.join(root_name, top_name))
                continue
            for current_dir, dirs, files in os.walk(full_top):
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_NAMES]
                for file_name in files:
                    if file_name.endswith(EXCLUDE_FILE_SUFFIXES):
                        continue
                    full_path = os.path.join(current_dir, file_name)
                    arcname = os.path.join(
                        root_name, os.path.relpath(full_path, BASE_DIR)
                    )
                    zf.write(full_path, arcname)
    buffer.seek(0)
    return buffer


def build_zip_buffer() -> io.BytesIO:
    return _build_zip_buffer(INCLUDE_TOP_LEVEL)


def build_windows_zip_buffer() -> io.BytesIO:
    """Carga el ZIP precompilado para publicarlo mediante el servidor FTP."""
    path = windows_package_path()
    with open(path, "rb") as package:
        return io.BytesIO(package.read())


def windows_package_path() -> str:
    """Devuelve el ZIP listo para streaming HTTP, sin comprimir por petición."""
    if os.path.isfile(WINDOWS_ZIP_PATH):
        return WINDOWS_ZIP_PATH
    if not os.path.isdir(WINDOWS_BUILD_DIR):
        raise FileNotFoundError("El cliente Windows no está compilado.")
    raise FileNotFoundError("El ZIP del cliente Windows no está generado.")


def linux_package_path() -> str:
    if not os.path.isfile(LINUX_ZIP_PATH):
        raise FileNotFoundError("El ZIP del cliente Linux no está generado.")
    return LINUX_ZIP_PATH
