# SPDX-License-Identifier: GPL-2.0-or-later
"""GPU multi-channel paint path — everything testable headless.

GPU object creation raises SystemError in --background (probed on
5.1.2), so this suite exercises what surrounds the draw callback: MRT
shader-source generation per channel count, stroke payload planning,
blend-batch grouping, brush/dab/dirty-rect math, the harmless headless
session round-trip, and operator registration/poll. Real strokes are
GUI-checklist territory (README).
"""

import sys
import traceback
from pathlib import Path
from unittest import mock

import bpy
import numpy as np

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import gpu_engine, model, ops, paint, props
from impasto.gpu import brush_math, caliper, overlays


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


check("GPU brush math has a focused implementation module",
      gpu_engine.brush_falloff is brush_math.brush_falloff
      and gpu_engine.sanitize_pressure is brush_math.sanitize_pressure
      and gpu_engine.overlap_compensated_opacity is
      brush_math.overlap_compensated_opacity)
check("GPU caliper keeps its legacy public engine import",
      gpu_engine.sss_caliper_layout is caliper.sss_caliper_layout)
stencil_settings = {
    "stencil_enabled": True, "stencil_image_name": "test",
    "stencil_projection": "BRUSH_ALPHA", "stencil_scale": (1.0, 0.5),
}
check("GPU stencil layout is delegated to the overlay module",
      gpu_engine.stencil_preview_quad(
          (640, 480), (20, 30), 10, stencil_settings)
      == overlays.stencil_preview_quad(
          (640, 480), (20, 30), 10, stencil_settings))
check("GPU overlay scene length formatting remains compatible",
      gpu_engine._format_scene_length(0.002) == "2 mm"
      and overlays.format_scene_length(0.002) == "2 mm")

hover = gpu_engine.HoverTelemetry()
hover.add("view", 4.0)
hover.add("view", 8.0)
hover.add("preview", 3.0)
hover.add("prepass", 10.0)
hover.add("not_a_stage", 1000.0)
hover.geometry(400, 25)
hover_summary = hover.summary()
check("passive hover telemetry aggregates bounded averages and maxima",
      hover_summary["frames"] == 2
      and hover_summary["view_avg_ms"] == 6.0
      and hover_summary["view_max_ms"] == 8.0
      and hover_summary["preview_count"] == 1
      and hover_summary["prepass_avg_ms"] == 10.0)
check("passive hover telemetry records projection health",
      hover_summary["triangles"] == 400
      and hover_summary["unprojectable"] == 25
      and hover_summary["unprojectable_pct"] == 6.25)
hover_line = gpu_engine.format_hover_telemetry(hover_summary)
check("passive hover summary has a stable machine-readable format",
      hover_line.startswith("GPU_PAINT_SPIKE_HOVER ")
      and "frames=2" in hover_line
      and "preview_avg_ms=3.0000" in hover_line
      and "unprojectable_pct=6.2500" in hover_line)
empty_hover = gpu_engine.HoverTelemetry().summary()
check("empty passive hover telemetry avoids division by zero",
      empty_hover["frames"] == 0
      and empty_hover["view_avg_ms"] == 0.0
      and empty_hover["unprojectable_pct"] == 0.0)
stop_line = gpu_engine.format_stop_telemetry({
    "handlers_ms": 1.0, "history_ms": 2.0, "gpu_release_ms": 3.0,
    "total_ms": 6.0,
})
check("shutdown telemetry is bounded and machine-readable",
      stop_line.startswith("GPU_PAINT_SPIKE_STOP ")
      and "handlers_ms=1.0000" in stop_line
      and "history_ms=2.0000" in stop_line
      and "operator_finish_ms=0.0000" in stop_line
      and len(stop_line.split()) == 9)

# Capability probes are expensive GPU-context work.  Their cache is strictly
# process-local and keyed by the complete backend identity; a cache hit must
# also restore the readback strategy globals consumed by stroke finalization.
saved_probe_cache = dict(gpu_engine._capability_probe_cache)
try:
    gpu_engine._capability_probe_cache.clear()
    probe_calls = []

    def fake_probe():
        probe_calls.append(True)
        gpu_engine._buffer_numpy_path = "memoryview"
        gpu_engine._read_into_numpy = True
        return ["backend=TEST vendor=Vendor renderer=Renderer",
                "feature=yes"]

    with mock.patch.object(gpu_engine, "_gpu_backend_identity",
                           return_value=(1, "TEST", "Vendor", "Renderer")), \
            mock.patch.object(gpu_engine, "_probe_capabilities",
                              side_effect=fake_probe):
        first_lines, first_source = gpu_engine._cached_probe_capabilities()
        gpu_engine._buffer_numpy_path = "to_list_fallback"
        gpu_engine._read_into_numpy = False
        cached_lines, cached_source = gpu_engine._cached_probe_capabilities()
    check("GPU capability probe is cached per backend identity",
          len(probe_calls) == 1 and first_source == "runtime"
          and cached_source == "cache" and cached_lines == first_lines)
    check("cached GPU probe restores readback strategy latches",
          gpu_engine._buffer_numpy_path == "memoryview"
          and gpu_engine._read_into_numpy is True)

    with mock.patch.object(gpu_engine, "_gpu_backend_identity",
                           return_value=None), \
            mock.patch.object(gpu_engine, "_probe_capabilities",
                              side_effect=fake_probe):
        _lines, unknown_source = gpu_engine._cached_probe_capabilities()
    check("unknown GPU identity deliberately bypasses capability cache",
          unknown_source == "runtime" and len(probe_calls) == 2)
finally:
    gpu_engine._capability_probe_cache.clear()
    gpu_engine._capability_probe_cache.update(saved_probe_cache)

positions, remainder = brush_math.interpolate_dabs(
    0.0, 0.0, 100.0, 0.0, 10.0, max_dabs=3)
check("extracted dab interpolation retains its safety cap",
      len(positions) == 3 and remainder == 70.0)


for target in (0.2, 0.8):
    spacing = 0.1
    per_dab = gpu_engine.overlap_compensated_opacity(target, spacing)
    accumulated = 1.0 - (1.0 - per_dab) ** round(1.0 / spacing)
    check("pressure opacity survives dense dab overlap at %.1f" % target,
          abs(accumulated - target) < 1e-6, repr(accumulated))

erase_src = gpu_engine.dab_frag_src(2)
check("eraser shader scales complete resident RGBA coverage",
      erase_src.count("profile_flags.w > 0.5") == 2
      and "brush_values[0].a" in erase_src
      and "brush_values[1].a" in erase_src
      and "dab_params.paint_flags.y * f" in erase_src)
