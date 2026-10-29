import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import cloudflare_tunnel as cf  # noqa: E402


def test_validate_hostname_ok():
    assert cf.validate_hostname("Oficina1.MiEmpresa.ES") == "oficina1.miempresa.es"


def test_validate_hostname_rejects_single_label():
    try:
        cf.validate_hostname("solouna")
        assert False, "debería haber lanzado CloudflareError"
    except cf.CloudflareError:
        pass


def test_validate_hostname_rejects_invalid_chars():
    try:
        cf.validate_hostname("sub dominio.com")
        assert False, "debería haber lanzado CloudflareError"
    except cf.CloudflareError:
        pass


def test_upsert_ingress_inserts_before_catchall(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "tunnel: abc123\n"
        "credentials-file: /x/abc123.json\n"
        "ingress:\n"
        "  - hostname: existing.example.com\n"
        "    service: http://localhost:9999\n"
        "  - service: http_status:404\n"
    )

    cf.upsert_ingress(str(config_path), "nuevo.example.com", "http://localhost:5000")

    import yaml
    data = yaml.safe_load(config_path.read_text())
    hostnames = [r.get("hostname") for r in data["ingress"] if isinstance(r, dict)]
    assert "nuevo.example.com" in hostnames
    assert "existing.example.com" in hostnames
    # La regla catch-all (sin hostname) debe seguir siendo la última.
    assert "hostname" not in data["ingress"][-1]


def test_upsert_ingress_is_idempotent_updates_existing(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "tunnel: abc123\n"
        "ingress:\n"
        "  - hostname: existing.example.com\n"
        "    service: http://localhost:9999\n"
        "  - service: http_status:404\n"
    )

    cf.upsert_ingress(str(config_path), "existing.example.com", "http://localhost:5000")

    import yaml
    data = yaml.safe_load(config_path.read_text())
    rules = [r for r in data["ingress"] if isinstance(r, dict) and r.get("hostname") == "existing.example.com"]
    assert len(rules) == 1
    assert rules[0]["service"] == "http://localhost:5000"


def _fake_runner_factory(responses):
    """responses: lista de (returncode, stdout) en el orden de llamada."""
    calls = []

    def runner(cmd, capture_output, text, timeout):
        calls.append(cmd)
        returncode, stdout = responses[len(calls) - 1]

        class Result:
            pass

        r = Result()
        r.returncode = returncode
        r.stdout = stdout
        r.stderr = ""
        return r

    runner.calls = calls
    return runner


def test_generate_subdomain_full_success(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("tunnel: abc123\ningress:\n  - service: http_status:404\n")

    cfg = {
        "cloudflare_tunnel": "abc123",
        "cloudflare_config_path": str(config_path),
        "cloudflare_binary": "cloudflared",
        "cloudflare_service_name": "cloudflared-test.service",
        "cloudflare_restart_method": "systemd-user",
    }

    runner = _fake_runner_factory([
        (0, "route dns created"),      # cloudflared tunnel route dns
        (0, "OK"),                     # ingress validate
        (0, "restarted"),              # systemctl --user restart
    ])

    result = cf.generate_subdomain("oficina1.miempresa.es", cfg, 5000, runner=runner)

    assert result["ok"] is True
    assert result["url"] == "https://oficina1.miempresa.es"
    assert len(runner.calls) == 3
    assert runner.calls[0][:4] == ["cloudflared", "tunnel", "route", "dns"]
    assert runner.calls[2] == ["systemctl", "--user", "restart", "cloudflared-test.service"]

    import yaml
    data = yaml.safe_load(config_path.read_text())
    hostnames = [r.get("hostname") for r in data["ingress"] if isinstance(r, dict)]
    assert "oficina1.miempresa.es" in hostnames


def test_generate_subdomain_stops_on_dns_failure(tmp_path):
    config_path = tmp_path / "config.yml"
    config_path.write_text("tunnel: abc123\ningress:\n  - service: http_status:404\n")

    cfg = {
        "cloudflare_tunnel": "abc123",
        "cloudflare_config_path": str(config_path),
        "cloudflare_binary": "cloudflared",
        "cloudflare_service_name": "cloudflared-test.service",
        "cloudflare_restart_method": "systemd-user",
    }

    runner = _fake_runner_factory([
        (1, "zone not found for domain"),  # falla la creación del registro DNS
    ])

    result = cf.generate_subdomain("oficina1.dominio-inexistente.zz", cfg, 5000, runner=runner)

    assert result["ok"] is False
    assert result["url"] is None
    assert len(runner.calls) == 1  # no debe seguir con los siguientes pasos

    # El YAML no debe haberse modificado si la creación del DNS falló.
    import yaml
    data = yaml.safe_load(config_path.read_text())
    hostnames = [r.get("hostname") for r in data["ingress"] if isinstance(r, dict)]
    assert "oficina1.dominio-inexistente.zz" not in hostnames


def test_generate_subdomain_missing_config_reports_error():
    result = cf.generate_subdomain("sub.dominio.com", {}, 5000)
    assert result["ok"] is False
    assert any(s["step"] == "configuracion" for s in result["steps"])
