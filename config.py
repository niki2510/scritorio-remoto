"""
Gestión de configuración y credenciales de la aplicación.

En el primer arranque se crea `instance/config.json` con una clave secreta
aleatoria y una contraseña de acceso generada automáticamente (o tomada de
las variables de entorno RDWEB_USERNAME / RDWEB_PASSWORD si se definen).
A partir de ahí, la mayoría de los ajustes (streaming, carpeta de archivos,
integración con Cloudflare Tunnel...) se gestionan desde la pestaña
"Configuración" de la propia aplicación y quedan guardados en ese mismo
fichero. Las variables de entorno RDWEB_* siguen teniendo prioridad sobre
lo guardado, para facilitar despliegues automatizados.
"""
import getpass
import json
import os
import platform
import secrets
import shutil

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
CONFIG_PATH = os.path.join(INSTANCE_DIR, "config.json")


def _as_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "si", "sí"}

# Campos que persisten en instance/config.json y pueden editarse desde la UI.
# (env_var, valor_por_defecto, caster)
PERSISTED_DEFAULTS = {
    "host": ("RDWEB_HOST", "0.0.0.0", str),
    "port": ("RDWEB_PORT", 5000, int),
    "fps": ("RDWEB_FPS", 12, int),
    "jpeg_quality": ("RDWEB_QUALITY", 50, int),
    "max_width": ("RDWEB_MAX_WIDTH", 1600, int),
    "direct_access_enabled": ("RDWEB_DIRECT_ENABLED", False, _as_bool),
    "direct_access_protocol": ("RDWEB_DIRECT_PROTOCOL", "https", str),
    "direct_public_ip": ("RDWEB_DIRECT_PUBLIC_IP", "", str),
    "direct_public_port": ("RDWEB_DIRECT_PUBLIC_PORT", 5000, int),
    "direct_local_ip": ("RDWEB_DIRECT_LOCAL_IP", "", str),
    "direct_local_port": ("RDWEB_DIRECT_LOCAL_PORT", 5000, int),
    "file_manager_root": ("RDWEB_FILE_ROOT", os.path.expanduser("~"), str),
    "cloudflare_tunnel": ("RDWEB_CF_TUNNEL", "", str),
    "cloudflare_config_path": ("RDWEB_CF_CONFIG", "", str),
    "cloudflare_service_name": ("RDWEB_CF_SERVICE", "", str),
    "cloudflare_restart_method": ("RDWEB_CF_RESTART", "", str),
    "cloudflare_binary": ("RDWEB_CF_BINARY", "", str),
    "ftp_mode": ("RDWEB_FTP_MODE", "server", str),
    "ftp_server_enabled": ("RDWEB_FTP_SERVER_ENABLED", False, _as_bool),
    "ftp_server_host": ("RDWEB_FTP_SERVER_HOST", "0.0.0.0", str),
    "ftp_server_public_host": ("RDWEB_FTP_PUBLIC_HOST", "", str),
    "ftp_server_port": ("RDWEB_FTP_SERVER_PORT", 2121, int),
    "ftp_server_username": ("RDWEB_FTP_SERVER_USER", "remote", str),
    "ftp_server_password": ("RDWEB_FTP_SERVER_PASSWORD", "", str),
    "ftp_server_root": ("RDWEB_FTP_SERVER_ROOT", os.path.join(INSTANCE_DIR, "ftp"), str),
    "ftp_client_host": ("RDWEB_FTP_CLIENT_HOST", "", str),
    "ftp_client_port": ("RDWEB_FTP_CLIENT_PORT", 21, int),
    "ftp_client_username": ("RDWEB_FTP_CLIENT_USER", "anonymous", str),
    "ftp_client_password": ("RDWEB_FTP_CLIENT_PASSWORD", "", str),
    "ftp_client_tls": ("RDWEB_FTP_CLIENT_TLS", False, _as_bool),
    "ftp_client_remote_path": ("RDWEB_FTP_CLIENT_PATH", "escritorio-remoto-windows.zip", str),
    "ftp_left_host": ("RDWEB_FTP_LEFT_HOST", "", str),
    "ftp_left_port": ("RDWEB_FTP_LEFT_PORT", 21, int),
    "ftp_left_username": ("RDWEB_FTP_LEFT_USER", "anonymous", str),
    "ftp_left_password": ("RDWEB_FTP_LEFT_PASSWORD", "", str),
    "ftp_left_tls": ("RDWEB_FTP_LEFT_TLS", False, _as_bool),
    "ftp_left_root": ("RDWEB_FTP_LEFT_ROOT", "/", str),
    "ftp_right_host": ("RDWEB_FTP_RIGHT_HOST", "", str),
    "ftp_right_port": ("RDWEB_FTP_RIGHT_PORT", 21, int),
    "ftp_right_username": ("RDWEB_FTP_RIGHT_USER", "anonymous", str),
    "ftp_right_password": ("RDWEB_FTP_RIGHT_PASSWORD", "", str),
    "ftp_right_tls": ("RDWEB_FTP_RIGHT_TLS", False, _as_bool),
    "ftp_right_root": ("RDWEB_FTP_RIGHT_ROOT", "/", str),
}