soften_src = gpu_engine.soften_frag_src()
check("soften shader uses a separate resident source and 3x3 kernel",
      "textureSize(source_tex, 0)" in soften_src
      and soften_src.count("texture(source_tex") == 10
      and "paint_flags.y * f" in soften_src
      and gpu_engine.soften_shader_create_info() is not None)
smear_src = gpu_engine.smear_frag_src()
check("smear shader transports resident pixels from a directional offset",
      "paintUV + dab_params.profile_flags.zw" in smear_src
      and "mix(current, carried, smear_strength)" in smear_src
      and gpu_engine.smear_shader_create_info() is not None)

effective, ring_px, percentages, too_small = gpu_engine.sss_caliper_layout(
    0.01, (1.0, 0.5, 0.25), 100.0, 2.0)
check("SSS caliper uses Scale times RGB Radius",
      effective == (0.01, 0.005, 0.0025))
check("SSS caliper projects literal distances without magnification",
      ring_px == (1.0, 0.5, 0.25) and not too_small)
check("SSS caliper reports mesh-relative effective distances",
      percentages == (0.5, 0.25, 0.125))
effective, ring_px, _percentages, too_small = gpu_engine.sss_caliper_layout(
    0.5, (1.0, 0.2, 0.1), 100.0, 10.0)
check("large SSS calipers remain literal",
      ring_px == (50.0, 10.0, 5.0) and not too_small)
_effective, zoomed_px, _percentages, zoomed_too_small = \
    gpu_engine.sss_caliper_layout(
        0.01, (1.0, 0.5, 0.25), 25.0, 2.0)
check("zoom changes ring pixels continuously without changing magnification",
      zoomed_px == (0.25, 0.125, 0.0625)
      and not zoomed_too_small)
_effective, tiny_px, _percentages, tiny_warning = \
    gpu_engine.sss_caliper_layout(
        0.0001, (1.0, 0.5, 0.25), 100.0, 2.0)
check("tiny mesh-relative SSS distances warn without scaling",
      tiny_px == (0.01, 0.005, 0.0025) and tiny_warning)


