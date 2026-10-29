"""Construye artifacts/device-agent.pyz con todas sus dependencias."""
import os
import shutil
import subprocess
import sys
import tempfile
import zipapp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "artifacts", "device-agent.pyz")


def main():
    with tempfile.TemporaryDirectory() as build_dir:
        subprocess.run([
            sys.executable, "-m", "pip", "install", "-q", "--target", build_dir,
            "-r", os.path.join(ROOT, "agent-requirements.txt"),
        ], check=True)
        shutil.copy2(os.path.join(ROOT, "device_agent.py"), os.path.join(build_dir, "__main__.py"))
        os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
        zipapp.create_archive(build_dir, OUTPUT, interpreter="/usr/bin/env python3", compressed=True)
    print(OUTPUT)


if __name__ == "__main__":
    main()
