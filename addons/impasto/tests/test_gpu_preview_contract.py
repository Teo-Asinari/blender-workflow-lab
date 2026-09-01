# SPDX-License-Identifier: GPL-2.0-or-later
"""Independent contract tests for the GPU-resident preview modes.

These are intentionally headless: they validate shader structure, mode-state
purity, and the numerical direction of diagnostic normal/height responses.
Actual framebuffer output remains a foreground acceptance test.
"""

import inspect
import math
import sys
from pathlib import Path

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto import gpu_engine


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


def normalize(v):
    length = math.sqrt(sum(x * x for x in v))
    return tuple(x / length for x in v)


def encoded_normal(rgb, alpha):
    neutral = (0.5, 0.5, 1.0)
    encoded = tuple(n + (c - n) * alpha for n, c in zip(neutral, rgb))
    return normalize(tuple(c * 2.0 - 1.0 for c in encoded))


def height_normal(dhdx, dhdy, scale=8.0):
    # Python mirror of cross((1,0,s*dhdx), (0,1,s*dhdy)).
    return normalize((-scale * dhdx, -scale * dhdy, 1.0))


expected = ("LIT_PBR", "RAW_TANGENT_NORMAL",
            "NEUTRAL_NORMAL_LIGHTING", "HEIGHT_GRAYSCALE")
check("preview mode identifiers and shader indices are stable",
      gpu_engine.PREVIEW_MODES == expected
      and [gpu_engine.preview_mode_index(m) for m in expected]
      == [0, 1, 2, 3])
check("invalid preview mode safely normalizes to Lit PBR",
      gpu_engine.normalize_preview_mode("unknown") == "LIT_PBR")

src = gpu_engine.PREVIEW_FRAG_SRC
main = src.split("void main()", 1)[1]
create_info_source = inspect.getsource(gpu_engine.preview_shader_create_info)
draw_preview_source = inspect.getsource(gpu_engine._draw_composed_preview)
check("large per-channel preview state uses one uniform block",
      "uniform_buf(PREVIEW_UBO_SLOT" in create_info_source
      and '"ImpastoPreviewParams"' in create_info_source
      and "typedef_source(PREVIEW_UBO_TYPEDEF)" in create_info_source
      and "for name in GPU_PAINT_CHANNEL_KEYS" not in create_info_source
      and "push_constant" not in create_info_source)
check("preview UBO layout covers every channel plus view depth",
      gpu_engine.PREVIEW_UBO_VEC4_COUNT
      == (gpu_engine.PREVIEW_UBO_CHANNEL_BASE
          + len(gpu_engine.GPU_PAINT_CHANNEL_KEYS)
          * gpu_engine.PREVIEW_UBO_STRIDE + 1)
      and gpu_engine.PREVIEW_UBO_VIEW_DEPTH
      == gpu_engine.PREVIEW_UBO_VEC4_COUNT - 1
      and "vec4 values[%d]" % gpu_engine.PREVIEW_UBO_VEC4_COUNT
      in gpu_engine.PREVIEW_UBO_TYPEDEF)
check("draw path updates and binds packed preview state",
      "preview_records = {}" in draw_preview_source
      and "pack_preview_ubo(" in draw_preview_source
      and "s.preview_ubo.update(s.preview_ubo_data)" in draw_preview_source
      and "shader.uniform_block(PREVIEW_UBO_NAME, s.preview_ubo)"
      in draw_preview_source
      and 'shader.uniform_float("has_" + key' not in draw_preview_source)
raw_normal_at = main.index("if (preview_mode == 1)")
raw_height_at = main.index("if (preview_mode == 3)")
detail_at = main.index("vec3 dpdx")
neutral_at = main.index("if (preview_mode == 2)")
pbr_at = main.index("vec3 v = normalize(camera_position - worldPos)")

check("diagnostics return before PBR work; Raw follows TBN composition",
      raw_height_at < detail_at < raw_normal_at < neutral_at)
check("neutral detail mode returns before microfacet lighting",
      detail_at < neutral_at < pbr_at)
check("all diagnostic channels use resolved lower-plus-active samples",
      "resolve_stack_channel" in main
      and "active_tangent_n * 0.5 + 0.5" in main
      and "float h = height_sample.r" in main)
check("base/scalar PBR channels reuse the resolved samples",
      "? base.rgb : vec3(0.5)" in main
      and "? metal_sample.r : 0.0" in main
      and "? rough_sample.r : 0.5" in main)
