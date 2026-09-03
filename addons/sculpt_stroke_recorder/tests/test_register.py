# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless registration and serialization tests."""

import json
import os
import sys
import tempfile
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


def make_point():
    return SimpleNamespace(
        name="", location=(1.0, 2.0, 3.0), mouse=(40.0, 50.0),
        mouse_event=(41.0, 51.0), pressure=0.75, size=32.0,
        x_tilt=0.1, y_tilt=-0.2, time=0.25, is_start=True)


bpy.ops.wm.read_factory_settings(use_empty=True)
recorder.register()

check("scene settings registered",
      hasattr(bpy.types.Scene, "sculpt_stroke_recorder"))
check("record operator registered",
      hasattr(bpy.ops.sculpt_recorder, "record_toggle"))
check("replay operator registered",
      hasattr(bpy.ops.sculpt_recorder, "replay"))
check("delete operator registered",
      hasattr(bpy.ops.sculpt_recorder, "remove_take"))
check("sidebar panel registered",
      hasattr(bpy.types, "VIEW3D_PT_sculpt_stroke_recorder"))
check("global 3D View recording shortcut registered",
      len(recorder._keymaps) == 2)
check("non-empty deletion has confirmation path",
      "invoke" in recorder.SCULPTREC_OT_remove_take.__dict__)

props = SimpleNamespace(stroke=[make_point()], mode='NORMAL',
                        brush_toggle='NONE', pen_flip=False)
op = SimpleNamespace(bl_idname="SCULPT_OT_brush_stroke", properties=props)
payload = recorder.serialize_stroke_operator(op)
check("native sculpt operator serialized", payload is not None)
check("sculpt payload tagged sculpt", payload["kind"] == recorder.KIND_SCULPT)
check("all native sample fields retained",
      payload["samples"][0]["location"] == [1.0, 2.0, 3.0]
      and payload["samples"][0]["pressure"] == 0.75
      and payload["samples"][0]["is_start"] is True)
check("non-stroke operator ignored",
      recorder.serialize_stroke_operator(
          SimpleNamespace(bl_idname="MESH_OT_primitive_cube_add")) is None)

paint_props = SimpleNamespace(stroke=[make_point()], mode='NORMAL',
                              brush_toggle='None', pen_flip=True)
paint_op = SimpleNamespace(bl_idname="PAINT_OT_image_paint",
                           properties=paint_props)
paint_payload = recorder.serialize_stroke_operator(paint_op)
check("native texture-paint operator serialized", paint_payload is not None)
check("paint payload tagged texture_paint",
      paint_payload["kind"] == recorder.KIND_PAINT)
check("paint pen_flip retained", paint_payload["pen_flip"] is True)
check("legacy payload without kind is sculpt",
      recorder.payload_kind({"samples": []}) == recorder.KIND_SCULPT)

replay = recorder.replay_samples(payload)
check("replay sample has native tuple vectors",
      replay[0]["location"] == (1.0, 2.0, 3.0)
      and replay[0]["mouse"] == (40.0, 50.0))
check("replay sample preserves tablet data",
      replay[0]["pressure"] == 0.75
      and replay[0]["x_tilt"] == 0.1
      and replay[0]["y_tilt"] == -0.2)
check("paint replay samples share the same contract",
      recorder.replay_samples(paint_payload)[0]["pressure"] == 0.75)
check("HUD stroke log summarizes samples, duration, and pressure",
      recorder.stroke_log_summary(json.dumps(paint_payload))
      == "1 samples · 0.25s · pressure 0.75–0.75")

settings = bpy.context.scene.sculpt_stroke_recorder
take = settings.takes.add()
take.name = "Test Take"
take.object_name = "Sculpt"
take.source_mode = "SCULPT"
stroke = take.strokes.add()
stroke.payload = json.dumps(payload)
check("nested take data stored on Scene",
      len(settings.takes) == 1 and len(settings.takes[0].strokes) == 1)
sidecar = os.path.join(tempfile.gettempdir(), "stroke_recorder_test.json.gz")
recorder.externalize_recordings(settings, sidecar)
check("takes externalize to compressed sidecar",
      not take.strokes and take.stroke_count == 1
      and open(sidecar, "rb").read(2) == b"\x1f\x8b"
      and len(recorder._take_records(take, recorder._read_sidecar(sidecar))) == 1)

bpy.ops.mesh.primitive_uv_sphere_add()
obj = bpy.context.active_object
check("object mode cannot start a take",
      bpy.ops.sculpt_recorder.record_toggle() == {'CANCELLED'})

bpy.ops.object.mode_set(mode='SCULPT')
check("sculpt mode starts a take",
      bpy.ops.sculpt_recorder.record_toggle() == {'FINISHED'})
check("recording flag set", settings.recording is True)
check("recording overlay is registered", recorder._overlay_handle is not None)
check("new take locked to SCULPT",
      settings.takes[settings.active_take].source_mode == "SCULPT")
bpy.ops.sculpt_recorder.record_toggle()
check("second toggle stops recording", settings.recording is False)
check("recording overlay is removed", recorder._overlay_handle is None)