EDITABLE_KEYS = set(PERSISTED_DEFAULTS.keys())


def _generate_password(length: int = 12) -> str:
    return secrets.token_urlsafe(length)


def _read_stored_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _write_raw(cfg: dict) -> None:
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def _autodetect_cloudflare_defaults() -> dict:
    """Intenta detectar una configuración de cloudflared ya existente para
    pre-rellenar el formulario de subdominios (no falla si no encuentra nada)."""
    detected = {
        "cloudflare_binary": shutil.which("cloudflared") or "",
    }
    candidate_dirs = [
        os.path.join(os.path.expanduser("~"), ".cloudflared"),
        "/etc/cloudflared",
        r"C:\ProgramData\Cloudflare\cloudflared",
    ]
    for directory in candidate_dirs:
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if not name.endswith((".yml", ".yaml")):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            if "tunnel:" in content and "ingress:" in content:
                detected["cloudflare_config_path"] = path
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("tunnel:"):
                        detected["cloudflare_tunnel"] = line.split(":", 1)[1].strip()
                        break
                break
        if "cloudflare_config_path" in detected:
            break

    if platform.system() == "Linux":
        detected.setdefault("cloudflare_restart_method", "systemd-user")
    elif platform.system() == "Windows":
        detected.setdefault("cloudflare_restart_method", "windows-service")
        detected.setdefault("cloudflare_service_name", "Cloudflared")

    return detected


def load_or_create_config() -> dict:
    cfg = _read_stored_config()
    changed = False

    if "secret_key" not in cfg:
        cfg["secret_key"] = secrets.token_hex(32)
        changed = True

    if "device_agent_token" not in cfg:
        cfg["device_agent_token"] = secrets.token_urlsafe(32)
        changed = True

    if "username" not in cfg:
        cfg["username"] = os.environ.get("RDWEB_USERNAME", "admin")
        changed = True

    if "password_hash" not in cfg:
        env_password = os.environ.get("RDWEB_PASSWORD")
        generated = env_password is None
        password = env_password or _generate_password()
        cfg["password_hash"] = generate_password_hash(password)
        changed = True
        if generated:
            print("=" * 64)
            print(" Se generó una contraseña de acceso automáticamente.")
            print(f"   Usuario:    {cfg['username']}")
            print(f"   Contraseña: {password}")
            print(" Guárdala en un lugar seguro: no volverá a mostrarse.")
            print(" Para cambiarla más adelante ejecuta:")
            print("   python app.py --set-password")
            print("   (o usa la pestaña Configuración > Credenciales)")
            print("=" * 64)

    # Primer arranque: rellena los ajustes persistidos con sus valores por
    # defecto (o variable de entorno si está definida en ese momento).
    first_pass_autodetect = {}
    for key, (env_name, default, caster) in PERSISTED_DEFAULTS.items():
        if key not in cfg:
            raw = os.environ.get(env_name)
            cfg[key] = caster(raw) if raw is not None else default
            changed = True
            if key.startswith("cloudflare_") and not first_pass_autodetect:
                first_pass_autodetect = _autodetect_cloudflare_defaults()

    for key, value in first_pass_autodetect.items():
        if not cfg.get(key):
            cfg[key] = value
            changed = True

    if changed:
        _write_raw(cfg)

    # Las variables de entorno, si están presentes en este arranque concreto,
    # siempre tienen prioridad sobre lo persistido (sin modificar el fichero).
    for key, (env_name, _default, caster) in PERSISTED_DEFAULTS.items():
        raw = os.environ.get(env_name)
        if raw is not None:
            cfg[key] = caster(raw)

    return cfg


def save_settings(updates: dict) -> dict:
    """Persiste un subconjunto de ajustes editables y devuelve la config completa."""
    cfg = _read_stored_config()
    for key, value in updates.items():
        if key in EDITABLE_KEYS:
            cfg[key] = value
    _write_raw(cfg)
    return load_or_create_config()


def update_credentials(username: str, password_hash: str) -> None:
    cfg = _read_stored_config()
    cfg["username"] = username
    cfg["password_hash"] = password_hash
    _write_raw(cfg)


def set_password() -> None:
    """Flujo interactivo de línea de comandos para cambiar usuario/contraseña."""
    cfg = _read_stored_config()
    if "secret_key" not in cfg:
        cfg["secret_key"] = secrets.token_hex(32)
    current_user = cfg.get("username", "admin")

    username = input(f"Usuario [{current_user}]: ").strip() or current_user
    password = getpass.getpass("Nueva contraseña: ")
    password_confirm = getpass.getpass("Confirmar contraseña: ")

    if not password or password != password_confirm:
        print("Las contraseñas no coinciden o están vacías. No se realizaron cambios.")
        return

    update_credentials(username, generate_password_hash(password))
    print("Credenciales actualizadas correctamente.")
