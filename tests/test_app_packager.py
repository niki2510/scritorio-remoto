import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import app_packager  # noqa: E402


def test_build_zip_buffer_excludes_secrets_and_venv():
    buffer = app_packager.build_zip_buffer()
    with zipfile.ZipFile(buffer) as zf:
        names = zf.namelist()

    assert any(n.endswith("app.py") for n in names)
    assert any(n.endswith("README.md") for n in names)
    assert not any("instance/" in n for n in names)
    assert not any("/venv/" in n or n.startswith("venv/") for n in names)
    assert not any("__pycache__" in n for n in names)


def test_windows_package_contains_compiled_client(tmp_path, monkeypatch):
    zip_path = tmp_path / "windows.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Escritorio-Remoto-Bravo/Escritorio-Remoto-Bravo.exe", b"MZ-test")
    monkeypatch.setattr(app_packager, "WINDOWS_ZIP_PATH", str(zip_path))

    with zipfile.ZipFile(app_packager.build_windows_zip_buffer()) as zf:
        names = zf.namelist()

    assert "Escritorio-Remoto-Bravo/Escritorio-Remoto-Bravo.exe" in names
    assert not any(name.endswith("/run.bat") or name.endswith("/app.py") for name in names)