try:
    impasto.register()

    brush_modes = props.ImpastoLayer.bl_rna.properties[
        "brush_mode"].enum_items
    check("GPU brush exposes Paint, Soften, Smear, and Erase modes",
          tuple(item.identifier for item in brush_modes)
          == ('PAINT', 'SOFTEN', 'SMEAR', 'ERASE'))
    erase_tree = bpy.data.node_groups.new("Impasto Erase Defaults",
                                          "ShaderNodeTree")
    layer = erase_tree.impasto.layers.add()
    check("every GPU brush defaults to targeting every material channel",
          all(len(getattr(layer, name)) == len(model.CHANNELS)
                  and all(getattr(layer, name))
              for name in ("paint_channels", "soften_channels",
                           "smear_channels", "erase_channels")))

    # ---- registry contract behind stroke_payloads --------------------
    keys = gpu_engine.GPU_PAINT_CHANNEL_KEYS
    check("paintable keys all exist in the registry",
          all(k in model.CHANNEL_MAP for k in keys))
    check("paintable keys are in registry order",
          list(keys) == sorted(keys, key=lambda k: model.CHANNEL_ORDER[k]))
    check("only display colors use sRGB paint canvases",
          {k for k in keys if model.CHANNEL_MAP[k].colorspace == "sRGB"}
          == {"base_color", "emission_color"})

    # ---- MRT fragment source generation -------------------------------
    one = gpu_engine.dab_frag_src(1)
    four = gpu_engine.dab_frag_src(4)
    check("N=1 source assigns exactly the baseline output",
          "fragColor =" in one and "fragColor1" not in one)
    check("N=4 source assigns four distinct outputs",
          all(("fragColor%d =" % i) in four for i in (1, 2, 3))
          and all(("brush_values[%d]" % i) in four for i in range(4)))
    additive = gpu_engine.dab_frag_src(2, additive=True)
    check("additive variant premultiplies the signed payload",
          "brush_values[0].rgb" in additive
          and "brush_values[0].a" in additive
          and "brush_values[1].rgb" in additive)
    check("alpha-blend variant deposits the raw payload color",
          "vec4(dab_params.brush_values[0].rgb," in one)
    try:
        gpu_engine.dab_frag_src(gpu_engine.MAX_CHANNELS + 1)
        check("channel-count ceiling enforced", False)
    except ValueError:
        check("channel-count ceiling enforced", True)
    info = gpu_engine.dab_shader_create_info(4)
    check("create-info population works headless (pure bookkeeping)",
          info is not None)
    check("dab shader uses a std140-friendly UBO contract",
          "struct ImpastoDabParams" in gpu_engine.DAB_UBO_TYPEDEF
          and "dab_params.model_matrix" in gpu_engine.DAB_VERT_SRC
          and "dab_params.view_proj_matrix" in
          gpu_engine._DAB_FRAG_PRELUDE)
    matrix = ((1.0, 2.0, 3.0, 4.0),
              (5.0, 6.0, 7.0, 8.0),
              (9.0, 10.0, 11.0, 12.0),
              (13.0, 14.0, 15.0, 16.0))
    packed = gpu_engine.dab_uniform_data(
        matrix, matrix, (0.0, 0.0, 1.0, 2.0), (800, 600), (20, 30),
        40, 0.5, 1e-4, 2e-5, True, 0.75, True, True, False, 0.6,
        (0.25, 0.75), (1.2, 0.8), 0.4,
        ((0.1, 0.2, 0.3, 0.9),))
    check("dab UBO pack is contiguous vec4 std140 data",
          packed.shape == (gpu_engine.DAB_UBO_VEC4_COUNT, 4)
          and packed.dtype.name == "float32"
          and packed.flags.c_contiguous)
    check("dab UBO pack transposes matrices to GLSL column-major order",
          tuple(packed[gpu_engine.DAB_UBO_MODEL]) == (1.0, 5.0, 9.0, 13.0))
    check("dab UBO pack preserves dynamic and MRT values",
          tuple(packed[gpu_engine.DAB_UBO_REGION_CENTER]) ==
          (800.0, 600.0, 20.0, 30.0)
          and abs(packed[gpu_engine.DAB_UBO_PAINT_FLAGS, 1] - 0.75) < 1e-6
          and all(abs(float(a) - b) < 1e-6 for a, b in zip(
              packed[gpu_engine.DAB_UBO_BRUSH_VALUES],
              (0.1, 0.2, 0.3, 0.9))))
    preview_records = {
        "base_color": {
            "has": 1.0, "active": 1.0, "active_factor": 0.75,
            "active_blend": 3,
            "baseline_value": (0.1, 0.2, 0.3, 1.0),
            "baseline_is_texture": 1.0,
            "upper_c": (0.5, 0.5, 0.5, 0.5),
            "upper_d": (0.2, 0.2, 0.2, 0.2),
            "upper_present": 1.0, "upper_factor": 0.4,
            "upper_blend": 2,
        },
        "roughness": {
            "has": 1.0, "active_factor": 0.25,
            "baseline_value": (0.6, 0.6, 0.6, 1.0),
        },
    }
    preview_packed = gpu_engine.pack_preview_ubo(preview_records)
    base_offset = (gpu_engine.PREVIEW_UBO_CHANNEL_BASE
                   + gpu_engine.GPU_PAINT_CHANNEL_KEYS.index("base_color")
                   * gpu_engine.PREVIEW_UBO_STRIDE)
    rough_offset = (gpu_engine.PREVIEW_UBO_CHANNEL_BASE
                    + gpu_engine.GPU_PAINT_CHANNEL_KEYS.index("roughness")
                    * gpu_engine.PREVIEW_UBO_STRIDE)
    check("preview UBO pack is contiguous std140 vec4 data",
          preview_packed.shape == (gpu_engine.PREVIEW_UBO_VEC4_COUNT, 4)
          and preview_packed.dtype.name == "float32"
          and preview_packed.flags.c_contiguous)
    check("preview UBO preserves active, baseline and upper stack values",
          all(abs(float(a) - b) < 1e-6 for a, b in zip(
              preview_packed[base_offset], (1.0, 1.0, 0.75, 3.0)))
          and all(abs(float(a) - b) < 1e-6 for a, b in zip(
              preview_packed[base_offset + 1], (0.1, 0.2, 0.3, 1.0)))
          and preview_packed[base_offset + 2, 0] == 1.0
          and all(abs(float(a) - b) < 1e-6 for a, b in zip(
              preview_packed[base_offset + 3], (0.5,) * 4))
          and all(abs(float(a) - b) < 1e-6 for a, b in zip(
              preview_packed[base_offset + 4], (0.2,) * 4))
          and all(abs(float(a) - b) < 1e-6 for a, b in zip(
              preview_packed[base_offset + 5], (1.0, 0.4, 2.0, 0.0)))
          and preview_packed[rough_offset, 2] == 0.25)
    check("missing preview records retain safe neutral upper transform",
          all(preview_packed[
              gpu_engine.PREVIEW_UBO_CHANNEL_BASE
              + gpu_engine.GPU_PAINT_CHANNEL_KEYS.index("metallic")
              * gpu_engine.PREVIEW_UBO_STRIDE + 3] == 1.0)
          and not any(preview_packed[
              gpu_engine.PREVIEW_UBO_CHANNEL_BASE
              + gpu_engine.GPU_PAINT_CHANNEL_KEYS.index("metallic")
              * gpu_engine.PREVIEW_UBO_STRIDE + 4]))
    preview_info = gpu_engine.preview_shader_create_info()
    preview_pack = gpu_engine.pack_preview_ubo({
        "roughness": {
            "has": 1.0, "active": 1.0, "active_factor": 0.75,
            "active_blend": 4, "baseline_value": (0.2, 0.2, 0.2, 1.0),
            "baseline_is_texture": 1.0,
            "upper_c": (0.5, 0.5, 0.5, 1.0),
            "upper_d": (0.1, 0.1, 0.1, 0.0),
            "upper_present": 1.0, "upper_factor": 0.6,
            "upper_blend": 3,
        },
    })
    roughness_slot = (
        gpu_engine.PREVIEW_UBO_CHANNEL_BASE
        + gpu_engine.GPU_PAINT_CHANNEL_KEYS.index("roughness")
        * gpu_engine.PREVIEW_UBO_STRIDE)
    check("preview UBO is a compact std140 vec4 array",
          preview_pack.shape == (gpu_engine.PREVIEW_UBO_VEC4_COUNT, 4)
          and preview_pack.dtype.name == "float32"
          and preview_pack.flags.c_contiguous
          and preview_pack.nbytes
          == gpu_engine.PREVIEW_UBO_VEC4_COUNT * 16)
    check("preview UBO float flags preserve shader-side integer fields",
          tuple(preview_pack[roughness_slot]) == (1.0, 1.0, 0.75, 4.0)
          and tuple(preview_pack[roughness_slot + 5]) ==
          (1.0, 0.6000000238418579, 3.0, 0.0))
    check("composed preview create-info covers every PBR paint channel",
          preview_info is not None
          and all((key + "_tex") in gpu_engine.PREVIEW_FRAG_SRC
                  for key in gpu_engine.GPU_PAINT_CHANNEL_KEYS))
    check("live preview is composed rather than raw Base Color",
          "metallic" in gpu_engine.PREVIEW_FRAG_SRC
          and "roughness" in gpu_engine.PREVIEW_FRAG_SRC
          and "normal_sample" in gpu_engine.PREVIEW_FRAG_SRC
          and "height" in gpu_engine.PREVIEW_FRAG_SRC)
    check("Lit preview has roughness-sensitive studio keys",
          "preview_key_light" in gpu_engine.PREVIEW_FRAG_SRC
          and "roughness * roughness" in gpu_engine.PREVIEW_FRAG_SRC)
    check("preview display mode identifiers are stable",
          gpu_engine.PREVIEW_MODES == (
              "LIT_PBR", "RAW_TANGENT_NORMAL",
              "NEUTRAL_NORMAL_LIGHTING", "HEIGHT_GRAYSCALE"))
    check("preview shader has explicit diagnostic branches",
          "preview_mode == 1" in gpu_engine.PREVIEW_FRAG_SRC
          and "normal_sample.rgb" in gpu_engine.PREVIEW_FRAG_SRC
          and "preview_mode == 2" in gpu_engine.PREVIEW_FRAG_SRC
          and "preview_mode == 3" in gpu_engine.PREVIEW_FRAG_SRC)
    check("unknown preview mode safely normalizes to Lit PBR",
          gpu_engine.normalize_preview_mode("not-a-mode") == "LIT_PBR"
          and gpu_engine.preview_mode_index("HEIGHT_GRAYSCALE") == 3)

    # ---- payload planning ---------------------------------------------
    brush = {"color": (0.5, 0.25, 1.0), "roughness": 0.7,
             "metallic": 0.2, "normal": (0.5, 0.5, 1.0),
             "height_strength": 0.05, "height_direction": "RAISE"}
    payloads = gpu_engine.stroke_payloads(keys, brush)
    by_key = dict(zip(keys, payloads))
    srgb = gpu_engine.linear_to_srgb
    check("linear_to_srgb endpoints exact",
          srgb(0.0) == 0.0 and abs(srgb(1.0) - 1.0) < 1e-9)
    check("linear_to_srgb midpoint matches IEC 61966-2-1",
          abs(srgb(0.5) - 0.7353569) < 1e-4, repr(srgb(0.5)))
    check("base color payload is sRGB-encoded",
          all(abs(v - srgb(c)) < 1e-9 for v, c in
              zip(by_key["base_color"]["value"], brush["color"])))
    check("scalar payloads are raw grayscale triples",
          by_key["roughness"]["value"] == (0.7, 0.7, 0.7)
          and by_key["metallic"]["value"] == (0.2, 0.2, 0.2))
    check("normal payload passes encoded RGB through",
          by_key["normal"]["value"] == (0.5, 0.5, 1.0))
    check("only height is additive",
          by_key["height"]["blend"] == "ADD"
          and all(by_key[k]["blend"] == "MIX" for k in keys
                  if k != "height"))
    check("GPU occlusion compares linear view-space depth",
          "impasto_visible_surface" in gpu_engine._DAB_FRAG_PRELUDE
          and "viewDepth" in gpu_engine.PREPASS_FRAG_SRC
          and "clipPos.z / clipPos.w" not in
          gpu_engine.PREPASS_FRAG_SRC)
    check("raise deposits a positive height step",
          by_key["height"]["value"] == (0.05, 0.05, 0.05))
    lower = gpu_engine.stroke_payloads(
        ("height",), dict(brush, height_direction="LOWER"))[0]
    check("lower deposits a negative height step",
          lower["value"] == (-0.05, -0.05, -0.05)
          and lower["blend"] == "ADD")
    expanded_brush = dict(
        brush, emission_color=(0.25, 0.5, 1.0), emission_strength=8.0,
        sss_weight=0.7, sss_radius=(1.0, 0.25, 0.1), sss_scale=0.03)
    expanded = dict(zip(
        ("emission_color", "emission_strength", "sss_weight",
         "sss_radius", "sss_scale"),
        gpu_engine.stroke_payloads(
            ("emission_color", "emission_strength", "sss_weight",
             "sss_radius", "sss_scale"), expanded_brush)))
    check("emission color uses the same sRGB storage boundary as Base",
          expanded["emission_color"]["value"] == tuple(
              srgb(c) for c in expanded_brush["emission_color"]))
    check("HDR emission strength remains unclipped",
          expanded["emission_strength"]["value"] == (8.0, 8.0, 8.0))
    check("SSS factor/vector/distance remain raw Non-Color values",
          expanded["sss_weight"]["value"] == (0.7, 0.7, 0.7)
          and expanded["sss_radius"]["value"] == (1.0, 0.25, 0.1)
          and expanded["sss_scale"]["value"] == (0.03, 0.03, 0.03))
    try:
        gpu_engine.stroke_payloads(("sss_ior",), brush)
        check("specialized non-brush SSS channels are rejected", False)
    except ValueError:
        check("specialized non-brush SSS channels are rejected", True)

    # ---- straight <-> premultiplied canvas boundary conversions -------
    # gpu 'ALPHA' blending accumulates premultiplied; canvases are
    # straight alpha and the compiled chains mix VALUE by alpha, so both
    # boundaries must convert (the Material Preview scalar/normal
    # regression).
    import numpy as np
    straight = np.array([1.0, 0.5, 0.25, 0.5,     # half-covered texel
                         0.3, 0.6, 0.9, 0.0,      # uncovered (rgb junk)
                         0.9, 0.8, 0.7, 1.0],     # opaque texel
                        dtype=np.float32)
    pm = gpu_engine.premultiply_canvas(straight.copy())
    check("premultiply scales rgb by alpha and preserves alpha",
          np.allclose(pm.reshape(-1, 4)[:, :3],
                      [[0.5, 0.25, 0.125], [0.0, 0.0, 0.0],
                       [0.9, 0.8, 0.7]])
          and np.allclose(pm.reshape(-1, 4)[:, 3], [0.5, 0.0, 1.0]))
    rt = gpu_engine.unpremultiply_readback(pm)
    check("readback un-premultiply restores straight values "
          "(rgb zeroed where a=0)",
          np.allclose(rt.reshape(-1, 4),
                      [[1.0, 0.5, 0.25, 0.5], [0.0, 0.0, 0.0, 0.0],
                       [0.9, 0.8, 0.7, 1.0]], atol=1e-6))
    check("readback conversion copies (mirrors stay in fb space)",
          rt is not pm and np.allclose(pm.reshape(-1, 4)[0, :3],
                                       [0.5, 0.25, 0.125]))
    kiln_upload = gpu_engine.prepare_canvas_upload(
        np.array([0.7, 0.3, 1.0, 0.0], dtype=np.float32), opaque=True)
    check("authoritative zero-alpha normal survives active upload",
          np.allclose(kiln_upload, [0.7, 0.3, 1.0, 1.0]),
          repr(kiln_upload))
    # One source-over dab at coverage a onto a transparent canvas must
    # round-trip to (value, a) — NOT (value*a, a), which was the bug.
    dab_v, dab_a = 0.8, 0.5
    fb = np.zeros(4, dtype=np.float32)             # premult accumulator
    fb[:3] = dab_v * dab_a + fb[:3] * (1.0 - dab_a)
    fb[3] = dab_a + fb[3] * (1.0 - dab_a)
    synced = gpu_engine.unpremultiply_readback(fb).reshape(4)
    check("soft dab syncs back the painted value at its coverage",
          abs(synced[0] - dab_v) < 1e-6 and abs(synced[3] - dab_a) < 1e-6,
          str(synced.tolist()))

    # ---- blend-batch grouping (one blend mode per MRT draw) -----------
    mixed = [{"blend": "MIX"}, {"blend": "ADD"}, {"blend": "MIX"},
             {"blend": "MIX"}, {"blend": "MIX"}, {"blend": "MIX"}]
    batches = gpu_engine.plan_target_batches(mixed)
    check("equal-blend targets pack into framebuffer-sized batches",
          batches == (("MIX", (0, 2, 3, 4)), ("MIX", (5,)),
                      ("ADD", (1,))), str(batches))
    check("every target lands in exactly one batch",
          sorted(i for _b, idx in batches for i in idx)
          == list(range(len(mixed))))

    # ---- brush / dab / dirty-rect math ---------------------------------
    check("falloff is 1 inside the hardness core and 0 at the rim",
          gpu_engine.brush_falloff(0.3, 0.5) == 1.0
          and gpu_engine.brush_falloff(1.0, 0.5) == 0.0
          and 0.0 < gpu_engine.brush_falloff(0.75, 0.5) < 1.0)
    dabs, leftover = gpu_engine.interpolate_dabs(0.0, 0.0, 10.0, 0.0, 4.0)
    check("dab interpolation spaces evenly and carries leftover",
          [round(d[0], 6) for d in dabs] == [4.0, 8.0]
          and abs(leftover - 2.0) < 1e-9)
    check("tablet pressure rejects transient zero and invalid samples",
          gpu_engine.sanitize_pressure(0.0, 0.4) == 0.4
          and gpu_engine.sanitize_pressure(float("nan"), 0.6) == 0.6
          and gpu_engine.sanitize_pressure(2.0) == 1.0)
    rect = gpu_engine.dab_rect_union([(10.0, 20.0), (30.0, 5.0)], 4.0)
    check("dab union rect covers every disc",
          rect == (6.0, 1.0, 34.0, 24.0), str(rect))
    identity = np.eye(4, dtype=np.float32)
    projected, invalid = gpu_engine.triangle_screen_bboxes(
        [(-0.5, -0.5, 0.0), (0.5, -0.5, 0.0), (0.0, 0.5, 0.0)],
        identity, 100, 80)
    check("screen bounds project ordinary triangles with a guard pixel",
          np.allclose(projected[0], (24.0, 19.0, 76.0, 61.0))
          and not invalid[0], str((projected, invalid)))
    offscreen, invalid = gpu_engine.triangle_screen_bboxes(
        [(2.0, -0.2, 0.0), (3.0, -0.2, 0.0), (2.5, 0.2, 0.0)],
        identity, 100, 80)
    check("screen bounds reject fully offscreen triangles",
          np.isposinf(offscreen[0, :2]).all()
          and np.isneginf(offscreen[0, 2:]).all()
          and not invalid[0], str((offscreen, invalid)))

    # A simple perspective transform with clip.w = object z lets these pure
    # tests exercise camera-plane clipping without Blender context.
    perspective = np.eye(4, dtype=np.float32)
    perspective[3] = (0.0, 0.0, 1.0, 0.0)
    behind, invalid = gpu_engine.triangle_screen_bboxes(
        [(-0.2, -0.2, -1.0), (0.2, -0.2, -1.0),
         (0.0, 0.2, -1.0)], perspective, 100, 80)
    check("screen bounds reject triangles fully behind the camera",
          np.isposinf(behind[0, :2]).all()
          and np.isneginf(behind[0, 2:]).all()
          and not invalid[0], str((behind, invalid)))
    straddling, invalid = gpu_engine.triangle_screen_bboxes(
        [(-0.3, -0.2, 1.0), (0.3, -0.2, 1.0),
         (0.0, 0.2, -1.0)], perspective, 100, 80)
    check("camera-straddling triangles get conservative bounded extents",
          np.isfinite(straddling).all()
          and (straddling[0] >= (0.0, 0.0, 0.0, 0.0)).all()
          and (straddling[0] <= (100.0, 80.0, 100.0, 80.0)).all()
          and not invalid[0], str((straddling, invalid)))
    near_camera, invalid = gpu_engine.triangle_screen_bboxes(
        [(-1e-4, -1e-4, 1e-6), (1e-4, -1e-4, 1e-6),
         (0.0, 0.2, 1.0)], perspective, 100, 80)
    check("near-camera perspective expansion remains viewport bounded",
          np.isfinite(near_camera).all()
          and near_camera[0, 0] == 0.0 and near_camera[0, 2] == 100.0
          and not invalid[0], str((near_camera, invalid)))
    perspective_depth, invalid = gpu_engine.triangle_screen_bboxes(
        [(-0.5, 0.0, 1.0), (0.5, 0.0, 2.0),
         (0.0, 0.5, 4.0)], perspective, 200, 100)
    check("screen bounds honor perspective division for varying depth",
          np.allclose(perspective_depth[0], (49.0, 49.0, 126.0, 57.25))
          and not invalid[0], str((perspective_depth, invalid)))
    nonfinite, invalid = gpu_engine.triangle_screen_bboxes(
        [(float("nan"), 0.0, 1.0), (0.0, 0.0, 1.0),
         (0.0, 0.5, 1.0)], perspective, 100, 80)
    check("non-finite projection retains always-dirty fallback",
          invalid[0], str((nonfinite, invalid)))
    check("uv bbox to texel rect clamps and pads",
          gpu_engine.uv_bbox_to_pixel_rect((0.0, 0.0, 0.5, 0.25), 64,
                                           pad=2)
          == (0, 0, 34, 18))
    check("union_bbox tolerates None",
          gpu_engine.union_bbox(None, (0, 0, 1, 1)) == (0, 0, 1, 1)
          and gpu_engine.union_bbox((0, 0, 1, 1), None) == (0, 0, 1, 1))
    screen_boxes = np.array(
        [[8.0, 8.0, 24.0, 24.0], [200.0, 200.0, 220.0, 220.0]],
        dtype=np.float32)
    uv_boxes = np.array(
        [[0.25, 0.25, 0.5, 0.5], [0.75, 0.75, 1.0, 1.0]],
        dtype=np.float32)
    sparse = gpu_engine.dirty_uv_pixel_rects(
        screen_boxes, np.array([False, False]), uv_boxes,
        (0.0, 0.0, 230.0, 230.0), 64, pad=0)
    check("sparse undo bounds do not bridge scattered UV islands",
          sparse == ((16, 16, 16, 16), (48, 48, 16, 16)), str(sparse))
    sparse_near = gpu_engine.dirty_uv_pixel_rects(
        screen_boxes, np.array([False, False]), uv_boxes,
        (10.0, 10.0, 20.0, 20.0), 64, pad=0)
    check("sparse undo includes only screen-hit triangles",
          sparse_near == ((16, 16, 16, 16),), str(sparse_near))
    shared_hits = gpu_engine._screen_bbox_hit_indices(
        screen_boxes, np.array([False, True]), (10.0, 10.0, 20.0, 20.0))
    check("shared exact hit selection preserves always-dirty fallback",
          shared_hits.tolist() == [0, 1], str(shared_hits.tolist()))
    sparse_unprojectable = gpu_engine.dirty_uv_pixel_rects(
        screen_boxes, np.array([False, True]), uv_boxes,
        (10.0, 10.0, 20.0, 20.0), 64, pad=0)
    check("unprojectable triangles remain conservatively undoable",
          sparse_unprojectable
          == ((16, 16, 16, 16), (48, 48, 16, 16)),
          str(sparse_unprojectable))
    work = gpu_engine.dab_dirty_pixel_rects(
        screen_boxes, np.array([False, False]), uv_boxes,
        [(16.0, 16.0, 1.0), (100.0, 100.0, 1.0)], 4.0, 64,
        sample_pad=3)
    check("neighbour brushes bound each dab to padded UV work",
          work == ((11, 11, 26, 26), None), str(work))
    with mock.patch.object(gpu_engine, "dab_dirty_pixel_rects",
                           return_value=("detailed",)) as detailed:
        paint_work = gpu_engine.detailed_dab_work_rects(
            "PAINT", screen_boxes, np.array([False, False]), uv_boxes,
            [(16.0, 16.0, 1.0)], 4.0, 64)
        erase_work = gpu_engine.detailed_dab_work_rects(
            "ERASE", screen_boxes, np.array([False, False]), uv_boxes,
            [(16.0, 16.0, 1.0)], 4.0, 64)
        check("Paint and Erase skip unused per-dab UV work",
              paint_work is None and erase_work is None
              and detailed.call_count == 0)
        soften_work = gpu_engine.detailed_dab_work_rects(
            "SOFTEN", screen_boxes, np.array([False, False]), uv_boxes,
            [(16.0, 16.0, 1.0)], 4.0, 64)
        smear_work = gpu_engine.detailed_dab_work_rects(
            "SMEAR", screen_boxes, np.array([False, False]), uv_boxes,
            [(16.0, 16.0, 1.0)], 4.0, 64)
        pads = [call.args[-1] for call in detailed.call_args_list]
        check("Soften and Smear retain detailed per-dab UV work",
              soften_work == ("detailed",)
              and smear_work == ("detailed",)
              and pads == [1.0, 1.4], str(pads))
    engine_source = Path(gpu_engine.__file__).read_text(encoding="utf-8")
    prepass_source = engine_source[
        engine_source.index("def _update_prepass"):
        engine_source.index("# Dab dispatch")]
    check("viewport depth prepass never forces a navigation readback",
          ".read_color(" not in prepass_source)
    update_source = prepass_source[
        :prepass_source.index("def _ensure_projection_bounds")]
    check("viewport navigation defers triangle bounds until painting",
          "triangle_screen_bboxes(" not in update_source
          and "_ensure_projection_bounds(s, region)" in engine_source)
    check("soften and smear use bounded copies and scissored in-place draws",
          engine_source.count("gpu.state.scissor_set(*work_rect)") == 2
          and "s.soften_scratch_fb, work_rect" in engine_source
          and "s.paint_texs[index], s.soften_scratch = target, source"
          not in engine_source)
    ops_source = Path(ops.__file__).read_text(encoding="utf-8")
    sync_source = ops_source[
        ops_source.index("def _apply_pending_sync"):
        ops_source.index("def _perform_deferred_save")]
    check("GPU flush re-packs packed images through the shared write helper",
          "paint.write_flushed_image_pixels(image, arr)" in sync_source
          and "image.pack()" in Path(paint.__file__).read_text(encoding="utf-8"))

    packed = bpy.data.images.new("Impasto Packed Flush Test", 4, 4, alpha=True)
    red = np.array([1.0, 0.0, 0.0, 1.0] * 16, dtype=np.float32)
    green = np.array([0.0, 1.0, 0.0, 1.0] * 16, dtype=np.float32)
    packed.pixels.foreach_set(red)
    packed.pack()
    packed.pixels.foreach_set(green)
    packed.update()
    packed.reload()
    after_stale = np.empty(16 * 4, dtype=np.float32)
    packed.pixels.foreach_get(after_stale)
    check("reload restores stale packed bytes if flush did not re-pack",
          abs(float(after_stale[1]) - 0.0) < 1e-5
          and abs(float(after_stale[0]) - 1.0) < 1e-5)
    paint.write_flushed_image_pixels(packed, green)
    packed.reload()
    after_pack = np.empty(16 * 4, dtype=np.float32)
    packed.pixels.foreach_get(after_pack)
    check("re-pack after GPU flush survives image.reload()",
          packed.packed_file is not None
          and abs(float(after_pack[1]) - 1.0) < 1e-5
          and abs(float(after_pack[0]) - 0.0) < 1e-5)
    generated = bpy.data.images.new(
        "Impasto Generated Flush Test", 4, 4, alpha=True)
    generated.pixels.foreach_set(red)
    paint.write_flushed_image_pixels(generated, green)
    check("generated canvases stay unpacked after GPU flush",
          generated.packed_file is None)
    out = np.empty(16 * 4, dtype=np.float32)
    generated.pixels.foreach_get(out)
    check("generated canvases still receive flushed pixels",
          abs(float(out[1]) - 1.0) < 1e-5)

    # ---- headless session round-trip (harmless no-op contract) --------
    bpy.ops.mesh.primitive_plane_add(size=2.0)
    obj = bpy.context.object
    obj.data.uv_layers.new(name="UVMap")
    images = [bpy.data.images.new("Impasto GPU Test %d" % i, 64, 64,
                                  alpha=True) for i in range(2)]
    started = gpu_engine.start_session(
        obj, images, None,
        payloads=gpu_engine.stroke_payloads(("base_color", "height"),
                                            brush),
        settings={"radius": 40.0, "hardness": 0.5})
    check("headless session starts as a logical no-op", started)
    check("session reports active", gpu_engine.session_active())
    check("GPU session defaults to Lit PBR preview",
          gpu_engine.current_preview_mode() == "LIT_PBR")
    check("preview mode changes without session restart",
          gpu_engine.set_preview_mode("RAW_TANGENT_NORMAL")
          and gpu_engine.current_preview_mode() == "RAW_TANGENT_NORMAL")
    gpu_engine.set_cursor(21, 37)
    check("GPU reticle tracks viewport mouse coordinates",
          gpu_engine.cursor_position() == (21.0, 37.0))
    check("SSS caliper toggles update without restarting the session",
          gpu_engine.set_sss_caliper({"sss_caliper_enabled": True})
          and gpu_engine._session.settings["sss_caliper_enabled"] is True
          and gpu_engine.set_sss_caliper({"sss_caliper_enabled": False})
          and gpu_engine._session.settings["sss_caliper_enabled"] is False)
    refreshed_brush = dict(brush)
    refreshed_brush["color"] = (0.2, 0.4, 0.6)
    refreshed_brush["height_strength"] = 0.125
    refreshed = gpu_engine.stroke_payloads(
        ("base_color", "height"), refreshed_brush)
    check("GPU values refresh between strokes without restart",
          gpu_engine.update_stroke_settings(
              refreshed, radius=73.0, hardness=0.25,
              brush_mode='SOFTEN',
              brush_target_channel_keys=('height',),
              erase_channel_keys=('base_color',)))
    current_payloads, current_settings = \
        gpu_engine.stroke_settings_snapshot()
    check("GPU session uses refreshed payload, radius and hardness",
          current_payloads == refreshed
          and current_settings["radius"] == 73.0
          and current_settings["hardness"] == 0.25
          and current_settings["brush_mode"] == 'SOFTEN'
          and current_settings["brush_target_channel_keys"] == ('height',)
          and current_settings["erase_channel_keys"] == ('base_color',))
    received = []
    gpu_engine.add_stroke_listener(received.append)
    gpu_engine.begin_stroke(10.0, 10.0, 0.2, {"x_tilt": 0.1, "y_tilt": -0.2})
    gpu_engine.move_stroke(30.0, 10.0, 0.8, 40.0)
    queued_pressures = [dab[2] for dab in gpu_engine._session.dab_queue]
    check("tablet pressure interpolates across generated dabs",
          queued_pressures[0] == 0.2
          and queued_pressures[-1] == 0.8
          and queued_pressures == sorted(queued_pressures),
          repr(queued_pressures))
    check("stroke state tracks headlessly", gpu_engine.stroke_active())
    check("GPU input samples keep the raw pointer stream",
          gpu_engine._session.input_samples[0]["mouse"] == [10.0, 10.0]
          and gpu_engine._session.input_samples[-1]["mouse"] == [30.0, 10.0]
          and gpu_engine._session.input_samples[0]["is_start"] is True)
    gpu_engine.end_stroke()
    check("pen-up notifies GPU stroke listeners",
          len(received) == 1
          and received[0]["kind"] == gpu_engine.KIND_IMPASTO_GPU
          and received[0]["samples"][0]["pressure"] == 0.2
          and received[0]["samples"][0]["x_tilt"] == 0.1
          and received[0]["samples"][-1]["mouse"] == [30.0, 10.0],
          repr(received[:1]))
    gpu_engine.remove_stroke_listener(received.append)
    check("pen-up does not queue blocking Image synchronization",
          gpu_engine.take_pending_pixels() is None)
    check("explicit GPU flush can be queued independently of pen-up",
          gpu_engine.request_flush() and gpu_engine.busy())
    check("no error latched headlessly",
          gpu_engine.last_error() is None)
    gpu_engine.stop_session()
    check("session stops cleanly", not gpu_engine.session_active())

    # The stack plan must exist before lazy GPU allocation; otherwise the
    # first draw cannot build lower/Kiln baseline textures.
    active_model = model.LayerModel(
        uid="active", label="Detail", layer_type="PAINT", uv_map="UVMap",
        bindings=(model.BindingModel(
            key="normal", mode="SHARED", image_name=images[0].name),))
    kiln_model = model.LayerModel(
        uid="kiln", label="Kiln Baked Normal", layer_type="PAINT",
        uv_map="UVMap", bindings=(model.BindingModel(
            key="normal", mode="SHARED", image_name="Kiln Runtime Normal"),))
    resident_model = model.StackModel(
        root_tree_name="Runtime", channels=("normal",),
        layers=(active_model, kiln_model))
    check("resident stack session starts",
          gpu_engine.start_session(
              obj, [images[0]], None,
              payloads=gpu_engine.stroke_payloads(("normal",), brush),
              settings={"channel_keys": ("normal",),
                        "stack_model": resident_model,
                        "active_layer_uid": "active"}))
    check("lower/Kiln stack plan exists before first GPU draw",
          gpu_engine._session.stack_spec["enabled"]
          and gpu_engine._session.stack_spec["channels"]["normal"]
          ["lower_steps"][0]["source"]["image_name"]
          == "Kiln Runtime Normal",
          repr(gpu_engine._session.stack_spec))
    check("live base-normal image/UV edits mark preview resources dirty",
          gpu_engine.set_preview_base_normal({
              "base_normal_image_name": images[1].name,
              "base_normal_uv_map": "UVMap",
              "base_normal_strength": 0.75,
              "base_normal_invert_green": True})
          and gpu_engine._session.base_normal_resources_dirty)
    gpu_engine.stop_session()

    # Reported interactive regression: the active layer may own ordinary
    # material canvases even when this stroke targets only Emission.  A
    # same-UV visible layer above it must participate in Lit PBR without
    # changing resident ownership, write targets, or pausing input.
    interactive_keys = (
        "base_color", "roughness", "emission_color", "emission_strength")
    interactive_images = [
        bpy.data.images.new("Impasto Intermediate %s" % key, 64, 64,
                            alpha=True)
        for key in interactive_keys
    ]
    active_bindings = tuple(
        model.BindingModel(key=key, mode="SHARED",
                           image_name=image.name)
        for key, image in zip(interactive_keys, interactive_images))
    upper_roughness_image = bpy.data.images.new(
        "Impasto Upper Paint roughness", 64, 64, alpha=True)
    intermediate_active = model.LayerModel(
        uid="intermediate", label="Intermediate emission brush",
        layer_type="PAINT", uv_map="UVMap", bindings=active_bindings)
    visible_upper = model.LayerModel(
        uid="visible_upper", label="Visible upper Paint", layer_type="PAINT",
        uv_map="UVMap", opacity=0.5,
        bindings=(model.BindingModel(
            key="roughness", mode="SHARED",
            image_name=upper_roughness_image.name),))
    visible_lower = model.LayerModel(
        uid="visible_lower", label="Visible lower", layer_type="FILL",
        uv_map="UVMap",
        bindings=(
            model.BindingModel(
                key="base_color", mode="COLOR",
                color=(0.1, 0.2, 0.3, 1.0)),
            model.BindingModel(key="roughness", mode="VALUE", value=0.8),
        ))
    interactive_stack = model.StackModel(
        root_tree_name="Interactive intermediate",
        channels=interactive_keys,
        layers=(visible_upper, intermediate_active, visible_lower))
    emission_targets = ("emission_color", "emission_strength")
    check("intermediate full-stack resident session starts",
          gpu_engine.start_session(
              obj, interactive_images, None,
              payloads=gpu_engine.stroke_payloads(
                  interactive_keys, brush),
              settings={
                  "channel_keys": interactive_keys,
                  "brush_target_channel_keys": emission_targets,
                  "preview_mode": "LIT_PBR",
                  "stack_model": interactive_stack,
                  "active_layer_uid": "intermediate",
              }))
    interactive_spec = gpu_engine._session.stack_spec
    _payloads, interactive_settings = gpu_engine.stroke_settings_snapshot()
    check("overlapping same-UV upper layer remains resident Lit PBR",
          interactive_spec["enabled"]
          and gpu_engine.current_preview_mode() == "LIT_PBR"
          and not gpu_engine.material_inspect_active()
          and not gpu_engine.material_inspect_requested()
          and not gpu_engine.input_paused(),
          repr(interactive_spec))
    check("preview stack does not alter active ownership or write targets",
          all(interactive_spec["channels"][key]["active"] is not None
              for key in interactive_keys)
          and interactive_settings["brush_target_channel_keys"]
          == emission_targets)
    rough_channel_spec = interactive_spec["channels"]["roughness"]
    check("visible upper Paint Roughness follows resident active canvas",
          rough_channel_spec["upper_steps"] == [{
              "kind": "IMAGE",
              "image_name": upper_roughness_image.name,
              "uv_map": "UVMap",
              "factor": 0.5,
              "blend": "MIX",
              "use_alpha": True,
          }],
          repr(rough_channel_spec))
    gpu_engine.stop_session()

    # Full reported ordering: active Base canvas, B+Emission Fill, then a
    # farther upper BMR Paint layer. Preview reads every visible participant,
    # but the live stroke remains owned solely by active Base.
    ordered_active_image = bpy.data.images.new(
        "Impasto Ordered Active Base", 64, 64, alpha=True)
    ordered_upper_images = {
        key: bpy.data.images.new("Impasto Ordered Upper " + key, 64, 64,
                                 alpha=True)
        for key in ("base_color", "metallic", "roughness")
    }
    ordered_active = model.LayerModel(
        uid="ordered_active", layer_type="PAINT", uv_map="UVMap",
        bindings=(model.BindingModel(
            key="base_color", mode="SHARED",
            image_name=ordered_active_image.name),))
    ordered_b_emission = model.LayerModel(
        uid="ordered_b_emission", layer_type="FILL", uv_map="UVMap",
        opacity=0.5, bindings=(
            model.BindingModel(
                key="base_color", mode="COLOR",
                color=(0.4, 0.4, 0.4, 1.0)),
            model.BindingModel(
                key="emission_color", mode="COLOR",
                color=(1.0, 0.2, 0.1, 1.0)),
            model.BindingModel(
                key="emission_strength", mode="VALUE", value=3.0),
        ))
    ordered_upper_bmr = model.LayerModel(
        uid="ordered_upper_bmr", layer_type="PAINT", uv_map="UVMap",
        opacity=0.8, bindings=tuple(
            model.BindingModel(
                key=key, mode="SHARED", image_name=image.name)
            for key, image in ordered_upper_images.items()))
    ordered_model = model.StackModel(
        "Ordered interactive",
        ("base_color", "metallic", "roughness",
         "emission_color", "emission_strength"),
        (ordered_upper_bmr, ordered_b_emission, ordered_active))
    check("ordered upper-stack resident session starts",
          gpu_engine.start_session(
              obj, [ordered_active_image], None,
              payloads=gpu_engine.stroke_payloads(
                  ("base_color",), brush),
              settings={
                  "channel_keys": ("base_color",),
                  "brush_target_channel_keys": ("base_color",),
                  "preview_mode": "LIT_PBR",
                  "stack_model": ordered_model,
                  "active_layer_uid": "ordered_active",
              }))
    ordered_spec = gpu_engine._session.stack_spec
    _ordered_payloads, ordered_settings = \
        gpu_engine.stroke_settings_snapshot()
    check("B+Emission then upper BMR remain in resident Lit preview",
          ordered_spec["enabled"]
          and gpu_engine.current_preview_mode() == "LIT_PBR"
          and not gpu_engine.material_inspect_active()
          and not gpu_engine.input_paused(),
          repr(ordered_spec))
    check("ordered preview leaves writes on active Base only",
          ordered_settings["brush_target_channel_keys"] == ("base_color",)
          and ordered_spec["channels"]["base_color"]["active"] is not None
          and all(ordered_spec["channels"][key]["active"] is None
                  for key in ("metallic", "roughness",
                              "emission_color", "emission_strength")))
    check("Base upper stages preserve Fill-before-Paint order",
          ordered_spec["channels"]["base_color"]["upper_affine"][0][:3]
          == (0.5, 0.5, 0.5)
          and ordered_spec["channels"]["base_color"]["upper_affine"][1][:3]
          == (0.2, 0.2, 0.2)
          and ordered_spec["channels"]["base_color"]["upper_steps"][-1]
          ["image_name"] == ordered_upper_images["base_color"].name,
          repr(ordered_spec["channels"]["base_color"]))
    gpu_engine.stop_session()

    # ---- operator surface ----------------------------------------------
    check("gpu paint operator registered",
          getattr(bpy.types, "IMPASTO_OT_gpu_paint", None) is not None)
    check("explicit GPU flush operator registered",
          getattr(bpy.types, "IMPASTO_OT_gpu_flush", None) is not None)
    check("poll requires an Impasto paint layer",
          not bpy.ops.impasto.gpu_paint.poll())
    check("stack init", bpy.ops.impasto.stack_init(
        template="PRINCIPLED_STANDARD") == {"FINISHED"})
    check("paint layer add", bpy.ops.impasto.layer_add(
        layer_type="PAINT") == {"FINISHED"})
    check("bind height", bpy.ops.impasto.binding_add(
        channel_key="height") == {"FINISHED"})
    check("poll accepts the multi-channel paint layer",
          bpy.ops.impasto.gpu_paint.poll())
    layer = bpy.data.node_groups[
        obj.active_material.node_tree.nodes[
            model.n_material_stack()].node_tree.name].impasto.active_layer()
    check("operator target planning matches the layer's bindings",
          [key for key, _img in ops.gpu_paint_targets(layer)]
          == ["base_color", "height"])
    try:
        # Headless there is no window/event: Blender either refuses the
        # call outright (PASS_THROUGH + "Invalid operator call"), or the
        # invoke's own area guard cancels. Both are graceful declines.
        result = bpy.ops.impasto.gpu_paint('INVOKE_DEFAULT')
        check("headless invoke declines gracefully",
              result in ({'CANCELLED'}, {'PASS_THROUGH'}), str(result))
    except RuntimeError:
        # bpy.ops raises on {'ERROR'} reports — an equally graceful no.
        check("headless invoke declines gracefully", True)
    check("no session leaked by the declined invoke",
          not gpu_engine.session_active())

    # Reload/disable safety: an active session must not outlive the add-on.
    check("reload-safety session starts", gpu_engine.start_session(
        obj, images, None,
        payloads=gpu_engine.stroke_payloads(("base_color", "height"),
                                            brush),
        settings={"radius": 40.0, "hardness": 0.5}))
    check("reload-safety precondition", gpu_engine.session_active())
    impasto.unregister()
    check("unregister tears down the GPU session",
          not gpu_engine.session_active())
    print("IMPASTO_GPU_PAINT_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_GPU_PAINT_FAILED")
