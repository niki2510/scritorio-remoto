"""
Aplicación Flask de escritorio remoto.

Sirve una interfaz web (canvas + WebSocket) que permite ver y controlar
el escritorio del equipo donde se ejecuta este servidor desde cualquier
navegador, típicamente a través de un dominio/túnel como
un dominio propio mediante un proxy o túnel. Incluye además una pestaña de Configuración
con ajustes de streaming, cambio de credenciales, un gestor de archivos
(subida/descarga de ficheros y carpetas) y un generador de subdominios
mediante Cloudflare Tunnel.

Uso:
    python app.py                 # arranca el servidor
    python app.py --set-password  # cambia usuario/contraseña de acceso
"""
import functools
import hashlib
import hmac
import ipaddress
import os
import sys
import threading
import time
import uuid

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_socketio import SocketIO, disconnect, emit, join_room
from werkzeug.security import check_password_hash, generate_password_hash

import config as config_module
from config import load_or_create_config, set_password
from modules import app_packager, cloudflare_tunnel, file_manager, ftp_transfer, input_control, screen_capture
from modules.file_manager import FileManagerError

cfg = load_or_create_config()

app = Flask(__name__)
app.config["SECRET_KEY"] = cfg["secret_key"]
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GiB por subida
# Evita servir una plantilla HTML "vieja" en caché si se edita sin reiniciar
# el proceso (p. ej. durante despliegues manuales de ficheros estáticos).
app.config["TEMPLATES_AUTO_RELOAD"] = True


@app.after_request
def _no_cache_for_dynamic_assets(response):
    # Evita que Cloudflare/el navegador sirvan una versión antigua del HTML,
    # CSS o JS tras una actualización (causaría errores de JS por
    # desajuste entre markup y script). Nota: algunos proveedores (p. ej.
    # Cloudflare en el plan gratuito) reescriben igualmente este encabezado
    # con su propio "Browser Cache TTL"; por eso además usamos URLs con
    # versión (ver `asset_url` más abajo), que invalidan la caché siempre.
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.context_processor
def _inject_asset_url():
    def asset_url(filename):
        """URL de un fichero estático con `?v=<mtime>` para forzar que el
        navegador (y cualquier CDN intermedio) descargue siempre la versión
        actual en lugar de una copia en caché desactualizada."""
        full_path = os.path.join(app.static_folder, filename)
        try:
            version = int(os.path.getmtime(full_path))
        except OSError:
            version = 0
        return url_for("static", filename=filename) + f"?v={version}"

    return {"asset_url": asset_url, "app_version": _frontend_version()}


def _frontend_version() -> str:
    """Huella de la interfaz (fecha de modificación más reciente de plantillas
    y estáticos). Si cambia mientras un navegador tiene la página abierta, el
    cliente se recarga solo al reconectar y nunca se queda con CSS/JS viejos."""
    latest = 0.0
    for folder in (app.static_folder, app.template_folder and os.path.join(app.root_path, app.template_folder)):
        if not folder:
            continue
        for current_dir, _dirs, files in os.walk(folder):
            for name in files:
                try:
                    latest = max(latest, os.path.getmtime(os.path.join(current_dir, name)))
                except OSError:
                    continue
    return str(int(latest))

socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

VIEWERS_ROOM = "viewers"
DEVICES_ROOM = "connected_devices"
_viewers = set()
_frame_ready_viewers = set()
_viewers_lock = threading.Lock()
_devices = {}
_devices_lock = threading.Lock()
_agent_sids = set()
_ftp_agent_jobs = {}
_agent_directory_requests = {}
_capture_thread = None
_stop_capture = threading.Event()
_ftp_server = ftp_transfer.FTPServerService()

# Ajustes de streaming aplicables en caliente (sin reiniciar el proceso).
_runtime_lock = threading.Lock()
_runtime = {
    "fps": cfg.get("fps", 12),
    "jpeg_quality": cfg.get("jpeg_quality", 50),
    "max_width": cfg.get("max_width", 1600),
}


def _is_logged_in() -> bool:
    return session.get("logged_in") is True


