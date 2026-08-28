# SPDX-License-Identifier: GPL-2.0-or-later
"""Quit Dropbox while a watched .blend is open; start it again afterward.

Dropbox has no per-folder pause that keeps the cloud copy. Ignoring a folder
deletes it from dropbox.com, so this add-on stops the Dropbox client instead
while the open file lives under a configured directory.
"""

import os
import subprocess
import sys
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

bl_info = {
    "name": "Dropbox Blend Pause",
    "author": "Teo Asinari",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Dropbox Pause",
    "description": "Stop Dropbox while a watched project is open in Blender",
    "category": "System",
}

DEFAULT_WATCH = (
    r"C:\Users\Teo Asinari\Dropbox\My Sculptures\2026-02-16_#1"
)
DROPBOX_EXE = Path(r"C:\Program Files (x86)\Dropbox\Client\Dropbox.exe")

_paused_by_us = False


def _preferences(context=None):
    context = context or bpy.context
    addons = getattr(context.preferences, "addons", None)
    if addons is None:
        return None
    return addons.get(__name__)


def _watch_dir(context=None):
    prefs = _preferences(context)
    raw = DEFAULT_WATCH
    if prefs is not None and prefs.preferences.watch_directory.strip():
        raw = prefs.preferences.watch_directory
    return Path(bpy.path.abspath(raw)).expanduser()


def _open_path():
    filepath = bpy.data.filepath
    if not filepath:
        return None
    return Path(bpy.path.abspath(filepath))


def _is_watched_open(context=None):
    path = _open_path()
    if path is None:
        return False
    try:
        path = path.resolve()
        watch = _watch_dir(context).resolve()
    except OSError:
        return False
    if path == watch or watch in path.parents:
        return True
    return False


def _dropbox_pids():
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Dropbox.exe", "/FO", "CSV",
             "/NH"],
            capture_output=True, text=True, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        return []
    pids = []
    for line in completed.stdout.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == "dropbox.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def _stop_dropbox():
    global _paused_by_us
    if os.name != "nt":
        return False
    if not _dropbox_pids():
        return False
    subprocess.run(
        ["taskkill", "/IM", "Dropbox.exe", "/F"],
        capture_output=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _paused_by_us = True
    return True


def _start_dropbox():
    global _paused_by_us
    if os.name != "nt":
        _paused_by_us = False
        return False
    if _dropbox_pids():
        _paused_by_us = False
        return False
    exe = DROPBOX_EXE
    if not exe.is_file():
        _paused_by_us = False
        return False
    subprocess.Popen(
        [str(exe)],
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _paused_by_us = False
    return True


def _sync_dropbox_state(context=None):
    prefs = _preferences(context)
    enabled = True if prefs is None else prefs.preferences.enabled
    if enabled and _is_watched_open(context):
        _stop_dropbox()
    elif _paused_by_us:
        _start_dropbox()


@persistent
def _on_load_post(_dummy):
    _sync_dropbox_state()


@persistent
def _on_save_post(_dummy):
    _sync_dropbox_state()


class DROPBOXPAUSE_OT_sync(Operator):
    bl_idname = "dropboxpause.sync"
    bl_label = "Update Dropbox Now"
    bl_description = "Stop or start Dropbox from the current .blend path"

    def execute(self, context):
        _sync_dropbox_state(context)
        if _paused_by_us:
            self.report({'INFO'}, "Dropbox stopped while this project is open")
        else:
            self.report({'INFO'}, "Dropbox left running")
        return {'FINISHED'}


class DROPBOXPAUSE_OT_resume(Operator):
    bl_idname = "dropboxpause.resume"
    bl_label = "Start Dropbox"
    bl_description = "Start Dropbox even if the watched project is still open"

    def execute(self, context):
        _start_dropbox()
        self.report({'INFO'}, "Dropbox start requested")
        return {'FINISHED'}


class DROPBOXPAUSE_PT_sidebar(Panel):
    bl_label = "Dropbox Pause"
    bl_idname = "DROPBOXPAUSE_PT_sidebar"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Dropbox Pause"

    def draw(self, context):
        layout = self.layout
        prefs = _preferences(context)
        if prefs is not None:
            layout.prop(prefs.preferences, "enabled")
            layout.prop(prefs.preferences, "watch_directory")
        watched = _is_watched_open(context)
        layout.label(text="This file is in the watched folder"
                     if watched else "This file is outside the watched folder",
                     icon='CHECKMARK' if watched else 'INFO')
        layout.label(text="Dropbox stopped by this add-on"
                     if _paused_by_us else "Dropbox not stopped by this add-on")
        layout.operator(DROPBOXPAUSE_OT_sync.bl_idname)
        layout.operator(DROPBOXPAUSE_OT_resume.bl_idname)


class DROPBOXPAUSE_Preferences(AddonPreferences):
    bl_idname = __name__

    enabled: BoolProperty(
        name="Pause Dropbox for the watched folder",
        description="Quit Dropbox while the open .blend lives under the "
                    "watched directory; start it again when you leave",
        default=True,
        update=lambda self, context: _sync_dropbox_state(context))
    watch_directory: StringProperty(
        name="Watched folder",
        description="Stop Dropbox when the open .blend is inside this folder",
        subtype='DIR_PATH',
        default=DEFAULT_WATCH,
        update=lambda self, context: _sync_dropbox_state(context))

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "enabled")
        layout.prop(self, "watch_directory")
        layout.label(text="Quits Dropbox.exe on Windows. Does not ignore "
                          "the folder in Dropbox (that would delete the "
                          "cloud copy).")


classes = (
    DROPBOXPAUSE_OT_sync,
    DROPBOXPAUSE_OT_resume,
    DROPBOXPAUSE_PT_sidebar,
    DROPBOXPAUSE_Preferences,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    if _on_save_post not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(_on_save_post)
    _sync_dropbox_state()


def unregister():
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if _on_save_post in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_on_save_post)
    if _paused_by_us:
        _start_dropbox()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
