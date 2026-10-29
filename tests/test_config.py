import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as config_module  # noqa: E402
from werkzeug.security import check_password_hash  # noqa: E402


def test_load_or_create_config_generates_credentials(tmp_path, monkeypatch):
    config_module.INSTANCE_DIR = str(tmp_path)
    config_module.CONFIG_PATH = str(tmp_path / "config.json")
    monkeypatch.delenv("RDWEB_USERNAME", raising=False)
    monkeypatch.delenv("RDWEB_PASSWORD", raising=False)

    cfg = config_module.load_or_create_config()

    assert cfg["username"] == "admin"
    assert "password_hash" in cfg
    assert os.path.exists(config_module.CONFIG_PATH)

    with open(config_module.CONFIG_PATH) as f:
        stored = json.load(f)
    assert stored["username"] == "admin"
    assert "secret_key" in stored


def test_load_or_create_config_respects_env_overrides(tmp_path, monkeypatch):
    config_module.INSTANCE_DIR = str(tmp_path)
    config_module.CONFIG_PATH = str(tmp_path / "config.json")
    monkeypatch.setenv("RDWEB_USERNAME", "custom_user")
    monkeypatch.setenv("RDWEB_PASSWORD", "supersecreta123")
    monkeypatch.setenv("RDWEB_PORT", "8080")

    cfg = config_module.load_or_create_config()

    assert cfg["username"] == "custom_user"
    assert check_password_hash(cfg["password_hash"], "supersecreta123")
    assert cfg["port"] == 8080


def test_config_is_stable_across_reloads(tmp_path, monkeypatch):
    config_module.INSTANCE_DIR = str(tmp_path)
    config_module.CONFIG_PATH = str(tmp_path / "config.json")
    monkeypatch.delenv("RDWEB_USERNAME", raising=False)
    monkeypatch.delenv("RDWEB_PASSWORD", raising=False)

    first = config_module.load_or_create_config()
    second = config_module.load_or_create_config()

    assert first["secret_key"] == second["secret_key"]
    assert first["password_hash"] == second["password_hash"]
