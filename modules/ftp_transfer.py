"""Publicación y descarga del paquete Windows mediante FTP/FTPS."""
import ftplib
import os
import posixpath
import tempfile
import threading
from datetime import datetime, timezone


PACKAGE_NAME = "escritorio-remoto-windows.zip"
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_TRANSFER_ENTRIES = 10000
FTP_PASSIVE_PORT_START = 30000
FTP_PASSIVE_PORT_END = 30049


class FTPTransferError(RuntimeError):
    pass


def _safe_path(value: str) -> str:
    value = str(value or "/").replace("\\", "/")
    if "\r" in value or "\n" in value:
        raise FTPTransferError("La ruta FTP no es válida.")
    if ".." in value.split("/"):
        raise FTPTransferError("La ruta FTP sale de la raíz permitida.")
    normalized = posixpath.normpath("/" + value.lstrip("/"))
    return normalized


def _profile(settings: dict, side: str) -> dict:
    if side not in {"left", "right"}:
        raise FTPTransferError("Panel FTP no válido.")
    prefix = f"ftp_{side}_"
    root = _safe_path(settings.get(prefix + "root", "/"))
    return {
        "host": (settings.get(prefix + "host") or "").strip(),
        "port": int(settings.get(prefix + "port", 21)),
        "username": settings.get(prefix + "username") or "anonymous",
        "password": settings.get(prefix + "password") or "",
        "tls": bool(settings.get(prefix + "tls")),
        "root": root,
    }


def _remote_path(profile: dict, relative_path: str) -> str:
    relative = _safe_path(relative_path)
    return posixpath.normpath(posixpath.join(profile["root"], relative.lstrip("/")))


def _connect_profile(profile: dict):
    if not profile["host"]:
        raise FTPTransferError("Configura el host de este panel FTP.")
    ftp_class = ftplib.FTP_TLS if profile["tls"] else ftplib.FTP
    try:
        ftp = ftp_class()
        ftp.connect(profile["host"], profile["port"], timeout=20)
        ftp.login(profile["username"], profile["password"])
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        return ftp
    except ftplib.all_errors as exc:
        raise FTPTransferError(f"No se pudo conectar a {profile['host']}: {exc}") from exc


def list_remote_directory(settings: dict, side: str, relative_path: str = "/") -> dict:
    profile = _profile(settings, side)
    relative = _safe_path(relative_path)
    remote = _remote_path(profile, relative)
    ftp = _connect_profile(profile)
    entries = []
    try:
        for name, facts in ftp.mlsd(remote, facts=["type", "size", "modify"]):
            entry_type = facts.get("type", "")
            if name in {".", ".."} or entry_type not in {"file", "dir"}:
                continue
            modified = facts.get("modify", "")
            try:
                modified_epoch = datetime.strptime(modified[:14], "%Y%m%d%H%M%S").replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except (TypeError, ValueError):
                modified_epoch = 0
            entries.append({
                "name": name,
                "is_dir": entry_type == "dir",
                "size": int(facts.get("size", 0) or 0),
                "modified": modified_epoch,
            })
    except ftplib.all_errors as exc:
        raise FTPTransferError(f"No se pudo listar {relative}: {exc}") from exc
    finally:
        try:
            ftp.quit()
        except ftplib.all_errors:
            ftp.close()
    entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].lower()))
    return {"path": relative, "entries": entries}


def transfer_remote_entry(
    settings: dict,
    source_side: str,
    destination_side: str,
    source_path: str,
    destination_directory: str,
    is_dir: bool,
) -> dict:
    if source_side == destination_side:
        raise FTPTransferError("Arrastra el elemento al panel FTP contrario.")
    source_profile = _profile(settings, source_side)
    destination_profile = _profile(settings, destination_side)
    source = _remote_path(source_profile, source_path)
    destination_dir = _remote_path(destination_profile, destination_directory)
    name = posixpath.basename(_safe_path(source_path))
    if not name:
        raise FTPTransferError("No se puede transferir la raíz FTP.")
    destination = posixpath.join(destination_dir, name)
    source_ftp = _connect_profile(source_profile)
    try:
        destination_ftp = _connect_profile(destination_profile)
    except Exception:
        try:
            source_ftp.quit()
        except ftplib.all_errors:
            source_ftp.close()
        raise
    counters = {"files": 0, "directories": 0, "bytes": 0}

    def copy_file(src, dst):
        temporary = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
        try:
            def receive(chunk):
                counters["bytes"] += len(chunk)
                if counters["bytes"] > MAX_DOWNLOAD_BYTES:
                    raise FTPTransferError("La transferencia supera el límite de 2 GiB.")
                temporary.write(chunk)
            source_ftp.retrbinary("RETR " + src, receive)
            temporary.seek(0)
            destination_ftp.storbinary("STOR " + dst, temporary)
            counters["files"] += 1
        finally:
            temporary.close()

    def copy_directory(src, dst):
        if counters["files"] + counters["directories"] >= MAX_TRANSFER_ENTRIES:
            raise FTPTransferError("El directorio supera el límite de 10.000 elementos.")
        try:
            destination_ftp.mkd(dst)
        except ftplib.error_perm as exc:
            if not str(exc).startswith("550"):
                raise
        counters["directories"] += 1
        for child_name, facts in source_ftp.mlsd(src, facts=["type"]):
            if child_name in {".", ".."}:
                continue
            child_src = posixpath.join(src, child_name)
            child_dst = posixpath.join(dst, child_name)
            if facts.get("type") == "dir":
                copy_directory(child_src, child_dst)
            elif facts.get("type") == "file":
                copy_file(child_src, child_dst)

    try:
        if is_dir:
            copy_directory(source, destination)
        else:
            copy_file(source, destination)
    except FTPTransferError:
        raise
    except ftplib.all_errors as exc:
        raise FTPTransferError(f"Falló la transferencia FTP: {exc}") from exc
    finally:
        for ftp in (source_ftp, destination_ftp):
            try:
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()
    return counters


