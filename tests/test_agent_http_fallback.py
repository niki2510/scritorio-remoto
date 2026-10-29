import os

import app as server
import device_agent


class UnreachableFTP:
    def connect(self, *_args, **_kwargs):
        raise TimeoutError(10060, "Se produjo un error durante el intento de conexión")

    def close(self):
        pass


class FlaskSession:
    """Sustituye a requests.Session usando el cliente de pruebas de Flask."""

    def __init__(self):
        self.headers = {}
        self.client = server.app.test_client()

    def get(self, url, params=None, stream=False, timeout=None):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1]
        response = self.client.get(path, query_string=params, headers=self.headers)
        response.iter_content = lambda _size: [response.get_data()]
        return response


class FakeClient:
    def __init__(self):
        self.events = []

    def emit(self, name, data):
        self.events.append((name, data))


def _job(tmp_path, monkeypatch):
    source = tmp_path / "servidor" / "proyecto"
    (source / "sub").mkdir(parents=True)
    (source / "vacia").mkdir()
    (source / "a.txt").write_bytes(b"uno")
    (source / "sub" / "b.txt").write_bytes(b"dos")
    monkeypatch.setitem(server.cfg, "device_agent_token", "secreto")
    monkeypatch.setitem(server._ftp_agent_jobs, "job-http", {"requester": "r", "agent": "a", "source": os.fspath(source)})
    return source


def test_agent_falls_back_to_https_when_ftp_is_unreachable(monkeypatch, tmp_path):
    _job(tmp_path, monkeypatch)
    monkeypatch.setattr(device_agent.ftplib, "FTP", UnreachableFTP)
    monkeypatch.setattr(device_agent.requests, "Session", FlaskSession)
    monkeypatch.setenv("HOME", os.fspath(tmp_path))
    destination = tmp_path / "Descargas"
    destination.mkdir()
    client = FakeClient()

    device_agent.download_via_ftp(client, {
        "job_id": "job-http", "host": "192.168.0.100", "path": "proyecto", "is_dir": True,
        "destination_directory": os.fspath(destination),
        "http_files": ["a.txt", "sub/b.txt"], "http_dirs": ["sub", "vacia"],
    }, "https://servidor.test", "secreto")

    assert client.events[-1][0] == "agent_ftp_complete"
    assert (destination / "proyecto" / "a.txt").read_bytes() == b"uno"
    assert (destination / "proyecto" / "sub" / "b.txt").read_bytes() == b"dos"
    assert (destination / "proyecto" / "vacia").is_dir()


def test_job_file_endpoint_requires_token_and_stays_inside_source(monkeypatch, tmp_path):
    _job(tmp_path, monkeypatch)
    (tmp_path / "servidor" / "secreto.txt").write_bytes(b"no")
    client = server.app.test_client()
    url = "/api/device-agent/jobs/job-http/file"

    assert client.get(url, query_string={"path": "a.txt"}).status_code == 403
    headers = {"X-Agent-Token": "secreto"}
    assert client.get(url, query_string={"path": "a.txt"}, headers=headers).data == b"uno"
    assert client.get(url, query_string={"path": "../secreto.txt"}, headers=headers).status_code == 400
    assert client.get("/api/device-agent/jobs/otro/file", headers=headers).status_code == 404


def test_job_file_endpoint_serves_declared_symlink_fallback(monkeypatch, tmp_path):
    source = _job(tmp_path, monkeypatch)
    external = tmp_path / "externo.txt"
    external.write_bytes(b"necesario")
    (source / "enlace").symlink_to(external)
    server._ftp_agent_jobs["job-http"]["link_fallback_files"] = {"enlace"}

    response = server.app.test_client().get(
        "/api/device-agent/jobs/job-http/file",
        query_string={"path": "enlace"},
        headers={"X-Agent-Token": "secreto"},
    )

    assert response.status_code == 200
    assert response.data == b"necesario"


class _Response:
    def __init__(self, ok=True, payload=None, content=b""):
        self.ok, self._payload, self.content = ok, payload, content

    def json(self):
        return self._payload


def test_agent_replaces_its_bundle_when_server_has_another(monkeypatch, tmp_path):
    bundle = tmp_path / "device-agent.pyz"
    bundle.write_bytes(b"version vieja")
    nueva = b"version nueva"
    responses = {
        "https://s.test/api/device-agent/bundle-sha256": _Response(payload={"sha256": device_agent.hashlib.sha256(nueva).hexdigest()}),
        "https://s.test/api/device-agent/download/bundle": _Response(content=nueva),
    }
    monkeypatch.setattr(device_agent.sys, "argv", [os.fspath(bundle)])
    monkeypatch.setattr(device_agent.requests, "get", lambda url, timeout: responses[url])

    assert device_agent.update_agent_bundle("https://s.test") is True
    assert bundle.read_bytes() == nueva
    assert device_agent.update_agent_bundle("https://s.test") is False
