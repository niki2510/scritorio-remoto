import ntpath
import os

import device_agent
import pytest


class FakeClient:
    def __init__(self):
        self.events = []

    def emit(self, name, data):
        self.events.append((name, data))


class FakeFTP:
    instances = []

    def __init__(self):
        self.commands = []
        self.__class__.instances.append(self)

    def connect(self, *_args, **_kwargs):
        pass

    def login(self, *_args):
        pass

    def mlsd(self, path, facts):
        self.commands.append(("MLSD", path))
        return iter([("archivo.txt", {"type": "file"})])

    def retrbinary(self, command, callback, blocksize):
        self.commands.append(("RETR", command))
        callback(b"contenido")

    def quit(self):
        pass


class FakeFTPWithOutsideLink(FakeFTP):
    def mlsd(self, path, facts):
        self.commands.append(("MLSD", path))
        return iter([
            ("archivo.txt", {"type": "file"}),
            ("enlace-externo", {"type": "file"}),
        ])

    def retrbinary(self, command, callback, blocksize):
        self.commands.append(("RETR", command))
        if command.endswith("/enlace-externo"):
            raise device_agent.ftplib.error_perm(
                "550 '/' points to a path which is outside the user's root directory."
            )
        callback(b"contenido")


def test_transfer_control_pause_resume_and_stop():
    control = device_agent.TransferControl()
    control.apply("pause")
    assert not control.running.is_set()
    control.apply("resume")
    control.checkpoint()
    control.apply("stop")
    with pytest.raises(device_agent.TransferStopped):
        control.checkpoint()


def test_download_uses_paths_relative_to_ftp_root(monkeypatch, tmp_path):
    FakeFTP.instances.clear()
    monkeypatch.setattr(device_agent.ftplib, "FTP", FakeFTP)
    monkeypatch.setenv("HOME", os.fspath(tmp_path))
    client = FakeClient()

    device_agent.download_via_ftp(client, {
        "job_id": "job-1", "host": "ftp.test", "path": "/proyecto",
        "is_dir": True, "total_bytes": 9,
    })

    assert FakeFTP.instances[0].commands == [
        ("MLSD", "proyecto"),
        ("RETR", "RETR proyecto/archivo.txt"),
    ]
    event, payload = client.events[-1]
    assert event == "agent_ftp_complete"
    assert payload["entries"] == [{
        "name": "proyecto", "is_dir": True, "size": 0,
        "modified": payload["entries"][0]["modified"],
    }]


def test_directory_download_restores_links_outside_ftp_root(monkeypatch, tmp_path):
    monkeypatch.setattr(device_agent.ftplib, "FTP", FakeFTPWithOutsideLink)
    monkeypatch.setenv("HOME", os.fspath(tmp_path))
    client = FakeClient()

    device_agent.download_via_ftp(client, {
        "job_id": "job-2", "host": "ftp.test", "path": "proyecto",
        "is_dir": True, "total_bytes": 9,
        "links": [{"path": "enlace-externo", "target": "/usr/bin/python3", "is_dir": False}],
    })

    event, payload = client.events[-1]
    assert event == "agent_ftp_complete"
    assert payload["skipped"] == []
    link = tmp_path / "Downloads" / "Escritorio-Remoto" / "proyecto" / "enlace-externo"
    assert link.is_symlink()
    assert os.readlink(link) == "/usr/bin/python3"
    assert not (tmp_path / "Downloads" / "Escritorio-Remoto" / "proyecto" / "enlace-externo.part").exists()


def test_download_uses_selected_agent_directory(monkeypatch, tmp_path):
    FakeFTP.instances.clear()
    monkeypatch.setattr(device_agent.ftplib, "FTP", FakeFTP)
    monkeypatch.setenv("HOME", os.fspath(tmp_path))
    destination = tmp_path / "destino"
    destination.mkdir()

    device_agent.download_via_ftp(FakeClient(), {
        "job_id": "job-3", "host": "ftp.test", "path": "servidor/proyecto",
        "is_dir": True, "destination_directory": os.fspath(destination),
    })

    assert (destination / "proyecto" / "archivo.txt").read_bytes() == b"contenido"




