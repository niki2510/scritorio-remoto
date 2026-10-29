import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import ftp_transfer  # noqa: E402


def test_download_windows_package_uses_configured_ftp(monkeypatch):
    calls = []

    class FakeFTP:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def connect(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def login(self, username, password):
            calls.append(("login", username, password))

        def retrbinary(self, command, callback):
            calls.append(("retrieve", command))
            callback(b"PK-test")

    monkeypatch.setattr(ftp_transfer.ftplib, "FTP", FakeFTP)
    result = ftp_transfer.download_windows_package({
        "ftp_client_host": "ftp.example.test",
        "ftp_client_port": 2121,
        "ftp_client_username": "user",
        "ftp_client_password": "secret",
        "ftp_client_remote_path": "/packages/windows.zip",
        "ftp_client_tls": False,
    })

    assert result.read() == b"PK-test"
    assert calls == [
        ("connect", "ftp.example.test", 2121, 20),
        ("login", "user", "secret"),
        ("retrieve", "RETR /packages/windows.zip"),
    ]
    result.close()


def test_transfer_file_between_two_ftp_servers(monkeypatch):
    uploaded = []

    class SourceFTP:
        def retrbinary(self, command, callback):
            assert command == "RETR /source/report.txt"
            callback(b"remote-data")

        def quit(self):
            pass

        def close(self):
            pass

    class DestinationFTP:
        def storbinary(self, command, stream):
            uploaded.append((command, stream.read()))

        def quit(self):
            pass

        def close(self):
            pass

    connections = iter([SourceFTP(), DestinationFTP()])
    monkeypatch.setattr(ftp_transfer, "_connect_profile", lambda _profile: next(connections))
    settings = {
        "ftp_left_host": "source.test", "ftp_left_port": 21,
        "ftp_left_username": "source", "ftp_left_password": "a",
        "ftp_left_root": "/", "ftp_left_tls": False,
        "ftp_right_host": "destination.test", "ftp_right_port": 21,
        "ftp_right_username": "destination", "ftp_right_password": "b",
        "ftp_right_root": "/", "ftp_right_tls": False,
    }

    result = ftp_transfer.transfer_remote_entry(
        settings, "left", "right", "/source/report.txt", "/backup", False
    )

    assert uploaded == [("STOR /backup/report.txt", b"remote-data")]
    assert result == {"files": 1, "directories": 0, "bytes": 11}
