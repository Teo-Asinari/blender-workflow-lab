#!/usr/bin/env python3
"""Package and exactly mirror add-ons into one Blender user directory."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

from package_addons import ADDONS_DIR, discover_addon_ids, package_addon


def _windows_appdata_from_wsl() -> Path | None:
    try:
        value = subprocess.check_output(
            ["cmd.exe", "/d", "/c", "echo", "%APPDATA%"],
            text=True, stderr=subprocess.DEVNULL).strip().replace("\r", "")
        converted = subprocess.check_output(
            ["wslpath", "-u", value], text=True).strip()
        return Path(converted)
    except (OSError, subprocess.SubprocessError):
        return None


def default_install_dir(blender_version: str) -> Path | None:
    configured = os.environ.get("BLENDER_USER_ADDONS")
    if configured:
        return Path(configured).expanduser()
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else _windows_appdata_from_wsl()
    if root is None:
        return None
    return (root / "Blender Foundation" / "Blender" / blender_version
            / "scripts" / "addons")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mirror_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    wanted = {item.relative_to(source) for item in source.rglob("*")
              if item.is_file()}
    for installed in sorted(target.rglob("*"), reverse=True):
        relative = installed.relative_to(target)
        if installed.is_file() and relative not in wanted:
            installed.unlink()
        elif installed.is_dir() and not any(installed.iterdir()):
            installed.rmdir()
    for relative in sorted(wanted):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)
        if digest(source / relative) != digest(destination):
            raise RuntimeError(f"Verification failed: {destination}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("addons", nargs="*", help="Default: all add-ons")
    parser.add_argument("--blender-version", default="5.1")
    parser.add_argument("--install-dir", type=Path)
    args = parser.parse_args()
    requested = args.addons or discover_addon_ids()
    install_dir = args.install_dir or default_install_dir(args.blender_version)
    if install_dir is None:
        raise SystemExit("Set BLENDER_USER_ADDONS or pass --install-dir")
    install_dir = install_dir.resolve()
    if install_dir.name != "addons":
        raise SystemExit(f"Refusing non-addons target: {install_dir}")
    with tempfile.TemporaryDirectory(prefix="blender-addon-deploy-") as tmp:
        staging = Path(tmp)
        for addon_id in requested:
            archive = package_addon(addon_id)
            with ZipFile(archive) as zipped:
                zipped.extractall(staging)
            mirror_tree(staging / addon_id, install_dir / addon_id)
            manifest = install_dir / addon_id / "blender_manifest.toml"
            print(f"deployed {addon_id}: {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