def _deny_symlinks(*_args, **_kwargs):
    raise OSError(1314, "El cliente no dispone de un privilegio requerido")


def test_link_inside_ftp_root_keeps_content_when_symlinks_are_not_allowed(monkeypatch, tmp_path):
    # Windows sin modo desarrollador: el contenido descargado a través del
    # enlace debe conservarse y la descarga terminar bien.
    monkeypatch.setattr(device_agent.ftplib, "FTP", FakeFTP)
    monkeypatch.setattr(device_agent.os, "symlink", _deny_symlinks)
    monkeypatch.setenv("HOME", os.fspath(tmp_path))
    client = FakeClient()

    device_agent.download_via_ftp(client, {
        "job_id": "job-4", "host": "ftp.test", "path": "proyecto", "is_dir": True,
        "links": [{"path": "archivo.txt", "target": "otro.txt", "is_dir": False}],
    })

    event, payload = client.events[-1]
    assert event == "agent_ftp_complete"
    assert payload["skipped"] == []
    target = tmp_path / "Downloads" / "Escritorio-Remoto" / "proyecto" / "archivo.txt"
    assert target.read_bytes() == b"contenido"


def test_link_outside_ftp_root_is_materialized_when_symlinks_are_not_allowed(monkeypatch, tmp_path):
    import app as server
    from tests.test_agent_http_fallback import FlaskSession

    source = tmp_path / "servidor" / "proyecto"
    source.mkdir(parents=True)
    target = tmp_path / "externo.txt"
    target.write_bytes(b"contenido externo")
    (source / "enlace-externo").symlink_to(target)
    monkeypatch.setitem(server.cfg, "device_agent_token", "secreto")
    monkeypatch.setitem(server._ftp_agent_jobs, "job-5", {
        "requester": "r", "agent": "a", "source": os.fspath(source),
        "link_fallback_files": {"enlace-externo"},
    })
    monkeypatch.setattr(device_agent.ftplib, "FTP", FakeFTPWithOutsideLink)
    monkeypatch.setattr(device_agent.requests, "Session", FlaskSession)
    monkeypatch.setattr(device_agent.os, "symlink", _deny_symlinks)
    monkeypatch.setenv("HOME", os.fspath(tmp_path))
    client = FakeClient()

    device_agent.download_via_ftp(client, {
        "job_id": "job-5", "host": "ftp.test", "path": "proyecto", "is_dir": True,
        "links": [{"path": "enlace-externo", "target": os.fspath(target), "is_dir": False}],
        "link_fallback_files": ["enlace-externo"], "link_fallback_dirs": [],
    }, "https://servidor.test", "secreto")

    event, payload = client.events[-1]
    assert event == "agent_ftp_complete"
    assert payload["skipped"] == []
    folder = tmp_path / "Downloads" / "Escritorio-Remoto" / "proyecto"
    assert (folder / "archivo.txt").read_bytes() == b"contenido"
    assert (folder / "enlace-externo").read_bytes() == b"contenido externo"


def test_windows_local_names_are_sanitized(monkeypatch):
    monkeypatch.setattr(device_agent, "IS_WINDOWS", True)
    assert device_agent._local_name("informe:v2?.txt") == "informe_v2_.txt"
    assert device_agent._local_name("aux.c") == "_aux.c"
    assert device_agent._local_name("carpeta. ") == "carpeta"
    assert device_agent._local_name("normal.txt") == "normal.txt"
    monkeypatch.setattr(device_agent.os.path, "abspath", ntpath.abspath)
    assert device_agent._long_path("C:\\Users\\a\\b.txt") == "C:\\Users\\a\\b.txt"
    long_file = "C:\\Users\\a\\" + "x" * 250 + ".txt"
    assert device_agent._long_path(long_file) == "\\\\?\\" + long_file
