"""Agente local que publica información real de red al servidor corporativo."""
import argparse
import ftplib
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import socketio


class TransferStopped(Exception):
    pass


class TransferControl:
    def __init__(self):
        self.running = threading.Event()
        self.running.set()
        self.stopped = threading.Event()

    def apply(self, action):
        if action == "pause":
            self.running.clear()
        elif action == "resume":
            self.running.set()
        elif action == "stop":
            self.stopped.set()
            self.running.set()

    def checkpoint(self):
        while not self.running.wait(0.25):
            if self.stopped.is_set():
                raise TransferStopped()
        if self.stopped.is_set():
            raise TransferStopped()


def _run(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _netmask(prefix):
    try:
        return str(ipaddress.ip_network(f"0.0.0.0/{int(prefix)}").netmask)
    except (TypeError, ValueError):
        return ""


def _fallback_local_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


def _linux_network():
    info = {}
    try:
        routes = json.loads(_run(["ip", "-j", "route", "show", "default"]) or "[]")
        route = routes[0] if routes else {}
        info.update({
            "network_interface": route.get("dev", ""),
            "gateway": route.get("gateway", ""),
            "local_ip": route.get("prefsrc", ""),
        })
        if info["network_interface"]:
            addresses = json.loads(_run(["ip", "-j", "addr", "show", "dev", info["network_interface"]]) or "[]")
            inet = next((address for item in addresses for address in item.get("addr_info", [])
                         if address.get("family") == "inet" and address.get("scope") == "global"), {})
            info["local_ip"] = inet.get("local", info["local_ip"])
            info["netmask"] = _netmask(inet.get("prefixlen"))
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    wifi = _run(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
    info["wifi_name"] = next((line[4:] for line in wifi.splitlines() if line.startswith("yes:")), "")
    return info


def _windows_network():
    script = (
        "$c=Get-NetIPConfiguration|Where-Object {$_.IPv4DefaultGateway}|Select-Object -First 1;"
        "[pscustomobject]@{interface=$c.InterfaceAlias;ip=$c.IPv4Address.IPAddress;"
        "prefix=$c.IPv4Address.PrefixLength;gateway=$c.IPv4DefaultGateway.NextHop}|ConvertTo-Json -Compress"
    )
    info = {}
    try:
        data = json.loads(_run(["powershell", "-NoProfile", "-Command", script]) or "{}")
        info = {
            "network_interface": data.get("interface", ""),
            "local_ip": data.get("ip", ""),
            "netmask": _netmask(data.get("prefix")),
            "gateway": data.get("gateway", ""),
        }
    except (json.JSONDecodeError, TypeError):
        pass
    wlan = _run(["netsh", "wlan", "show", "interfaces"])
    match = re.search(r"^\s*SSID\s*:\s*(.+)$", wlan, re.MULTILINE | re.IGNORECASE)
    info["wifi_name"] = match.group(1).strip() if match else ""
    return info


def collect_device_info(name=""):
    system = platform.system()
    network = _windows_network() if system == "Windows" else _linux_network() if system == "Linux" else {}
    network["local_ip"] = network.get("local_ip") or _fallback_local_ip()
    hostname = socket.gethostname()
    stable_source = f"{hostname}|{platform.node()}|{platform.machine()}"
    return {
        "name": name or hostname,
        "hostname": hostname,
        "system_user": getpass.getuser(),
        "os": f"{system} {platform.release()}",
        "platform": platform.platform(),
        "local_ip": network.get("local_ip", ""),
        "netmask": network.get("netmask", ""),
        "gateway": network.get("gateway", ""),
        "network_interface": network.get("network_interface", ""),
        "wifi_name": network.get("wifi_name", "") or "No conectado mediante Wi-Fi",
        "device_uid": hashlib.sha256(stable_source.encode()).hexdigest()[:20],
        "agent": True,
    }


IS_WINDOWS = platform.system() == "Windows"
_WINDOWS_INVALID_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _local_name(name):
    """Adapta un nombre de Linux a uno que Windows acepte (p. ej. "a:b" o "aux")."""
    if not IS_WINDOWS:
        return name
    name = _WINDOWS_INVALID_CHARS.sub("_", name).rstrip(" .") or "_"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


def _long_path(path):
    """En Windows evita el límite de 260 caracteres (habitual en node_modules)."""
    if not IS_WINDOWS or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    # Las rutas normales se dejan tal cual; el prefijo solo hace falta al
    # acercarse al límite (se reserva margen para el sufijo ".part").
    if len(path) < 240:
        return path
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def download_via_ftp(client, command, server_url="", agent_token="", control=None):
    control = control or TransferControl()
    job_id = str(command.get("job_id", ""))
    # Trabaja siempre desde la raiz virtual asignada al usuario FTP.
    remote_path = str(command.get("path", "")).replace("\\", "/").strip("/")
    parts = [part for part in remote_path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("Ruta FTP no válida.")
    requested_destination = str(command.get("destination_directory", "")).strip()
    if requested_destination:
        download_root = resolve_agent_directory(requested_destination)
        local_target = os.path.join(download_root, _local_name(parts[-1]))
    else:
        download_root = os.path.join(os.path.expanduser("~"), "Downloads", "Escritorio-Remoto")
        local_target = os.path.join(download_root, *map(_local_name, parts))
    total_bytes = int(command.get("total_bytes", 0) or 0)
    transferred = 0
    last_progress_emit = 0.0
    progress_lock = threading.Lock()
    skipped = []
    os.makedirs(download_root, exist_ok=True)

    host, port = str(command["host"]), int(command.get("port", 21))
    http = None
    file_url = ""
    ftp = ftplib.FTP()
    try:
        ftp.connect(host, port, timeout=10)
        ftp.login(str(command.get("username", "anonymous")), str(command.get("password", "")))
    except OSError as error:
        ftp.close()
        if not (server_url and agent_token and "http_files" in command):
            raise RuntimeError(
                f"No se pudo conectar al servidor FTP {host}:{port} desde la IP "
                f"{_fallback_local_ip() or 'desconocida'} ({error}). "
                "Comprueba que este equipo está en la misma red local que el servidor."
            ) from error
        # Fuera de la red local del servidor la IP FTP no es alcanzable: se
        # descarga lo mismo por HTTPS a través del servidor web.
        ftp = None
        http = requests.Session()
        http.headers["X-Agent-Token"] = agent_token
        try:
            if getattr(client, "connected", True):
                client.emit("agent_ftp_progress", {
                    "job_id": job_id,
                    "loaded": transferred,
                    "total": total_bytes,
                    "current": "FTP directo no disponible; buscando ruta alternativa",
                })
        except socketio.exceptions.BadNamespaceError:
            pass
        http_server = server_url
        local_http_url = str(command.get("local_http_url", "")).rstrip("/")
        if local_http_url:
            try:
                probe = http.get(f"{local_http_url}/api/device-agent/bundle-sha256", timeout=3)
                if probe.ok:
                    http_server = local_http_url
            except requests.RequestException:
                pass
        file_url = f"{http_server}/api/device-agent/jobs/{job_id}/file"

    def ensure_http_channel():
        nonlocal http, file_url
        if http is not None:
            return
        if not server_url or not agent_token:
            raise RuntimeError("No hay un canal HTTPS autenticado para recuperar el contenido del enlace.")
        http = requests.Session()
        http.headers["X-Agent-Token"] = agent_token
        file_url = f"{server_url.rstrip('/')}/api/device-agent/jobs/{job_id}/file"

    def http_retrieve(remote_file, write_chunk):
        ensure_http_channel()
        relative = remote_file[len(remote_path):].strip("/")
        with http.get(file_url, params={"path": relative}, stream=True, timeout=60) as response:
            if response.status_code != 200:
                raise RuntimeError(f"El servidor rechazó la descarga de {remote_file} (HTTP {response.status_code}).")
            for chunk in response.iter_content(256 * 1024):
                write_chunk(chunk)

    def receive_file(remote_file, local_file, use_http=False):
        nonlocal transferred, last_progress_emit
        local_file = _long_path(local_file)
        os.makedirs(os.path.dirname(local_file), exist_ok=True)
        temporary = local_file + ".part"
        try:
            with open(temporary, "wb") as output:
                def write_chunk(chunk):
                    nonlocal transferred, last_progress_emit
                    control.checkpoint()
                    output.write(chunk)
                    with progress_lock:
                        transferred += len(chunk)
                        now = time.monotonic()
                        should_emit = now - last_progress_emit >= 0.25 or transferred >= total_bytes
                        if should_emit:
                            last_progress_emit = now
                        loaded = transferred
                    if should_emit:
                        try:
                            if getattr(client, "connected", True):
                                client.emit("agent_ftp_progress", {
                                    "job_id": job_id,
                                    "loaded": loaded,
                                    "total": total_bytes,
                                    "current": os.path.basename(local_file),
                                })
                        except socketio.exceptions.BadNamespaceError:
                            # La descarga continúa aunque el canal de progreso
                            # se reconecte durante una transferencia grande.
                            pass
                if ftp is None or use_http:
                    http_retrieve(remote_file, write_chunk)
                else:
                    ftp.retrbinary("RETR " + remote_file, write_chunk, blocksize=256 * 1024)
        except Exception:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass
            raise
        os.replace(temporary, local_file)

    def receive_directory(remote_dir, local_dir):
        os.makedirs(_long_path(local_dir), exist_ok=True)
        if ftp is None:
            # El servidor ya envía el árbol completo en la orden.
            for relative in command.get("http_dirs", []):
                os.makedirs(_long_path(os.path.join(local_dir, *map(_local_name, _safe_parts(relative)))), exist_ok=True)
            http_files = command.get("http_files", [])
            def receive_http_file(relative):
                receive_file(
                    remote_dir + "/" + relative,
                    os.path.join(local_dir, *map(_local_name, _safe_parts(relative))),
                )
            with ThreadPoolExecutor(max_workers=min(6, max(1, len(http_files)))) as executor:
                list(executor.map(receive_http_file, http_files))
            return
        for name, facts in ftp.mlsd(remote_dir, facts=["type"]):
            if name in {".", ".."}:
                continue
            remote_child = remote_dir.rstrip("/") + "/" + name
            local_child = os.path.join(local_dir, _local_name(name))
            try:
                if facts.get("type") == "dir":
                    receive_directory(remote_child, local_child)
                elif facts.get("type") == "file":
                    receive_file(remote_child, local_child)
            except ftplib.error_perm as error:
                # pyftpdlib muestra enlaces simbolicos como archivos, pero
                # rechaza con 550 los que salen de la raiz publicada.
                if not str(error).startswith("550"):
                    raise
                skipped.append(remote_child)

    def restore_links():
        restored_remote_paths = set()
        for link in command.get("links", []):
            relative = str(link.get("path", "")).replace("\\", "/").strip("/")
            link_parts = [part for part in relative.split("/") if part]
            if any(part in {".", ".."} for part in link_parts):
                raise ValueError("Ruta de enlace no válida.")
            local_link = os.path.join(local_target, *map(_local_name, link_parts)) if link_parts else local_target
            local_link = _long_path(local_link)
            os.makedirs(os.path.dirname(local_link), exist_ok=True)
            remote_link = remote_path + (("/" + relative) if relative else "")
            # Se crea primero con otro nombre: en Windows os.symlink exige
            # permisos de administrador o modo desarrollador, y si falla debe
            # conservarse el contenido que ya se descargó a través del enlace.
            pending_link = local_link + ".link"
            try:
                if os.path.lexists(pending_link):
                    os.remove(pending_link)
                os.symlink(str(link["target"]), pending_link, target_is_directory=bool(link.get("is_dir")))
            except OSError:
                if os.path.lexists(local_link) and remote_link not in skipped:
                    restored_remote_paths.add(remote_link)
                    continue
                fallback_files = command.get("link_fallback_files", [])
                fallback_dirs = command.get("link_fallback_dirs", [])
                prefix = relative + "/" if relative else ""
                matching_files = [path for path in fallback_files if path == relative or path.startswith(prefix)]
                matching_dirs = [path for path in fallback_dirs if path == relative or path.startswith(prefix)]
                if not matching_files and not matching_dirs:
                    skipped.append(remote_link)
                    continue
                for fallback_dir in matching_dirs:
                    suffix = fallback_dir[len(relative):].strip("/") if relative else fallback_dir
                    destination = os.path.join(local_link, *map(_local_name, _safe_parts(suffix))) if suffix else local_link
                    os.makedirs(_long_path(destination), exist_ok=True)
                for fallback_file in matching_files:
                    suffix = fallback_file[len(relative):].strip("/") if relative else fallback_file
                    destination = os.path.join(local_link, *map(_local_name, _safe_parts(suffix))) if suffix else local_link
                    receive_file(remote_path + (("/" + fallback_file) if fallback_file else ""), destination, use_http=True)
                restored_remote_paths.add(remote_link)
                continue
            if os.path.lexists(local_link):
                if os.path.isdir(local_link) and not os.path.islink(local_link):
                    shutil.rmtree(local_link)
                else:
                    os.remove(local_link)
            os.replace(pending_link, local_link)
            restored_remote_paths.add(remote_link)
        skipped[:] = [path for path in skipped if path not in restored_remote_paths]

    try:
        try:
            if command.get("symlink_only"):
                pass
            elif command.get("is_dir") is True:
                receive_directory(remote_path, local_target)
            else:
                receive_file(remote_path, local_target)
        except (TimeoutError, ConnectionError) as error:
            # El canal de control funciona pero no el de datos (FTP pasivo).
            raise RuntimeError(
                f"Conectado a {host}:{port}, pero no se pudo abrir el canal de datos FTP ({error}). "
                "Permite las conexiones a los puertos 30000-30049 del servidor."
            ) from error
        restore_links()
        skipped = sorted(set(skipped))
        if getattr(client, "connected", True):
            client.emit("agent_ftp_complete", {
                "job_id": job_id,
                "loaded": transferred,
                "total": total_bytes,
                "destination": local_target,
                "skipped": skipped,
                "directory": download_root,
                "entries": sorted(({
                    "name": entry.name,
                    "is_dir": entry.is_dir(follow_symlinks=False),
                    "size": entry.stat(follow_symlinks=False).st_size if entry.is_file(follow_symlinks=False) else 0,
                    "modified": entry.stat(follow_symlinks=False).st_mtime,
                } for entry in os.scandir(download_root)), key=lambda entry: (not entry["is_dir"], entry["name"].lower())),
            })
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()


def _safe_parts(relative):
    parts = [part for part in str(relative).replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("Ruta de descarga no válida.")
    return parts


def resolve_agent_directory(requested_path):
    candidate = os.path.realpath(os.path.expanduser(str(requested_path or "")))
    if not candidate or not os.path.isabs(candidate) or not os.path.isdir(candidate):
        raise ValueError("La carpeta destino no existe.")
    if platform.system() != "Windows":
        home = os.path.realpath(os.path.expanduser("~"))
        try:
            inside_home = os.path.commonpath((home, candidate)) == home
        except ValueError:
            inside_home = False
        if not inside_home:
            raise ValueError("La carpeta destino está fuera del directorio personal del agente.")
    return candidate


def list_agent_directory(requested_path=""):
    if platform.system() == "Windows" and not requested_path:
        entries = []
        for code in range(ord("A"), ord("Z") + 1):
            drive = chr(code) + ":\\"
            if os.path.isdir(drive):
                entries.append({"name": drive, "path": drive, "is_dir": True, "size": 0, "modified": 0})
        return {"path": "", "parent": None, "entries": entries}

    directory = resolve_agent_directory(requested_path or os.path.expanduser("~"))
    entries = []
    with os.scandir(directory) as iterator:
        for entry in iterator:
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                "name": entry.name,
                "path": entry.path,
                "is_dir": entry.is_dir(follow_symlinks=False),
                "size": stat.st_size if entry.is_file(follow_symlinks=False) else 0,
                "modified": stat.st_mtime,
            })
    entries.sort(key=lambda entry: (not entry["is_dir"], entry["name"].lower()))
    parent_dir = os.path.dirname(directory)
    parent = "" if platform.system() == "Windows" and parent_dir == directory else (
        None if parent_dir == directory else parent_dir
    )
    return {"path": directory, "parent": parent, "entries": entries}


def choose_agent_directory(initial_path=""):
    if platform.system() != "Windows":
        raise ValueError("El selector nativo de carpetas solo está disponible en el agente Windows.")
    import tkinter
    from tkinter import filedialog

    initial = initial_path if initial_path and os.path.isdir(initial_path) else os.path.expanduser("~")
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root, initialdir=initial, title="Selecciona la carpeta de destino FTP",
            mustexist=True,
        )
    finally:
        root.destroy()
    if not selected:
        raise ValueError("No se seleccionó ninguna carpeta.")
    return resolve_agent_directory(selected)


def update_agent_bundle(server_url):
    """Sustituye este paquete .pyz por el del servidor si ha cambiado.
    Devuelve True si hay que reiniciar (lo hace el lanzador al salir)."""
    bundle = os.path.abspath(sys.argv[0])
    if not bundle.endswith(".pyz") or not os.path.isfile(bundle):
        return False
    try:
        response = requests.get(f"{server_url}/api/device-agent/bundle-sha256", timeout=20)
        expected = response.json().get("sha256", "") if response.ok else ""
        with open(bundle, "rb") as current:
            if not expected or hashlib.sha256(current.read()).hexdigest() == expected:
                return False
        response = requests.get(f"{server_url}/api/device-agent/download/bundle", timeout=120)
        if not response.ok or hashlib.sha256(response.content).hexdigest() != expected:
            return False
        pending = bundle + ".new"
        with open(pending, "wb") as output:
            output.write(response.content)
        if IS_WINDOWS:
            updater = bundle + ".update.cmd"
            pid = os.getpid()
            with open(updater, "w", encoding="ascii", newline="\r\n") as output:
                output.write(
                    "@echo off\n"
                    ":wait\n"
                    f'tasklist /FI "PID eq {pid}" | find "{pid}" >nul\n'
                    "if not errorlevel 1 (timeout /t 1 /nobreak >nul & goto wait)\n"
                    f'move /Y "{pending}" "{bundle}" >nul\n'
                    'del "%~f0"\n'
                )
            subprocess.Popen(
                ["cmd.exe", "/c", updater],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.replace(pending, bundle)
    except (OSError, ValueError, requests.RequestException) as error:
        print(f"No se pudo actualizar el agente: {error}", flush=True)
        return False
    print("Agente actualizado; reiniciando…", flush=True)
    return True


def main():
    parser = argparse.ArgumentParser(description="Agente local de Escritorio Remoto")
    parser.add_argument("--server", default=os.environ.get("RDWEB_AGENT_SERVER"), required=not os.environ.get("RDWEB_AGENT_SERVER"))
    parser.add_argument("--token", default=os.environ.get("RDWEB_AGENT_TOKEN"), required=not os.environ.get("RDWEB_AGENT_TOKEN"))
    parser.add_argument("--name", default=os.environ.get("RDWEB_AGENT_NAME", ""))
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    windows_mutex = None
    if IS_WINDOWS:
        import ctypes
        windows_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\EscritorioRemotoAgent")
        if ctypes.windll.kernel32.GetLastError() == 183:
            print("El agente Windows ya está ejecutándose.", flush=True)
            return 0
    if update_agent_bundle(args.server.rstrip("/")):
        return 0

    client = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
    transfer_controls = {}
    transfer_controls_lock = threading.Lock()

    @client.event
    def connect():
        client.emit("register_device_agent", collect_device_info(args.name))
        print("Agente conectado.", flush=True)

    @client.event
    def disconnect():
        print("Agente desconectado; reintentando…", flush=True)

    @client.on("ftp_download_command")
    def ftp_download_command(command):
        job_id = str(command.get("job_id", ""))
        control = TransferControl()
        with transfer_controls_lock:
            transfer_controls[job_id] = control
        def worker():
            try:
                download_via_ftp(client, command, args.server.rstrip("/"), args.token, control)
            except TransferStopped:
                if getattr(client, "connected", True):
                    client.emit("agent_ftp_stopped", {"job_id": job_id})
            except Exception as error:
                try:
                    if getattr(client, "connected", True):
                        client.emit("agent_ftp_failed", {
                            "job_id": str(command.get("job_id", "")),
                            "message": str(error),
                        })
                except socketio.exceptions.BadNamespaceError:
                    pass
            finally:
                with transfer_controls_lock:
                    transfer_controls.pop(job_id, None)
        threading.Thread(target=worker, daemon=True, name="ftp-download").start()

    @client.on("agent_ftp_control_command")
    def agent_ftp_control_command(command):
        with transfer_controls_lock:
            control = transfer_controls.get(str(command.get("job_id", "")))
        if control:
            control.apply(str(command.get("action", "")))

    @client.on("agent_list_directory_command")
    def agent_list_directory_command(command):
        try:
            path = choose_agent_directory(command.get("path", "")) if command.get("choose") else command.get("path", "")
            listing = list_agent_directory(path)
            listing["request_id"] = str(command.get("request_id", ""))
            client.emit("agent_directory_listing", listing)
        except Exception as error:
            client.emit("agent_directory_listing", {
                "request_id": str(command.get("request_id", "")), "error": str(error),
            })

    client.connect(args.server.rstrip("/"), auth={"agent_token": args.token})
    last_update_check = time.monotonic()
    try:
        while True:
            time.sleep(max(15, args.interval))
            if time.monotonic() - last_update_check > 15 * 60:
                last_update_check = time.monotonic()
                if update_agent_bundle(args.server.rstrip("/")):
                    client.disconnect()
                    return 0
            if client.connected:
                client.emit("register_device_agent", collect_device_info(args.name))
    except KeyboardInterrupt:
        client.disconnect()
        return 0


if __name__ == "__main__":
    sys.exit(main())