check("Lit PBR adds normal- and roughness-sensitive studio keys",
      "preview_key_light" in src
      and "distribution = a2" in src
      and "n, v, normalize(vec3" in main)
check("Lit PBR lighting is driven by packed live preview parameters",
      "#define preview_lighting preview_params.values[10]"
      in gpu_engine._preview_ubo_aliases()
      and "environment_intensity = exp2(preview_lighting.x)" in main
      and "rotate_around_z(reflection, preview_lighting.y)" in main
      and "preview_lighting.z" in main
      and "preview_fill.x" in main)
check("preview lighting updates do not rebuild or synchronize textures",
      "set_preview_lighting" in dir(gpu_engine)
      and "request_flush" not in
      inspect.getsource(gpu_engine.set_preview_lighting)
      and "environment_tex" not in
      inspect.getsource(gpu_engine.set_preview_lighting))

check("resident alpha gates the active layer exactly once",
      "active_factor * source.a" in src)
check("normal stack uses RNM before tangent-to-world decoding",
      "vec3 rnm_blend(" in src
      and "vec4 resolve_stack_normal(" in src
      and "vec3 encoded_n = normal_sample.rgb" in src
      and "active_normal_blend" not in src
      and "active_normal_blend" not in
      inspect.getsource(gpu_engine.preview_shader_create_info))
check("preview-only base normal composes beneath resolved paint in all modes",
      "texture(base_normal_tex, baseNormalUV)" in main
      and "base_world_n + (n - geometric_n)" in main
      and main.index("base_world_n + (n - geometric_n)") < raw_normal_at
      and 'sampler(21, \'FLOAT_2D\', "base_normal_tex")' in
      inspect.getsource(gpu_engine.preview_shader_create_info))
check("base normal path has independent UV, strength and green inversion",
      "baseNormalUV = base_uv" in gpu_engine.PREVIEW_VERT_SRC
      and "base_normal_options.x" in main
      and "base_normal_options.y" in main
      and "base_normal_image_name" in
      inspect.getsource(gpu_engine._ensure_base_normal_texture)
      and "base_uv_det" in main and "base_tangent" in main)
check("live image and UV changes rebuild only preview GPU resources",
      "base_normal_resources_dirty" in
      inspect.getsource(gpu_engine.set_preview_base_normal)
      and "build_uv_soup" in
      inspect.getsource(gpu_engine._refresh_base_normal_resources)
      and "batch_preview" in
      inspect.getsource(gpu_engine._refresh_base_normal_resources)
      and "request_flush" not in
      inspect.getsource(gpu_engine.set_preview_base_normal))
check("emission color and HDR strength remain independently resolved",
      "active_emission_color_blend, 1.0" in src
      and "active_emission_strength_blend, 0.0" in src
      and "rgb += emission_color * emission_strength" in src)
check("subsurface preview uses Weight and Radius-times-Scale distance",
      "vec3 scatter_distance = sss_radius * sss_scale" in src
      and "sss_weight * scatter_extent" in src
      and "sample_environment_panel(-environment_n, 0.0)" in src)
check("roughness readability adds light without remapping roughness",
      "preview_fill.y" in src
      and "original roughness in the same GGX light evaluation" in src
      and "roughness = mix(" not in src)
check("degenerate and mirrored UVs have explicit handling",
      "abs(uv_det) > 1e-8" in src and "orientation = sign(uv_det)" in src
      and "cross(axis, geometric_n)" in src)
check("Lit preview uses Blender corner normals instead of triangle normals",
      "surfaceNormal" in gpu_engine.PREVIEW_VERT_SRC
      and "geometric_n = normalize(surfaceNormal)" in src
      and "cross(dpdx, dpdy)" not in src)
check("resident preview uses biased framebuffer depth without prepass cracks",
      "gl_Position.z -=" in gpu_engine.PREVIEW_VERT_SRC
      and "impasto_visible_surface" not in src
      and "preview_depth_tex" not in src
      and "depth_test_set('LESS_EQUAL')" in draw_preview_source
      and "face_culling_set('BACK')" in draw_preview_source)
check("preview clip matrix comes from gpu.matrix, not rv3d.perspective_matrix",
      "preview_framebuffer_view_proj(s.view_proj)" in draw_preview_source
      and '"view_proj_matrix": s.view_proj' not in draw_preview_source
      and "gpu.matrix.get_projection_matrix()" in
      inspect.getsource(gpu_engine.preview_framebuffer_view_proj)
      and "get_model_view_matrix()" in
      inspect.getsource(gpu_engine.preview_framebuffer_view_proj))