def login_required(view):
    """Para rutas de páginas HTML: redirige a /login si no hay sesión."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not _is_logged_in():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def api_login_required(view):
    """Para endpoints JSON/descargas: responde 401 si no hay sesión."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not _is_logged_in():
            return jsonify({"ok": False, "error": "No autenticado."}), 401
        return view(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# Rutas HTML
# --------------------------------------------------------------------------
@app.route("/", methods=["GET"])
@login_required
def index():
    return render_template("viewer.html", username=cfg["username"])


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == cfg["username"] and check_password_hash(cfg["password_hash"], password):
            session.clear()
            session["logged_in"] = True
            session.permanent = True
            return redirect(url_for("index"))
        # Pequeño retardo para dificultar ataques de fuerza bruta.
        time.sleep(1)
        error = "Usuario o contraseña incorrectos."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/device-agent/config", methods=["GET"])
@api_login_required
def api_device_agent_config():
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    scheme = forwarded_proto or request.scheme
    response = jsonify({
        "ok": True,
        "server_url": f"{scheme}://{request.host}",
        "agent_token": cfg["device_agent_token"],
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/device-agent/download/windows-configured-installer", methods=["GET"])
@api_login_required
def api_windows_configured_installer():
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    scheme = forwarded_proto if forwarded_proto in {"http", "https"} else request.scheme
    host = request.host
    if not host or any(not (char.isalnum() or char in ".-:[]") for char in host):
        return jsonify({"ok": False, "error": "Host no válido."}), 400
    token = str(cfg["device_agent_token"])
    if not token or any(not (char.isalnum() or char in "_-") for char in token):
        return jsonify({"ok": False, "error": "Token del agente no válido."}), 500
    server_url = f"{scheme}://{host}"
    installer_url = f"{server_url}/api/device-agent/download/windows-installer"
    # Los valores proceden de la configuración/Host ya validados; se escapan
    # comillas simples para insertarlos como literales de PowerShell.
    ps_server = server_url.replace("'", "''")
    ps_installer = installer_url.replace("'", "''")
    ps_token = token
    command = (
        f"$p=Join-Path $env:TEMP 'install-device-agent.ps1'; "
        f"Invoke-WebRequest -UseBasicParsing '{ps_installer}' -OutFile $p; "
        f"& $p -ServerUrl '{ps_server}' -AgentToken '{ps_token}'"
    )
    content = (
        "@echo off\r\n"
        "title Instalar agente FTP de Escritorio Remoto\r\n"
        f"powershell.exe -NoProfile -ExecutionPolicy Bypass -Command \"{command}\"\r\n"
        "if errorlevel 1 (echo. & echo ERROR: no se pudo instalar el agente.) "
        "else (echo. & echo Agente Windows instalado y conectado.)\r\n"
        "pause\r\n"
    )
    response = app.response_class(content, mimetype="application/octet-stream")
    response.headers["Content-Disposition"] = 'attachment; filename="instalar-agente-windows.cmd"'
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/device-agent/bundle-sha256", methods=["GET"])
def api_device_agent_bundle_sha256():
    """Huella del paquete del agente, para que los agentes se actualicen solos."""
    path = os.path.join(app.root_path, "artifacts", "device-agent.pyz")
    try:
        with open(path, "rb") as bundle:
            digest = hashlib.sha256(bundle.read()).hexdigest()
    except OSError:
        digest = ""
    response = jsonify({"ok": bool(digest), "sha256": digest})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/device-agent/download/<name>", methods=["GET"])
def api_device_agent_download(name):
    files = {
        "agent": (os.path.join(app.root_path, "device_agent.py"), "device_agent.py"),
        "requirements": (os.path.join(app.root_path, "agent-requirements.txt"), "agent-requirements.txt"),
        "linux-service": (os.path.join(app.root_path, "deploy", "escritorio-remoto-agent.service"), "escritorio-remoto-agent.service"),
        "linux-installer": (os.path.join(app.root_path, "deploy", "install-device-agent.sh"), "install-device-agent.sh"),
        "windows-installer": (os.path.join(app.root_path, "deploy", "install-device-agent.ps1"), "install-device-agent.ps1"),
        "bundle": (os.path.join(app.root_path, "artifacts", "device-agent.pyz"), "device-agent.pyz"),
    }
    if name not in files:
        return jsonify({"ok": False, "error": "Archivo de agente desconocido."}), 404
    path, download_name = files[name]
    return send_file(path, as_attachment=True, download_name=download_name)


# --------------------------------------------------------------------------
# API: ajustes generales (streaming + credenciales + Cloudflare)
# --------------------------------------------------------------------------
_PUBLIC_SETTINGS_KEYS = (
    "host", "port", "fps", "jpeg_quality", "max_width", "file_manager_root",
    "cloudflare_tunnel", "cloudflare_config_path", "cloudflare_service_name",
    "cloudflare_restart_method", "cloudflare_binary",
    "direct_access_enabled", "direct_access_protocol", "direct_public_ip",
    "direct_public_port", "direct_local_ip", "direct_local_port",
    "ftp_mode", "ftp_server_enabled", "ftp_server_host", "ftp_server_public_host",
    "ftp_server_port", "ftp_server_username", "ftp_server_root", "ftp_client_host",
    "ftp_client_port", "ftp_client_username", "ftp_client_tls", "ftp_client_remote_path",
    "ftp_left_host", "ftp_left_port", "ftp_left_username", "ftp_left_tls", "ftp_left_root",
    "ftp_right_host", "ftp_right_port", "ftp_right_username", "ftp_right_tls", "ftp_right_root",
)


@app.route("/api/settings", methods=["GET"])
@api_login_required
def api_get_settings():
    data = {key: cfg.get(key) for key in _PUBLIC_SETTINGS_KEYS}
    data["username"] = cfg["username"]
    data["ftp_server_password_configured"] = bool(cfg.get("ftp_server_password"))
    data["ftp_client_password_configured"] = bool(cfg.get("ftp_client_password"))
    data["ftp_left_password_configured"] = bool(cfg.get("ftp_left_password"))
    data["ftp_right_password_configured"] = bool(cfg.get("ftp_right_password"))
    with _runtime_lock:
        data.update(_runtime)
    return jsonify({"ok": True, "settings": data})


@app.route("/api/settings/stream", methods=["POST"])
@api_login_required
def api_update_stream_settings():
    body = request.get_json(silent=True) or {}
    try:
        fps = int(body.get("fps"))
        quality = int(body.get("jpeg_quality"))
        max_width = int(body.get("max_width"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Valores numéricos no válidos."}), 400

    if not (1 <= fps <= 30):
        return jsonify({"ok": False, "error": "El FPS debe estar entre 1 y 30."}), 400
    if not (10 <= quality <= 95):
        return jsonify({"ok": False, "error": "La calidad JPEG debe estar entre 10 y 95."}), 400
    if not (320 <= max_width <= 7680):
        return jsonify({"ok": False, "error": "El ancho máximo debe estar entre 320 y 7680."}), 400

    updates = {"fps": fps, "jpeg_quality": quality, "max_width": max_width}
    cfg.update(config_module.save_settings(updates))
    with _runtime_lock:
        _runtime.update(updates)
    return jsonify({"ok": True, "settings": updates})


@app.route("/api/settings/credentials", methods=["POST"])
@api_login_required
def api_update_credentials():
    body = request.get_json(silent=True) or {}
    current_password = body.get("current_password", "")
    new_username = (body.get("username") or "").strip()
    new_password = body.get("new_password", "")
    new_password_confirm = body.get("new_password_confirm", "")

    if not check_password_hash(cfg["password_hash"], current_password):
        return jsonify({"ok": False, "error": "La contraseña actual no es correcta."}), 403
    if not new_username:
        return jsonify({"ok": False, "error": "El usuario no puede estar vacío."}), 400
    if new_password and new_password != new_password_confirm:
        return jsonify({"ok": False, "error": "Las contraseñas nuevas no coinciden."}), 400

    password_hash = generate_password_hash(new_password) if new_password else cfg["password_hash"]
    config_module.update_credentials(new_username, password_hash)
    cfg["username"] = new_username
    cfg["password_hash"] = password_hash
    return jsonify({"ok": True})


@app.route("/api/settings/cloudflare", methods=["POST"])
@api_login_required
def api_update_cloudflare_settings():
    body = request.get_json(silent=True) or {}
    updates = {
        key: (body.get(key) or "").strip()
        for key in (
            "cloudflare_tunnel", "cloudflare_config_path", "cloudflare_service_name",
            "cloudflare_restart_method", "cloudflare_binary",
        )
        if key in body
    }
    cfg.update(config_module.save_settings(updates))
    return jsonify({"ok": True, "settings": {k: cfg.get(k) for k in updates}})


@app.route("/api/settings/direct-access", methods=["POST"])
@api_login_required
def api_update_direct_access():
    body = request.get_json(silent=True) or {}
    protocol = str(body.get("direct_access_protocol", "https")).lower()
    public_ip = str(body.get("direct_public_ip", "")).strip()
    local_ip = str(body.get("direct_local_ip", "")).strip()
    try:
        public_port = int(body.get("direct_public_port", 5000))
        local_port = int(body.get("direct_local_port", 5000))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Los puertos deben ser numéricos."}), 400
    if protocol not in {"http", "https"}:
        return jsonify({"ok": False, "error": "Protocolo no válido."}), 400
    if not 1 <= public_port <= 65535 or not 1 <= local_port <= 65535:
        return jsonify({"ok": False, "error": "Los puertos deben estar entre 1 y 65535."}), 400
    if any(char in public_ip + local_ip for char in "/\\\r\n\t "):
        return jsonify({"ok": False, "error": "Las direcciones IP o nombres no son válidos."}), 400
    enabled = body.get("direct_access_enabled") is True
    if enabled and (not public_ip or not local_ip):
        return jsonify({"ok": False, "error": "Indica la IP pública y la IP local."}), 400
    updates = {
        "direct_access_enabled": enabled,
        "direct_access_protocol": protocol,
        "direct_public_ip": public_ip,
        "direct_public_port": public_port,
        "direct_local_ip": local_ip,
        "direct_local_port": local_port,
    }
    cfg.update(config_module.save_settings(updates))
    return jsonify({
        "ok": True,
        "url": f"{protocol}://{public_ip}:{public_port}" if public_ip else "",
        "rule": f"TCP {public_ip}:{public_port} → {local_ip}:{local_port}",
    })


def _apply_ftp_runtime(settings=None):
    active = settings or cfg
    if active.get("ftp_mode") == "server" and active.get("ftp_server_enabled"):
        _ftp_server.start(active)
    else:
        _ftp_server.stop()


@app.route("/api/settings/ftp", methods=["POST"])
@api_login_required
def api_update_ftp_settings():
    body = request.get_json(silent=True) or {}
    mode = body.get("ftp_mode")
    if mode not in {"server", "client"}:
        return jsonify({"ok": False, "error": "El modo FTP debe ser servidor o cliente."}), 400
    try:
        server_port = int(body.get("ftp_server_port", 2121))
        client_port = int(body.get("ftp_client_port", 21))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Los puertos FTP no son válidos."}), 400
    if not (1 <= server_port <= 65535 and 1 <= client_port <= 65535):
        return jsonify({"ok": False, "error": "Los puertos FTP deben estar entre 1 y 65535."}), 400

    updates = {
        "ftp_mode": mode,
        "ftp_server_enabled": bool(body.get("ftp_server_enabled")),
        "ftp_server_host": (body.get("ftp_server_host") or "0.0.0.0").strip(),
        "ftp_server_public_host": (body.get("ftp_server_public_host") or "").strip(),
        "ftp_server_port": server_port,
        "ftp_server_username": (body.get("ftp_server_username") or "").strip(),
        "ftp_server_root": (body.get("ftp_server_root") or "").strip(),
        "ftp_client_host": (body.get("ftp_client_host") or "").strip(),
        "ftp_client_port": client_port,
        "ftp_client_username": (body.get("ftp_client_username") or "anonymous").strip(),
        "ftp_client_tls": bool(body.get("ftp_client_tls")),
        "ftp_client_remote_path": (body.get("ftp_client_remote_path") or "").strip(),
    }
    if not updates["ftp_server_username"] or not updates["ftp_server_root"]:
        return jsonify({"ok": False, "error": "Usuario y carpeta del servidor FTP son obligatorios."}), 400
    # El navegador y la aplicación Windows deben ver exactamente la misma
    # raíz que publica el servidor FTP de este equipo.
    updates["file_manager_root"] = updates["ftp_server_root"]
    for password_key in ("ftp_server_password", "ftp_client_password"):
        if body.get(password_key):
            updates[password_key] = body[password_key]

    proposed = {**cfg, **updates}
    try:
        _apply_ftp_runtime(proposed)
    except ftp_transfer.FTPTransferError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    cfg.update(config_module.save_settings(updates))
    return jsonify({"ok": True, "status": _ftp_server.status()})


@app.route("/api/ftp/status", methods=["GET"])
@api_login_required
def api_ftp_status():
    host = cfg.get("ftp_server_public_host") or request.host.split(":", 1)[0]
    return jsonify({
        "ok": True,
        "mode": cfg.get("ftp_mode", "server"),
        "server": _ftp_server.status(),
        "ftp_url": f"ftp://{host}:{cfg.get('ftp_server_port', 2121)}/{ftp_transfer.PACKAGE_NAME}",
        "username": cfg.get("ftp_server_username", ""),
    })


@app.route("/api/settings/ftp-connections", methods=["POST"])
@api_login_required
def api_update_ftp_connections():
    body = request.get_json(silent=True) or {}
    updates = {}
    for side in ("left", "right"):
        prefix = f"ftp_{side}_"
        try:
            port = int(body.get(prefix + "port", 21))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": f"Puerto FTP {side} no válido."}), 400
        if not 1 <= port <= 65535:
            return jsonify({"ok": False, "error": "Los puertos deben estar entre 1 y 65535."}), 400
        updates.update({
            prefix + "host": (body.get(prefix + "host") or "").strip(),
            prefix + "port": port,
            prefix + "username": (body.get(prefix + "username") or "anonymous").strip(),
            prefix + "tls": body.get(prefix + "tls") is True,
            prefix + "root": (body.get(prefix + "root") or "/").strip(),
        })
        password_key = prefix + "password"
        if body.get(password_key):
            updates[password_key] = body[password_key]
    cfg.update(config_module.save_settings(updates))
    return jsonify({"ok": True})


@app.route("/api/ftp/browse", methods=["GET"])
@api_login_required
def api_ftp_browse():
    try:
        result = ftp_transfer.list_remote_directory(
            cfg, request.args.get("side", ""), request.args.get("path", "/")
        )
    except (ftp_transfer.FTPTransferError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/ftp/transfer", methods=["POST"])
@api_login_required
def api_ftp_transfer():
    body = request.get_json(silent=True) or {}
    try:
        result = ftp_transfer.transfer_remote_entry(
            cfg,
            body.get("source_side", ""),
            body.get("destination_side", ""),
            body.get("source_path", ""),
            body.get("destination_path", "/"),
            body.get("is_dir") is True,
        )
    except (ftp_transfer.FTPTransferError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/cloudflare/generate", methods=["POST"])
@api_login_required
def api_cloudflare_generate():
    body = request.get_json(silent=True) or {}
    hostname = body.get("hostname", "")
    result = cloudflare_tunnel.generate_subdomain(hostname, cfg, cfg.get("port", 5000))
    return jsonify(result)


# --------------------------------------------------------------------------
# API: gestor de archivos (equivalente a FTP vía HTTP ya autenticado)
# --------------------------------------------------------------------------
@app.route("/api/files", methods=["GET"])
@api_login_required
def api_list_files():
    try:
        data = file_manager.list_directory(cfg["file_manager_root"], request.args.get("path", ""))
    except FileManagerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **data})


@app.route("/api/files/download", methods=["GET"])
@api_login_required
def api_download_file():
    try:
        path = file_manager.file_path_for_download(cfg["file_manager_root"], request.args.get("path", ""))
    except FileManagerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return send_file(path, as_attachment=True)


@app.route("/api/files/download_zip", methods=["GET"])
@api_login_required
def api_download_zip():
    rel_path = request.args.get("path", "")
    try:
        buffer = file_manager.zip_directory_to_buffer(cfg["file_manager_root"], rel_path)
    except FileManagerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    folder_name = os.path.basename(rel_path.rstrip("/\\")) or "carpeta"
    return send_file(buffer, as_attachment=True, download_name=f"{folder_name}.zip", mimetype="application/zip")


@app.route("/api/files/upload", methods=["POST"])
@api_login_required
def api_upload_files():
    rel_path = request.form.get("path", "")
    uploaded = request.files.getlist("files")
    relative_paths = request.form.getlist("relative_paths")
    if not uploaded:
        return jsonify({"ok": False, "error": "No se recibió ningún archivo."}), 400
    saved = []
    try:
        for index, file_storage in enumerate(uploaded):
            upload_path = relative_paths[index] if index < len(relative_paths) else file_storage.filename
            file_manager.save_upload_tree(cfg["file_manager_root"], rel_path, upload_path, file_storage)
            saved.append(upload_path)
    except FileManagerError as exc:
        return jsonify({"ok": False, "error": str(exc), "saved": saved}), 400
    return jsonify({"ok": True, "saved": saved})


@app.route("/api/files/mkdir", methods=["POST"])
@api_login_required
def api_mkdir():
    body = request.get_json(silent=True) or {}
    try:
        file_manager.make_directory(cfg["file_manager_root"], body.get("path", ""), body.get("name", ""))
    except FileManagerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.route("/api/files/text", methods=["GET"])
@api_login_required
def api_read_text_file():
    try:
        content = file_manager.read_text_file(cfg["file_manager_root"], request.args.get("path", ""))
    except FileManagerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "content": content})


@app.route("/api/files/text", methods=["POST"])
@api_login_required
def api_write_text_file():
    body = request.get_json(silent=True) or {}
    try:
        file_manager.write_text_file(
            cfg["file_manager_root"], body.get("path", ""), body.get("content", ""), bool(body.get("create"))
        )
    except FileManagerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.route("/api/files/delete", methods=["POST"])
@api_login_required
def api_delete_path():
    body = request.get_json(silent=True) or {}
    try:
        file_manager.delete_path(cfg["file_manager_root"], body.get("path", ""))
    except FileManagerError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# Descarga de la propia aplicación (para instalarla en otro equipo)
# --------------------------------------------------------------------------
@app.route("/api/download/app", methods=["GET"])
@api_login_required
def api_download_app():
    buffer = app_packager.build_zip_buffer()
    return send_file(
        buffer, as_attachment=True, download_name="escritorio-remoto.zip", mimetype="application/zip"
    )


@app.route("/api/windows/download", methods=["GET"])
@api_login_required
def api_download_windows_app():
    return send_file(
        app_packager.windows_package_path(),
        as_attachment=True,
        download_name=ftp_transfer.PACKAGE_NAME,
        mimetype="application/zip",
        conditional=True,
    )


@app.route("/api/linux/download", methods=["GET"])
@api_login_required
def api_download_linux_app():
    return send_file(
        app_packager.linux_package_path(),
        as_attachment=True,
        download_name="escritorio-remoto-linux-x64.zip",
        mimetype="application/zip",
        conditional=True,
    )


# --------------------------------------------------------------------------
# Captura de pantalla en segundo plano, retransmitida a los visores conectados
# --------------------------------------------------------------------------
def _capture_loop():
    capturer = screen_capture.ScreenCapture()

    while not _stop_capture.is_set():
        start = time.time()
        with _runtime_lock:
            fps = max(1, _runtime["fps"])
            quality = _runtime["jpeg_quality"]
            max_width = _runtime["max_width"]
        with _viewers_lock:
            ready_viewers = tuple(_viewers & _frame_ready_viewers)
            _frame_ready_viewers.difference_update(ready_viewers)
        if ready_viewers:
            try:
                image_b64, width, height = capturer.capture_jpeg(quality=quality, max_width=max_width)
                frame = {"image": image_b64, "width": width, "height": height}
                for viewer_sid in ready_viewers:
                    socketio.emit("frame", frame, to=viewer_sid)
            except Exception as exc:  # pragma: no cover - depende del entorno gráfico
                with _viewers_lock:
                    _frame_ready_viewers.update(_viewers.intersection(ready_viewers))
                socketio.emit("server_error", {"message": str(exc)}, to=VIEWERS_ROOM)
        elapsed = time.time() - start
        socketio.sleep(max(0.0, (1.0 / fps) - elapsed))


def _ensure_capture_thread():
    global _capture_thread
    if _capture_thread is None or not _capture_thread.is_alive():
        _stop_capture.clear()
        _capture_thread = socketio.start_background_task(_capture_loop)


# --------------------------------------------------------------------------
# Eventos WebSocket
# --------------------------------------------------------------------------
@socketio.on("connect")
def on_connect(auth=None):
    if _is_logged_in():
        return True
    supplied = str((auth or {}).get("agent_token", ""))
    if supplied and hmac.compare_digest(supplied, str(cfg["device_agent_token"])):
        with _devices_lock:
            _agent_sids.add(request.sid)
        return True
    return False
    return True


def _device_list():
    with _devices_lock:
        return list(_devices.values())


def _clean_device_metadata(data):
    data = data if isinstance(data, dict) else {}
    clean = {}
    clean["name"] = str(data.get("name", "Dispositivo")).strip()[:40] or "Dispositivo"
    for key in ("browser", "os", "platform", "language", "timezone", "local_ip", "wifi_name",
                "screen_resolution", "viewport", "user_agent", "hostname", "system_user",
                "network_interface", "netmask", "gateway", "device_uid", "agent"):
        clean[key] = str(data.get(key, ""))[:500]
    forwarded_ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    client_ip = str(forwarded_ip or request.remote_addr or "")[:100]
    # Los navegadores ocultan la IP local; si el cliente entra directamente por
    # la red local, la dirección desde la que conecta ya es su IP local.
    if not _is_ip(clean["local_ip"]) and _is_lan_ip(client_ip):
        clean["local_ip"] = client_ip
    clean.update({
        "id": request.sid,
        "ip": client_ip,
        "connected_at": int(time.time() * 1000),
    })
    return clean


def _is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _is_lan_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_private and not address.is_loopback and not address.is_link_local


def _broadcast_devices():
    socketio.emit("devices_updated", {"devices": _device_list()}, to=DEVICES_ROOM)


def _resolve_agent_sid_locked(agent_id, agent_uid=""):
    """Resuelve un agente aunque su Socket.IO sid haya cambiado al reconectar."""
    if agent_id in _agent_sids:
        return agent_id
    if agent_uid:
        return next((sid for sid in _agent_sids
                     if _devices.get(sid, {}).get("device_uid") == agent_uid), "")
    return ""


def _rebind_agent_jobs_locked(agent_sid, agent_uid):
    if not agent_uid:
        return
    for job in _ftp_agent_jobs.values():
        if job.get("agent_uid") == agent_uid:
            job["agent"] = agent_sid


@socketio.on("join_viewer")
def on_join_viewer():
    if not _is_logged_in():
        disconnect()
        return
    join_room(VIEWERS_ROOM)
    join_room(DEVICES_ROOM)
    with _viewers_lock:
        _viewers.add(request.sid)
        _frame_ready_viewers.add(request.sid)
    with _devices_lock:
        _devices[request.sid] = _clean_device_metadata({})
    _broadcast_devices()
    _ensure_capture_thread()
    emit("app_version", {"version": _frontend_version()})
    try:
        width, height = input_control.get_screen_size()
    except Exception as exc:  # pragma: no cover - depende del entorno gráfico
        emit("server_error", {"message": str(exc)})
        return
    emit("screen_info", {"width": width, "height": height})


@socketio.on("disconnect")
def on_disconnect():
    with _viewers_lock:
        _viewers.discard(request.sid)
        _frame_ready_viewers.discard(request.sid)
    removed = False
    with _devices_lock:
        _agent_sids.discard(request.sid)
        removed = _devices.pop(request.sid, None) is not None
    if removed:
        _broadcast_devices()
    try:
        input_control.release_all_keys()
    except Exception:  # pragma: no cover - depende del entorno gráfico
        pass


@socketio.on("frame_ready")
def on_frame_ready():
    if not _is_logged_in():
        disconnect()
        return
    with _viewers_lock:
        if request.sid in _viewers:
            _frame_ready_viewers.add(request.sid)


def _guarded(handler):
    def wrapper(data):
        if not _is_logged_in():
            disconnect()
            return
        try:
            handler(data)
        except Exception as exc:  # pragma: no cover - depende del entorno gráfico
            emit("server_error", {"message": str(exc)})
    return wrapper


@socketio.on("mouse_move")
@_guarded
def on_mouse_move(data):
    input_control.move_mouse(float(data["x"]), float(data["y"]))


@socketio.on("mouse_button")
@_guarded
def on_mouse_button(data):
    input_control.mouse_button(data["action"], int(data.get("button", 0)))


@socketio.on("mouse_scroll")
@_guarded
def on_mouse_scroll(data):
    input_control.mouse_scroll(float(data.get("dx", 0)), float(data.get("dy", 0)))


@socketio.on("key_event")
@_guarded
def on_key_event(data):
    input_control.key_event(data["action"], data["key"])


@socketio.on("type_text")
@_guarded
def on_type_text(data):
    input_control.type_text(data.get("text", ""))


@socketio.on("register_device")
@_guarded
def on_register_device(data):
    with _devices_lock:
        current = _devices.get(request.sid)
        if current is None:
            return
        updated = _clean_device_metadata(data)
        updated["connected_at"] = current["connected_at"]
        _devices[request.sid] = updated
    _broadcast_devices()


@socketio.on("register_device_agent")
def on_register_device_agent(data):
    with _devices_lock:
        if request.sid not in _agent_sids:
            disconnect()
            return
        current = _devices.get(request.sid)
        updated = _clean_device_metadata(data)
        updated["agent"] = True
        if current:
            updated["connected_at"] = current["connected_at"]
        _devices[request.sid] = updated
        device_uid = updated.get("device_uid")
        _rebind_agent_jobs_locked(request.sid, device_uid)
    print(
        f"Agente registrado: {updated.get('name', 'Dispositivo')} · "
        f"{updated.get('os', 'SO desconocido')} · {updated.get('local_ip', 'IP desconocida')}",
        flush=True,
    )
    join_room(DEVICES_ROOM)
    _broadcast_devices()


@socketio.on("request_devices")
@_guarded
def on_request_devices(_data=None):
    emit("devices_updated", {"devices": _device_list()})


@socketio.on("chat_message")
@_guarded
def on_chat_message(data):
    target_id = str(data.get("target_id", ""))
    message = str(data.get("message", "")).strip()
    if not message or len(message) > 2000:
        emit("chat_error", {"message": "El mensaje debe contener entre 1 y 2000 caracteres."})
        return
    with _devices_lock:
        sender = _devices.get(request.sid)
        target_exists = target_id == "__all__" or target_id in _devices
    if not sender or not target_exists:
        emit("chat_error", {"message": "El dispositivo ya no está conectado."})
        return
    payload = {
        "sender_id": request.sid,
        "sender_name": sender.get("name") or "Dispositivo",
        "target_id": target_id,
        "message": message,
        "sent_at": int(time.time() * 1000),
        "chat_type": "group" if target_id == "__all__" else "private",
    }
    if target_id == "__all__":
        emit("chat_message", payload, to=DEVICES_ROOM)
        return
    emit("chat_message", payload, to=target_id)
    if target_id != request.sid:
        emit("chat_message", payload)


@socketio.on("request_agent_ftp_download")
@_guarded
def on_request_agent_ftp_download(data):
    agent_id = str(data.get("agent_id", ""))
    agent_uid = str(data.get("agent_uid", ""))
    relative_path = str(data.get("path", ""))
    with _devices_lock:
        agent_id = _resolve_agent_sid_locked(agent_id, agent_uid)
        agent_uid = agent_uid or str(_devices.get(agent_id, {}).get("device_uid", ""))
    if not agent_id:
        emit("agent_ftp_error", {"message": "El agente seleccionado ya no está conectado."})
        return
    if not _ftp_server.status()["running"]:
        emit("agent_ftp_error", {"message": "El servidor FTP no está activo."})
        return
    host = (cfg.get("ftp_server_public_host") or "").strip()
    if not host:
        emit("agent_ftp_error", {"message": "Configura el host público o la IP local del servidor FTP."})
        return
    try:
        source = file_manager.resolve_path(cfg["file_manager_root"], relative_path)
    except FileManagerError as exc:
        emit("agent_ftp_error", {"message": str(exc)})
        return
    if not os.path.exists(source):
        emit("agent_ftp_error", {"message": "El archivo o carpeta ya no existe."})
        return
    ftp_root = os.path.abspath(os.path.expanduser(cfg.get("ftp_server_root", "")))
    ftp_relative_path = os.path.relpath(source, ftp_root)
    if ftp_relative_path == ".." or ftp_relative_path.startswith(".." + os.sep):
        emit("agent_ftp_error", {"message": "El elemento está fuera de la carpeta publicada por FTP."})
        return
    source_is_link = os.path.islink(source)
    is_dir = os.path.isdir(source) and not source_is_link
    links = []
    link_fallback_files, link_fallback_dirs = [], []
    http_files, http_dirs = [], []
    total_bytes = 0
    def collect_link_fallback(link_path, relative):
        if os.path.isfile(link_path):
            link_fallback_files.append(relative)
            return
        if not os.path.isdir(link_path):
            return
        link_fallback_dirs.append(relative)
        visited = set()
        for fallback_root, fallback_dirs, fallback_names in os.walk(link_path, followlinks=True):
            real_root = os.path.realpath(fallback_root)
            if real_root in visited:
                fallback_dirs[:] = []
                continue
            visited.add(real_root)
            fallback_dirs[:] = [name for name in fallback_dirs
                                if os.path.realpath(os.path.join(fallback_root, name)) not in visited]
            fallback_relative = os.path.relpath(fallback_root, link_path)
            fallback_relative = "" if fallback_relative == "." else fallback_relative.replace(os.sep, "/")
            for name in fallback_dirs:
                path = "/".join(filter(None, (relative, fallback_relative, name)))
                link_fallback_dirs.append(path)
            for name in fallback_names:
                item = os.path.join(fallback_root, name)
                if os.path.isfile(item):
                    path = "/".join(filter(None, (relative, fallback_relative, name)))
                    link_fallback_files.append(path)

    if source_is_link:
        links.append({"path": "", "target": os.readlink(source), "is_dir": os.path.isdir(source)})
        collect_link_fallback(source, "")
    elif is_dir:
        for root, dirs, names in os.walk(source, followlinks=False):
            for name in list(dirs) + names:
                item = os.path.join(root, name)
                relative = os.path.relpath(item, source).replace(os.sep, "/")
                if os.path.islink(item):
                    links.append({"path": relative, "target": os.readlink(item), "is_dir": os.path.isdir(item)})
                    collect_link_fallback(item, relative)
                    if name in dirs:
                        dirs.remove(name)
                elif name in dirs:
                    http_dirs.append(relative)
                elif os.path.isfile(item):
                    total_bytes += os.path.getsize(item)
                    http_files.append(relative)
    else:
        total_bytes = os.path.getsize(source)
    job_id = uuid.uuid4().hex
    with _devices_lock:
        _ftp_agent_jobs[job_id] = {
            "requester": request.sid, "agent": agent_id,
            "agent_uid": agent_uid, "source": source,
            "link_fallback_files": set(link_fallback_files),
        }
    socketio.emit("ftp_download_command", {
        "job_id": job_id,
        "host": host,
        "port": int(cfg.get("ftp_server_port", 2121)),
        "username": cfg.get("ftp_server_username", ""),
        "password": cfg.get("ftp_server_password", ""),
        "path": ftp_relative_path.replace("\\", "/"),
        "destination_directory": str(data.get("destination_directory", "")),
        "is_dir": is_dir,
        "symlink_only": source_is_link,
        "links": links,
        "link_fallback_files": link_fallback_files,
        "link_fallback_dirs": link_fallback_dirs,
        "total_bytes": total_bytes,
        # Alternativa HTTPS si el agente no alcanza el servidor FTP.
        "http_files": http_files,
        "http_dirs": http_dirs,
        "local_http_url": f"http://{host}:{int(cfg.get('port', 5000))}",
    }, to=agent_id)
    emit("agent_ftp_started", {"job_id": job_id, "total_bytes": total_bytes})


@socketio.on("request_agent_ftp_control")
@_guarded
def on_request_agent_ftp_control(data):
    job_id = str((data or {}).get("job_id", ""))
    action = str((data or {}).get("action", ""))
    if action not in {"pause", "resume", "stop"}:
        return
    with _devices_lock:
        job = _ftp_agent_jobs.get(job_id)
        authorized = job and job["requester"] == request.sid
    if authorized:
        socketio.emit("agent_ftp_control_command", {
            "job_id": job_id, "action": action,
        }, to=job["agent"])


@app.route("/api/device-agent/jobs/<job_id>/file", methods=["GET"])
def api_device_agent_job_file(job_id):
    """Descarga por HTTPS de un archivo de un trabajo FTP en curso, para agentes
    que no alcanzan el servidor FTP (por ejemplo, fuera de la red local)."""
    supplied = request.headers.get("X-Agent-Token", "")
    if not supplied or not hmac.compare_digest(supplied, str(cfg["device_agent_token"])):
        return jsonify({"ok": False, "error": "No autorizado."}), 403
    with _devices_lock:
        job = _ftp_agent_jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Trabajo no encontrado."}), 404
    source = job["source"]
    relative = request.args.get("path", "").replace("\\", "/").strip("/")
    if any(part == ".." for part in relative.split("/")):
        return jsonify({"ok": False, "error": "Ruta no válida."}), 400
    target = os.path.normpath(os.path.join(source, relative)) if relative else source
    real_source = os.path.realpath(source)
    inside = os.path.commonpath((real_source, os.path.realpath(target))) == real_source
    allowed_link_fallback = relative in job.get("link_fallback_files", set())
    if (not inside and not allowed_link_fallback) or (os.path.islink(target) and not allowed_link_fallback) or not os.path.isfile(target):
        return jsonify({"ok": False, "error": "Archivo no disponible."}), 404
    return send_file(target, as_attachment=True, conditional=False)


@socketio.on("request_agent_directory")
@_guarded
def on_request_agent_directory(data):
    agent_id = str(data.get("agent_id", ""))
    with _devices_lock:
        valid_agent = agent_id in _agent_sids
    if not valid_agent:
        emit("agent_directory_listing", {"error": "El agente seleccionado ya no está conectado."})
        return
    request_id = uuid.uuid4().hex
    with _devices_lock:
        _agent_directory_requests[request_id] = {
            "requester": request.sid,
            "agent": agent_id,
            "context": str(data.get("context", "main")),
        }
    socketio.emit("agent_list_directory_command", {
        "request_id": request_id,
        "path": str(data.get("path", "")),
        "choose": bool(data.get("choose", False)),
    }, to=agent_id)


@socketio.on("agent_directory_listing")
def on_agent_directory_listing(data):
    request_id = str((data or {}).get("request_id", ""))
    with _devices_lock:
        pending = _agent_directory_requests.get(request_id)
        authorized = request.sid in _agent_sids and pending and pending["agent"] == request.sid
        if authorized:
            _agent_directory_requests.pop(request_id, None)
    if authorized:
        payload = dict(data or {})
        payload["agent_id"] = pending["agent"]
        payload["context"] = pending.get("context", "main")
        socketio.emit("agent_directory_listing", payload, to=pending["requester"])


@socketio.on("agent_ftp_progress")
def on_agent_ftp_progress(data):
    job_id = str((data or {}).get("job_id", ""))
    with _devices_lock:
        job = _ftp_agent_jobs.get(job_id)
        authorized = request.sid in _agent_sids and job and job["agent"] == request.sid
    if authorized:
        socketio.emit("agent_ftp_progress", data, to=job["requester"])


@socketio.on("agent_ftp_complete")
def on_agent_ftp_complete(data):
    job_id = str((data or {}).get("job_id", ""))
    with _devices_lock:
        job = _ftp_agent_jobs.get(job_id)
        authorized = request.sid in _agent_sids and job and job["agent"] == request.sid
        if authorized:
            _ftp_agent_jobs.pop(job_id, None)
    if authorized:
        socketio.emit("agent_ftp_complete", data, to=job["requester"])


@socketio.on("agent_ftp_failed")
def on_agent_ftp_failed(data):
    job_id = str((data or {}).get("job_id", ""))
    with _devices_lock:
        job = _ftp_agent_jobs.get(job_id)
        authorized = request.sid in _agent_sids and job and job["agent"] == request.sid
        if authorized:
            _ftp_agent_jobs.pop(job_id, None)
    if authorized:
        socketio.emit("agent_ftp_error", {"message": str(data.get("message", "Falló la descarga FTP."))[:500]}, to=job["requester"])


@socketio.on("agent_ftp_stopped")
def on_agent_ftp_stopped(data):
    job_id = str((data or {}).get("job_id", ""))
    with _devices_lock:
        job = _ftp_agent_jobs.get(job_id)
        authorized = request.sid in _agent_sids and job and job["agent"] == request.sid
        if authorized:
            _ftp_agent_jobs.pop(job_id, None)
    if authorized:
        socketio.emit("agent_ftp_stopped", {"job_id": job_id}, to=job["requester"])


# --------------------------------------------------------------------------
# Punto de entrada
# --------------------------------------------------------------------------
def main():
    if "--set-password" in sys.argv:
        set_password()
        return

    host = cfg.get("host", "0.0.0.0")
    port = cfg.get("port", 5000)
    try:
        _apply_ftp_runtime()
    except ftp_transfer.FTPTransferError as exc:
        print(f"Aviso FTP: {exc}")
    print(f"Escritorio remoto escuchando en http://{host}:{port} (usuario: {cfg['username']})")
    # Servidor de desarrollo de Werkzeug en modo threaded: suficiente para el
    # uso típico (un administrador conectado). Para exponerlo a internet,
    # colócalo detrás de un reverse proxy con TLS (ver deploy/nginx-*.conf).
    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
