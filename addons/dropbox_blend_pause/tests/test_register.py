# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless enable/register: RestrictData must not fail register()."""

import os
import sys
from types import SimpleNamespace

import bpy

ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDONS_ROOT = os.path.dirname(ADDON_DIR)
if ADDONS_ROOT not in sys.path:
    sys.path.insert(0, ADDONS_ROOT)

import dropbox_blend_pause as addon  # noqa: E402


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("  ok  " + name)


class _RestrictData:
    pass


saved_data = bpy.data
try:
    bpy.data = _RestrictData()
    check("open path is None under RestrictData", addon._open_path() is None)
    addon.register()
    check("register succeeds while data is restricted", True)
    addon.unregister()
finally:
    bpy.data = saved_data

bpy.ops.wm.read_factory_settings(use_empty=True)
addon.register()
check("register succeeds with real bpy.data", True)
check("sync operator registered",
      hasattr(bpy.ops, "dropboxpause")
      and hasattr(bpy.ops.dropboxpause, "sync"))
addon.unregister()
print("DROPBOX_BLEND_PAUSE_TESTS_PASSED")