try:
    import gpu as _gpu
    _gpu.matrix.get_projection_matrix()
    _gpu_matrix_ok = True
except Exception:
    _gpu_matrix_ok = False
if _gpu_matrix_ok:
    check("preview clip matrix uses gpu.matrix when available",
          gpu_engine.preview_framebuffer_view_proj("FALLBACK") != "FALLBACK")
else:
    check("preview clip matrix falls back when gpu.matrix is unavailable",
          gpu_engine.preview_framebuffer_view_proj("FALLBACK") == "FALLBACK")
check("resident preview rejects other meshes with private linear depth",
      "occluder_ready > 0.5" in src
      and "texelFetch(occluder_depth_tex" in src
      and 'preview_globals["occluder_ready"]' in draw_preview_source
      and 'preview_globals["view_depth_plane"]' in draw_preview_source
      and 'uniform_sampler("occluder_depth_tex"' in draw_preview_source)
check("topmost Lit preview owns the complete front surface",
      "fragColor = vec4(rgb, preview_opacity)" in src
      and "coverage = max(coverage" not in src)
check("height uses screen derivatives rather than four neighbor taps",
      "dFdx(height)" in src and "dFdy(height)" in src
      and "uvInterp + vec2" not in src and "uvInterp - vec2" not in src)

# Normal alpha must be a strength interpolation, not a binary presence test.
tilt = (1.0, 0.5, 1.0)
flat = encoded_normal(tilt, 0.0)
quarter = encoded_normal(tilt, 0.25)
full = encoded_normal(tilt, 1.0)
check("zero-alpha encoded normal is exactly flat",
      flat == (0.0, 0.0, 1.0), repr(flat))
check("partial normal alpha produces intermediate tilt",
      0.0 < quarter[0] < full[0] and full[2] < quarter[2] < 1.0,
      "quarter=%r full=%r" % (quarter, full))

base = (0.7, 0.3, 1.0)
neutral_detail = gpu_engine.compose_preview_normals(base)
zero_strength = gpu_engine.compose_preview_normals(base, strength=0.0)
inverted = gpu_engine.compose_preview_normals(base, invert_green=True)
check("neutral painted normal preserves preview base normal",
      neutral_detail[0] > 0.5 and neutral_detail[1] < 0.5)
check("zero base strength is flat",
      all(abs(a - b) < 1e-7 for a, b in
          zip(zero_strength, (0.5, 0.5, 1.0))), repr(zero_strength))
check("green inversion flips only base normal Y polarity",
      abs(inverted[0] - neutral_detail[0]) < 1e-7
      and inverted[1] > 0.5 > neutral_detail[1])

# Height directions should be symmetric, with 0.5/constant height neutral.
height_flat = height_normal(0.0, 0.0)
height_raise_x = height_normal(0.05, 0.0)
height_lower_x = height_normal(-0.05, 0.0)
check("constant height leaves the geometric normal unchanged",
      height_flat == (0.0, 0.0, 1.0))
check("opposite height derivatives produce opposite equal tilts",
      height_raise_x[0] == -height_lower_x[0]
      and abs(height_raise_x[2] - height_lower_x[2]) < 1e-12)

# Mode switching and drawing must remain resident-state operations. Looking at
# these narrow functions avoids false positives from explicit flush routines
# elsewhere in the module.
mode_source = inspect.getsource(gpu_engine.set_preview_mode)
draw_source = inspect.getsource(gpu_engine._draw_composed_preview)
forbidden = ("read_color", ".read(", "foreach_set", "flush_gpu",
             "pending_pixels")
check("mode switch performs no synchronization",
      not any(word in mode_source for word in forbidden), mode_source)
check("preview draw performs no synchronization",
      not any(word in draw_source for word in forbidden), draw_source)
check("preview mode is packed into the draw-time UBO",
      '"preview_mode": preview_mode_index' in draw_source
      and "#define preview_mode int(preview_params.values[9].x)"
      in gpu_engine._preview_ubo_aliases())
check("preview does not upload optimized-away resolved-stack uniform",
      "resolved_stack" not in draw_source
      and "resolved_stack" not in
      inspect.getsource(gpu_engine.preview_shader_create_info))

print("IMPASTO_GPU_PREVIEW_CONTRACT_PASSED")
