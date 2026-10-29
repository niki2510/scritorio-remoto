"""
Automatización de "Generar y Configurar subdominio" usando Cloudflare Tunnel.

Dado un hostname completo (p. ej. "oficina1.miempresa.es"), este módulo:

  1. Crea/actualiza el registro DNS en Cloudflare apuntando al túnel, usando
     el propio binario `cloudflared` ya autenticado en este equipo
     (`cloudflared tunnel route dns <tunnel> <hostname>`). Esto requiere que
     el dominio esté añadido como zona en la misma cuenta de Cloudflare con
     la que se creó el túnel.
  2. Añade (o actualiza) una regla de "ingress" en el YAML del túnel que
     reenvía ese hostname hacia esta aplicación (http://localhost:<puerto>).
  3. Valida el YAML resultante.
  4. Reinicia el servicio del túnel para aplicar los cambios.

No gestiona la creación del túnel en sí (se asume que ya existe, como en
este equipo), solo añade nuevos hostnames a uno ya configurado.
"""
import platform
import subprocess

import yaml


class CloudflareError(Exception):
    pass


def _run(cmd, runner=None, timeout=30):
    runner = runner or subprocess.run
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise CloudflareError(f"No se encontró el ejecutable: {cmd[0]} ({exc})")
    except subprocess.TimeoutExpired as exc:
        raise CloudflareError(f"Comando agotó el tiempo de espera: {' '.join(cmd)} ({exc})")
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def validate_hostname(hostname: str) -> str:
    hostname = (hostname or "").strip().strip(".").lower()
    if not hostname or " " in hostname or "/" in hostname:
        raise CloudflareError("Hostname no válido.")
    labels = hostname.split(".")
    if len(labels) < 2:
        raise CloudflareError(
            "Debes indicar un subdominio completo, por ejemplo: oficina1.miempresa.es"
        )
    for label in labels:
        if not label or not all(c.isalnum() or c == "-" for c in label):
            raise CloudflareError(f"Etiqueta de dominio no válida: '{label}'")
    return hostname


def route_dns(cloudflared_binary: str, tunnel: str, hostname: str, runner=None):
    cmd = [cloudflared_binary or "cloudflared", "tunnel", "route", "dns", "--overwrite-dns", tunnel, hostname]
    return _run(cmd, runner=runner, timeout=30)


def upsert_ingress(config_path: str, hostname: str, service: str) -> None:
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    ingress = data.get("ingress") or []
    updated = False
    new_ingress = []
    for rule in ingress:
        if isinstance(rule, dict) and rule.get("hostname") == hostname:
            rule = dict(rule)
            rule["service"] = service
            updated = True
        new_ingress.append(rule)

    if not updated:
        # Inserta antes de la regla "catch-all" final (sin hostname), si existe.
        insert_at = len(new_ingress)
        for idx, rule in enumerate(new_ingress):
            if isinstance(rule, dict) and "hostname" not in rule:
                insert_at = idx
                break
        new_ingress.insert(insert_at, {"hostname": hostname, "service": service})

    data["ingress"] = new_ingress
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def validate_ingress(cloudflared_binary: str, config_path: str, runner=None):
    cmd = [cloudflared_binary or "cloudflared", "tunnel", "--config", config_path, "ingress", "validate"]
    return _run(cmd, runner=runner, timeout=20)


def restart_service(method: str, service_name: str, runner=None):
    method = (method or "manual").strip()
    system = platform.system()

    if method == "systemd-user":
        return _run(["systemctl", "--user", "restart", service_name], runner=runner)
    if method == "systemd-system":
        return _run(["sudo", "-n", "systemctl", "restart", service_name], runner=runner)
    if method == "windows-service":
        ok1, out1 = _run(["sc", "stop", service_name], runner=runner)
        ok2, out2 = _run(["sc", "start", service_name], runner=runner)
        return ok2, (out1 + "\n" + out2).strip()
    if method == "manual":
        return True, (
            "Reinicio manual requerido: reinicia el servicio/proceso de cloudflared "
            "para aplicar los cambios."
        )

    return False, f"Método de reinicio desconocido: {method} (sistema detectado: {system})"


def generate_subdomain(hostname: str, cfg: dict, local_port: int, runner=None) -> dict:
    """Orquesta todo el flujo y devuelve un log paso a paso."""
    steps = []

    def record(name, ok, message):
        steps.append({"step": name, "ok": ok, "message": message})
        return ok

    try:
        hostname = validate_hostname(hostname)
    except CloudflareError as exc:
        record("validar_hostname", False, str(exc))
        return {"ok": False, "steps": steps, "hostname": hostname, "url": None}
    record("validar_hostname", True, f"Hostname válido: {hostname}")

    tunnel = cfg.get("cloudflare_tunnel")
    config_path = cfg.get("cloudflare_config_path")
    binary = cfg.get("cloudflare_binary") or "cloudflared"
    service_name = cfg.get("cloudflare_service_name")
    restart_method = cfg.get("cloudflare_restart_method") or "manual"

    if not tunnel or not config_path:
        record(
            "configuracion",
            False,
            "Faltan datos de Cloudflare Tunnel (nombre/ID del túnel y ruta del "
            "fichero de configuración). Complétalos en 'Opciones avanzadas'.",
        )
        return {"ok": False, "steps": steps, "hostname": hostname, "url": None}
    record("configuracion", True, "Configuración de Cloudflare Tunnel encontrada.")

    ok, output = route_dns(binary, tunnel, hostname, runner=runner)
    if not record("crear_registro_dns", ok, output or "Registro DNS creado/actualizado."):
        return {"ok": False, "steps": steps, "hostname": hostname, "url": None}

    try:
        upsert_ingress(config_path, hostname, f"http://localhost:{local_port}")
        record("actualizar_ingress", True, f"Regla añadida en {config_path}.")
    except OSError as exc:
        record("actualizar_ingress", False, f"No se pudo editar el YAML: {exc}")
        return {"ok": False, "steps": steps, "hostname": hostname, "url": None}

    ok, output = validate_ingress(binary, config_path, runner=runner)
    if not record("validar_ingress", ok, output or "Configuración válida."):
        return {"ok": False, "steps": steps, "hostname": hostname, "url": None}

    ok, output = restart_service(restart_method, service_name, runner=runner)
    record("reiniciar_tunel", ok, output)

    return {
        "ok": all(s["ok"] for s in steps),
        "steps": steps,
        "hostname": hostname,
        "url": f"https://{hostname}" if all(s["ok"] for s in steps) else None,
    }
