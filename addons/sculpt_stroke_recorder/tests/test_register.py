# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless registration and serialization tests."""

import os
import sys
from types import SimpleNamespace

import bpy

ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDONS_ROOT = os.path.dirname(ADDON_DIR)
if ADDONS_ROOT not in sys.path:
    sys.path.insert(0, ADDONS_ROOT)

import sculpt_stroke_recorder as recorder  # noqa: E402


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print("  ok  " + name)


bpy.ops.wm.read_factory_settings(use_empty=True)
recorder.register()

check("scene settings registered",
      hasattr(bpy.types.Scene, "sculpt_stroke_recorder"))
check("record operator registered",
      hasattr(bpy.ops.sculpt_recorder, "record_toggle"))
check("replay operator registered",
      hasattr(bpy.ops.sculpt_recorder, "replay"))
check("sidebar panel registered",
      hasattr(bpy.types, "VIEW3D_PT_sculpt_stroke_recorder"))

point = SimpleNamespace(
    name="", location=(1.0, 2.0, 3.0), mouse=(40.0, 50.0),
    mouse_event=(41.0, 51.0), pressure=0.75, size=32.0,
    x_tilt=0.1, y_tilt=-0.2, time=0.25, is_start=True)
props = SimpleNamespace(stroke=[point], mode='NORMAL', brush_toggle='NONE',
                        pen_flip=False)
op = SimpleNamespace(bl_idname="SCULPT_OT_brush_stroke", properties=props)
payload = recorder.serialize_stroke_operator(op)
check("native sculpt operator serialized", payload is not None)
check("all native sample fields retained",
      payload["samples"][0]["location"] == [1.0, 2.0, 3.0]
      and payload["samples"][0]["pressure"] == 0.75
      and payload["samples"][0]["is_start"] is True)
check("non-sculpt operator ignored",
      recorder.serialize_stroke_operator(
          SimpleNamespace(bl_idname="MESH_OT_primitive_cube_add")) is None)

replay = recorder.replay_samples(payload)
check("replay sample has native tuple vectors",
      replay[0]["location"] == (1.0, 2.0, 3.0)
      and replay[0]["mouse"] == (40.0, 50.0))
check("replay sample preserves tablet data",
      replay[0]["pressure"] == 0.75
      and replay[0]["x_tilt"] == 0.1
      and replay[0]["y_tilt"] == -0.2)

settings = bpy.context.scene.sculpt_stroke_recorder
take = settings.takes.add()
take.name = "Test Take"
take.object_name = "Sculpt"
stroke = take.strokes.add()
stroke.payload = __import__('json').dumps(payload)
check("nested take data stored on Scene",
      len(settings.takes) == 1 and len(settings.takes[0].strokes) == 1)

recorder.unregister()
check("scene settings unregistered",
      not hasattr(bpy.types.Scene, "sculpt_stroke_recorder"))
print("SCULPT_STROKE_RECORDER_TESTS_PASSED")