bpy.ops.object.mode_set(mode='OBJECT')
mat = bpy.data.materials.new("PaintMat")
mat.use_nodes = True
obj.data.materials.append(mat)
image = bpy.data.images.new("PaintCanvas", 16, 16)
bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
check("texture paint starts a take",
      bpy.ops.sculpt_recorder.record_toggle() == {'FINISHED'})
paint_take = settings.takes[settings.active_take]
check("new take locked to TEXTURE_PAINT",
      paint_take.source_mode == "TEXTURE_PAINT")
bpy.ops.sculpt_recorder.record_toggle()

paint_take.strokes.add().payload = json.dumps(paint_payload)
check("texture-paint take replays in texture paint",
      bpy.ops.sculpt_recorder.replay.poll())
bpy.ops.object.mode_set(mode='SCULPT')
check("texture-paint take refuses sculpt replay",
      not bpy.ops.sculpt_recorder.replay.poll())
settings.active_take = 0
check("sculpt take replays in sculpt",
      bpy.ops.sculpt_recorder.replay.poll())

from impasto import gpu_engine  # noqa: E402

bpy.ops.object.mode_set(mode='OBJECT')
check("object mode without GPU session cannot start a take",
      bpy.ops.sculpt_recorder.record_toggle() == {'CANCELLED'})

images = [bpy.data.images.new("Recorder GPU Canvas", 16, 16, alpha=True)]
started = gpu_engine.start_session(
    obj, images, None,
    payloads=gpu_engine.stroke_payloads(
        ("base_color",),
        {"color": (0.8, 0.2, 0.1), "roughness": 0.5, "metallic": 0.0,
         "normal": (0.5, 0.5, 1.0), "height_strength": 0.1,
         "height_direction": "RAISE"}),
    settings={"radius": 40.0, "hardness": 0.5, "opacity": 1.0,
              "brush_mode": "PAINT", "channel_keys": ("base_color",),
              "brush_target_channel_keys": ("base_color",),
              "active_layer_uid": "layer"})
check("headless Impasto GPU session starts", started)
check("active Impasto session overrides incidental Texture Paint mode",
      recorder._mesh_record_kind(SimpleNamespace(
          type='MESH', mode='TEXTURE_PAINT')) == recorder.KIND_IMPASTO)
check("Impasto GPU session starts a take from Object Mode",
      bpy.ops.sculpt_recorder.record_toggle() == {'FINISHED'})
gpu_take = settings.takes[settings.active_take]
check("new take locked to IMPASTO_GPU",
      gpu_take.source_mode == recorder.MODE_IMPASTO)
gpu_engine.begin_stroke(8.0, 9.0, 0.4, {"x_tilt": 0.05})
gpu_engine.move_stroke(18.0, 9.0, 0.9, 40.0)
gpu_engine.end_stroke()
check("Impasto GPU pen-up is stored on the take",
      len(gpu_take.strokes) == 1)
stored = json.loads(gpu_take.strokes[0].payload)
check("stored GPU payload keeps kind and pointer samples",
      stored["kind"] == recorder.KIND_IMPASTO
      and stored["samples"][0]["mouse"] == [8.0, 9.0]
      and stored["samples"][0]["x_tilt"] == 0.05
      and stored["samples"][-1]["mouse"] == [18.0, 9.0])
bpy.ops.sculpt_recorder.record_toggle()
check("Impasto take replays while the GPU session is active",
      bpy.ops.sculpt_recorder.replay.poll())
check("Impasto payload replays into the live session",
      recorder.replay_impasto_payload(stored) is True)
gpu_engine.stop_session()
check("Impasto take refuses replay after the GPU session ends",
      not bpy.ops.sculpt_recorder.replay.poll())
check("legacy payload without kind is sculpt",
      recorder.payload_kind({"samples": []}) == recorder.KIND_SCULPT)

# Deletion must update both Scene metadata and compressed storage atomically.
delete_a = settings.takes.add()
delete_a.name = "Delete A"
delete_a.source_mode = "SCULPT"
delete_a.strokes.add().payload = json.dumps(payload)
delete_b = settings.takes.add()
delete_b.name = "Delete B"
delete_b.source_mode = "SCULPT"
delete_b.strokes.add().payload = json.dumps(payload)
delete_path = os.path.join(tempfile.gettempdir(),
                           "stroke_recorder_delete_test.json.gz")
recorder.externalize_recordings(settings, delete_path)
delete_index = len(settings.takes) - 2
delete_a_id = settings.takes[delete_index].storage_id
delete_b_id = settings.takes[delete_index + 1].storage_id
settings.active_take = delete_index
check("stored take deletion succeeds",
      recorder.remove_take(settings, delete_index, delete_path))
stored_ids = recorder._read_sidecar(delete_path)["takes"]
check("deleted take leaves no sidecar payload",
      delete_a_id not in stored_ids and delete_b_id in stored_ids)
check("deletion selects the take that moved into its row",
      settings.active_take == delete_index
      and settings.takes[delete_index].storage_id == delete_b_id)
check("deleting final row selects the new last row",
      recorder.remove_take(settings, delete_index, delete_path)
      and settings.active_take == len(settings.takes) - 1)

recorder.unregister()
check("scene settings unregistered",
      not hasattr(bpy.types.Scene, "sculpt_stroke_recorder"))
check("global recording shortcut removed", not recorder._keymaps)
print("SCULPT_STROKE_RECORDER_TESTS_PASSED")
