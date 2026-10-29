"""
Gestor de archivos integrado: permite subir/descargar archivos y carpetas
entre el navegador y el equipo que actúa como servidor, igual que haría un
cliente FTP, pero reutilizando el mismo canal HTTPS/WebSocket ya autenticado
de la aplicación (un servidor FTP real no es compatible con túneles HTTP
como Cloudflare Tunnel, que es el transporte habitual de esta app).

Todas las rutas se resuelven y validan contra una carpeta raíz configurable
(`file_manager_root`) para impedir que un cliente pueda escapar de ella
(path traversal).
"""
import io
import os
import shutil
import zipfile
from dataclasses import dataclass


class FileManagerError(Exception):
    """Error de validación o de operación sobre el sistema de archivos."""


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int
    modified: float


def _normalize_root(root: str) -> str:
    root = os.path.abspath(os.path.expanduser(root or os.path.expanduser("~")))
    if not os.path.isdir(root):
        raise FileManagerError(f"La carpeta raíz configurada no existe: {root}")
    return root


def resolve_path(root: str, relative_path: str) -> str:
    """Convierte una ruta relativa (enviada por el cliente) en una ruta
    absoluta segura dentro de `root`. Lanza FileManagerError si intenta
    escapar de la raíz."""
    root = _normalize_root(root)
    relative_path = (relative_path or "").strip().lstrip("/\\")
    candidate = os.path.abspath(os.path.join(root, relative_path))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise FileManagerError("Ruta fuera de la carpeta permitida.")
    return candidate


def list_directory(root: str, relative_path: str = "") -> dict:
    target = resolve_path(root, relative_path)
    if not os.path.isdir(target):
        raise FileManagerError("La ruta indicada no es una carpeta.")

    entries = []
    with os.scandir(target) as it:
        for item in it:
            try:
                stat = item.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append(
                Entry(
                    name=item.name,
                    is_dir=item.is_dir(follow_symlinks=False),
                    size=0 if item.is_dir(follow_symlinks=False) else stat.st_size,
                    modified=stat.st_mtime,
                )
            )
    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))

    root_abs = _normalize_root(root)
    rel_display = os.path.relpath(target, root_abs)
    rel_display = "" if rel_display == "." else rel_display.replace(os.sep, "/")

    return {
        "path": rel_display,
        "parent": None if target == root_abs else (os.path.dirname(rel_display) if "/" in rel_display else ""),
        "entries": [
            {"name": e.name, "is_dir": e.is_dir, "size": e.size, "modified": e.modified}
            for e in entries
        ],
    }


def make_directory(root: str, relative_path: str, name: str) -> None:
    if not name or any(ch in name for ch in ('/', '\\', '\0')):
        raise FileManagerError("Nombre de carpeta no válido.")
    parent = resolve_path(root, relative_path)
    new_dir = os.path.join(parent, name)
    if os.path.exists(new_dir):
        raise FileManagerError("Ya existe un archivo o carpeta con ese nombre.")
    os.makedirs(new_dir)


def delete_path(root: str, relative_path: str) -> None:
    target = resolve_path(root, relative_path)
    root_abs = _normalize_root(root)
    if target == root_abs:
        raise FileManagerError("No se puede borrar la carpeta raíz.")
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    elif os.path.exists(target):
        os.remove(target)
    else:
        raise FileManagerError("El archivo o carpeta no existe.")


def save_upload(root: str, relative_path: str, filename: str, file_storage) -> None:
    filename = os.path.basename(filename or "")
    if not filename:
        raise FileManagerError("Nombre de archivo no válido.")
    target_dir = resolve_path(root, relative_path)
    if not os.path.isdir(target_dir):
        raise FileManagerError("La carpeta de destino no existe.")
    destination = os.path.join(target_dir, filename)
    file_storage.save(destination)


def save_upload_tree(root: str, relative_path: str, upload_path: str, file_storage) -> None:
    """Guarda un archivo conservando su ruta relativa al subir una carpeta."""
    upload_path = (upload_path or "").replace("\\", "/")
    parts = [part for part in upload_path.split("/") if part]
    if not parts or any(part in {".", ".."} or "\0" in part for part in parts):
        raise FileManagerError("Ruta de archivo subida no válida.")
    destination = resolve_path(root, os.path.join(relative_path, *parts))
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    file_storage.save(destination)


MAX_TEXT_EDIT_BYTES = 2 * 1024 * 1024


def read_text_file(root: str, relative_path: str) -> str:
    """Devuelve el contenido de un archivo de texto para editarlo en la web."""
    target = resolve_path(root, relative_path)
    if not os.path.isfile(target):
        raise FileManagerError("El archivo no existe.")
    if os.path.getsize(target) > MAX_TEXT_EDIT_BYTES:
        raise FileManagerError("El archivo es demasiado grande para editarlo (máx. 2 MB).")
    with open(target, "rb") as handle:
        data = handle.read()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise FileManagerError("El archivo no es de texto (UTF-8) y no se puede editar.")


def write_text_file(root: str, relative_path: str, content: str, create: bool = False) -> None:
    """Guarda texto en un archivo. Con `create=True` falla si ya existe."""
    name = os.path.basename((relative_path or "").replace("\\", "/").rstrip("/"))
    if not name or name in {".", ".."} or "\0" in name:
        raise FileManagerError("Nombre de archivo no válido.")
    target = resolve_path(root, relative_path)
    if not os.path.isdir(os.path.dirname(target)):
        raise FileManagerError("La carpeta de destino no existe.")
    if os.path.isdir(target):
        raise FileManagerError("Ya existe una carpeta con ese nombre.")
    if create and os.path.exists(target):
        raise FileManagerError("Ya existe un archivo con ese nombre.")
    if not create and not os.path.isfile(target):
        raise FileManagerError("El archivo no existe.")
    data = (content or "").encode("utf-8")
    if len(data) > MAX_TEXT_EDIT_BYTES:
        raise FileManagerError("El contenido supera el máximo de 2 MB.")
    with open(target, "wb") as handle:
        handle.write(data)


def file_path_for_download(root: str, relative_path: str) -> str:
    target = resolve_path(root, relative_path)
    if not os.path.isfile(target):
        raise FileManagerError("El archivo no existe.")
    return target


def zip_directory_to_buffer(root: str, relative_path: str) -> io.BytesIO:
    target = resolve_path(root, relative_path)
    if not os.path.isdir(target):
        raise FileManagerError("La ruta indicada no es una carpeta.")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for current_dir, _dirs, files in os.walk(target):
            for file_name in files:
                full_path = os.path.join(current_dir, file_name)
                arcname = os.path.relpath(full_path, target)
                try:
                    zf.write(full_path, arcname)
                except OSError:
                    continue
    buffer.seek(0)
    return buffer