class FTPServerService:
    def __init__(self):
        self._lock = threading.Lock()
        self._server = None
        self._thread = None
        self._address = None

    def stop(self):
        with self._lock:
            server, thread = self._server, self._thread
            self._server = self._thread = self._address = None
        if server:
            server.close_all()
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def start(self, settings: dict, package_buffer=None):
        self.stop()
        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.handlers import FTPHandler
            from pyftpdlib.servers import FTPServer
        except ImportError as exc:
            raise FTPTransferError("Falta pyftpdlib; instala requirements.txt.") from exc

        password = settings.get("ftp_server_password", "")
        if not password:
            raise FTPTransferError("Configura una contraseña para el servidor FTP.")
        root = os.path.abspath(os.path.expanduser(settings["ftp_server_root"]))
        os.makedirs(root, exist_ok=True)
        if package_buffer is not None:
            package_path = os.path.join(root, PACKAGE_NAME)
            package_buffer.seek(0)
            with open(package_path, "wb") as output:
                output.write(package_buffer.read())

        authorizer = DummyAuthorizer()
        authorizer.add_user(settings["ftp_server_username"], password, root, perm="elr")
        handler = type("ConfiguredFTPHandler", (FTPHandler,), {})
        handler.authorizer = authorizer
        # Rango fijo para que el router/firewall pueda permitir también las
        # conexiones de datos FTP pasivo entre subredes.
        handler.passive_ports = range(FTP_PASSIVE_PORT_START, FTP_PASSIVE_PORT_END + 1)
        try:
            server = FTPServer(
                (settings["ftp_server_host"], int(settings["ftp_server_port"])), handler
            )
        except (OSError, ValueError) as exc:
            raise FTPTransferError(f"No se pudo iniciar el servidor FTP: {exc}") from exc

        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"timeout": 0.5, "blocking": True, "handle_exit": False},
            daemon=True,
            name="ftp-server",
        )
        with self._lock:
            self._server, self._thread = server, thread
            self._address = server.socket.getsockname()
        thread.start()

    def status(self) -> dict:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            address = self._address
        return {"running": running, "address": list(address) if address else None}


def download_windows_package(settings: dict):
    host = (settings.get("ftp_client_host") or "").strip()
    remote_path = (settings.get("ftp_client_remote_path") or "").strip()
    if not host or not remote_path:
        raise FTPTransferError("Configura el servidor y la ruta remota del cliente FTP.")
    if "\r" in remote_path or "\n" in remote_path:
        raise FTPTransferError("La ruta remota FTP no es válida.")

    output = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    received = 0

    def write_chunk(chunk):
        nonlocal received
        received += len(chunk)
        if received > MAX_DOWNLOAD_BYTES:
            raise FTPTransferError("El archivo FTP supera el límite de 2 GiB.")
        output.write(chunk)

    ftp_class = ftplib.FTP_TLS if settings.get("ftp_client_tls") else ftplib.FTP
    try:
        with ftp_class() as ftp:
            ftp.connect(host, int(settings.get("ftp_client_port", 21)), timeout=20)
            ftp.login(
                settings.get("ftp_client_username") or "anonymous",
                settings.get("ftp_client_password") or "",
            )
            if isinstance(ftp, ftplib.FTP_TLS):
                ftp.prot_p()
            ftp.retrbinary("RETR " + posixpath.normpath(remote_path), write_chunk)
    except FTPTransferError:
        output.close()
        raise
    except ftplib.all_errors as exc:
        output.close()
        raise FTPTransferError(f"No se pudo descargar desde FTP: {exc}") from exc
    output.seek(0)
    return output
