#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build Install-from-Disk ZIPs for blender-workflow-lab add-ons.

Each archive has the add-on folder at its root (``impasto/__init__.py``,
not ``addons/impasto/...``) and is named ``<id>-<version>.zip`` from
``bl_info``. Packaging only copies source files; optional extras such as
SciPy are not required.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
ADDONS_DIR = REPO_ROOT / "addons"
DEFAULT_DIST = REPO_ROOT / "dist"

# DOS-epoch timestamp so ZIP bytes do not depend on mtime.
_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_FILE_ATTR = 0o644 << 16

EXCLUDE_DIR_NAMES = frozenset({
    "__pycache__",
    "tests",
    "probes",
    "docs",
    ".git",
})
EXCLUDE_FILE_NAMES = frozenset({
    "PROGRESS.md",
    "ROADMAP.md",
    "CHANGELOG.md",
    ".DS_Store",
    "Thumbs.db",
})
EXCLUDE_SUFFIXES = frozenset({".pyc", ".pyo", ".zip"})

_BL_INFO_VERSION_RE = re.compile(
    r"""["']version["']\s*:\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)""",
)


def discover_addon_ids(addons_dir: Path = ADDONS_DIR) -> list[str]:
    ids = [
        path.name
        for path in sorted(addons_dir.iterdir())
        if path.is_dir() and (path / "__init__.py").is_file()
    ]
    if not ids:
        raise SystemExit(f"No add-ons found in {addons_dir}")
    return ids


def parse_bl_info_version(init_py: Path) -> str:
    text = init_py.read_text(encoding="utf-8")
    match = _BL_INFO_VERSION_RE.search(text)
    if match is None:
        raise SystemExit(f"Could not parse bl_info version in {init_py}")
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}"


def parse_toml_string(manifest: Path, key: str) -> str:
    text = manifest.read_text(encoding="utf-8")
    match = re.search(
        rf'^{re.escape(key)}\s*=\s*"([^"]+)"',
        text,
        re.MULTILINE,
    )
    if match is None:
        raise SystemExit(f"Could not parse {key!r} in {manifest}")
    return match.group(1)


def iter_addon_files(addon_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in addon_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(addon_dir)
        if any(part in EXCLUDE_DIR_NAMES for part in relative.parts[:-1]):
            continue
        if relative.name in EXCLUDE_FILE_NAMES:
            continue
        if relative.suffix in EXCLUDE_SUFFIXES:
            continue
        if relative.name.startswith("."):
            continue
        files.append(relative)
    files.sort(key=lambda item: item.as_posix())
    return files


def _zip_info(arcname: str) -> ZipInfo:
    info = ZipInfo(filename=arcname, date_time=_ZIP_DATE_TIME)
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = _FILE_ATTR
    return info


def package_addon(
    addon_id: str,
    *,
    addons_dir: Path = ADDONS_DIR,
    output_dir: Path = DEFAULT_DIST,
) -> Path:
    addon_dir = addons_dir / addon_id
    init_py = addon_dir / "__init__.py"
    manifest = addon_dir / "blender_manifest.toml"
    license_file = addon_dir / "LICENSE"
    if not init_py.is_file():
        raise SystemExit(f"Missing {init_py}")
    if not manifest.is_file():
        raise SystemExit(f"Missing {manifest}")
    if not license_file.is_file():
        raise SystemExit(f"Missing {license_file}")

    version = parse_bl_info_version(init_py)
    manifest_id = parse_toml_string(manifest, "id")
    manifest_version = parse_toml_string(manifest, "version")
    if manifest_id != addon_id:
        raise SystemExit(
            f"{manifest} id {manifest_id!r} does not match folder {addon_id!r}"
        )
    if manifest_version != version:
        raise SystemExit(
            f"{manifest} version {manifest_version!r} does not match "
            f"bl_info {version!r}"
        )

    members = iter_addon_files(addon_dir)
    if not members:
        raise SystemExit(f"No files to pack in {addon_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob(f"{addon_id}-*.zip"):
        stale.unlink()

    zip_path = output_dir / f"{addon_id}-{version}.zip"
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for relative in members:
            arcname = f"{addon_id}/{relative.as_posix()}"
            data = (addon_dir / relative).read_bytes()
            archive.writestr(_zip_info(arcname), data)
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Package add-ons as Install-from-Disk ZIPs "
            "(folder at archive root, versioned filenames)."
        ),
    )
    parser.add_argument(
        "addons",
        nargs="*",
        help="Add-on folder names under addons/. Default: pack all.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_DIST,
        help="Directory for ZIP files (default: <repo>/dist)",
    )
    args = parser.parse_args(argv)

    available = discover_addon_ids()
    requested = args.addons or available
    unknown = [name for name in requested if name not in available]
    if unknown:
        names = ", ".join(available)
        raise SystemExit(
            f"Unknown add-on(s): {', '.join(unknown)}. Available: {names}"
        )

    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir

    for addon_id in requested:
        zip_path = package_addon(
            addon_id,
            output_dir=output_dir,
        )
        with ZipFile(zip_path) as archive:
            count = len(archive.namelist())
        try:
            display = zip_path.relative_to(REPO_ROOT)
        except ValueError:
            display = zip_path
        print(f"wrote {display} ({count} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
