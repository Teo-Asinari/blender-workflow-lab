# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless idle SSS caliper overlay contracts.

GPU drawing is not exercised; this covers background-safe register,
active-layer target resolution, and GPU-session suppression.
"""

import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import engine, gpu_engine
from impasto.gpu import overlays, sss_caliper_overlay


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


BRUSH = {"color": (0.5, 0.25, 1.0), "roughness": 0.7,
         "metallic": 0.2, "normal": (0.5, 0.5, 1.0),
         "height_strength": 0.05, "height_direction": "RAISE"}


registered = False
try:
    sss_caliper_overlay.unregister()
    sss_caliper_overlay.register()
    sss_caliper_overlay.register()
    sss_caliper_overlay.unregister()
    check("idle overlay register/unregister is safe in background", True)

    class _RestrictData:
        pass

    saved_data = bpy.data
    try:
        bpy.data = _RestrictData()
        check("restricted data has no node_groups",
              not hasattr(bpy.data, "node_groups"))
        check("stack discovery is empty while data is restricted",
              list(engine.iter_stack_trees()) == [])
        check("caliper scan is false while data is restricted",
              not sss_caliper_overlay.any_paint_caliper_enabled())
        sss_caliper_overlay.register()
        check("idle overlay register succeeds while data is restricted", True)
        sss_caliper_overlay.unregister()
        impasto.register()
        registered = True
        check("package register succeeds while data is restricted", True)
        impasto.unregister()
        registered = False
    finally:
        bpy.data = saved_data

    source = overlays.SSSCaliperSource(
        cursor=(10.0, 20.0), obj_name="Missing",
        settings={"sss_caliper_enabled": False})
    overlays.draw_sss_caliper(source, None, None, lambda: False)
    overlays.draw_sss_caliper(
        overlays.SSSCaliperSource(
            cursor=(10.0, 20.0),
            settings={"sss_caliper_enabled": True}),
        None, None, lambda: True)
    check("caliper drawing no-ops without GPU work when disabled or inspecting",
          True)
    check("ray-cast helper misses without a cursor",
          overlays._sss_cursor_surface(
              overlays.SSSCaliperSource(cursor=None, obj_name="Cube"),
              None, None) is None)
    check("ray-cast helper misses without a region",
          overlays._sss_cursor_surface(
              overlays.SSSCaliperSource(cursor=(10.0, 20.0), obj_name="Cube"),
              None, SimpleNamespace()) is None)
    window = SimpleNamespace(mouse_x=120, mouse_y=80)
    region = SimpleNamespace(x=100, y=50, width=200, height=100)
    check("idle cursor uses window minus region origin",
          sss_caliper_overlay.region_local_cursor(window, region)
          == (20.0, 30.0))
    check("idle cursor ignores the pointer outside the region",
          sss_caliper_overlay.region_local_cursor(
              SimpleNamespace(mouse_x=50, mouse_y=80), region) is None)
    check("idle cursor ignores overflow past the region far edge",
          sss_caliper_overlay.region_local_cursor(
              SimpleNamespace(mouse_x=300, mouse_y=80), region) is None
          and sss_caliper_overlay.region_local_cursor(
              SimpleNamespace(mouse_x=120, mouse_y=160), region) is None)
    check("idle cursor rejects a missing window or region",
          sss_caliper_overlay.region_local_cursor(None, region) is None
          and sss_caliper_overlay.region_local_cursor(window, None) is None)

    impasto.register()
    registered = True
    check("no draw contract without an Impasto stack",
          sss_caliper_overlay.resolve_target(bpy.context) is None
          and not sss_caliper_overlay.should_draw(bpy.context))

    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    if not obj.data.uv_layers:
        obj.data.uv_layers.new(name="UVMap")
    check("stack init",
          bpy.ops.impasto.stack_init(
              template="PRINCIPLED_STANDARD") == {"FINISHED"})
    check("missing paint layer is not a caliper target",
          sss_caliper_overlay.resolve_target(bpy.context) is None)
    check("add paint",
          bpy.ops.impasto.layer_add(layer_type="PAINT",
                                    canvas_size='1024') == {"FINISHED"})
    stack = engine.find_stack_for_material(obj.active_material)
    layer = stack.impasto.active_layer()
    check("toggle off is not a caliper target",
          not layer.show_sss_caliper
          and sss_caliper_overlay.resolve_target(bpy.context) is None
          and not sss_caliper_overlay.any_paint_caliper_enabled())
    layer.paint_sss_scale = 0.12
    layer.paint_sss_radius = (0.8, 0.4, 0.2)
    layer.show_sss_caliper = True
    expected_scale = float(layer.paint_sss_scale)
    expected_radius = tuple(float(v) for v in layer.paint_sss_radius)
    target = sss_caliper_overlay.resolve_target(bpy.context)
    check("enabled paint layer reports Scale and Radius RGB",
          target is not None
          and target["object"].name == obj.name
          and target["layer"].name == layer.name
          and target["scale"] == expected_scale
          and target["radius"] == expected_radius
          and target["settings"]["sss_caliper_enabled"] is True
          and target["settings"]["sss_caliper_scale"] == expected_scale
          and target["settings"]["sss_caliper_radius"] == expected_radius,
          repr(None if target is None else {
              "object": target["object"].name,
              "layer": target["layer"].name,
              "scale": target["scale"],
              "radius": target["radius"],
              "settings": target["settings"],
              "expected_scale": expected_scale,
              "expected_radius": expected_radius,
          }))
    check("idle overlay would draw without a GPU session",
          sss_caliper_overlay.should_draw(bpy.context)
          and not gpu_engine.session_active())
    check("caliper toggle starts the idle cursor timer",
          sss_caliper_overlay.timer_is_registered())

    paint_uid = layer.name
    check("add fill",
          bpy.ops.impasto.layer_add(layer_type="FILL") == {"FINISHED"})
    check("active Fill layer does not draw the idle caliper",
          stack.impasto.active_layer().layer_type == 'FILL'
          and sss_caliper_overlay.resolve_target(bpy.context) is None
          and not sss_caliper_overlay.should_draw(bpy.context)
          and sss_caliper_overlay.any_paint_caliper_enabled())

    stack.impasto.active_index = next(
        i for i, ly in enumerate(stack.impasto.layers)
        if ly.name == paint_uid)
    layer = stack.impasto.active_layer()
    check("switching back to the Paint layer restores the draw contract",
          layer is not None and layer.name == paint_uid
          and sss_caliper_overlay.should_draw(bpy.context))

    images = [bpy.data.images.new("Impasto SSS Overlay Test", 64, 64,
                                  alpha=True)]
    started = gpu_engine.start_session(
        obj, images, None,
        payloads=gpu_engine.stroke_payloads(("base_color",), BRUSH),
        settings={"radius": 40.0, "hardness": 0.5})
    check("headless session starts as a logical no-op", started)
    check("GPU session suppresses the idle overlay",
          gpu_engine.session_active()
          and sss_caliper_overlay.resolve_target(bpy.context) is not None
          and not sss_caliper_overlay.should_draw(bpy.context))
    gpu_engine.stop_session()
    check("stopping GPU painting restores the idle caliper",
          not gpu_engine.session_active()
          and sss_caliper_overlay.should_draw(bpy.context))

    layer.show_sss_caliper = False
    check("disabling the last caliper stops the idle timer",
          not sss_caliper_overlay.any_paint_caliper_enabled()
          and sss_caliper_overlay.resolve_target(bpy.context) is None
          and not sss_caliper_overlay.timer_is_registered())

    impasto.unregister()
    registered = False
    check("unregister leaves the idle overlay stopped",
          not sss_caliper_overlay.timer_is_registered()
          and not sss_caliper_overlay.draw_handler_registered())
    print("IMPASTO_SSS_CALIPER_OVERLAY_PASSED")
except Exception:
    if registered:
        try:
            impasto.unregister()
        except Exception:
            pass
    if gpu_engine.session_active():
        try:
            gpu_engine.stop_session()
        except Exception:
            pass
    traceback.print_exc()
    print("IMPASTO_SSS_CALIPER_OVERLAY_FAILED")
