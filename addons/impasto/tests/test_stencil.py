# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless stencil transform, mask, state, and shader-contract tests."""

import math
import sys
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import engine, gpu_engine, ops, stencil


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


try:
    centered = stencil.normalized(
        True, "Mask", 'VIEW_STENCIL', position=(0.5, 0.5),
        scale=(0.5, 0.25))
    check("viewport stencil center maps to image center",
          stencil.image_uv((400, 300), (0, 0), 25, (800, 600), centered)
          == (0.5, 0.5))
    check("viewport stencil respects independent normalized scale",
          stencil.image_uv((600, 300), (0, 0), 25, (800, 600), centered)
          == (1.0, 0.5))
    check("viewport points outside stencil are rejected",
          stencil.image_uv((601, 300), (0, 0), 25, (800, 600), centered)
          is None)
    rotated = stencil.normalized(
        True, "Mask", 'VIEW_STENCIL', position=(0.5, 0.5),
        scale=(0.5, 0.25), rotation=math.pi * 0.5)
    uv = stencil.image_uv((400, 400), (0, 0), 25, (800, 600), rotated)
    check("rotation is inverse-applied into image space",
          uv is not None and abs(uv[0] - 0.75) < 1e-6
          and abs(uv[1] - 0.5) < 1e-6, repr(uv))
    tip = stencil.normalized(True, "Tip", 'BRUSH_ALPHA', scale=(1.0, 0.5))
    check("brush alpha follows dab center and radius",
          stencil.image_uv((120, 100), (100, 100), 20, (800, 600), tip)
          == (1.0, 0.5))
    check("alpha interpretation uses image alpha",
          abs(stencil.interpreted_mask((1.0, 0.0, 0.0, 0.25),
                                       'ALPHA', 0.5) - 0.125) < 1e-9)
    check("luminance interpretation uses linear Rec.709 weights",
          abs(stencil.interpreted_mask((1.0, 0.0, 0.0, 0.0),
                                       'LUMINANCE', 1.0) - 0.2126) < 1e-9)
    profile = stencil.profile_tangent_normal(0.0, 1.0, 0.5, 0.5,
                                             strength=2.0)
    inverse = stencil.profile_tangent_normal(0.0, 1.0, 0.5, 0.5,
                                             strength=2.0, invert=True)
    flat = stencil.profile_tangent_normal(0.5, 0.5, 0.5, 0.5,
                                          strength=10.0)
    check("normal-profile contract derives tangent detail from gradients",
          profile[0] < 0.5 < inverse[0]
          and abs(profile[1] - 0.5) < 1e-9
          and flat == (0.5, 0.5, 1.0))
    check("normal-profile invert reverses relief without changing magnitude",
          abs((profile[0] - 0.5) + (inverse[0] - 0.5)) < 1e-9
          and abs(profile[2] - inverse[2]) < 1e-9)
    low_res = stencil.profile_tangent_normal(
        0.25, 0.75, 0.4, 0.6, image_size=(4, 10))
    high_res = stencil.profile_tangent_normal(
        0.375, 0.625, 0.45, 0.55, image_size=(8, 20))
    check("normal-profile strength is independent of stencil resolution",
          all(abs(a - b) < 1e-9 for a, b in zip(low_res, high_res)),
          repr((low_res, high_res)))
    wide = stencil.profile_tangent_normal(
        0.375, 0.625, 0.25, 0.75, image_size=(8, 4))
    check("normal-profile derivatives scale independently per image axis",
          abs(wide[0] - wide[1]) < 1e-9, repr(wide))
    invalid = stencil.normalized(True, "", 'UNKNOWN', 'UNKNOWN', 4.0,
                                 scale=(0.0, -2.0))
    check("invalid settings normalize safely and missing image disables",
          invalid.projection == 'VIEW_STENCIL'
          and invalid.interpretation == 'ALPHA'
          and invalid.opacity == 1.0 and not invalid.active
          and invalid.scale == (0.001, 2.0))

    impasto.register()
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    obj.data.uv_layers.new(name="UVMap")
    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {'FINISHED'})
    check("paint layer add", bpy.ops.impasto.layer_add(
        layer_type="PAINT") == {'FINISHED'})
    tree = engine.find_stack_for_material(obj.active_material)
    layer = tree.impasto.active_layer()
    for scalar_key in ("metallic", "roughness"):
        check("bind %s stencil target" % scalar_key,
              bpy.ops.impasto.binding_add(
                  channel_key=scalar_key) == {'FINISHED'})
    mask = bpy.data.images.new("Impasto Stencil Test", 16, 8, alpha=True)
    layer.brush_stencil_enabled = True
    layer.brush_stencil_image = mask
    layer.brush_stencil_projection = 'BRUSH_ALPHA'
    layer.brush_stencil_interpretation = 'LUMINANCE'
    layer.brush_stencil_usage = 'NORMAL_PROFILE'
    layer.brush_stencil_coverage = True
    layer.brush_stencil_opacity = 0.6
    layer.brush_stencil_position = (0.25, 0.75)
    check("Brush Alpha defaults to one full brush diameter",
          tuple(layer.brush_stencil_brush_scale) == (1.0, 1.0)
          and ops.gpu_stencil_settings(layer).scale == (1.0, 1.0))
    layer.brush_stencil_brush_scale = (1.2, 0.8)
    layer.brush_stencil_rotation = 0.4
    layer.brush_stencil_profile_strength = 2.5
    layer.brush_stencil_profile_invert = True
    settings = ops.gpu_stencil_settings(layer)
    check("persistent layer state maps to plain GPU settings",
          settings.active and settings.image_name == mask.name
          and settings.projection == 'BRUSH_ALPHA'
          and settings.interpretation == 'LUMINANCE'
          and settings.usage == 'NORMAL_PROFILE'
          and settings.coverage
          and settings.opacity > 0.59
          and settings.profile_strength == 2.5
          and settings.profile_invert
          and settings.position == (0.25, 0.75)
          and all(abs(a - b) < 1e-6 for a, b in
                  zip(settings.scale, (1.2, 0.8))))
    gpu_settings = settings.as_gpu_settings()
    check("runtime settings contain no Blender RNA objects",
          gpu_settings['stencil_image_name'] == mask.name
          and all(not isinstance(value, bpy.types.ID)
                  for value in gpu_settings.values()))

    source = gpu_engine.dab_frag_src(4)
    check("dab shader samples one shared stencil factor",
          source.count("texture(stencil_tex") == 1
          and "f *= stencil_factor" in source)
    check("every MRT output uses the same modulated falloff",
          all(("paint_flags.y * f" in line) for line in source.splitlines()
              if "fragColor" in line and "=" in line))
    info = gpu_engine.dab_shader_create_info(4)
    check("shader create-info exposes stencil sampler and transform",
          info is not None)
    preview_info = gpu_engine.stencil_preview_shader_create_info()
    check("POST_PIXEL stencil preview has a resident texture contract",
          preview_info is not None
          and "texture(stencil_preview_tex" in
          gpu_engine.STENCIL_PREVIEW_FRAG_SRC)
    planar_quad = gpu_engine.stencil_preview_quad(
        (800, 600), None, 40.0, {
            "stencil_enabled": True, "stencil_image_name": mask.name,
            "stencil_projection": 'VIEW_STENCIL',
            "stencil_position": (0.5, 0.5), "stencil_scale": (0.5, 0.25),
            "stencil_rotation": 0.0})
    check("planar preview uses the exact normalized stencil footprint",
          planar_quad == ((200.0, 225.0), (600.0, 225.0),
                          (600.0, 375.0), (200.0, 375.0)))
    handle_settings = stencil.normalized(
        True, "Mask", 'VIEW_STENCIL', position=(0.5, 0.5),
        scale=(0.5, 0.25), rotation=0.0)
    handles = dict(stencil.planar_handle_points((800, 600), handle_settings))
    check("planar handles sit on the preview corners and top rotate knob",
          handles["scale_bl"] == (200.0, 225.0)
          and handles["scale_tr"] == (600.0, 375.0)
          and abs(handles["rotate"][0] - 400.0) < 1e-6
          and abs(handles["rotate"][1] - (375.0 + stencil.ROTATE_OFFSET_PX))
          < 1e-6)
    check("corner handle hit-tests in pixel space",
          stencil.hit_planar_handle((200.0, 225.0), (800, 600),
                                    handle_settings) == "scale_bl")
    check("interior of the stencil is not a handle",
          stencil.hit_planar_handle((400.0, 300.0), (800, 600),
                                    handle_settings) is None)
    scaled, rotation = stencil.apply_planar_handle_drag(
        "scale_tr", (700.0, 400.0), (800, 600), handle_settings)
    check("corner drag writes normalized viewport scale",
          abs(scaled[0] - 0.75) < 1e-6 and abs(scaled[1] - 1.0 / 3.0) < 1e-6
          and abs(rotation) < 1e-9, repr((scaled, rotation)))
    uniform, _rot = stencil.apply_planar_handle_drag(
        "scale_tr", (700.0, 400.0), (800, 600), handle_settings,
        preserve_aspect=True)
    start_aspect = handle_settings.scale[0] / handle_settings.scale[1]
    check("shift-scale preserves aspect",
          abs(uniform[0] / uniform[1] - start_aspect) < 1e-6, repr(uniform))
    _scale, rest = stencil.apply_planar_handle_drag(
        "rotate", handles["rotate"], (800, 600), handle_settings)
    check("rotate handle at rest keeps current rotation",
          abs(rest) < 1e-6, repr(rest))
    _scale, turned = stencil.apply_planar_handle_drag(
        "rotate", (500.0, 300.0), (800, 600), handle_settings)
    check("rotate handle follows the view-plane cursor",
          abs(turned + math.pi * 0.5) < 1e-6, repr(turned))
    brush_handles = stencil.planar_handle_points(
        (800, 600), stencil.normalized(True, "Tip", 'BRUSH_ALPHA'))
    check("brush-footprint stencils have no view-plane handles",
          brush_handles == ())
    position, scale, rotation = stencil.default_planar_transform()
    check("planar reset restores RNA placement defaults",
          position == (0.5, 0.5) and scale == (0.35, 0.35)
          and abs(rotation) < 1e-12)
    brush_quad = gpu_engine.stencil_preview_quad(
        (800, 600), (100.0, 120.0), 20.0, {
            "stencil_enabled": True, "stencil_image_name": mask.name,
            "stencil_projection": 'BRUSH_ALPHA',
            "stencil_scale": (1.0, 0.5), "stencil_rotation": 0.0})
    check("brush preview follows the cursor and exact dab-scaled footprint",
          brush_quad == ((80.0, 110.0), (120.0, 110.0),
                         (120.0, 130.0), (80.0, 130.0)))
    profile_source = gpu_engine.dab_frag_src(
        2, profile_slots=(False, True))
    check("normal-profile shader uses resolution-independent derivatives",
          "textureSize(stencil_tex, 0)" in profile_source
          and "profile_texel" in profile_source
          and "profile_size * dab_params.profile_flags.y" in profile_source
          and "profile_aspect" not in profile_source)
    check("normal-profile output composes with configured normal",
          "compose_profile_normal" in profile_source
          and "profile_normal" in profile_source)
    check("normal-profile mode preserves non-Normal MRT outputs",
          "fragColor = profile_mode ? vec4(0.0)" not in profile_source
          and "fragColor = vec4(dab_params.brush_values[0].rgb" in
          profile_source)
    check("normal relief separates profile opacity from material coverage",
          "float profile_f = f * profile_factor" in profile_source
          and "mask_value *" in profile_source
          and "* profile_f" in profile_source)
    check("paint coverage and normal relief are independent shader effects",
          "stencil_flags.w > 0.5" in profile_source
          and layer.brush_stencil_coverage
          and layer.brush_stencil_normal_relief)
    layer.brush_stencil_coverage = False
    check("normal relief remains enabled without paint coverage",
          layer.brush_stencil_normal_relief
          and not ops.gpu_stencil_settings(layer).coverage)
    layer.brush_stencil_coverage = True
    additive_profile_source = gpu_engine.dab_frag_src(
        2, additive=True, profile_slots=(False, True))
    check("normal relief preserves additive non-Normal channel output",
          "brush_values[0].rgb *" in additive_profile_source
          and "paint_flags.y * f" in additive_profile_source)

    targets = ops.gpu_paint_targets(layer)
    keys = tuple(key for key, _image in targets)
    images = [image for _key, image in targets]
    layer.paint_metallic = 0.23
    layer.paint_roughness = 0.81
    payloads = gpu_engine.stroke_payloads(keys, ops._gpu_brush(layer))
    payload_by_key = dict(zip(keys, payloads))
    check("combined stencil keeps configured scalar channel values",
          all(abs(value - 0.23) < 1e-6 for value in
              payload_by_key["metallic"]["value"])
          and all(abs(value - 0.81) < 1e-6 for value in
                  payload_by_key["roughness"]["value"]))
    import numpy as np
    packed_values = [tuple(payload["value"]) +
                     (float(payload["strength"]),)
                     for payload in payloads]
    packed = gpu_engine.dab_uniform_data(
        np.identity(4), np.identity(4), (0.0, 0.0, 1.0, 0.0),
        (800, 600), (400, 300), 40.0, 0.5, 1e-4, 1e-4, True,
        1.0, True, True, True, 0.6, (0.5, 0.5), (1.0, 1.0), 0.0,
        packed_values, profile_usage=True)
    metallic_slot = keys.index("metallic")
    roughness_slot = keys.index("roughness")
    check("combined stencil packs scalar values into their MRT slots",
          abs(float(packed[gpu_engine.DAB_UBO_BRUSH_VALUES
                           + metallic_slot, 0]) - 0.23) < 1e-6
          and abs(float(packed[gpu_engine.DAB_UBO_BRUSH_VALUES
                               + roughness_slot, 0]) - 0.81) < 1e-6)
    runtime = {"channel_keys": keys, "radius": 40.0, "hardness": 0.5}
    runtime.update(gpu_settings)
    check("resident stencil session starts without image readback",
          gpu_engine.start_session(obj, images, None, payloads=payloads,
                                   settings=runtime))
    gpu_engine.begin_stroke(10.0, 10.0, 1.0)
    gpu_engine.end_stroke()
    check("stenciled pen-up queues no CPU image synchronization",
          gpu_engine.take_pending_pixels() is None)
    changed = stencil.normalized(True, mask.name, 'VIEW_STENCIL',
                                 'ALPHA', 0.25, (0.6, 0.4), (0.2, 0.3), 0.1)
    check("stencil transform refresh preserves resident session",
          gpu_engine.update_stroke_settings(
              payloads, stencil_settings=changed.as_gpu_settings())
          and gpu_engine.session_active()
          and gpu_engine.take_pending_pixels() is None)
    _payloads, refreshed = gpu_engine.stroke_settings_snapshot()
    check("refreshed stencil state is visible to next stroke",
          refreshed['stencil_projection'] == 'VIEW_STENCIL'
          and refreshed['stencil_position'] == (0.6, 0.4)
          and refreshed['stencil_opacity'] == 0.25)
    disabled = stencil.normalized(
        False, mask.name, 'VIEW_STENCIL', 'ALPHA', 0.25,
        (0.6, 0.4), (0.2, 0.3), 0.1)
    check("stencil toggle refresh does not restart resident session",
          gpu_engine.update_stencil_settings(disabled.as_gpu_settings())
          and gpu_engine.session_active()
          and gpu_engine.take_pending_pixels() is None)
    _payloads, refreshed = gpu_engine.stroke_settings_snapshot()
    check("live stencil toggle immediately hides its overlay contract",
          not refreshed['stencil_enabled']
          and gpu_engine.stencil_preview_quad(
              (800, 600), (400, 300), 40.0, refreshed) is None)
    reenabled = stencil.normalized(
        True, mask.name, 'BRUSH_ALPHA', 'LUMINANCE', 0.75,
        (0.25, 0.75), (1.2, 0.8), 0.4)
    check("live stencil settings refresh as one atomic contract",
          gpu_engine.update_stencil_settings(reenabled.as_gpu_settings()))
    _payloads, refreshed = gpu_engine.stroke_settings_snapshot()
    check("live stencil projection and transform reach the overlay",
          refreshed['stencil_enabled']
          and refreshed['stencil_projection'] == 'BRUSH_ALPHA'
          and refreshed['stencil_interpretation'] == 'LUMINANCE'
          and refreshed['stencil_scale'] == (1.2, 0.8)
          and gpu_engine.stencil_preview_quad(
              (800, 600), (400, 300), 40.0, refreshed) is not None)
    gpu_engine.stop_session()

    impasto.unregister()
    print("IMPASTO_STENCIL_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_STENCIL_FAILED")
