# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto GPU multi-target painting engine, promoted from the proven spike.

All gpu-module work for the GPU paint spike: shaders, offscreen
framebuffers, dab dispatch, depth prepass, viewport preview and
GPU->Image sync-back. This is a RESEARCH PROTOTYPE — see FINDINGS.md.

Design (mirrors addons/uv_island_overlay/overlay.py conventions):

- ALL gpu shader/texture/framebuffer work is deferred to draw time and
  exception-LATCH-guarded: gpu object creation raises SystemError in
  ``--background`` (probed on 5.1.2), and a GUI failure must be loud
  exactly once, not once per frame. Starting a session headlessly is a
  harmless no-op (handlers fail to register quietly; pure state works).
- Shaders are built with GPUShaderCreateInfo + gpu.shader.create_from_info
  ONLY: the legacy ``GPUShader(vert, frag)`` constructor raises
  TypeError("cannot create 'GPUShader' instances") on 5.1.2 (probed).
- The modal operator NEVER touches gpu directly: it enqueues dabs and
  tags a redraw; the POST_VIEW draw callback (where a GPU context is
  guaranteed) flushes the queue. Readback results travel the other way:
  the draw callback stashes a numpy array, the modal operator writes it
  into the Image datablock (ID writes from draw callbacks are unsafe).

Dab rasterization technique
---------------------------
The mesh is rendered INTO UV SPACE: the vertex shader emits the UV
coordinate as the clip-space position (``vec4(uv * 2 - 1, 0, 1)``)
while passing the world-space position through to the fragment stage.
Each fragment therefore IS one texel of the paint texture, and knows
the 3D point it textures. The fragment shader projects that point
through the CURRENT VIEW (the same view_proj matrix the depth prepass
used), tests it against the screen-space brush disc (radius + hardness
falloff), tests occlusion against the prepass depth, and emits the
brush color with falloff alpha. Fixed-function 'ALPHA' blending
accumulates the dab into the paint texture — no ping-pong needed
(probed at runtime by _probe_capabilities; a probe line reports it).

Occlusion
---------
The viewport's own depth buffer is not readable from Python, so the
mesh is rendered once per VIEW CHANGE (not per dab) into a private
framebuffer: DEPTH_COMPONENT32F depth attachment for z-testing plus an
R32F color attachment storing positive linear view-space depth. The dab
shader uses exact texel fetches plus a continuity-gated local depth gradient,
which preserves steep visible surfaces without widening the threshold enough
to admit rear shells. The DEPTH texture itself is used only for prepass
z-testing, avoiding backend depth-convention ambiguity.

Deferred sync-back
------------------
Pen-up performs no readback. Paint textures stay GPU-resident for live
composed preview and atomic tile undo. At explicit flush or normal session
exit the framebuffer is read back once. Preferred path
(probed at session start): ``fb.read_color(..., data=Buffer)`` where
the Buffer wraps pre-allocated numpy memory — the pixels land directly
in the array ``image.pixels.foreach_set`` consumes, so the
Buffer->numpy conversion step disappears entirely. Otherwise the
returned ``gpu.types.Buffer`` is converted through the fastest probed
rung of BUFFER_TO_NUMPY_LADDER (asarray / frombuffer / memoryview /
to_list fallback; the ``buffer_to_numpy_path=`` probe line names the
winner). The naive ``np.asarray(buf)`` this replaces was measured at
~1.05 s for one 4K readback — numpy silently degrading to element-wise
sequence iteration over 16.7M Python floats (see FINDINGS.md). CPU
cost is paid once per flush rather than once per stroke.

Multi-channel (v0.3.0)
----------------------
N (1/2/4/8) RGBA16F textures attach to ONE GPUFrameBuffer (MRT); the
dab fragment shader writes a DISTINCT value per attachment (color, a
scalar packed in R, a second color, a height-ish scalar), all modulated
by the same brush falloff alpha. Design constraint (probed):
``gpu.state.blend_set`` is GLOBAL — one blend mode for every attachment
(per-attachment blend is not exposed by the Python API), which forces
same-blend-per-stroke semantics; that matches the shared-mask model
anyway. Sync-back reads N attachments (``fb.read_color(slot=i)``) and
writes N Image datablocks. A CONSERVATIVE dirty-rect shrinks the reads:
per-triangle screen bboxes (numpy, cached per prepass) are intersected
with each flush's dab-disc bbox; the union of the hit triangles' UV
bboxes bounds every texel the rasterizer can have touched (triangles
that cross the near plane count as always-dirty). Sub-rect reads land
in full-size CPU mirror arrays that ``foreach_set`` consumes whole —
Image.pixels has no partial-write API, so the CPU write stays full-cost
(a finding, not a bug).

Instrumentation reports submission and explicit-flush costs separately with
time.perf_counter and reported in a blf overlay, the N-panel, and one
machine-readable ``GPU_PAINT_SPIKE_STROKE ...`` console line per stroke.
NOTE: per-dab times measured on the CPU are SUBMISSION times (the GPU
runs asynchronously); pen-up deliberately does not drain. An explicit flush
forces completion and reports its transfer cost. Set
DEBUG_COMPARE_READS = True to restore the 0.1.0 A/B probe that timed
GPUTexture.read() next to fb.read_color (a second full transfer per
stroke; off in the production path).
"""

import math
import time
import traceback
from contextlib import contextmanager

import bpy
import gpu

from . import visibility
from . import tile_undo
from . import ibl
from . import model
from . import preview_stack
from . import channel_paint
from .gpu.brush_math import (
    brush_falloff,
    dab_spacing as _dab_spacing,
    interpolate_dabs as _interpolate_dabs,
    overlap_compensated_opacity,
    sanitize_pressure,
)
from .gpu.caliper import sss_caliper_layout
from .gpu import overlays as gpu_overlays
from .gpu import uv_gutters
from .gpu import uv_seams

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Clip-space depth bias for the preview pass (same mechanism as
# uv_island_overlay: pull toward the viewer by a w-scaled constant so
# the painted preview wins z-fighting against the mesh's own surface).
# The bias is a tiny NDC fraction; occlusion against *other* meshes
# depends on matching the POST_VIEW framebuffer's clip matrix, not on
# enlarging this value.
CLIP_DEPTH_BIAS = 1e-4

# Absolute floor for the linear view-space depth tolerance. The shader also
# applies a tiny distance-relative tolerance, avoiding the old nonlinear NDC
# epsilon that admitted rear surfaces when the viewport near clip was small.
DEPTH_EPSILON = 1e-4

# Screen-space dab spacing as a fraction of the brush radius, and a
# safety cap on dabs generated by a single mouse-move event.
DAB_SPACING_FACTOR = 0.25
MIN_DAB_SPACING_PX = 2.0
MAX_DABS_PER_EVENT = 256

# Multi-channel painting: how many RGBA16F targets one session may
# attach to the paint framebuffer (the panel offers 1/2/4/8; the probe
# reports what GPUFrameBuffer actually accepts on this build/GPU).
MAX_CHANNELS = 16

# Dab parameters live in one std140-friendly float UBO.  Keeping every field
# vec4-aligned avoids backend-specific padding (and lets Python upload a plain
# contiguous float32 array).  The former push-constant declaration could grow
# beyond Vulkan's portable 128-byte minimum once stencils/MRT were enabled.
DAB_UBO_NAME = "dab_params"
DAB_UBO_SLOT = 0
DAB_UBO_MODEL = 0
DAB_UBO_VIEW_PROJ = 4
DAB_UBO_VIEW_DEPTH = 8
DAB_UBO_REGION_CENTER = 9
DAB_UBO_BRUSH_DEPTH = 10
DAB_UBO_PAINT_FLAGS = 11
DAB_UBO_STENCIL_FLAGS = 12
DAB_UBO_STENCIL_TRANSFORM = 13
DAB_UBO_PROFILE_FLAGS = 14
DAB_UBO_BRUSH_VALUES = 15
DAB_UBO_VEC4_COUNT = DAB_UBO_BRUSH_VALUES + MAX_CHANNELS

DAB_UBO_TYPEDEF = """
struct ImpastoDabParams {
    mat4 model_matrix;
    mat4 view_proj_matrix;
    vec4 view_depth_plane;
    vec4 region_brush_center;  /* region_size.xy, brush_center_px.xy */
    vec4 brush_depth;          /* radius, hardness, abs eps, rel eps */
    vec4 paint_flags;          /* occlusion, pressure, stencil, projection */
    vec4 stencil_flags;        /* interpretation, opacity, rotation, unused */
    vec4 stencil_transform;    /* position.xy, scale.xy */
    vec4 profile_flags;        /* usage, strength, invert, unused */
    vec4 brush_values[16];
};
"""

PREVIEW_UBO_NAME = "preview_params"
PREVIEW_UBO_SLOT = 1
PREVIEW_UBO_CHANNEL_BASE = 13
PREVIEW_UBO_STRIDE = 6
PREVIEW_UBO_VIEW_DEPTH = PREVIEW_UBO_CHANNEL_BASE + 10 * PREVIEW_UBO_STRIDE
PREVIEW_UBO_VEC4_COUNT = PREVIEW_UBO_VIEW_DEPTH + 1
PREVIEW_UBO_TYPEDEF = """
struct ImpastoPreviewParams {
    vec4 values[%d];
};
""" % PREVIEW_UBO_VEC4_COUNT


def _preview_ubo_aliases():
    lines = [
        "#define model_matrix mat4(preview_params.values[0], preview_params.values[1], preview_params.values[2], preview_params.values[3])",
        "#define view_proj_matrix mat4(preview_params.values[4], preview_params.values[5], preview_params.values[6], preview_params.values[7])",
        "#define camera_position preview_params.values[8].xyz",
        "#define preview_opacity preview_params.values[8].w",
        "#define preview_mode int(preview_params.values[9].x)",
        "#define environment_ready preview_params.values[9].y",
        "#define base_normal_enabled preview_params.values[9].z",
        "#define occluder_ready preview_params.values[9].w",
        "#define preview_lighting preview_params.values[10]",
        "#define preview_fill preview_params.values[11]",
        "#define base_normal_options preview_params.values[12].xy",
        "#define view_depth_plane preview_params.values[%d]" % PREVIEW_UBO_VIEW_DEPTH,
    ]
    for i, name in enumerate(GPU_PAINT_CHANNEL_KEYS):
        n = PREVIEW_UBO_CHANNEL_BASE + i * PREVIEW_UBO_STRIDE
        lines.extend((
            "#define has_%s preview_params.values[%d].x" % (name, n),
            "#define active_%s preview_params.values[%d].y" % (name, n),
            "#define active_%s_factor preview_params.values[%d].z" % (name, n),
            "#define active_%s_blend int(preview_params.values[%d].w)" % (name, n),
            "#define baseline_%s_value preview_params.values[%d]" % (name, n + 1),
            "#define baseline_%s_is_texture preview_params.values[%d].x" % (name, n + 2),
            "#define upper_%s_c preview_params.values[%d]" % (name, n + 3),
            "#define upper_%s_d preview_params.values[%d]" % (name, n + 4),
            "#define upper_%s_present preview_params.values[%d].x" % (name, n + 5),
            "#define upper_%s_factor preview_params.values[%d].y" % (name, n + 5),
            "#define upper_%s_blend int(preview_params.values[%d].z)" % (name, n + 5),
        ))
    return "\n".join(lines) + "\n"

# Stable engine/UI contract for diagnostic live-preview display modes.
PREVIEW_MODES = (
    "LIT_PBR",
    "RAW_TANGENT_NORMAL",
    "NEUTRAL_NORMAL_LIGHTING",
    "HEIGHT_GRAYSCALE",
)
PREVIEW_MODE_INDEX = {name: index for index, name in enumerate(PREVIEW_MODES)}

STENCIL_PREVIEW_VERT_SRC = """
void main()
{
    gl_Position = vec4(pos, 0.0, 1.0);
    stencilUV = uv;
}
"""

STENCIL_PREVIEW_FRAG_SRC = """
void main()
{
    vec4 sample_value = texture(stencil_preview_tex, stencilUV);
    vec3 display_rgb = mix(vec3(dot(sample_value.rgb,
        vec3(0.2126, 0.7152, 0.0722))), sample_value.rgb, 0.35);
    fragColor = vec4(display_rgb, stencil_preview_opacity);
}
"""

# Conservative dirty-rect: texel padding added around the accumulated
# UV bbox before the sub-rect read (guards float->texel rounding).
DIRTY_RECT_PAD_PX = 2

# When True, the stroke-end finalize ALSO times GPUTexture.read() next
# to the production fb.read_color — the 0.1.0 A/B probe that produced
# the FINDINGS numbers (they measured ~equal: ~100 vs ~105 ms at 4K on
# OpenGL/Quadro RTX 5000). It costs a second full GPU->CPU transfer per
# stroke, so it is OFF in the production path; the tex_read_ms stat
# only appears when this is enabled.
DEBUG_COMPARE_READS = False

# ---------------------------------------------------------------------------
# GLSL (module-level constants so the headless suite can check them
# structurally — compiling is impossible in --background)
# ---------------------------------------------------------------------------

DAB_VERT_SRC = """
void main()
{
    /* Rasterize in UV space: the UV coordinate IS the clip position, so
     * every covered fragment is exactly one texel of the paint texture. */
    gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0);
    worldPos = (dab_params.model_matrix * vec4(pos, 1.0)).xyz;
    paintUV = uv;
}
"""

# Everything up to (and including) the falloff computation is shared by
# the single-channel and MRT variants; dab_frag_src() appends the
# per-attachment output assignments.
_DAB_FRAG_PRELUDE = """
float impasto_stencil_intensity(vec2 sample_uv)
{
    vec4 sample_value = texture(stencil_tex, clamp(sample_uv, 0.0, 1.0));
    return (dab_params.stencil_flags.x < 0.5)
        ? sample_value.a
        : dot(sample_value.rgb, vec3(0.2126, 0.7152, 0.0722));
}

vec3 compose_profile_normal(vec3 configured, vec3 detail)
{
    vec3 base_n = normalize(configured * 2.0 - 1.0);
    vec3 detail_n = normalize(detail * 2.0 - 1.0);
    float base_z = max(base_n.z, 1e-4);
    float detail_z = max(detail_n.z, 1e-4);
    vec3 combined = normalize(vec3(base_n.xy / base_z
                                   + detail_n.xy / detail_z, 1.0));
    return combined * 0.5 + 0.5;
}

void main()
{
    /* Project this texel's 3D point through the same view_proj the
     * depth prepass used. */
    vec4 clip = dab_params.view_proj_matrix * vec4(worldPos, 1.0);
    if (clip.w <= 0.0) {
        discard;   /* behind the eye */
    }
    vec3 ndc = clip.xyz / clip.w;

    /* Screen-space brush disc test (region pixels, origin bottom-left,
     * matching Blender's region-relative mouse coordinates). */
    vec2 region_size = dab_params.region_brush_center.xy;
    vec2 brush_center_px = dab_params.region_brush_center.zw;
    float brush_radius_px = dab_params.brush_depth.x;
    vec2 px = (ndc.xy * 0.5 + 0.5) * region_size;
    float d = distance(px, brush_center_px);
    if (d > brush_radius_px) {
        discard;
    }

    /* Occlusion: compare positive linear view-space depth against the
     * frontmost value stored by the prepass. */
    if (dab_params.paint_flags.x > 0.5) {
        vec2 suv = ndc.xy * 0.5 + 0.5;
        if (suv.x < 0.0 || suv.x > 1.0 || suv.y < 0.0 || suv.y > 1.0) {
            discard;
        }
        float view_depth = dot(dab_params.view_depth_plane,
                               vec4(worldPos, 1.0));
        if (!impasto_visible_surface(scene_depth_tex, suv, view_depth,
                                     dab_params.brush_depth.z,
                                     dab_params.brush_depth.w)) {
            discard;   /* this texel's surface point is hidden */
        }
    }

    /* Round brush falloff: 1 inside the hardness core, smoothstep to 0
     * at the rim. MUST match engine.brush_falloff() (tested headless). */
    float t = d / max(brush_radius_px, 1e-6);
    float h = clamp(dab_params.brush_depth.y, 0.0, 0.999);
    float f = 1.0 - smoothstep(h, 1.0, t);

    /* Evaluate one shared image mask before any MRT output so every enabled
     * material channel receives exactly the same spatial modulation. */
    float stencil_factor = 1.0;
    float profile_factor = 1.0;
    vec3 profile_normal = vec3(0.5, 0.5, 1.0);
    bool profile_mode = (dab_params.paint_flags.z > 0.5 &&
                         dab_params.profile_flags.x > 0.5);
    if (dab_params.paint_flags.z > 0.5) {
        vec2 stencil_center;
        vec2 stencil_half_extent;
        if (dab_params.paint_flags.w < 0.5) {
            stencil_center = dab_params.stencil_transform.xy * region_size;
            stencil_half_extent = 0.5 * dab_params.stencil_transform.zw *
                                  region_size;
        } else {
            stencil_center = brush_center_px;
            stencil_half_extent = brush_radius_px *
                                  dab_params.stencil_transform.zw;
        }
        vec2 delta = px - stencil_center;
        float cs = cos(dab_params.stencil_flags.z);
        float sn = sin(dab_params.stencil_flags.z);
        vec2 local = vec2(cs * delta.x + sn * delta.y,
                         -sn * delta.x + cs * delta.y);
        vec2 stencil_uv = local / (2.0 * max(stencil_half_extent,
                                             vec2(1e-4))) + 0.5;
        if (any(lessThan(stencil_uv, vec2(0.0))) ||
            any(greaterThan(stencil_uv, vec2(1.0)))) {
            stencil_factor = (dab_params.stencil_flags.w > 0.5) ? 0.0 : 1.0;
            profile_factor = 0.0;
        } else {
            if (profile_mode) {
                float mask_value = impasto_stencil_intensity(stencil_uv);
                vec2 profile_size = vec2(textureSize(stencil_tex, 0));
                vec2 profile_texel = 1.0 / max(profile_size, vec2(1.0));
                float left = impasto_stencil_intensity(
                    stencil_uv - vec2(profile_texel.x, 0.0));
                float right = impasto_stencil_intensity(
                    stencil_uv + vec2(profile_texel.x, 0.0));
                float down = impasto_stencil_intensity(
                    stencil_uv - vec2(0.0, profile_texel.y));
                float up = impasto_stencil_intensity(
                    stencil_uv + vec2(0.0, profile_texel.y));
                float direction = (dab_params.profile_flags.z > 0.5)
                    ? -1.0 : 1.0;
                vec2 gradient = 0.5 * vec2(right - left, up - down) *
                    profile_size * dab_params.profile_flags.y * direction;
                vec3 detail_n = normalize(vec3(-gradient, 1.0));
                profile_normal = detail_n * 0.5 + 0.5;
                stencil_factor = (dab_params.stencil_flags.w > 0.5)
                    ? clamp(mask_value * dab_params.stencil_flags.y,
                            0.0, 1.0)
                    : 1.0;
                profile_factor = clamp(dab_params.stencil_flags.y,
                                       0.0, 1.0);
            } else {
                float mask_value = impasto_stencil_intensity(stencil_uv);
                stencil_factor = (dab_params.stencil_flags.w > 0.5)
                    ? clamp(mask_value * dab_params.stencil_flags.y,
                            0.0, 1.0)
                    : 1.0;
            }
        }
    }
    float profile_f = f * profile_factor;
    f *= stencil_factor;
"""

# v0.2.0's single-channel source, byte-for-byte (the N=1 case must not
# regress): prelude + the one output assignment.
def dab_frag_src(channels=1, additive=False, profile_slots=None,
                 profile_enabled=None, coverage_guard=False):
    """MRT fragment source with one independent RGBA payload per slot."""
    if channels > MAX_CHANNELS:
        raise ValueError("channels > MAX_CHANNELS")
    profile_slots = tuple(profile_slots or (False,) * channels)
    if len(profile_slots) != channels:
        raise ValueError("profile_slots must match channels")
    if profile_enabled is None:
        profile_enabled = any(profile_slots)
    lines = []
    if coverage_guard:
        lines.append(
            "    ivec2 coverage_size = textureSize(coverage_tex, 0);\n"
            "    ivec2 coverage_pixel = clamp(ivec2(floor(paintUV * "
            "vec2(coverage_size))), ivec2(0), coverage_size - ivec2(1));\n"
            "    if (texelFetch(coverage_tex, coverage_pixel, 0).r > 1e-6) "
            "discard;\n"
            "    if (texelFetch(interior_tex, coverage_pixel, 0).r > 0.5) "
            "discard;")
    for i in range(channels):
        output = "fragColor" if i == 0 else "fragColor%d" % i
        lines.append(
            "    if (dab_params.profile_flags.w > 0.5) { %s = "
            "vec4(1.0 - clamp(dab_params.brush_values[%d].a * "
            "dab_params.paint_flags.y * f, 0.0, 1.0)); } "
            "else" % (output, i))
        if profile_slots[i]:
            lines.append(
                "    { %s = profile_mode ? vec4(compose_profile_normal("
                "dab_params.brush_values[%d].rgb, profile_normal), "
                "dab_params.brush_values[%d].a * dab_params.paint_flags.y "
                "* profile_f) : vec4(dab_params.brush_values[%d].rgb, "
                "dab_params.brush_values[%d].a * dab_params.paint_flags.y "
                "* f); }" % (output, i, i, i, i))
        elif additive:
            lines.append(
                "    { %s = vec4(dab_params.brush_values[%d].rgb * "
                "dab_params.brush_values[%d].a * "
                "dab_params.paint_flags.y * f, "
                "dab_params.brush_values[%d].a * "
                "dab_params.paint_flags.y * f); }"
                % (output, i, i, i))
        else:
            lines.append("    { %s = vec4(dab_params.brush_values[%d].rgb, "
                         "dab_params.brush_values[%d].a * "
                         "dab_params.paint_flags.y * f); }"
                         % (output, i, i))
    return _DAB_FRAG_PRELUDE + "\n".join(lines) + "\n}\n"


SEAM_COVERAGE_FRAG_SRC = (_DAB_FRAG_PRELUDE + """
    float coverage = clamp(dab_params.paint_flags.y * f, 0.0, 1.0);
    fragColor = vec4(coverage);
}
""")

SEAM_TRANSFER_VERT_SRC = """
void main()
{
    gl_Position = vec4(destination_uv * 2.0 - 1.0, 0.0, 1.0);
    destinationUV = destination_uv;
    sourceUV = source_uv;
}
"""

SEAM_TRANSFER_FRAG_SRC = """
void main()
{
    ivec2 limit = textureSize(coverage_tex, 0) - ivec2(1);
    ivec2 destination_pixel = clamp(
        ivec2(floor(destinationUV * vec2(limit + ivec2(1)))),
        ivec2(0), limit);
    ivec2 source_pixel = clamp(
        ivec2(floor(sourceUV * vec2(limit + ivec2(1)))),
        ivec2(0), limit);
    float destination_coverage = texelFetch(
        coverage_tex, destination_pixel, 0).r;
    float source_coverage = texelFetch(coverage_tex, source_pixel, 0).r;
    if (source_coverage <= 1e-6 || destination_coverage > 1e-6)
        discard;
    fragColor = texelFetch(source_pixels, source_pixel, 0);
}
"""


def soften_frag_src():
    """One-channel resident Gaussian soften dab.

    The source and destination are deliberately different textures; sampling
    an attached render target is undefined on Blender's supported GPU APIs.
    RGBA is filtered together because MIX canvases are resident in
    premultiplied-alpha form.
    """
    output = """
    vec2 texel = 1.0 / vec2(textureSize(source_tex, 0));
    vec4 softened = texture(source_tex, paintUV) * 4.0;
    softened += texture(source_tex, paintUV + vec2(texel.x, 0.0)) * 2.0;
    softened += texture(source_tex, paintUV - vec2(texel.x, 0.0)) * 2.0;
    softened += texture(source_tex, paintUV + vec2(0.0, texel.y)) * 2.0;
    softened += texture(source_tex, paintUV - vec2(0.0, texel.y)) * 2.0;
    softened += texture(source_tex, paintUV + texel);
    softened += texture(source_tex, paintUV - texel);
    softened += texture(source_tex, paintUV + vec2(texel.x, -texel.y));
    softened += texture(source_tex, paintUV + vec2(-texel.x, texel.y));
    softened *= 0.0625;
    float soften_strength = clamp(dab_params.paint_flags.y * f, 0.0, 1.0);
    fragColor = mix(texture(source_tex, paintUV), softened, soften_strength);
"""
    return _DAB_FRAG_PRELUDE + output + "\n}\n"


def smear_frag_src():
    """Resident directional transport from a separate source texture.

    ``profile_flags.zw`` is expressed in texture UV and points back toward the
    preceding dab.  Keeping source and destination separate avoids undefined
    render-target feedback, just as the Soften path does.
    """
    output = """
    vec4 current = texture(source_tex, paintUV);
    vec4 carried = texture(source_tex, clamp(paintUV + dab_params.profile_flags.zw,
                                             vec2(0.0), vec2(1.0)));
    float smear_strength = clamp(dab_params.paint_flags.y * f, 0.0, 1.0);
    fragColor = mix(current, carried, smear_strength);
"""
    return _DAB_FRAG_PRELUDE + output + "\n}\n"

PREPASS_VERT_SRC = """
void main()
{
    vec4 world = model_matrix * vec4(pos, 1.0);
    vec4 clip = view_proj_matrix * world;
    gl_Position = clip;
    viewDepth = dot(view_depth_plane, world);
}
"""

PREPASS_FRAG_SRC = """
void main()
{
    fragColor = vec4(viewDepth, 0.0, 0.0, 1.0);
}
"""

PREVIEW_VERT_SRC = """
void main()
{
    vec4 world = model_matrix * vec4(pos, 1.0);
    /* view_proj_matrix is the POST_VIEW gpu.matrix projection @ view so
     * hardware depth testing matches the overlay framebuffer. Other
     * scene meshes are occluded in the fragment stage from a private
     * linear-depth target; rv3d.perspective_matrix stays on the dab path. */
    gl_Position = view_proj_matrix * world;
    /* Depth bias: pull toward the viewer in clip space (w-scaled, same
     * mechanism as uv_island_overlay) so the preview coat wins
     * z-fighting against the mesh surface it sits on. */
    gl_Position.z -= %r * gl_Position.w;
    uvInterp = uv;
    baseNormalUV = base_uv;
    worldPos = world.xyz;
    viewDepth = dot(view_depth_plane, world);
    surfaceNormal = normalize(mat3(transpose(inverse(model_matrix))) * normal);
}
""" % CLIP_DEPTH_BIAS

PREVIEW_FRAG_SRC = """
vec3 impasto_decode_normal(vec3 encoded, float invert_green)
{
    vec3 n = encoded * 2.0 - 1.0;
    if (invert_green > 0.5) n.y = -n.y;
    return normalize(n);
}

vec3 srgb_to_linear(vec3 c)
{
    vec3 lo = c / 12.92;
    vec3 hi = pow((c + 0.055) / 1.055, vec3(2.4));
    return mix(hi, lo, lessThanEqual(c, vec3(0.04045)));
}

vec4 straight_sample(sampler2D tex, vec2 uv)
{
    vec4 c = texture(tex, uv);
    if (c.a > 1e-6) c.rgb /= c.a;
    else c.rgb = vec3(0.0);
    return c;
}

vec4 stack_blend(vec4 a, vec4 b, float factor, int mode)
{
    float f = clamp(factor, 0.0, 1.0);
    if (mode == 1) return a + b * f;
    if (mode == 2) return a - b * f;
    if (mode == 3) return a * mix(vec4(1.0), b, f);
    if (mode == 4) return vec4(1.0) -
        (vec4(1.0) - a) * (vec4(1.0) - b * f);
    if (mode == 5) {
        vec4 over = mix(2.0 * a * b,
                        vec4(1.0) - 2.0 * (vec4(1.0) - a) *
                        (vec4(1.0) - b),
                        greaterThanEqual(a, vec4(0.5)));
        return mix(a, over, f);
    }
    return mix(a, b, f);
}

vec3 rnm_blend(vec3 base_encoded, vec3 detail_encoded, float factor)
{
    vec3 n1 = normalize(base_encoded * 2.0 - 1.0);
    vec3 raw_detail = detail_encoded * 2.0 - 1.0;
    float f = clamp(factor, 0.0, 1.0);
    vec3 n2 = normalize(vec3(raw_detail.xy * f,
                             1.0 + (raw_detail.z - 1.0) * f));
    vec3 t = n1 + vec3(0.0, 0.0, 1.0);
    vec3 u = n2 * vec3(-1.0, -1.0, 1.0);
    return normalize(t * dot(t, u) / max(t.z, 1e-5) - u) * 0.5 + 0.5;
}

vec4 resolve_stack_channel(sampler2D active_tex, sampler2D baseline_tex,
                           vec4 baseline_value, float baseline_is_texture,
                           float active_present, float active_factor,
                           int active_blend, float decode_active_srgb)
{
    vec4 value = baseline_is_texture > 0.5
        ? texture(baseline_tex, uvInterp) : baseline_value;
    if (active_present > 0.5) {
        vec4 source = straight_sample(active_tex, uvInterp);
        if (decode_active_srgb > 0.5)
            source.rgb = srgb_to_linear(source.rgb);
        value = stack_blend(value, source, active_factor * source.a,
                            active_blend);
    }
    value.a = 1.0;
    return value;
}

vec4 resolve_stack_normal(sampler2D active_tex, sampler2D baseline_tex,
                          vec4 baseline_value, float baseline_is_texture,
                          float active_present, float active_factor)
{
    vec4 value = baseline_is_texture > 0.5
        ? texture(baseline_tex, uvInterp) : baseline_value;
    if (active_present > 0.5) {
        vec4 source = straight_sample(active_tex, uvInterp);
        float f = active_factor * source.a;
        value.rgb = rnm_blend(value.rgb, source.rgb, f);
    }
    value.a = 1.0;
    return value;
}

vec2 environment_uv(vec3 direction)
{
    direction = normalize(direction);
    return vec2(atan(direction.y, direction.x) / (2.0 * 3.14159265) + 0.5,
                asin(clamp(direction.z, -1.0, 1.0)) / 3.14159265 + 0.5);
}

vec3 rotate_around_z(vec3 direction, float angle)
{
    float c = cos(angle), s = sin(angle);
    return vec3(c * direction.x - s * direction.y,
                s * direction.x + c * direction.y, direction.z);
}

vec3 sample_environment_panel(vec3 direction, float panel)
{
    vec2 uv = environment_uv(direction);
    /* Half-texel inset prevents bilinear bleed between roughness strips. */
    float panel_v = (0.5 + uv.y * 63.0) / 64.0;
    uv.y = (panel + panel_v) / 6.0;
    return texture(environment_atlas, uv).rgb;
}

vec3 sample_prefiltered_environment(vec3 direction, float roughness)
{
    float level = clamp(roughness, 0.0, 1.0) * 4.0;
    float lower = floor(level);
    float upper = min(lower + 1.0, 4.0);
    return mix(sample_environment_panel(direction, 1.0 + lower),
               sample_environment_panel(direction, 1.0 + upper),
               level - lower);
}

vec3 fresnel_schlick_roughness(float ndv, vec3 f0, float roughness)
{
    vec3 grazing = max(vec3(1.0 - roughness), f0);
    return f0 + (grazing - f0) * pow(1.0 - ndv, 5.0);
}

/* Epic's split-sum approximation of the integrated GGX environment BRDF. */
vec2 environment_brdf_ggx(float ndv, float roughness)
{
    vec4 c0 = vec4(-1.0, -0.0275, -0.572, 0.022);
    vec4 c1 = vec4(1.0, 0.0425, 1.04, -0.04);
    vec4 r = roughness * c0 + c1;
    float a004 = min(r.x * r.x, exp2(-9.28 * ndv)) * r.x + r.y;
    return vec2(-1.04, 1.04) * a004 + r.zw;
}

vec3 preview_key_light(vec3 n, vec3 v, vec3 l, vec3 albedo, vec3 f0,
                       float metallic, float roughness, vec3 radiance)
{
    vec3 h = normalize(v + l);
    float ndl = max(dot(n, l), 0.0);
    float ndv = max(dot(n, v), 0.001);
    float ndh = max(dot(n, h), 0.0);
    float vdh = max(dot(v, h), 0.0);
    float a = max(roughness * roughness, 0.002);
    float a2 = a * a;
    float denom = ndh * ndh * (a2 - 1.0) + 1.0;
    float distribution = a2 / max(3.14159265 * denom * denom, 1e-5);
    float k = (roughness + 1.0);
    k = k * k * 0.125;
    float gv = ndv / (ndv * (1.0 - k) + k);
    float gl = ndl / (ndl * (1.0 - k) + k);
    vec3 f = f0 + (vec3(1.0) - f0) * pow(1.0 - vdh, 5.0);
    vec3 specular = distribution * gv * gl * f /
                    max(4.0 * ndv * ndl, 1e-4);
    vec3 diffuse = (vec3(1.0) - f) * (1.0 - metallic) * albedo /
                   3.14159265;
    return (diffuse + specular) * radiance * ndl;
}

vec3 aces_fitted(vec3 color)
{
    return clamp((color * (2.51 * color + 0.03)) /
                 (color * (2.43 * color + 0.59) + 0.14), 0.0, 1.0);
}

vec4 apply_upper_transform(vec4 value, sampler2D coefficients)
{
    vec4 transform = texture(coefficients, uvInterp);
    value.rgb = transform.a * value.rgb + transform.rgb;
    value.a = 1.0;
    return value;
}

void main()
{
    /* POST_VIEW's framebuffer often lacks other scene meshes, so the
     * opaque coat would cover objects in front. Compare linear view
     * depth against a private raster of those meshes only — never the
     * painted surface, which previously punched preview cracks. */
    if (occluder_ready > 0.5) {
        ivec2 size = textureSize(occluder_depth_tex, 0);
        ivec2 p = clamp(ivec2(gl_FragCoord.xy), ivec2(0), size - ivec2(1));
        float occluder = texelFetch(occluder_depth_tex, p, 0).r;
        if (occluder > 0.0 && occluder < 1e29
            && viewDepth > occluder + 1e-4) {
            discard;
        }
    }
    vec4 base = resolve_stack_channel(
        base_color_tex, baseline_base_color_tex, baseline_base_color_value,
        baseline_base_color_is_texture, active_base_color,
        active_base_color_factor, active_base_color_blend, 1.0);
    vec4 metal_sample = resolve_stack_channel(
        metallic_tex, baseline_metallic_tex, baseline_metallic_value,
        baseline_metallic_is_texture, active_metallic,
        active_metallic_factor, active_metallic_blend, 0.0);
    vec4 rough_sample = resolve_stack_channel(
        roughness_tex, baseline_roughness_tex, baseline_roughness_value,
        baseline_roughness_is_texture, active_roughness,
        active_roughness_factor, active_roughness_blend, 0.0);
    vec4 normal_sample = resolve_stack_normal(
        normal_tex, baseline_normal_tex, baseline_normal_value,
        baseline_normal_is_texture, active_normal,
        active_normal_factor);
    vec4 height_sample = resolve_stack_channel(
        height_tex, baseline_height_tex, baseline_height_value,
        baseline_height_is_texture, active_height,
        active_height_factor, active_height_blend, 0.0);
    vec4 emission_color_sample = resolve_stack_channel(
        emission_color_tex, baseline_emission_color_tex,
        baseline_emission_color_value,
        baseline_emission_color_is_texture, active_emission_color,
        active_emission_color_factor, active_emission_color_blend, 1.0);
    vec4 emission_strength_sample = resolve_stack_channel(
        emission_strength_tex, baseline_emission_strength_tex,
        baseline_emission_strength_value,
        baseline_emission_strength_is_texture, active_emission_strength,
        active_emission_strength_factor, active_emission_strength_blend, 0.0);
    vec4 sss_weight_sample = resolve_stack_channel(
        sss_weight_tex, baseline_sss_weight_tex, baseline_sss_weight_value,
        baseline_sss_weight_is_texture, active_sss_weight,
        active_sss_weight_factor, active_sss_weight_blend, 0.0);
    vec4 sss_radius_sample = resolve_stack_channel(
        sss_radius_tex, baseline_sss_radius_tex, baseline_sss_radius_value,
        baseline_sss_radius_is_texture, active_sss_radius,
        active_sss_radius_factor, active_sss_radius_blend, 0.0);
    vec4 sss_scale_sample = resolve_stack_channel(
        sss_scale_tex, baseline_sss_scale_tex, baseline_sss_scale_value,
        baseline_sss_scale_is_texture, active_sss_scale,
        active_sss_scale_factor, active_sss_scale_blend, 0.0);
    if (upper_base_color_present > 0.5)
        base = apply_upper_transform(base, upper_base_color_tex);
    if (upper_metallic_present > 0.5)
        metal_sample = apply_upper_transform(
            metal_sample, upper_metallic_tex);
    if (upper_roughness_present > 0.5)
        rough_sample = apply_upper_transform(
            rough_sample, upper_roughness_tex);
    if (upper_height_present > 0.5)
        height_sample = apply_upper_transform(
            height_sample, upper_height_tex);
    if (upper_emission_color_present > 0.5)
        emission_color_sample = apply_upper_transform(
            emission_color_sample, upper_emission_color_tex);
    if (upper_emission_strength_present > 0.5)
        emission_strength_sample = apply_upper_transform(
            emission_strength_sample, upper_emission_strength_tex);
    if (upper_sss_weight_present > 0.5)
        sss_weight_sample = apply_upper_transform(
            sss_weight_sample, upper_sss_weight_tex);
    if (upper_sss_radius_present > 0.5)
        sss_radius_sample = apply_upper_transform(
            sss_radius_sample, upper_sss_radius_tex);
    if (upper_sss_scale_present > 0.5)
        sss_scale_sample = apply_upper_transform(
            sss_scale_sample, upper_sss_scale_tex);
    if (preview_mode == 3) {
        float h = height_sample.r;
        fragColor = vec4(vec3(h), 1.0);
        return;
    }

    float height = height_sample.r;

    vec3 dpdx = dFdx(worldPos), dpdy = dFdy(worldPos);
    vec2 dudx = dFdx(uvInterp), dudy = dFdy(uvInterp);
    vec2 base_dudx = dFdx(baseNormalUV), base_dudy = dFdy(baseNormalUV);
    vec3 geometric_n = normalize(surfaceNormal);
    if (!gl_FrontFacing) geometric_n = -geometric_n;
    float uv_det = dudx.x * dudy.y - dudx.y * dudy.x;
    vec3 tangent;
    vec3 bitangent;
    if (abs(uv_det) > 1e-8) {
        float orientation = sign(uv_det);
        tangent = normalize((dpdx * dudy.y - dpdy * dudx.y)
                            * orientation);
        bitangent = normalize((-dpdx * dudy.x + dpdy * dudx.x)
                              * orientation);
    } else {
        vec3 axis = abs(geometric_n.z) < 0.999
            ? vec3(0.0, 0.0, 1.0) : vec3(0.0, 1.0, 0.0);
        tangent = normalize(cross(axis, geometric_n));
        bitangent = normalize(cross(geometric_n, tangent));
    }
    vec3 n = geometric_n;
    if (has_normal > 0.5) {
        vec3 encoded_n = normal_sample.rgb;
        vec3 detail_tangent_n = normalize(encoded_n * 2.0 - 1.0);
        n = normalize(mat3(tangent, bitangent, geometric_n)
                      * detail_tangent_n);
    }
    if (base_normal_enabled > 0.5) {
        float base_uv_det = base_dudx.x * base_dudy.y
                          - base_dudx.y * base_dudy.x;
        vec3 base_tangent = tangent;
        vec3 base_bitangent = bitangent;
        if (abs(base_uv_det) > 1e-8) {
            float base_orientation = sign(base_uv_det);
            base_tangent = normalize(
                (dpdx * base_dudy.y - dpdy * base_dudx.y)
                * base_orientation);
            base_bitangent = normalize(
                (-dpdx * base_dudy.x + dpdy * base_dudx.x)
                * base_orientation);
        }
        vec3 base_tangent_n = impasto_decode_normal(
            texture(base_normal_tex, baseNormalUV).rgb,
            base_normal_options.y);
        base_tangent_n = normalize(vec3(
            base_tangent_n.xy * base_normal_options.x,
            max(base_tangent_n.z, 1e-5)));
        vec3 base_world_n = normalize(mat3(
            base_tangent, base_bitangent, geometric_n) * base_tangent_n);
        /* Detail is already in the active UV's world-space TBN. Apply its
         * deviation from the geometric normal over the independent base TBN. */
        n = normalize(base_world_n + (n - geometric_n));
    }
    if (preview_mode == 1) {
        vec3 active_tangent_n = transpose(
            mat3(tangent, bitangent, geometric_n)) * n;
        fragColor = vec4(active_tangent_n * 0.5 + 0.5, 1.0);
        return;
    }
    if (has_height > 0.5) {
        /* Screen derivatives avoid four extra texture taps and keep the
         * diagnostic response stable across texture resolutions. */
        float dhdx = dFdx(height);
        float dhdy = dFdy(height);
        vec3 displaced_dx = dpdx + geometric_n * dhdx * 8.0;
        vec3 displaced_dy = dpdy + geometric_n * dhdy * 8.0;
        vec3 height_n = normalize(cross(displaced_dx, displaced_dy));
        if (dot(height_n, geometric_n) < 0.0) height_n = -height_n;
        n = normalize(n + (height_n - geometric_n));
    }

    if (preview_mode == 2) {
        vec3 neutral_l0 = normalize(vec3(0.55, 0.20, 0.81));
        vec3 neutral_l1 = normalize(vec3(-0.65, 0.35, 0.67));
        float neutral = 0.12
            + 0.58 * max(dot(n, neutral_l0), 0.0)
            + 0.30 * max(dot(n, neutral_l1), 0.0);
        fragColor = vec4(vec3(neutral), 1.0);
        return;
    }

    vec3 albedo = has_base_color > 0.5
        ? base.rgb : vec3(0.5);
    float metallic = has_metallic > 0.5
        ? metal_sample.r : 0.0;
    float roughness = has_roughness > 0.5
        ? rough_sample.r : 0.5;
    vec3 emission_color = has_emission_color > 0.5
        ? emission_color_sample.rgb : vec3(1.0);
    float emission_strength = has_emission_strength > 0.5
        ? max(emission_strength_sample.r, 0.0) : 0.0;
    float sss_weight = has_sss_weight > 0.5
        ? clamp(sss_weight_sample.r, 0.0, 1.0) : 0.0;
    vec3 sss_radius = has_sss_radius > 0.5
        ? max(sss_radius_sample.rgb, vec3(0.0)) : vec3(1.0, 0.2, 0.1);
    float sss_scale = has_sss_scale > 0.5
        ? max(sss_scale_sample.r, 0.0) : 0.05;

    /* Split-sum GGX image-based lighting from Impasto's prefiltered linear
     * HDR studio atlas. Blender's material remains authoritative after an
     * explicit/session-exit flush. */
    vec3 v = normalize(camera_position - worldPos);
    metallic = clamp(metallic, 0.0, 1.0);
    roughness = clamp(roughness, 0.04, 1.0);
    vec3 f0 = mix(vec3(0.04), albedo, metallic);
    float ndv = max(dot(n, v), 0.001);
    vec3 fresnel = fresnel_schlick_roughness(ndv, f0, roughness);
    vec3 kd = (vec3(1.0) - fresnel) * (1.0 - metallic);
    vec3 rgb;
    if (environment_ready > 0.5) {
        float environment_intensity = exp2(preview_lighting.x);
        vec3 environment_n = rotate_around_z(n, preview_lighting.y);
        vec3 irradiance = sample_environment_panel(environment_n, 0.0)
                          * environment_intensity;
        vec3 reflection = reflect(-v, n);
        vec3 prefiltered = sample_prefiltered_environment(
            rotate_around_z(reflection, preview_lighting.y), roughness)
            * environment_intensity;
        vec2 env_brdf = environment_brdf_ggx(ndv, roughness);
        vec3 diffuse_ibl = irradiance * albedo * kd;
        vec3 scatter_distance = sss_radius * sss_scale;
        float scatter_extent = clamp(0.35 + length(scatter_distance) * 2.0,
                                     0.0, 1.0);
        float max_distance = max(max(scatter_distance.r,
                                     scatter_distance.g),
                                 max(scatter_distance.b, 1e-5));
        vec3 scatter_tint = scatter_distance / max_distance;
        vec3 back_irradiance = sample_environment_panel(-environment_n, 0.0)
                               * environment_intensity;
        vec3 scattered = (irradiance + back_irradiance * scatter_tint)
                         * 0.5 * albedo * kd;
        diffuse_ibl = mix(diffuse_ibl, scattered,
                          sss_weight * scatter_extent);
        vec3 specular_ibl = prefiltered * (f0 * env_brdf.x + env_brdf.y);
        rgb = diffuse_ibl + specular_ibl;
        /* A restrained pair of broad studio keys makes painted roughness and
         * tangent-normal changes legible even on dielectric materials. The
         * environment remains the dominant illumination. */
        rgb += preview_key_light(
            n, v, normalize(rotate_around_z(vec3(0.42, -0.34, 0.84),
                                           preview_lighting.w)), albedo, f0,
            metallic, roughness, vec3(0.48, 0.43, 0.38)
                                   * preview_lighting.z);
        rgb += preview_key_light(
            n, v, normalize(vec3(-0.58, 0.46, 0.67)), albedo, f0,
            metallic, roughness, vec3(0.14, 0.19, 0.28)
                                   * preview_fill.x);
        /* Optional display-only reflection aid.  This deliberately uses the
         * original roughness in the same GGX light evaluation: it changes
         * only the studio illumination, never the painted/material value. */
        rgb += preview_key_light(
            n, v, normalize(rotate_around_z(vec3(-0.18, -0.62, 0.76),
                                           preview_lighting.w)), albedo, f0,
            metallic, roughness, vec3(0.34, 0.37, 0.42)
                                   * preview_fill.y);
    } else {
        /* Graceful no-texture fallback: energy-conserving hemispheric light. */
        float sky = clamp(n.z * 0.5 + 0.5, 0.0, 1.0);
        vec3 environment = mix(vec3(0.035, 0.028, 0.025),
                               vec3(0.18, 0.23, 0.32), sky);
        rgb = environment * (albedo * kd + fresnel);
    }
    /* Emission remains separate from color so HDR strength is preserved in
     * the canvas and only display-mapped here. */
    rgb += emission_color * emission_strength;
    rgb = aces_fitted(rgb);

    /* The resident material preview owns the whole visible front surface.
     * Per-channel alpha already gates each active layer exactly once inside
     * resolve_stack_channel(). Reusing it as overlay alpha punched UV/texel
     * gaps through to Blender's underlying material, producing intermittent
     * white or surface-colored stripes on the topmost unresolved path. */
    fragColor = vec4(rgb, preview_opacity);
}
"""

COPY_VERT_SRC = """
void main()
{
    gl_Position = vec4(pos, 1.0);
    uvInterp = uv_origin + uv * uv_scale;
}
"""

COPY_FRAG_SRC = """
void main()
{
    fragColor = texture(source_tex, uvInterp);
}
"""

BASELINE_FRAG_SRC = """
vec3 rnm_blend(vec3 base_encoded, vec3 detail_encoded, float factor)
{
    vec3 n1 = normalize(base_encoded * 2.0 - 1.0);
    vec3 raw_detail = detail_encoded * 2.0 - 1.0;
    float f = clamp(factor, 0.0, 1.0);
    vec3 n2 = normalize(vec3(raw_detail.xy * f,
                             1.0 + (raw_detail.z - 1.0) * f));
    vec3 t = n1 + vec3(0.0, 0.0, 1.0);
    vec3 u = n2 * vec3(-1.0, -1.0, 1.0);
    return normalize(t * dot(t, u) / max(t.z, 1e-5) - u) * 0.5 + 0.5;
}

vec4 blend_step(vec4 a, vec4 b, float factor, int mode)
{
    float f = clamp(factor, 0.0, 1.0);
    if (mode == 1) return a + b * f;
    if (mode == 2) return a - b * f;
    if (mode == 3) return a * mix(vec4(1.0), b, f);
    if (mode == 4) return vec4(1.0) -
        (vec4(1.0) - a) * (vec4(1.0) - b * f);
    if (mode == 5) {
        vec4 over = mix(2.0 * a * b,
                        vec4(1.0) - 2.0 * (vec4(1.0) - a) *
                        (vec4(1.0) - b),
                        greaterThanEqual(a, vec4(0.5)));
        return mix(a, over, f);
    }
    return mix(a, b, f);
}

void main()
{
    vec4 a = texture(current_tex, uvInterp);
    vec4 b = source_is_texture > 0.5
        ? texture(source_tex, uvInterp) : source_value;
    float f = factor * (source_uses_alpha > 0.5 ? b.a : 1.0);
    fragColor = is_normal > 0.5
        ? vec4(rnm_blend(a.rgb, b.rgb, f), a.a)
        : blend_step(a, b, f, blend_mode);
}
"""

UPPER_TRANSFORM_FRAG_SRC = """
void main()
{
    vec4 a = texture(current_tex, uvInterp);
    vec4 b = source_is_texture > 0.5
        ? texture(source_tex, uvInterp) : source_value;
    float f = clamp(factor * (source_uses_alpha > 0.5 ? b.a : 1.0),
                    0.0, 1.0);
    if (mask_present > 0.5) {
        vec3 mask_rgb = texture(mask_tex, uvInterp).rgb;
        /* Match the stack compiler/exporter's implicit Color-to-Value
         * conversion instead of assuming imported masks are perfectly gray. */
        float m = dot(mask_rgb, vec3(0.2126, 0.7152, 0.0722));
        m = mask_invert > 0.5 ? 1.0 - m : m;
        f *= mix(1.0, m, clamp(mask_opacity, 0.0, 1.0));
    }
    vec3 d = a.rgb;
    float c = a.a;
    if (blend_mode == 1) d += b.rgb * f;
    else if (blend_mode == 2) d -= b.rgb * f;
    else if (blend_mode == 3) {
        float k = mix(1.0, b.r, f);
        c *= k;
        d *= k;
    }
    else if (blend_mode == 4) {
        float add = b.r * f;
        float k = 1.0 - add;
        c *= k;
        d = d * k + vec3(add);
    } else {
        c *= 1.0 - f;
        d = d * (1.0 - f) + b.rgb * f;
    }
    fragColor = vec4(d, c);
}
"""

UPPER_REPROJECT_VERT_SRC = """
void main()
{
    targetUvInterp = target_uv;
    sourceUvInterp = source_uv;
    gl_Position = vec4(target_uv * 2.0 - 1.0, 0.0, 1.0);
}
"""

UPPER_REPROJECT_FRAG_SRC = """
void main()
{
    vec4 a = texture(current_tex, targetUvInterp);
    vec4 b = texture(source_tex, sourceUvInterp);
    float f = clamp(factor * (source_uses_alpha > 0.5 ? b.a : 1.0),
                    0.0, 1.0);
    vec3 d = a.rgb;
    float c = a.a;
    if (blend_mode == 1) d += b.rgb * f;
    else if (blend_mode == 2) d -= b.rgb * f;
    else if (blend_mode == 3) {
        float k = mix(1.0, b.r, f);
        c *= k;
        d *= k;
    }
    else if (blend_mode == 4) {
        float add = b.r * f;
        float k = 1.0 - add;
        c *= k;
        d = d * k + vec3(add);
    } else {
        c *= 1.0 - f;
        d = d * (1.0 - f) + b.rgb * f;
    }
    fragColor = vec4(d, c);
}
"""

# ---------------------------------------------------------------------------
# Pure math (headless-testable; the GLSL above must agree)
# ---------------------------------------------------------------------------


def interpolate_dabs(x0, y0, x1, y1, spacing, leftover=0.0):
    """Dab positions along the segment (x0,y0)->(x1,y1) at ``spacing``
    px, carrying ``leftover`` distance from the previous segment so a
    fast stroke keeps even spacing across events. Returns
    ``(positions, new_leftover)`` where positions is a list of
    ``(x, y, t)`` with t in (0, 1]."""
    return _interpolate_dabs(
        x0, y0, x1, y1, spacing, leftover, MAX_DABS_PER_EVENT)


def dab_spacing(radius_px):
    return _dab_spacing(radius_px, DAB_SPACING_FACTOR, MIN_DAB_SPACING_PX)


def build_position_soup(obj):
    """Return loop-triangle positions. No UVs; occluders may be unwrapped."""
    import numpy as np

    me = getattr(obj, "data", None)
    if me is None or getattr(me, "vertices", None) is None:
        return None
    if len(me.vertices) == 0:
        return None
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    if hasattr(me, "calc_loop_triangles"):
        me.calc_loop_triangles()
    tris = me.loop_triangles
    if len(tris) == 0:
        return None
    tv = np.empty(len(tris) * 3, dtype=np.int32)
    tris.foreach_get("vertices", tv)
    return np.ascontiguousarray(co[tv], dtype=np.float32)


def iter_preview_occluders(painted_name, objects):
    """Yield other mesh objects that should hide the Lit PBR overlay."""
    for obj in objects:
        if obj is None or getattr(obj, "type", None) != "MESH":
            continue
        if getattr(obj, "name", None) == painted_name:
            continue
        try:
            if obj.hide_get():
                continue
        except Exception:
            pass
        if getattr(obj, "hide_viewport", False):
            continue
        yield obj


def build_mesh_soup(obj):
    """Return per-corner coordinates, UVs, and Blender shading normals."""
    import numpy as np

    me = obj.data
    uv_layer = me.uv_layers.active
    if uv_layer is None or len(me.loops) == 0:
        return None, None, None

    uv = np.empty(len(me.loops) * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)

    if hasattr(me, "calc_loop_triangles"):
        me.calc_loop_triangles()
    tris = me.loop_triangles
    n_tris = len(tris)
    if n_tris == 0:
        return None, None, None
    tv = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("vertices", tv)
    tl = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("loops", tl)

    coords = np.ascontiguousarray(co[tv], dtype=np.float32)
    uvs = np.ascontiguousarray(uv[tl], dtype=np.float32)
    corner_normals = getattr(me, "corner_normals", ())
    if len(corner_normals) == len(me.loops):
        loop_normals = np.empty(len(me.loops) * 3, dtype=np.float32)
        corner_normals.foreach_get("vector", loop_normals)
        loop_normals = loop_normals.reshape(-1, 3)
        normals = np.ascontiguousarray(loop_normals[tl], dtype=np.float32)
    else:
        tri_coords = coords.reshape(-1, 3, 3)
        face = np.cross(tri_coords[:, 1] - tri_coords[:, 0],
                        tri_coords[:, 2] - tri_coords[:, 0])
        lengths = np.linalg.norm(face, axis=1)
        lengths[lengths < 1e-12] = 1.0
        face /= lengths[:, None]
        normals = np.ascontiguousarray(
            np.repeat(face[:, None, :], 3, axis=1).reshape(-1, 3),
            dtype=np.float32)
    return coords, uvs, normals


def build_uv_soup(obj, uv_map_name=""):
    """Return triangle-corner UVs for a named map without changing active UV."""
    import numpy as np
    me = obj.data
    uv_layer = (me.uv_layers.get(uv_map_name) if uv_map_name
                else me.uv_layers.active)
    if uv_layer is None or len(me.loops) == 0:
        return None
    if hasattr(me, "calc_loop_triangles"):
        me.calc_loop_triangles()
    if len(me.loop_triangles) == 0:
        return None
    uv = np.empty(len(me.loops) * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uv)
    loops = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
    me.loop_triangles.foreach_get("loops", loops)
    return np.ascontiguousarray(uv.reshape(-1, 2)[loops], dtype=np.float32)


def build_vertex_triangle_soup(obj):
    """Return loop-triangle mesh vertex indices for topology-aware seams."""
    import numpy as np
    me = obj.data
    if hasattr(me, "calc_loop_triangles"):
        me.calc_loop_triangles()
    if len(me.loop_triangles) == 0:
        return None
    vertices = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
    me.loop_triangles.foreach_get("vertices", vertices)
    return vertices.reshape(-1, 3)


def seam_continuation_channel_keys(channel_keys):
    """Channels safe for literal seam transport.

    Encoded tangent normals are intentionally excluded: the two islands can
    have different tangent frames, so copying RGB without a basis transform
    changes the represented world-space direction.
    """
    return tuple(key for key in channel_keys if key != "normal")


def build_sparse_seam_strips(correspondence, uv_triangles, canvas_size,
                             width_px=2):
    """Build bidirectional, topology-paired UV strip triangles and rects."""
    import numpy as np
    destinations = []
    sources = []
    rects = []
    inset = max(1.0, float(width_px)) / float(canvas_size)

    def side_strip(destination, source):
        tri = uv_triangles[destination.triangle]
        third = tri[(destination.corner + 2) % 3]
        a = np.asarray(destination.uv0, dtype=np.float64)
        b = np.asarray(destination.uv1, dtype=np.float64)
        edge = b - a
        length = float(np.linalg.norm(edge))
        if length <= 1e-12:
            return
        normal = np.asarray((-edge[1], edge[0])) / length
        if float(np.dot(np.asarray(third) - (a + b) * 0.5, normal)) < 0.0:
            normal = -normal
        sa = np.asarray(source.uv0, dtype=np.float64)
        sb = np.asarray(source.uv1, dtype=np.float64)
        source_tri = uv_triangles[source.triangle]
        source_third = source_tri[(source.corner + 2) % 3]
        source_edge = sb - sa
        source_length = float(np.linalg.norm(source_edge))
        if source_length <= 1e-12:
            return
        source_normal = np.asarray((-source_edge[1], source_edge[0])) \
            / source_length
        if float(np.dot(np.asarray(source_third) - (sa + sb) * 0.5,
                        source_normal)) < 0.0:
            source_normal = -source_normal
        da, db = a + normal * inset, b + normal * inset
        sda, sdb = sa + source_normal * inset, sb + source_normal * inset
        destination_quad = (a, b, db, a, db, da)
        source_quad = (sa, sb, sdb, sa, sdb, sda)
        destinations.extend(tuple(map(float, uv)) for uv in destination_quad)
        sources.extend(tuple(map(float, uv)) for uv in source_quad)
        lo = np.minimum(np.minimum(a, b), np.minimum(da, db))
        hi = np.maximum(np.maximum(a, b), np.maximum(da, db))
        x0 = max(0, min(canvas_size, int(np.floor(lo[0] * canvas_size))))
        y0 = max(0, min(canvas_size, int(np.floor(lo[1] * canvas_size))))
        x1 = max(0, min(canvas_size, int(np.ceil(hi[0] * canvas_size)) + 1))
        y1 = max(0, min(canvas_size, int(np.ceil(hi[1] * canvas_size)) + 1))
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1 - x0, y1 - y0))

    for pair in correspondence.pairs:
        side_strip(pair.first, pair.second)
        side_strip(pair.second, pair.first)
    return (np.asarray(destinations, dtype=np.float32).reshape(-1, 2),
            np.asarray(sources, dtype=np.float32).reshape(-1, 2),
            tuple(rects))


def build_conservative_seam_strips(correspondence, uv_triangles,
                                    triangle_coords, canvas_size,
                                    expansion_px=0.75):
    """Raster strips outside each UV seam, clamped to its mesh edge.

    Outer strip vertices retain the corresponding edge world position. The
    ordinary dab shader therefore evaluates brush/stencil/occlusion at the
    nearest valid surface point while rasterizing the texel square just beyond
    the island boundary.
    """
    import numpy as np
    positions, destinations, rects = [], [], []
    inset = max(0.5, float(expansion_px)) / float(canvas_size)
    for pair in correspondence.pairs:
        for side in (pair.first, pair.second):
            tri_uv = np.asarray(uv_triangles[side.triangle], dtype=np.float64)
            tri_pos = np.asarray(
                triangle_coords[side.triangle], dtype=np.float32)
            corner = side.corner
            nxt = (corner + 1) % 3
            third = (corner + 2) % 3
            a, b = tri_uv[corner], tri_uv[nxt]
            pa, pb = tri_pos[corner], tri_pos[nxt]
            edge = b - a
            length = float(np.linalg.norm(edge))
            if length <= 1e-12:
                continue
            outward = np.asarray((edge[1], -edge[0])) / length
            midpoint = (a + b) * 0.5
            if float(np.dot(tri_uv[third] - midpoint, outward)) > 0.0:
                outward = -outward
            oa, ob = a + outward * inset, b + outward * inset
            destinations.extend(tuple(map(float, uv)) for uv in (
                a, b, ob, a, ob, oa))
            # Both outer vertices clamp to the closest point on the real edge.
            positions.extend(tuple(map(float, p)) for p in (
                pa, pb, pb, pa, pb, pa))
            lo = np.minimum(np.minimum(a, b), np.minimum(oa, ob))
            hi = np.maximum(np.maximum(a, b), np.maximum(oa, ob))
            x0 = max(0, min(canvas_size, int(np.floor(lo[0] * canvas_size))))
            y0 = max(0, min(canvas_size, int(np.floor(lo[1] * canvas_size))))
            x1 = max(0, min(canvas_size,
                            int(np.ceil(hi[0] * canvas_size)) + 1))
            y1 = max(0, min(canvas_size,
                            int(np.ceil(hi[1] * canvas_size)) + 1))
            if x1 > x0 and y1 > y0:
                rects.append((x0, y0, x1 - x0, y1 - y0))
    return (np.asarray(positions, dtype=np.float32).reshape(-1, 3),
            np.asarray(destinations, dtype=np.float32).reshape(-1, 2),
            tuple(rects))


def build_conservative_seam_records(correspondence, uv_triangles,
                                    triangle_coords, canvas_size,
                                    expansion_px=0.75):
    """Per-side conservative strips with vertex caps and owner triangles."""
    import numpy as np
    records = []
    radius = max(0.5, float(expansion_px)) / float(canvas_size)
    capped = set()
    for pair in correspondence.pairs:
        for side in (pair.first, pair.second):
            tri = side.triangle
            corner, nxt, third = side.corner, (side.corner + 1) % 3, \
                (side.corner + 2) % 3
            tuv = np.asarray(uv_triangles[tri], dtype=np.float64)
            tpos = np.asarray(triangle_coords[tri], dtype=np.float32)
            a, b = tuv[corner], tuv[nxt]
            pa, pb = tpos[corner], tpos[nxt]
            edge = b - a
            length = float(np.linalg.norm(edge))
            if length <= 1e-12:
                continue
            outward = np.asarray((edge[1], -edge[0])) / length
            if float(np.dot(tuv[third] - (a + b) * 0.5, outward)) > 0.0:
                outward = -outward
            oa, ob = a + outward * radius, b + outward * radius
            uv_vertices = [a, b, ob, a, ob, oa]
            pos_vertices = [pa, pb, pb, pa, pb, pa]
            # A half-texel square around each UV endpoint closes the wedge
            # which two independently expanded edge quads can otherwise miss.
            for vertex_corner, uv, pos in ((corner, a, pa), (nxt, b, pb)):
                cap_key = (tri, vertex_corner)
                if cap_key in capped:
                    continue
                capped.add(cap_key)
                lo = uv - radius
                hi = uv + radius
                cap_uvs = (lo, (hi[0], lo[1]), hi,
                           lo, hi, (lo[0], hi[1]))
                uv_vertices.extend(cap_uvs)
                pos_vertices.extend((pos,) * 6)
            uv_array = np.asarray(uv_vertices, dtype=np.float32).reshape(-1, 2)
            pos_array = np.asarray(pos_vertices, dtype=np.float32).reshape(-1, 3)
            lo = np.min(uv_array, axis=0)
            hi = np.max(uv_array, axis=0)
            x0 = max(0, min(canvas_size, int(np.floor(lo[0] * canvas_size))))
            y0 = max(0, min(canvas_size, int(np.floor(lo[1] * canvas_size))))
            x1 = max(0, min(canvas_size,
                            int(np.ceil(hi[0] * canvas_size)) + 1))
            y1 = max(0, min(canvas_size,
                            int(np.ceil(hi[1] * canvas_size)) + 1))
            if x1 > x0 and y1 > y0:
                records.append((tri, pos_array, uv_array,
                                (x0, y0, x1 - x0, y1 - y0)))
    return tuple(records)


def seam_record_triangle_index(records):
    """Cache record-owner triangles for vectorized screen-space selection."""
    import numpy as np
    return np.fromiter((record[0] for record in records), dtype=np.int32,
                       count=len(records))


def touched_seam_record_indices(records, triangle_screen_bboxes, dab_rect,
                                record_triangles=None,
                                touched_triangles=None):
    """Select seam sides whose owning face intersects this dab union exactly.

    ``record_triangles`` is cached once per session.  Indexing the numpy bbox
    table and applying one vectorized mask avoids walking every seam record in
    Python for every queued flush while preserving the original inclusive
    intersection test and record order.
    """
    import numpy as np
    if not records:
        return ()
    owners = (seam_record_triangle_index(records) if record_triangles is None
              else record_triangles)
    if touched_triangles is not None:
        # The boolean lookup retains seam-record order while reusing the exact
        # triangle selection already computed for dirty tracking this flush.
        triangle_hit = np.zeros(len(triangle_screen_bboxes), dtype=bool)
        triangle_hit[np.asarray(touched_triangles, dtype=np.int32)] = True
        return tuple(map(int, np.flatnonzero(triangle_hit[owners])))
    boxes = np.asarray(triangle_screen_bboxes)[owners]
    x0, y0, x1, y1 = dab_rect
    mask = ((boxes[:, 2] >= x0) & (boxes[:, 0] <= x1)
            & (boxes[:, 3] >= y0) & (boxes[:, 1] <= y1))
    return tuple(map(int, np.flatnonzero(mask)))


# ---------------------------------------------------------------------------
# Conservative dirty-rect tracking (pure numpy; headless-testable)
#
# Exact "which texels did the rasterizer touch" is unknowable on the
# CPU, but a CONSERVATIVE bound is cheap: a texel can only have been
# written if its triangle produced a fragment inside a dab disc, and a
# triangle can only do that if its screen-space bbox intersects the
# disc's bbox. So: project every triangle once per prepass (numpy
# matmul), conservatively clip camera-crossing triangles in homogeneous
# space, intersect the per-triangle screen bboxes with each flush's dab-union
# rect, and union the HIT triangles' UV bboxes. Only invalid/non-finite
# projections count as always-dirty. Occluded/discarded fragments only make
# the rect larger than needed, never smaller: conservative is correct.
# ---------------------------------------------------------------------------


def triangle_uv_bboxes(uvs):
    """(n_tris, 4) float32 [min_u, min_v, max_u, max_v] per triangle
    from the soup's per-corner UVs (n_tris*3, 2)."""
    import numpy as np
    t = np.asarray(uvs, dtype=np.float32).reshape(-1, 3, 2)
    return np.concatenate([t.min(axis=1), t.max(axis=1)], axis=1)


def triangle_screen_bboxes(coords, mvp, region_w, region_h):
    """(bboxes (n_tris, 4) [minx, miny, maxx, maxy] in region pixels,
    unprojectable (n_tris,) bool) for the soup's coords (n_tris*3, 3)
    under the 4x4 ``mvp`` (view_proj @ model, row-major like mathutils).
    Triangles are clipped in homogeneous space before the perspective divide.
    This matters when the camera intersects a triangle: projecting its three
    original vertices is undefined, but the portion the rasterizer can show
    still has a finite, viewport-bounded screen extent.  The z clip planes are
    deliberately omitted.  Keeping geometry rejected by the GPU near/far
    planes can only enlarge the dirty bound, while avoiding backend-specific
    depth conventions.  ``unprojectable`` is reserved for non-finite input;
    those triangles retain the old always-dirty safety fallback.

    Fully camera-hidden or offscreen triangles receive an empty bbox
    ``[+inf, +inf, -inf, -inf]`` and are not marked unprojectable."""
    import numpy as np
    co = np.asarray(coords, dtype=np.float32).reshape(-1, 3)
    m = np.asarray(mvp, dtype=np.float32)
    hom = co @ m[:3, :3].T + m[:3, 3]          # clip.xyz
    w = co @ m[3, :3].T + m[3, 3]              # clip.w
    clip = np.concatenate((hom, w[:, None]), axis=1).astype(np.float64)

    # Each function is >= 0 inside.  Clip w first so subsequent side-plane
    # intersections never retain a point behind the eye.  Sutherland-Hodgman
    # interpolation in clip space matches the fixed-function clipper.
    planes = (
        lambda p: p[3],
        lambda p: p[0] + p[3], lambda p: p[3] - p[0],
        lambda p: p[1] + p[3], lambda p: p[3] - p[1],
    )

    def clip_polygon(poly, distance):
        if not poly:
            return []
        result = []
        previous = poly[-1]
        previous_d = float(distance(previous))
        previous_in = previous_d >= 0.0
        for current in poly:
            current_d = float(distance(current))
            current_in = current_d >= 0.0
            if current_in != previous_in:
                denominator = previous_d - current_d
                if denominator != 0.0:
                    result.append(previous + (current - previous)
                                  * (previous_d / denominator))
            if current_in:
                result.append(current)
            previous, previous_d, previous_in = (
                current, current_d, current_in)
        return result

    triangle_count = len(clip) // 3
    bboxes = np.empty((triangle_count, 4), dtype=np.float32)
    bboxes[:] = (np.inf, np.inf, -np.inf, -np.inf)
    triangles = clip.reshape(-1, 3, 4)
    finite = np.isfinite(triangles).all(axis=(1, 2))
    unprojectable = ~finite
    distances = np.stack((triangles[:, :, 3],
                          triangles[:, :, 0] + triangles[:, :, 3],
                          triangles[:, :, 3] - triangles[:, :, 0],
                          triangles[:, :, 1] + triangles[:, :, 3],
                          triangles[:, :, 3] - triangles[:, :, 1]), axis=2)
    trivially_inside = (finite & (triangles[:, :, 3] > 0.0).all(axis=1)
                        & (distances >= 0.0).all(axis=(1, 2)))
    inside_indices = np.flatnonzero(trivially_inside)
    if len(inside_indices):
        accepted = triangles[inside_indices]
        ndc = accepted[:, :, :2] / accepted[:, :, 3, None]
        px = (ndc * 0.5 + 0.5) * (float(region_w), float(region_h))
        lo = np.maximum(px.min(axis=1) - 1.0, (0.0, 0.0))
        hi = np.minimum(px.max(axis=1) + 1.0,
                        (float(region_w), float(region_h)))
        bboxes[inside_indices] = np.concatenate((lo, hi), axis=1)
    # If all three vertices are outside the same plane, convexity guarantees
    # the triangle cannot enter the retained region.
    trivially_outside = finite & (distances < 0.0).all(axis=1).any(axis=1)
    needs_clipping = finite & ~trivially_inside & ~trivially_outside
    for triangle_index in np.flatnonzero(needs_clipping):
        vertices = triangles[triangle_index]
        polygon = list(vertices)
        for plane in planes:
            polygon = clip_polygon(polygon, plane)
            if not polygon:
                break
        if not polygon:
            continue
        polygon = np.asarray(polygon)
        positive_w = polygon[:, 3] > 0.0
        if not bool(positive_w.any()):
            continue
        # Zero-w vertices can only be the camera apex.  The side-plane-clipped
        # positive-w vertices determine the same finite viewport coverage.
        ndc = polygon[positive_w, :2] / polygon[positive_w, 3, None]
        px = (ndc * 0.5 + 0.5) * (float(region_w), float(region_h))
        # A one-pixel numerical guard keeps this CPU estimate conservative at
        # clip boundaries and under float32 GPU interpolation.
        lo = np.maximum(px.min(axis=0) - 1.0, (0.0, 0.0))
        hi = np.minimum(px.max(axis=0) + 1.0,
                        (float(region_w), float(region_h)))
        bboxes[triangle_index] = (lo[0], lo[1], hi[0], hi[1])
    return bboxes, unprojectable


def dab_rect_union(dabs, radius):
    """Screen-space bbox (minx, miny, maxx, maxy) covering every dab
    disc in ``dabs`` (iterable of (x, y, ...) tuples)."""
    xs = [d[0] for d in dabs]
    ys = [d[1] for d in dabs]
    r = float(radius)
    return (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)


def _screen_bbox_hit_indices(screen_bboxes, unprojectable, rect,
                             candidates=None):
    """Apply the authoritative inclusive bbox test to optional candidates."""
    import numpy as np
    if candidates is None:
        candidates = np.arange(len(screen_bboxes), dtype=np.int32)
    else:
        candidates = np.asarray(candidates, dtype=np.int32)
    boxes = np.asarray(screen_bboxes)[candidates]
    invalid = np.asarray(unprojectable)[candidates]
    hit = ~((boxes[:, 2] < rect[0]) | (boxes[:, 0] > rect[2])
            | (boxes[:, 3] < rect[1]) | (boxes[:, 1] > rect[3]))
    return candidates[hit | invalid]


def dirty_uv_bbox(screen_bboxes, unprojectable, uv_bboxes, rect,
                  candidates=None, hit_indices=None):
    """UV bbox (min_u, min_v, max_u, max_v) unioned over the triangles
    whose screen bbox intersects ``rect`` — plus every unprojectable
    triangle (always dirty). None when no triangle is hit."""
    import numpy as np
    indices = (np.asarray(hit_indices, dtype=np.int32)
               if hit_indices is not None else _screen_bbox_hit_indices(
                   screen_bboxes, unprojectable, rect, candidates))
    if not len(indices):
        return None
    sel = np.asarray(uv_bboxes)[indices]
    return (float(sel[:, 0].min()), float(sel[:, 1].min()),
            float(sel[:, 2].max()), float(sel[:, 3].max()))


def dirty_uv_pixel_rects(screen_bboxes, unprojectable, uv_bboxes, rect,
                         size, pad=DIRTY_RECT_PAD_PX, candidates=None,
                         hit_indices=None):
    """Conservative per-triangle texel rects intersecting a screen rect.

    Unlike :func:`dirty_uv_bbox`, this deliberately does not bridge gaps
    between scattered UV islands.  It is intended for sparse tile undo;
    session readback continues to use the cheaper union bbox.  Duplicate UV
    bounds are removed without changing their deterministic triangle order.
    """
    import numpy as np
    indices = (np.asarray(hit_indices, dtype=np.int32)
               if hit_indices is not None else _screen_bbox_hit_indices(
                   screen_bboxes, unprojectable, rect, candidates))
    result = []
    seen = set()
    for bbox in np.asarray(uv_bboxes)[indices]:
        pixel_rect = uv_bbox_to_pixel_rect(bbox, size, pad=pad)
        if pixel_rect is not None and pixel_rect not in seen:
            seen.add(pixel_rect)
            result.append(pixel_rect)
    return tuple(result)


def union_bbox(a, b):
    """Union of two (min_u, min_v, max_u, max_v) bboxes; either may be
    None (returns the other)."""
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]),
            max(a[2], b[2]), max(a[3], b[3]))


def append_sparse_pixel_rect(mapping, channel, rect):
    """Remember a destination rectangle without bridging atlas-space gaps."""
    rect = tuple(int(value) for value in rect)
    rects = mapping.setdefault(channel, [])
    if rect not in rects:
        rects.append(rect)


def sparse_pixel_rects(value):
    """Normalize legacy single rectangles and current sparse rectangle lists."""
    if value is None:
        return ()
    if (len(value) == 4
            and all(isinstance(item, (int, float)) for item in value)):
        return (tuple(int(item) for item in value),)
    return tuple(tuple(int(item) for item in rect) for rect in value)


def uv_bbox_to_pixel_rect(bbox, size, pad=DIRTY_RECT_PAD_PX):
    """Clamped integer texel rect (x, y, w, h) for a UV bbox on a
    size x size texture, padded by ``pad`` texels; None for a None/
    degenerate/out-of-range bbox."""
    if bbox is None:
        return None
    x0 = max(0, int(math.floor(bbox[0] * size)) - pad)
    y0 = max(0, int(math.floor(bbox[1] * size)) - pad)
    x1 = min(size, int(math.ceil(bbox[2] * size)) + pad)
    y1 = min(size, int(math.ceil(bbox[3] * size)) + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def dab_dirty_pixel_rects(screen_bboxes, unprojectable, uv_bboxes, dabs,
                          radius, size, sample_pad=0):
    """Return one conservative texture-space work rect per dab."""
    pad = DIRTY_RECT_PAD_PX + max(0, int(math.ceil(sample_pad)))
    return tuple(
        uv_bbox_to_pixel_rect(
            dirty_uv_bbox(screen_bboxes, unprojectable, uv_bboxes,
                          dab_rect_union((dab,), radius)),
            size, pad=pad)
        for dab in dabs)


def detailed_dab_work_rects(brush_mode, screen_bboxes, unprojectable,
                            uv_bboxes, dabs, radius, size):
    """Compute per-dab UV work only for neighbour-sampling brushes."""
    mode = str(brush_mode or "PAINT")
    if mode not in {"SOFTEN", "SMEAR"}:
        return None
    sample_pad = 1.0 if mode == "SOFTEN" else radius * 0.35
    return dab_dirty_pixel_rects(
        screen_bboxes, unprojectable, uv_bboxes, dabs, radius, size,
        sample_pad)


# ---------------------------------------------------------------------------
# Buffer -> numpy conversion ladder (stroke-end readback)
#
# The 0.1.0 sync-back converted the readback Buffer with a bare
# np.asarray(buf) and measured ~1050 ms at 4K (GUI, Quadro RTX 5000):
# when the exporter offers neither __array_interface__ nor a usable
# C buffer protocol, numpy silently degrades to element-wise sequence
# iteration over 16.7M Python floats. The ladder below tries the
# zero-copy mechanisms EXPLICITLY (so a "works but slow" path can never
# be selected) and falls back to the always-correct to_list. Which rung
# a given Blender build/backend supports is unknowable headless (Buffer
# creation needs a GPU context), so probe_buffer_to_numpy_path picks
# the rung at session start on a small real Buffer with known contents,
# and the choice is logged as GPU_PAINT_SPIKE_PROBE
# buffer_to_numpy_path=<rung>. The rung functions themselves are pure
# and headless-testable with stand-in objects.
# ---------------------------------------------------------------------------


def _conv_asarray(buf):
    """Zero-copy via numpy's __array_interface__. Gated on the
    attribute: a bare np.asarray(buf) would silently 'succeed' through
    the element-wise sequence protocol on objects without it — the
    exact ~1 s/stroke trap measured at 4K."""
    import numpy as np
    if not hasattr(buf, "__array_interface__"):
        raise TypeError("no __array_interface__")
    return np.asarray(buf)


def _conv_frombuffer(buf):
    """Zero-copy via the C buffer protocol (PEP 3118). Raises if the
    object is not a buffer exporter. float32 by contract: the readback
    is always fb.read_color(..., 'FLOAT')."""
    import numpy as np
    return np.frombuffer(buf, dtype=np.float32)


def _conv_memoryview(buf):
    """Zero-copy via an explicit memoryview flattened to bytes; can
    succeed where np.frombuffer's stricter buffer request fails (e.g.
    exporters that only serve multi-dimensional views)."""
    import numpy as np
    return np.frombuffer(memoryview(buf).cast('B'), dtype=np.float32)


def _conv_to_list(buf):
    """Always-correct fallback: element-wise Python conversion. SLOW —
    order of 1-2 s for a 4K RGBA float buffer (measured: 1858 ms for a
    16.7M-element list -> np.asarray, headless, this machine)."""
    import numpy as np
    return np.asarray(buf.to_list(), dtype=np.float32)


# Fastest first; the last rung must be the always-correct fallback.
BUFFER_TO_NUMPY_LADDER = (
    ("asarray", _conv_asarray),
    ("frombuffer", _conv_frombuffer),
    ("memoryview", _conv_memoryview),
    ("to_list_fallback", _conv_to_list),
)


def probe_buffer_to_numpy_path(buf, reference):
    """Name of the first BUFFER_TO_NUMPY_LADDER rung that converts
    ``buf`` into values matching ``reference`` (flat float32). Meant to
    run ONCE per session on a small Buffer whose contents are known
    (reference comes from the trusted-but-slow to_list). Pure logic —
    headless tests exercise it with stand-in objects."""
    import numpy as np
    ref = np.asarray(reference, dtype=np.float32).ravel()
    for name, conv in BUFFER_TO_NUMPY_LADDER:
        try:
            arr = np.asarray(conv(buf), dtype=np.float32).ravel()
        except Exception:
            continue
        if arr.size == ref.size and np.allclose(arr, ref, atol=1e-3):
            return name
    return "to_list_fallback"


def buffer_to_numpy(buf, path="to_list_fallback"):
    """Flat float32 numpy array from a float Buffer, converted through
    the ladder rung named ``path`` (as chosen by
    probe_buffer_to_numpy_path). Unknown names or a failing rung fall
    back to the always-correct to_list — slow but never wrong."""
    import numpy as np
    conv = dict(BUFFER_TO_NUMPY_LADDER).get(path)
    if conv is not None:
        try:
            return np.asarray(conv(buf), dtype=np.float32).ravel()
        except Exception:
            pass
    return _conv_to_list(buf).ravel()


# Latched by _probe_capabilities (GUI only; headless-safe defaults):
# which conversion rung _finalize_stroke_gpu uses, and whether
# fb.read_color can fill a Buffer wrapping numpy memory directly
# (which makes the conversion step disappear entirely).
if "_buffer_numpy_path" not in globals():
    _buffer_numpy_path = "to_list_fallback"
if "_read_into_numpy" not in globals():
    _read_into_numpy = False

# Keep backend-stable probe results across ordinary importlib.reload() calls.
# Blender reloads an add-on in the existing module dictionary, so guarding the
# initialization is enough to retain this small, process-local cache.  Entries
# are keyed by the complete GPU identity and never persisted to disk.
if "_capability_probe_cache" not in globals():
    _capability_probe_cache = {}
_CAPABILITY_PROBE_CACHE_SCHEMA = 1


# ---------------------------------------------------------------------------
# Shader create-infos (descriptor population is pure bookkeeping and
# works in --background — probed on 5.1.2; only create_from_info touches
# the GPU. The headless suite builds these structurally.)
# ---------------------------------------------------------------------------


def dab_shader_create_info(channels=1, additive=False, profile_slots=None,
                           profile_enabled=None):
    if channels > MAX_CHANNELS:
        raise ValueError("channels > MAX_CHANNELS")
    iface = gpu.types.GPUStageInterfaceInfo("gpu_paint_spike_dab_iface")
    iface.smooth('VEC3', "worldPos")
    iface.smooth('VEC2', "paintUV")
    info = gpu.types.GPUShaderCreateInfo()
    info.typedef_source(DAB_UBO_TYPEDEF)
    info.uniform_buf(DAB_UBO_SLOT, "ImpastoDabParams", DAB_UBO_NAME)
    info.sampler(0, 'FLOAT_2D', "scene_depth_tex")
    info.sampler(1, 'FLOAT_2D', "stencil_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    for i in range(1, channels):
        info.fragment_out(i, 'VEC4', "fragColor%d" % i)
    info.vertex_source(DAB_VERT_SRC)
    info.fragment_source(visibility.GLSL_SOURCE
                         + dab_frag_src(channels, additive=additive,
                                       profile_slots=profile_slots,
                                       profile_enabled=profile_enabled))
    return info


def seam_coverage_shader_create_info():
    """Single-channel dab coverage using the exact paint visibility mask."""
    iface = gpu.types.GPUStageInterfaceInfo("impasto_seam_coverage_iface")
    iface.smooth('VEC3', "worldPos")
    iface.smooth('VEC2', "paintUV")
    info = gpu.types.GPUShaderCreateInfo()
    info.typedef_source(DAB_UBO_TYPEDEF)
    info.uniform_buf(DAB_UBO_SLOT, "ImpastoDabParams", DAB_UBO_NAME)
    info.sampler(0, 'FLOAT_2D', "scene_depth_tex")
    info.sampler(1, 'FLOAT_2D', "stencil_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(DAB_VERT_SRC)
    info.fragment_source(visibility.GLSL_SOURCE + SEAM_COVERAGE_FRAG_SRC)
    return info


def seam_boundary_shader_create_info(channels=1, additive=False,
                                     profile_slots=None):
    iface = gpu.types.GPUStageInterfaceInfo("impasto_seam_boundary_iface")
    iface.smooth('VEC3', "worldPos")
    iface.smooth('VEC2', "paintUV")
    info = gpu.types.GPUShaderCreateInfo()
    info.typedef_source(DAB_UBO_TYPEDEF)
    info.uniform_buf(DAB_UBO_SLOT, "ImpastoDabParams", DAB_UBO_NAME)
    info.sampler(0, 'FLOAT_2D', "scene_depth_tex")
    info.sampler(1, 'FLOAT_2D', "stencil_tex")
    info.sampler(2, 'FLOAT_2D', "coverage_tex")
    info.sampler(3, 'FLOAT_2D', "interior_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    for index in range(channels):
        info.fragment_out(index, 'VEC4',
                          "fragColor" if index == 0 else "fragColor%d" % index)
    info.vertex_source(DAB_VERT_SRC)
    info.fragment_source(visibility.GLSL_SOURCE + dab_frag_src(
        channels, additive=additive, profile_slots=profile_slots,
        coverage_guard=True))
    return info


SEAM_INTERIOR_VERT_SRC = """
void main() { gl_Position = vec4(uv * 2.0 - 1.0, 0.0, 1.0); }
"""
SEAM_INTERIOR_FRAG_SRC = """
void main() { fragColor = vec4(1.0); }
"""


def seam_interior_shader_create_info():
    info = gpu.types.GPUShaderCreateInfo()
    info.vertex_in(0, 'VEC2', "uv")
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(SEAM_INTERIOR_VERT_SRC)
    info.fragment_source(SEAM_INTERIOR_FRAG_SRC)
    return info


def seam_transfer_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("impasto_seam_transfer_iface")
    iface.smooth('VEC2', "destinationUV")
    iface.smooth('VEC2', "sourceUV")
    info = gpu.types.GPUShaderCreateInfo()
    info.sampler(0, 'FLOAT_2D', "source_pixels")
    info.sampler(1, 'FLOAT_2D', "coverage_tex")
    info.vertex_in(0, 'VEC2', "destination_uv")
    info.vertex_in(1, 'VEC2', "source_uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(SEAM_TRANSFER_VERT_SRC)
    info.fragment_source(SEAM_TRANSFER_FRAG_SRC)
    return info


def soften_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("impasto_soften_dab_iface")
    iface.smooth('VEC3', "worldPos")
    iface.smooth('VEC2', "paintUV")
    info = gpu.types.GPUShaderCreateInfo()
    info.typedef_source(DAB_UBO_TYPEDEF)
    info.uniform_buf(DAB_UBO_SLOT, "ImpastoDabParams", DAB_UBO_NAME)
    info.sampler(0, 'FLOAT_2D', "scene_depth_tex")
    info.sampler(1, 'FLOAT_2D', "stencil_tex")
    info.sampler(2, 'FLOAT_2D', "source_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(DAB_VERT_SRC)
    info.fragment_source(visibility.GLSL_SOURCE + soften_frag_src())
    return info


def smear_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("impasto_smear_dab_iface")
    iface.smooth('VEC3', "worldPos")
    iface.smooth('VEC2', "paintUV")
    info = gpu.types.GPUShaderCreateInfo()
    info.typedef_source(DAB_UBO_TYPEDEF)
    info.uniform_buf(DAB_UBO_SLOT, "ImpastoDabParams", DAB_UBO_NAME)
    info.sampler(0, 'FLOAT_2D', "scene_depth_tex")
    info.sampler(1, 'FLOAT_2D', "stencil_tex")
    info.sampler(2, 'FLOAT_2D', "source_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(DAB_VERT_SRC)
    info.fragment_source(visibility.GLSL_SOURCE + smear_frag_src())
    return info


def stencil_preview_shader_create_info():
    """POST_PIXEL image shader for both stencil projection previews."""
    iface = gpu.types.GPUStageInterfaceInfo("impasto_stencil_preview_iface")
    iface.smooth('VEC2', "stencilUV")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('FLOAT', "stencil_preview_opacity")
    info.sampler(0, 'FLOAT_2D', "stencil_preview_tex")
    info.vertex_in(0, 'VEC2', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(STENCIL_PREVIEW_VERT_SRC)
    info.fragment_source(STENCIL_PREVIEW_FRAG_SRC)
    return info


def dab_uniform_data(model_matrix, view_proj_matrix, view_depth_plane,
                     region_size, brush_center, radius, hardness,
                     depth_epsilon, depth_relative_epsilon, occlusion,
                     pressure, use_stencil, stencil_projection,
                     stencil_interpretation, stencil_opacity,
                     stencil_position, stencil_scale, stencil_rotation,
                     brush_values, profile_usage=False,
                     profile_strength=1.0, profile_invert=False,
                     erase=False, data=None):
    """Pack dab state into the all-vec4 std140 UBO layout.

    Matrices are transposed before flattening because mathutils/numpy expose
    rows while GLSL's default matrix storage is column-major.  Integer-like
    switches are floats intentionally, keeping the whole buffer padding-free.
    """
    import numpy as np
    if data is None:
        data = np.zeros((DAB_UBO_VEC4_COUNT, 4), dtype=np.float32)
    else:
        data.fill(0.0)
    data[DAB_UBO_MODEL:DAB_UBO_MODEL + 4] = np.asarray(
        model_matrix, dtype=np.float32).T
    data[DAB_UBO_VIEW_PROJ:DAB_UBO_VIEW_PROJ + 4] = np.asarray(
        view_proj_matrix, dtype=np.float32).T
    data[DAB_UBO_VIEW_DEPTH] = tuple(view_depth_plane)
    data[DAB_UBO_REGION_CENTER] = (
        float(region_size[0]), float(region_size[1]),
        float(brush_center[0]), float(brush_center[1]))
    data[DAB_UBO_BRUSH_DEPTH] = (
        float(radius), float(hardness), float(depth_epsilon),
        float(depth_relative_epsilon))
    data[DAB_UBO_PAINT_FLAGS] = (
        1.0 if occlusion else 0.0, float(pressure),
        1.0 if use_stencil else 0.0,
        1.0 if stencil_projection else 0.0)
    data[DAB_UBO_STENCIL_FLAGS] = (
        1.0 if stencil_interpretation else 0.0,
        float(stencil_opacity), float(stencil_rotation), 0.0)
    data[DAB_UBO_STENCIL_TRANSFORM] = (
        float(stencil_position[0]), float(stencil_position[1]),
        float(stencil_scale[0]), float(stencil_scale[1]))
    data[DAB_UBO_PROFILE_FLAGS] = (
        1.0 if profile_usage else 0.0, float(profile_strength),
        1.0 if profile_invert else 0.0, 1.0 if erase else 0.0)
    for index, value in enumerate(brush_values[:MAX_CHANNELS]):
        data[DAB_UBO_BRUSH_VALUES + index] = value
    return data


def prepass_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("gpu_paint_spike_prepass_iface")
    iface.smooth('FLOAT', "viewDepth")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', "model_matrix")
    info.push_constant('MAT4', "view_proj_matrix")
    info.push_constant('VEC4', "view_depth_plane")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(PREPASS_VERT_SRC)
    info.fragment_source(PREPASS_FRAG_SRC)
    return info


def preview_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("gpu_paint_spike_preview_iface")
    iface.smooth('VEC2', "uvInterp")
    iface.smooth('VEC2', "baseNormalUV")
    iface.smooth('VEC3', "worldPos")
    iface.smooth('FLOAT', "viewDepth")
    iface.smooth('VEC3', "surfaceNormal")
    info = gpu.types.GPUShaderCreateInfo()
    info.typedef_source(PREVIEW_UBO_TYPEDEF)
    info.uniform_buf(PREVIEW_UBO_SLOT, "ImpastoPreviewParams",
                     PREVIEW_UBO_NAME)
    info.sampler(0, 'FLOAT_2D', "base_color_tex")
    info.sampler(1, 'FLOAT_2D', "metallic_tex")
    info.sampler(2, 'FLOAT_2D', "roughness_tex")
    info.sampler(3, 'FLOAT_2D', "normal_tex")
    info.sampler(4, 'FLOAT_2D', "height_tex")
    info.sampler(5, 'FLOAT_2D', "environment_atlas")
    for index, name in enumerate(("base_color", "metallic", "roughness",
                                  "normal", "height"), 6):
        info.sampler(index, 'FLOAT_2D', "baseline_" + name + "_tex")
    for index, name in enumerate(("emission_color", "emission_strength",
                                  "sss_weight", "sss_radius", "sss_scale"),
                                 11):
        info.sampler(index, 'FLOAT_2D', name + "_tex")
    for index, name in enumerate(("emission_color", "emission_strength",
                                  "sss_weight", "sss_radius", "sss_scale"),
                                 16):
        info.sampler(index, 'FLOAT_2D', "baseline_" + name + "_tex")
    info.sampler(21, 'FLOAT_2D', "base_normal_tex")
    upper_names = tuple(key for key in GPU_PAINT_CHANNEL_KEYS
                        if key != "normal")
    for index, name in enumerate(upper_names, 22):
        info.sampler(index, 'FLOAT_2D', "upper_" + name + "_tex")
    info.sampler(22 + len(upper_names), 'FLOAT_2D', "occluder_depth_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_in(2, 'VEC3', "normal")
    info.vertex_in(3, 'VEC2', "base_uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(_preview_ubo_aliases() + PREVIEW_VERT_SRC)
    info.fragment_source(_preview_ubo_aliases() + PREVIEW_FRAG_SRC)
    return info


def copy_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("impasto_tile_copy_iface")
    iface.smooth('VEC2', "uvInterp")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('VEC2', "uv_origin")
    info.push_constant('VEC2', "uv_scale")
    info.sampler(0, 'FLOAT_2D', "source_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(COPY_VERT_SRC)
    info.fragment_source(COPY_FRAG_SRC)
    return info


def baseline_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("impasto_baseline_iface")
    iface.smooth('VEC2', "uvInterp")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('VEC2', "uv_origin")
    info.push_constant('VEC2', "uv_scale")
    info.push_constant('VEC4', "source_value")
    info.push_constant('FLOAT', "source_is_texture")
    info.push_constant('FLOAT', "source_uses_alpha")
    info.push_constant('FLOAT', "factor")
    info.push_constant('FLOAT', "is_normal")
    info.push_constant('INT', "blend_mode")
    info.sampler(0, 'FLOAT_2D', "current_tex")
    info.sampler(1, 'FLOAT_2D', "source_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(COPY_VERT_SRC)
    info.fragment_source(BASELINE_FRAG_SRC)
    return info


def upper_transform_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("impasto_upper_transform_iface")
    iface.smooth('VEC2', "uvInterp")
    info = gpu.types.GPUShaderCreateInfo()
    for kind, name in (('VEC2', "uv_origin"), ('VEC2', "uv_scale"),
                       ('VEC4', "source_value")):
        info.push_constant(kind, name)
    for name in ("source_is_texture", "source_uses_alpha", "factor",
                 "mask_present", "mask_invert", "mask_opacity"):
        info.push_constant('FLOAT', name)
    info.push_constant('INT', "blend_mode")
    info.sampler(0, 'FLOAT_2D', "current_tex")
    info.sampler(1, 'FLOAT_2D', "source_tex")
    info.sampler(2, 'FLOAT_2D', "mask_tex")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(COPY_VERT_SRC)
    info.fragment_source(UPPER_TRANSFORM_FRAG_SRC)
    return info


def upper_reproject_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("impasto_upper_reproject_iface")
    iface.smooth('VEC2', "targetUvInterp")
    iface.smooth('VEC2', "sourceUvInterp")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('FLOAT', "source_uses_alpha")
    info.push_constant('FLOAT', "factor")
    info.push_constant('INT', "blend_mode")
    info.sampler(0, 'FLOAT_2D', "current_tex")
    info.sampler(1, 'FLOAT_2D', "source_tex")
    info.vertex_in(0, 'VEC2', "target_uv")
    info.vertex_in(1, 'VEC2', "source_uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(UPPER_REPROJECT_VERT_SRC)
    info.fragment_source(UPPER_REPROJECT_FRAG_SRC)
    return info


# ---------------------------------------------------------------------------
# Stroke payload planning (pure; headless-testable)
#
# One logical Impasto Paint layer deposits into several channels per
# stroke. Each channel's payload carries the value the dab shader
# writes into that channel's MRT attachment plus the blend class the
# batch planner groups by: material channels alpha-blend (MIX), Height
# accumulates (ADD — RAISE adds, LOWER subtracts, both around the
# neutral 0.5 canvas). COLOR-kind channels are stored sRGB-encoded in
# their Image datablocks, and the GPU path writes raw stored values,
# so their brush values are encoded here; scalar/Non-Color channels
# pass through raw.
# ---------------------------------------------------------------------------

# Channels the multi-channel brush can deposit into, in registry order.
# Other channels (emission, subsurface, ...) stay native-paint-only.
GPU_PAINT_CHANNEL_KEYS = channel_paint.PAINTABLE_CHANNEL_KEYS

_BLEND_INDEX = {
    "MIX": 0, "ADD": 1, "SUBTRACT": 2, "MULTIPLY": 3,
    "SCREEN": 4, "OVERLAY": 5,
}


def resident_stack_runtime_spec(stack_model, active_uid):
    """Pure lower-stack plan consumed by the live GPU compositor.

    The first implementation deliberately resolves the common, predictable
    case: one UV domain, no image masks, and the static layers below the
    resident Paint layer.  Unsupported topology is reported explicitly rather
    than silently sampling the wrong coordinates.  Upper layers remain a
    labelled approximation until their affine/nonlinear passes are resident.
    """
    try:
        plan = preview_stack.plan_resident_preview(
            stack_model, active_uid, GPU_PAINT_CHANNEL_KEYS)
    except ValueError as exc:
        return {"enabled": False, "status": "disabled: %s" % exc,
                "channels": {}}
    active = next(layer for layer in stack_model.layers
                  if layer.uid == active_uid)
    by_uid = {layer.uid: layer for layer in stack_model.layers}
    relevant_uids = (set(plan.lower_layer_uids)
                     | set(plan.upper_layer_uids) | {active_uid})
    unsupported_masks = [
        layer.uid for layer in stack_model.layers
        if layer.uid in relevant_uids
        and any(m.visible and m.image_name for m in layer.masks)
        and (layer.uid not in plan.upper_layer_uids
             or len([m for m in layer.masks
                     if m.visible and m.image_name]) > 1)]
    if unsupported_masks:
        return {"enabled": False,
                "status": "fallback: image masks require material preview",
                "channels": {}, "plan": plan,
                "safe_fallback": "MATERIAL_INSPECT"}
    composition = tuple(reversed(stack_model.layers))
    active_i = composition.index(active)
    # Lower baselines are still composed as texture-space full-screen passes,
    # so their image-bearing layers must share the active UV. Upper images
    # can use named alternative maps: the runtime reprojects those through
    # the mesh into the active canvas before the live preview samples them.
    active_uv = str(active.uv_map or "")
    lower_uvs = {active_uv}
    for layer in composition[:active_i]:
        if layer.uid not in plan.lower_layer_uids:
            continue
        for binding in layer.bindings:
            if (binding.enabled and binding.key in GPU_PAINT_CHANNEL_KEYS
                    and layer.layer_type == "PAINT"
                    and model.binding_image(layer, binding)):
                lower_uvs.add(str(layer.uv_map or ""))
    if len(lower_uvs) > 1:
        return {"enabled": False,
                "status": ("fallback: lower layers do not share the active "
                           "resolved UV map"),
                "channels": {}, "plan": plan,
                "safe_fallback": "MATERIAL_INSPECT"}

    channels = {}
    for key in GPU_PAINT_CHANNEL_KEYS:
        channel = model.CHANNEL_MAP[key]
        active_binding = next(
            (b for b in active.bindings if b.key == key), None)
        active_spec = None
        if (active_binding is not None and active_binding.enabled
                and active.visible
                and all(g.visible for g in model._ancestors(
                    stack_model, active))):
            active_spec = {
                "factor": model.const_factor(
                    stack_model, active, active_binding),
                "blend": model.effective_blend(active, active_binding),
                "use_alpha": True,
                "image_name": model.binding_image(active, active_binding),
            }
        # Sparse active layers do not interrupt unrelated channels. Those
        # channels are wholly static and can include every visible layer.
        static_layers = (composition if active_spec is None
                         else composition[:active_i])
        steps = []
        for layer in static_layers:
            if layer.uid == active_uid:
                continue
            binding = next((b for b in layer.bindings if b.key == key), None)
            if (binding is None or layer.layer_type == "GROUP"
                    or not binding.enabled or not layer.visible
                    or any(not g.visible for g in model._ancestors(
                        stack_model, layer))):
                continue
            image_name = model.binding_image(layer, binding)
            if (binding.mode == "SHARED" and layer.layer_type == "PAINT"
                    and image_name):
                # Kiln bake targets predate this invariant and may already be
                # stored in files with use_masks=True.  Their alpha is not
                # authoritative coverage, so keep existing imported baselines
                # visible without requiring a destructive stack migration.
                opaque_kiln_normal = (
                    key == "normal" and layer.label == "Kiln Baked Normal")
                source = {"kind": "IMAGE", "image_name": image_name,
                          "use_alpha": (bool(binding.use_masks)
                                        and not opaque_kiln_normal)}
            elif binding.mode == "COLOR":
                value = (float(binding.color[0]) if channel.kind == "SCALAR"
                         else tuple(float(x) for x in binding.color))
                source = {"kind": "CONSTANT", "value": value}
            elif binding.mode == "VALUE":
                value = float(binding.value)
                if channel.kind != "SCALAR":
                    value = (value, value, value, 1.0)
                source = {"kind": "CONSTANT", "value": value}
            else:
                source = {"kind": "CONSTANT",
                          "value": model.seed_native(channel)}
            steps.append({
                "layer_uid": layer.uid,
                "source": source,
                "factor": model.const_factor(stack_model, layer, binding),
                "blend": model.effective_blend(layer, binding),
            })
        channels[key] = {
            "seed": model.seed_native(channel),
            "lower_steps": tuple(steps),
            "active": active_spec,
            "upper_affine": (_vec4(1.0), _vec4(0.0)),
            "upper_steps": [],
        }
    unsupported = []
    for layer in composition[active_i + 1:]:
        if (not layer.visible or layer.layer_type == "GROUP"
                or any(not g.visible for g in model._ancestors(
                    stack_model, layer))):
            continue
        for binding in layer.bindings:
            if (binding.enabled and binding.key in channels
                    and channels[binding.key]["active"] is None
                    and layer.layer_type == "PAINT"
                    and model.binding_image(layer, binding)
                    and str(layer.uv_map or "") != active_uv):
                # With no resident active value this channel is folded wholly
                # into the static baseline, whose pass is still same-UV.
                unsupported.append((layer.uid, binding.key))
                continue
            if (binding.enabled and binding.key in channels
                    and channels[binding.key]["active"] is not None):
                key = binding.key
                blend = model.effective_blend(layer, binding)
                image_name = model.binding_image(layer, binding)
                if not preview_stack.affine_upper_channel_supported(
                        key, blend):
                    unsupported.append((layer.uid, key))
                    continue
                channel = model.CHANNEL_MAP[key]
                if (blend in {"MULTIPLY", "SCREEN"}
                        and channel.kind != "SCALAR"):
                    unsupported.append((layer.uid, key))
                    continue
                if layer.layer_type == "PAINT" and image_name:
                    masks = [m for m in layer.masks
                             if binding.use_masks and m.visible
                             and m.image_name]
                    if (masks and (
                            len(masks) > 1
                            or str(masks[0].uv_map or layer.uv_map or "")
                            != active_uv)):
                        unsupported.append((layer.uid, key))
                        continue
                    image_spec = {
                        "kind": "IMAGE",
                        "image_name": image_name,
                        "uv_map": str(layer.uv_map or ""),
                        "factor": model.const_factor(
                            stack_model, layer, binding),
                        "blend": blend,
                        "use_alpha": True,
                    }
                    if masks:
                        image_spec["mask"] = {
                            "image_name": masks[0].image_name,
                            "invert": bool(masks[0].invert),
                            "opacity": float(masks[0].opacity)}
                    channels[key]["upper_steps"].append(image_spec)
                    continue
                if binding.mode == "COLOR":
                    value = (float(binding.color[0])
                             if channel.kind == "SCALAR"
                             else tuple(float(x) for x in binding.color))
                elif binding.mode == "VALUE":
                    value = float(binding.value)
                    if channel.kind != "SCALAR":
                        value = (value, value, value, 1.0)
                else:
                    unsupported.append((layer.uid, key))
                    continue
                factor = model.const_factor(stack_model, layer, binding)
                masks = [m for m in layer.masks
                         if binding.use_masks and m.visible and m.image_name]
                if (masks and (
                        len(masks) > 1
                        or str(masks[0].uv_map or layer.uv_map or "")
                        != active_uv)):
                    unsupported.append((layer.uid, key))
                    continue
                channels[key]["upper_steps"].append({
                    "kind": "CONSTANT", "value": value,
                    "factor": factor, "blend": blend,
                    "mask": ({
                        "image_name": masks[0].image_name,
                        "invert": bool(masks[0].invert),
                        "opacity": float(masks[0].opacity)}
                        if masks else None)})
                old_c, old_d = channels[key]["upper_affine"]
                c, d = preview_stack.affine_coefficients(
                    _vec4(value), factor, blend)
                channels[key]["upper_affine"] = (
                    tuple(a * b for a, b in zip(_vec4(c), old_c)),
                    tuple(a * b + e for a, b, e in
                          zip(_vec4(c), old_d, _vec4(d))))
    if unsupported:
        return {
            "enabled": False,
            "status": ("fallback: upper layers affect resident channels "
                       "(live post-pass required)"),
            "channels": channels, "plan": plan,
            "safe_fallback": "MATERIAL_INSPECT",
            "unsupported_upper": tuple(unsupported),
        }
    status = "resolved: same-UV full visible stack + active isolation"
    return {"enabled": True, "status": status, "channels": channels,
            "plan": plan, "active_uv_map": active_uv}


def linear_to_srgb(v):
    """Scene-linear component -> sRGB-encoded component (IEC 61966-2-1).
    COLOR-kind canvases store encoded values; painting the raw linear
    swatch would render brighter than picked."""
    return channel_paint.linear_to_srgb(v)


def premultiply_canvas(arr):
    """Straight-alpha canvas pixels -> the PREMULTIPLIED space the MIX
    dab framebuffer accumulates in. In place; ``arr`` is float32 RGBA
    (flat or shaped); returns ``arr``.

    ``gpu.state.blend_set('ALPHA')`` is source-over with the destination
    held in premultiplied alpha (rgb: SRC_ALPHA / ONE_MINUS_SRC_ALPHA,
    a: ONE / ONE_MINUS_SRC_ALPHA), so every accumulated texel stores
    ``(value*coverage, coverage)``.  Image canvases are STRAIGHT alpha —
    the compiled node chains read the RGB as the channel VALUE and gate
    the mix by the alpha — so both boundaries must convert: premultiply
    on upload (here), divide on readback (unpremultiply_readback).
    Skipping the conversions was the v0.4 regression: soft brush rims
    and tablet pressure (any texel with a < 1) synced ``value*a`` back
    into the canvas, breaking Metallic/Roughness levels and decoding
    Tangent Normal rims into garbage directions in Material Preview.
    """
    view = arr.reshape(-1, 4)
    view[:, :3] *= view[:, 3:4]
    return arr


def prepare_canvas_upload(arr, opaque=False):
    """Prepare straight Image pixels for a resident MIX attachment.

    Imported authoritative canvases such as Kiln normals can carry
    non-authoritative zero alpha. When used as the active canvas, establish
    opaque coverage before premultiplication so their RGB survives upload.
    """
    if opaque:
        arr.reshape(-1, 4)[:, 3] = 1.0
    return premultiply_canvas(arr)


def unpremultiply_readback(arr):
    """Premultiplied MIX-framebuffer pixels -> straight-alpha canvas
    pixels. Returns a new float32 copy (the session's CPU mirror must
    stay in framebuffer space); a<=0 texels get rgb=0 (transparent —
    the compiled chains ignore their value)."""
    import numpy as np
    out = np.array(arr, dtype=np.float32, copy=True)
    view = out.reshape(-1, 4)
    alpha = view[:, 3:4]
    covered = alpha > 1e-8
    np.divide(view[:, :3], alpha, out=view[:, :3], where=covered)
    view[:, :3] *= covered
    return out


def stroke_payloads(channel_keys, brush):
    """MRT payload per channel key, aligned with ``channel_keys``.

    ``brush`` is a plain dict of the layer's brush properties:
    ``color`` (linear RGB), ``roughness``, ``metallic``, ``normal``
    (encoded RGB), ``height_strength``, ``height_direction``
    ('RAISE'|'LOWER'), optional ``strength`` (dab alpha, default 1.0).
    Pure — the operator snapshots PropertyGroups into the dict."""
    return channel_paint.resident_payloads(channel_keys, brush)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

MAX_FB_CHANNELS = 4


def plan_target_batches(payloads, max_slots=MAX_FB_CHANNELS):
    """Group equal-blend targets into framebuffer-sized batches.

    Blender's Python GPU state exposes one blend mode for the whole MRT
    framebuffer. MIX material channels and signed ADD Height therefore render
    in separate passes while consuming the exact same queued dabs.
    """
    groups = []
    for blend in ("MIX", "ADD"):
        indices = [i for i, item in enumerate(payloads)
                   if item.get("blend", "MIX") == blend]
        for start in range(0, len(indices), max_slots):
            groups.append((blend, tuple(indices[start:start + max_slots])))
    known = {i for _blend, indices in groups for i in indices}
    extra = [i for i in range(len(payloads)) if i not in known]
    for start in range(0, len(extra), max_slots):
        groups.append(("MIX", tuple(extra[start:start + max_slots])))
    return tuple(groups)


class _Session:
    def __init__(self, obj_name, image_names, size, region_ptr, channels=1,
                 payloads=None, settings=None):
        self.obj_name = obj_name
        # One Image datablock per channel (channel 0 first). Extra
        # channels beyond the provided images simply never sync.
        self.image_names = list(image_names)
        self.image_name = self.image_names[0] if self.image_names else ""
        self.size = size
        self.region_ptr = region_ptr
        self.channels = max(1, int(channels))
        self.payloads = list(payloads or [
            {"value": (0.8, 0.2, 0.1), "strength": 1.0,
             "blend": "MIX"} for _ in range(self.channels)])
        while len(self.payloads) < self.channels:
            self.payloads.append({"value": (0.0, 0.0, 0.0),
                                  "strength": 1.0, "blend": "MIX"})
        self.payloads = self.payloads[:self.channels]
        self.settings = dict(settings or {})
        self.target_batches = plan_target_batches(self.payloads)

        # Pure geometry (built eagerly at start — no gpu involved).
        self.coords = None
        self.uvs = None
        self.normals = None
        self.tri_uv_bboxes = None      # (n_tris, 4), pure numpy

        # GPU resources — ALL lazy, created at first draw.
        self.dab_shaders = None
        self.dab_ubos = None
        self.dab_ubo_data = None
        self.soften_shader = None
        self.smear_shader = None
        self.soften_ubo = None
        self.soften_ubo_data = None
        self.soften_scratch = None
        self.soften_scratch_fb = None
        self.batch_soften = None
        self.batch_smear = None
        self.smear_last_point = None
        self.prepass_shader = None
        self.preview_shader = None
        self.preview_ubo = None
        self.preview_ubo_data = None
        self.paint_texs = None         # list of N RGBA16F GPUTextures
        self.paint_fbs = None
        self.depth_color_tex = None    # R32F: prepass NDC depth
        self.depth_depth_tex = None    # DEPTH_COMPONENT32F: z-testing
        self.depth_fb = None
        self.depth_fb_size = None      # (w, h) the depth FB was built at
        self.occluder_tex = None       # R32F: other-mesh linear view depth
        self.occluder_depth_tex = None
        self.occluder_fb = None
        self.occluder_batches = None   # ((name, batch), ...)
        self.occluder_mesh_signature = None
        self.batch_dabs = None         # one pos+uv batch per shader
        self.batch_prepass = None      # pos only
        self.batch_preview = None      # pos + uv, composed surface overlay
        self.neutral_tex = None        # 1x1 fallback for absent channels
        self.copy_shader = None
        self.copy_batch = None
        self.single_fbs = None
        self.environment_tex = None
        self.base_normal_tex = None
        self.base_normal_gpu_ref = None
        self.base_normal_uvs = None
        self.base_normal_resources_dirty = False
        self.stencil_tex = None
        self.stencil_image_name = ""
        self.stencil_preview_shader = None
        self.stack_spec = self.settings.get("stack_spec")
        self.baseline_shader = None
        self.baseline_batch = None
        self.baseline_texs = {}
        self.baseline_values = {}
        self.baseline_gpu_refs = []
        self.upper_transform_shader = None
        self.upper_transform_batch = None
        self.upper_reproject_shader = None
        self.upper_reproject_batches = {}
        self.upper_transform_texs = {}
        self.upper_transform_gpu_refs = []
        self.active_preview_texs = {}
        self.active_preview_gpu_refs = []
        self.baseline_build_ms = 0.0
        self.environment_build_ms = 0.0
        self.preview_submit_ms = 0.0
        self.preview_submit_avg_ms = 0.0
        self.preview_submit_count = 0
        self.hover_stats = HoverTelemetry()
        self.gpu_ready = False
        self.probe_lines = None
        self.overlay_circle_batch = None
        self.overlay_color_shader = None
        self.gutter_uvs = None
        self.gutter_offset_map = None
        self.gutter_map_key = None
        self.gutter_diagnostics = None
        self.gutter_apply_shader = None
        self.gutter_apply_batch = None
        self.gutter_apply_ms = 0.0
        self.seam_correspondence = None
        self.seam_channel_keys = ()
        self.seam_coverage_shader = None
        self.seam_coverage_tex = None
        self.seam_coverage_fb = None
        self.batch_seam_coverage = None
        self.seam_coverage_needs_clear = True
        self.seam_transfer_shader = None
        self.batch_seam_transfer = None
        self.batch_seam_boundary = None
        self.seam_boundary_shaders = None
        self.seam_interior_tex = None
        self.seam_interior_fb = None
        self.seam_interior_shader = None
        self.seam_strip_rects = ()
        self.seam_history_touched = set()
        self.seam_transfer_ms = 0.0
        self.seam_records = ()
        self.seam_record_triangles = None
        self.seam_selected_indices = set()
        self.seam_flush_indices = ()

        # Prepass cache: key of the view state the prepass was rendered
        # for, plus the matrices captured at that moment (the dab shader
        # MUST use the same matrices for the comparison to be valid).
        self.prepass_key = None
        self.view_proj = None          # mathutils.Matrix copy
        self.view = None               # world -> view matrix copy
        self.view_depth_plane = None   # -Z row of world -> view matrix
        self.model = None              # mathutils.Matrix copy
        self.prepass_ms = 0.0
        self.projection_bounds_ms = 0.0

        # Dirty-rect: per-triangle screen bboxes cached per prepass
        # (same matrices the dab shader uses) + per-stroke accumulator.
        self.tri_screen_bboxes = None
        self.tri_unprojectable = None
        self.screen_exact_hits = 0
        self.stroke_dirty = None       # UV bbox or None
        self.stroke_dirty_full = False  # tracking unavailable: full read
        self.session_dirty = None      # accumulated until explicit flush
        self.session_dirty_full = False
        self.dirty_ms = 0.0            # CPU cost of the tracking math
        self.flush_count = 0
        self.flush_wall_ms = 0.0
        self.dirty_union_ms = 0.0
        self.work_rect_ms = 0.0
        self.seam_select_ms = 0.0
        self.undo_touch_ms = 0.0
        self.undo_commit_ms = 0.0
        self.pen_up_t = None

        # Full-size CPU mirror arrays (one per channel): sub-rect reads
        # scatter into them; foreach_set consumes them whole (there is
        # no partial Image.pixels write). Allocated at first finalize.
        self.cpu_mirrors = None

        # Stroke state.
        self.stroke_active = False
        self.stroke_t0 = None
        self.first_dab_t = None
        self.last_dab_t = None
        self.dab_count = 0
        self.submit_times = []         # seconds, one per dab
        self.dab_queue = []            # (x, y, pressure)
        self.input_samples = []        # raw pointer samples for replay
        self.last_px = None            # last dab position (x, y)
        self.last_pressure = 1.0       # last valid tablet sample
        self.leftover = 0.0
        self.pending_finalize = False
        self.pending_flush = False
        self.flush_in_flight = False
        self.stroke_transaction = None
        self.stroke_gutter_rects = {}
        self.history = tile_undo.TileHistory()
        self.history_backend = None
        self.pending_history_action = None
        # Screen-space cursor is independent of paint preview.  Keeping the
        # composed material visible avoids the old raw-channel overlay that
        # made PBR rendering appear to change while the session was active.
        self.cursor = None

        # Sync-back: the draw callback stashes the readback here; the
        # modal operator writes it into the Image datablock.
        self.pending_pixels = None
        self.pending_gpu_stats = None

        # Error latch (printed once; shown in panel + overlay).
        self.error = None

    def latch(self, where):
        self.error = "%s\n%s" % (where, traceback.format_exc())
        print("[gpu_paint_spike] %s — session suspended. Traceback:" % where)
        print(self.error)


_session = None
_handle_view = None
_handle_pixel = None

# Survives stop_session so the panel can show the last stroke's numbers.
_last_stroke_stats = {}


class HoverTelemetry:
    """Bounded passive-viewport timing accumulator (no per-frame log spam)."""

    STAGES = ("view", "prepass", "preview", "pixel", "stencil",
              "reticle", "caliper", "stats_overlay")

    def __init__(self):
        self.counts = {name: 0 for name in self.STAGES}
        self.totals = {name: 0.0 for name in self.STAGES}
        self.maximums = {name: 0.0 for name in self.STAGES}
        self.triangles = 0
        self.unprojectable = 0

    def add(self, stage, elapsed_ms):
        if stage not in self.counts:
            return
        value = max(0.0, float(elapsed_ms))
        self.counts[stage] += 1
        self.totals[stage] += value
        self.maximums[stage] = max(self.maximums[stage], value)

    def geometry(self, triangles, unprojectable):
        self.triangles = max(0, int(triangles))
        self.unprojectable = max(0, min(self.triangles,
                                        int(unprojectable)))

    def summary(self):
        result = {"frames": self.counts["view"],
                  "pixel_frames": self.counts["pixel"],
                  "triangles": self.triangles,
                  "unprojectable": self.unprojectable,
                  "unprojectable_pct": (100.0 * self.unprojectable
                                          / self.triangles
                                          if self.triangles else 0.0)}
        for stage in self.STAGES:
            count = self.counts[stage]
            result[stage + "_avg_ms"] = (self.totals[stage] / count
                                           if count else 0.0)
            result[stage + "_max_ms"] = self.maximums[stage]
            result[stage + "_count"] = count
        return result


def format_hover_telemetry(stats):
    """Stable machine-readable payload for a passive-hover session."""
    return "GPU_PAINT_SPIKE_HOVER " + " ".join(
        "%s=%s" % (key, ("%.4f" % value)
                    if isinstance(value, float) else value)
        for key, value in sorted(stats.items()))


def _passive_hover(s):
    return not (s.stroke_active or s.dab_queue or s.pending_finalize
                or s.pending_flush or s.flush_in_flight
                or s.pending_history_action is not None)

# Ordered (key, label, format) for UI display of the stats dict.
STATS_LAYOUT = (
    ("size", "Texture", "%d px"),
    ("channels", "Channels", "%d"),
    ("dabs", "Dabs", "%d"),
    ("stroke_s", "Stroke wall time", "%.3f s"),
    ("dabs_per_s", "Dabs/sec sustained", "%.1f"),
    ("submit_avg_ms", "Dab submit avg", "%.3f ms"),
    ("submit_max_ms", "Dab submit max", "%.3f ms"),
    ("prepass_ms", "Depth prepass", "%.2f ms"),
    ("dirty_ms", "Dirty-rect math", "%.2f ms"),
    ("drain_ms", "GPU drain (1px read)", "%.2f ms"),
    ("readback_rect", "Readback rect", "%s"),
    ("fb_read_ms", "FB readback (all ch)", "%.2f ms"),
    ("fb_read_avg_ch_ms", "FB read avg/channel", "%.2f ms"),
    ("readback_path", "Readback path", "%s"),
    ("tex_read_ms", "tex.read() (debug A/B)", "%.2f ms"),
    ("to_numpy_ms", "Buffer -> numpy", "%.2f ms"),
    ("pixels_write_ms", "pixels.foreach_set (all ch)", "%.2f ms"),
    ("pixels_write_avg_ch_ms", "pixels write avg/channel", "%.2f ms"),
    ("image_update_ms", "image.update()", "%.2f ms"),
    ("syncback_total_ms", "Sync-back total", "%.2f ms"),
)


# ---------------------------------------------------------------------------
# Public API (modal operator + panel)
# ---------------------------------------------------------------------------


KIND_IMPASTO_GPU = "impasto_gpu"

_stroke_listeners = []


def add_stroke_listener(callback):
    """Register for completed GPU strokes (pen-up). Idempotent."""
    if callback not in _stroke_listeners:
        _stroke_listeners.append(callback)


def remove_stroke_listener(callback):
    try:
        _stroke_listeners.remove(callback)
    except ValueError:
        pass


def _pointer_sample(s, x, y, pressure, is_start, extras=None):
    extras = extras or {}
    elapsed = 0.0
    if s.stroke_t0 is not None:
        elapsed = max(0.0, time.perf_counter() - s.stroke_t0)
    if extras.get("time") is not None:
        elapsed = max(0.0, float(extras["time"]))
    mouse = [float(x), float(y)]
    return {
        "name": "",
        "location": [0.0, 0.0, 0.0],
        "mouse": mouse,
        "mouse_event": list(mouse),
        "pressure": float(pressure),
        "size": float(s.settings.get("radius") or 0.0),
        "x_tilt": float(extras.get("x_tilt", 0.0) or 0.0),
        "y_tilt": float(extras.get("y_tilt", 0.0) or 0.0),
        "time": elapsed,
        "is_start": bool(is_start),
    }


def completed_stroke_payload(s):
    """JSON-friendly GPU stroke for recorders. None if there is nothing to store."""
    samples = list(getattr(s, "input_samples", None) or ())
    if not samples:
        return None
    settings = s.settings
    return {
        "schema": 1,
        "kind": KIND_IMPASTO_GPU,
        "mode": "NORMAL",
        "brush_mode": str(settings.get("brush_mode", "PAINT")),
        "radius": float(settings.get("radius") or 0.0),
        "hardness": float(settings.get("hardness") or 0.0),
        "opacity": float(settings.get("opacity") or 1.0),
        "channel_keys": list(settings.get("channel_keys") or ()),
        "target_channel_keys": list(
            settings.get("brush_target_channel_keys") or ()),
        "layer_uid": str(settings.get("active_layer_uid") or ""),
        "object_name": s.obj_name,
        "samples": samples,
    }


def _notify_completed_stroke(s):
    payload = completed_stroke_payload(s)
    if payload is None:
        return
    for callback in list(_stroke_listeners):
        try:
            callback(payload)
        except Exception:
            traceback.print_exc()


def session_active():
    return _session is not None


def stroke_active():
    return _session is not None and _session.stroke_active


def busy():
    """True while queued GPU work or an explicit flush is in flight."""
    s = _session
    return s is not None and (s.pending_finalize
                              or s.pending_flush
                              or s.flush_in_flight
                              or s.pending_history_action is not None
                              or s.pending_pixels is not None)


def last_error():
    return _session.error if _session is not None else None


def last_stroke_stats():
    return dict(_last_stroke_stats)


def probe_lines():
    if _session is not None and _session.probe_lines:
        return list(_session.probe_lines)
    return []


def normalize_preview_mode(mode):
    """Return a stable preview-mode identifier, defaulting safely to Lit."""
    mode = str(mode or "LIT_PBR").upper()
    return mode if mode in PREVIEW_MODE_INDEX else "LIT_PBR"


def compose_preview_normals(base_rgb, detail_rgb=(0.5, 0.5, 1.0),
                            strength=1.0, invert_green=False):
    """Pure same-basis mirror of preview base-plus-detail composition."""
    def unit(values):
        length = math.sqrt(sum(value * value for value in values))
        if length < 1e-12:
            return (0.0, 0.0, 1.0)
        return tuple(value / length for value in values)

    base = [float(value) * 2.0 - 1.0 for value in base_rgb[:3]]
    if invert_green:
        base[1] = -base[1]
    base = unit((base[0] * max(0.0, float(strength)),
                 base[1] * max(0.0, float(strength)), max(base[2], 1e-5)))
    detail = unit(tuple(float(value) * 2.0 - 1.0
                        for value in detail_rgb[:3]))
    combined = unit((base[0] + detail[0], base[1] + detail[1],
                     base[2] + detail[2] - 1.0))
    return tuple(value * 0.5 + 0.5 for value in combined)


def preview_mode_index(mode):
    """Integer shader branch for a stable public preview identifier."""
    return PREVIEW_MODE_INDEX[normalize_preview_mode(mode)]


def set_preview_mode(mode):
    """Change live display mode without restarting the GPU paint session."""
    s = _session
    if s is None:
        return False
    s.settings["preview_mode"] = normalize_preview_mode(mode)
    return True


def set_preview_lighting(values):
    """Update display-only lighting for the active resident preview."""
    s = _session
    if s is None:
        return False
    changed = False
    for key, value in values.items():
        value = float(value)
        if s.settings.get(key) != value:
            s.settings[key] = value
            changed = True
    return changed


def set_preview_base_normal(values):
    """Update preview-only base-normal inputs for the resident session."""
    s = _session
    if s is None:
        return False
    normalized = {
        "base_normal_image_name": str(
            values.get("base_normal_image_name", "") or ""),
        "base_normal_uv_map": str(
            values.get("base_normal_uv_map", "") or ""),
        "base_normal_strength": max(
            0.0, float(values.get("base_normal_strength", 1.0))),
        "base_normal_invert_green": bool(
            values.get("base_normal_invert_green", False)),
    }
    changed = False
    resources_changed = False
    for key, value in normalized.items():
        if s.settings.get(key) != value:
            s.settings[key] = value
            changed = True
            if key in {"base_normal_image_name", "base_normal_uv_map"}:
                resources_changed = True
    if resources_changed:
        s.base_normal_resources_dirty = True
    return changed


def set_sss_caliper(values):
    """Refresh the cursor caliper immediately during a live session."""
    s = _session
    if s is None:
        return False
    changed = False
    for key, value in values.items():
        if s.settings.get(key) != value:
            s.settings[key] = value
            changed = True
    return changed


def current_preview_mode():
    if _session is None:
        return "LIT_PBR"
    return normalize_preview_mode(_session.settings.get("preview_mode"))


def preview_runtime_stats():
    """Non-blocking CPU submission/upload measurements for the live preview."""
    s = _session
    if s is None:
        return {}
    return {
        "environment_ready": s.environment_tex is not None,
        "environment_build_ms": s.environment_build_ms,
        "baseline_build_ms": s.baseline_build_ms,
        "stack_preview_status": (s.stack_spec or {}).get(
            "status", "active layer only"),
        "preview_submit_ms": s.preview_submit_ms,
        "preview_submit_avg_ms": s.preview_submit_avg_ms,
        "preview_submit_count": s.preview_submit_count,
    }


def stack_preview_requires_material_inspect(stack_spec):
    """Whether resident composition cannot represent the material safely."""
    return bool(stack_spec
                and not stack_spec.get("enabled", False)
                and stack_spec.get("safe_fallback") == "MATERIAL_INSPECT")


def set_input_paused(paused):
    """Pause viewport dab capture without ending or synchronizing a session."""
    if _session is None:
        return False
    _session.settings["input_paused"] = bool(paused)
    return True


def input_paused():
    return bool(_session is not None
                and _session.settings.get("input_paused", False))


def request_material_inspect():
    """Synchronize, then show Blender's material without ending the session."""
    s = _session
    if s is None or s.error is not None:
        return False
    s.settings["input_paused"] = True
    s.settings["material_inspect_requested"] = True
    if not has_unflushed_changes() and s.pending_pixels is None:
        s.settings["material_inspect"] = True
        s.settings["material_inspect_requested"] = False
    else:
        s.pending_flush = True
    return True


def complete_material_inspect():
    s = _session
    if s is None or not s.settings.get("material_inspect_requested", False):
        return False
    s.settings["material_inspect_requested"] = False
    if s.stack_spec is None and s.settings.get("stack_model") is not None:
        s.stack_spec = resident_stack_runtime_spec(
            s.settings["stack_model"], s.settings.get("active_layer_uid", ""))
    s.settings["material_inspect"] = True
    s.settings["input_paused"] = True
    return True


def leave_material_inspect():
    if _session is None:
        return False
    if stack_preview_requires_material_inspect(_session.stack_spec):
        # There is no truthful resident overlay for this topology. Keep the
        # authoritative Blender material visible rather than exposing the
        # active layer as though it were the complete stack.
        _session.settings["material_inspect_requested"] = False
        _session.settings["material_inspect"] = True
        _session.settings["input_paused"] = True
        return False
    if (_session.settings.get("material_inspect_requested", False)
            and not _session.flush_in_flight
            and _session.pending_pixels is None):
        _session.pending_flush = False
    _session.settings["material_inspect_requested"] = False
    _session.settings["material_inspect"] = False
    _session.settings["input_paused"] = False
    return True


def material_inspect_active():
    return bool(_session is not None
                and _session.settings.get("material_inspect", False))


def material_inspect_requested():
    return bool(_session is not None
                and _session.settings.get("material_inspect_requested", False))


def start_session(obj, images, region, channels=None, payloads=None,
                  settings=None):
    """Create the paint session for ``obj``/``images`` (a single Image
    or a list — one per channel, channel 0 first). Pure work happens
    here (geometry soup + per-triangle UV bboxes); ALL gpu work waits
    for the first draw. Safe headless: handler registration failures
    are quietly ignored."""
    global _session
    start_t0 = time.perf_counter()
    if _session is not None:
        stop_session()
    if not isinstance(images, (list, tuple)):
        images = [images]
    mesh_t0 = time.perf_counter()
    coords, uvs, normals = build_mesh_soup(obj)
    mesh_ms = (time.perf_counter() - mesh_t0) * 1000.0
    if coords is None:
        return False
    channel_count = len(images) if channels is None else int(channels)
    s = _Session(obj.name, [im.name for im in images],
                 images[0].size[0], region.as_pointer() if region else 0,
                 channels=channel_count, payloads=payloads,
                 settings=settings)
    # Build the resident stack plan before the first draw. Previously this was
    # deferred to material-inspection completion, so ordinary sessions reached
    # _ensure_gpu() with stack_spec=None and never built lower/Kiln baselines.
    if s.stack_spec is None and s.settings.get("stack_model") is not None:
        s.stack_spec = resident_stack_runtime_spec(
            s.settings["stack_model"],
            s.settings.get("active_layer_uid", ""))
    s.settings["preview_mode"] = normalize_preview_mode(
        s.settings.get("preview_mode"))
    safe_inspect = stack_preview_requires_material_inspect(s.stack_spec)
    s.settings["input_paused"] = safe_inspect
    s.settings["material_inspect"] = safe_inspect
    s.settings["material_inspect_requested"] = False
    s.coords = coords
    s.uvs = uvs
    requested_base_uv = s.settings.get("base_normal_uv_map", "")
    base_uv_t0 = time.perf_counter()
    active_uv_layer = obj.data.uv_layers.active
    if (not requested_base_uv
            or (active_uv_layer is not None
                and requested_base_uv == active_uv_layer.name)):
        # build_mesh_soup() already extracted the active map in loop-triangle
        # order.  Reusing it avoids a second full foreach_get, triangle-index
        # allocation, and advanced-index copy on the common/default path.
        s.base_normal_uvs = uvs
    else:
        s.base_normal_uvs = build_uv_soup(obj, requested_base_uv)
    base_uv_ms = (time.perf_counter() - base_uv_t0) * 1000.0
    if s.base_normal_uvs is None:
        # A missing named map disables the fallback instead of silently
        # sampling a different UV domain.
        if requested_base_uv:
            s.settings["base_normal_image_name"] = ""
        s.base_normal_uvs = uvs
    s.normals = normals
    s.gutter_uvs = uvs.reshape(-1, 3, 2)
    keys = tuple(s.settings.get("channel_keys", ()))
    seam_t0 = time.perf_counter()
    vertex_triangles = build_vertex_triangle_soup(obj)
    if vertex_triangles is not None:
        s.seam_correspondence = uv_seams.build_seam_correspondence(
            vertex_triangles, s.gutter_uvs)
    seam_ms = (time.perf_counter() - seam_t0) * 1000.0
    s.seam_channel_keys = seam_continuation_channel_keys(keys)
    bbox_t0 = time.perf_counter()
    s.tri_uv_bboxes = triangle_uv_bboxes(uvs)
    bbox_ms = (time.perf_counter() - bbox_t0) * 1000.0
    _session = s
    targets = ",".join(
        "%s:%s" % (keys[i] if i < len(keys) else i, image.name)
        for i, image in enumerate(images))
    _log_line("GPU_PAINT_SPIKE_START channels=%d size=%d targets=%s"
              % (channel_count, images[0].size[0], targets))
    _log_line("GPU_PAINT_SPIKE_START_PHASES total_ms=%.4f mesh_soup_ms=%.4f "
              "base_uv_ms=%.4f seam_map_ms=%.4f uv_bboxes_ms=%.4f "
              "triangles=%d"
              % ((time.perf_counter() - start_t0) * 1000.0, mesh_ms,
                 base_uv_ms, seam_ms, bbox_ms, len(coords) // 3))
    _add_handlers()
    return True


def format_stop_telemetry(stats):
    """Return one bounded, stable lifecycle line for session teardown."""
    fields = (
        "handlers_ms", "hover_log_ms", "history_ms", "gpu_release_ms",
        "timer_remove_ms", "redraw_ms", "operator_finish_ms", "total_ms",
    )
    return "GPU_PAINT_SPIKE_STOP " + " ".join(
        "%s=%.4f" % (name, float(stats.get(name, 0.0))) for name in fields)


def log_stop_telemetry(stats):
    _log_line(format_stop_telemetry(stats))


def stop_session(log_summary=True):
    global _session
    started = time.perf_counter()
    had_session = _session is not None
    phase = started
    _remove_handlers()
    stats = {"handlers_ms": (time.perf_counter() - phase) * 1000.0}
    if _session is not None:
        phase = time.perf_counter()
        hover = _session.hover_stats.summary()
        if hover["frames"] or hover["pixel_frames"]:
            _log_line(format_hover_telemetry(hover))
        stats["hover_log_ms"] = (time.perf_counter() - phase) * 1000.0
        phase = time.perf_counter()
        if _session.history_backend is not None:
            # TileSnapshots directly own their GPU textures; dropping the
            # record graph releases them without a redundant per-tile walk.
            _session.history.drop_references()
        stats["history_ms"] = (time.perf_counter() - phase) * 1000.0
        phase = time.perf_counter()
        _release_gpu_references(_session)
        stats["gpu_release_ms"] = (time.perf_counter() - phase) * 1000.0
        _session = None
    stats["total_ms"] = (time.perf_counter() - started) * 1000.0
    stats["had_session"] = had_session
    if log_summary and had_session:
        log_stop_telemetry(stats)
    return stats


def _release_gpu_references(s):
    """Drop every session-owned GPU object deterministically.

    Blender releases the underlying resource when the final Python reference
    disappears.  Clearing the references explicitly prevents a stopped or
    restarted resident session from retaining a texture/framebuffer cycle
    until a later garbage-collection pass.
    """
    names = (
        "dab_shaders", "dab_ubos", "soften_shader", "smear_shader",
        "soften_ubo", "soften_scratch", "soften_scratch_fb",
        "batch_soften", "batch_smear",
        "prepass_shader",
        "preview_shader", "preview_ubo", "preview_ubo_data",
        "paint_texs", "paint_fbs", "depth_color_tex",
        "depth_depth_tex", "depth_fb", "occluder_tex",
        "occluder_depth_tex", "occluder_fb", "occluder_batches",
        "batch_dabs", "batch_prepass",
        "batch_preview", "neutral_tex", "copy_shader", "copy_batch",
        "single_fbs", "environment_tex", "base_normal_tex",
        "base_normal_gpu_ref", "stencil_tex", "stencil_preview_shader",
        "baseline_shader", "baseline_batch", "baseline_texs",
        "baseline_gpu_refs", "active_preview_texs",
        "upper_transform_shader", "upper_transform_batch",
        "upper_reproject_shader", "upper_reproject_batches",
        "upper_transform_texs", "upper_transform_gpu_refs",
        "active_preview_gpu_refs", "overlay_circle_batch",
        "overlay_color_shader", "gutter_offset_map", "gutter_apply_shader",
        "gutter_apply_batch", "seam_coverage_shader", "seam_coverage_tex",
        "seam_coverage_fb", "batch_seam_coverage", "seam_transfer_shader",
        "batch_seam_transfer",
        "batch_seam_boundary",
        "seam_boundary_shaders",
        "seam_interior_tex", "seam_interior_fb", "seam_interior_shader",
    )
    for name in names:
        if name in {"baseline_texs", "active_preview_texs",
                    "upper_transform_texs"}:
            setattr(s, name, {})
        elif name in {"baseline_gpu_refs", "active_preview_gpu_refs",
                      "upper_transform_gpu_refs"}:
            setattr(s, name, [])
        else:
            setattr(s, name, None)
    s.depth_fb_size = None
    s.occluder_mesh_signature = None
    s.gutter_map_key = None
    s.gutter_diagnostics = None
    s.gpu_ready = False


def begin_stroke(x, y, pressure, extras=None):
    s = _session
    if s is None or s.error is not None:
        return
    s.stroke_active = True
    s.stroke_t0 = time.perf_counter()
    s.first_dab_t = None
    s.last_dab_t = None
    s.dab_count = 0
    s.submit_times = []
    pressure = sanitize_pressure(pressure)
    s.last_px = (x, y)
    s.last_pressure = pressure
    s.leftover = 0.0
    s.input_samples = [_pointer_sample(s, x, y, pressure, True, extras)]
    s.stroke_dirty = None
    s.stroke_dirty_full = False
    s.stroke_gutter_rects = {}
    s.seam_coverage_needs_clear = True
    s.seam_history_touched = set()
    s.seam_selected_indices = set()
    s.seam_flush_indices = ()
    s.dirty_ms = 0.0
    s.projection_bounds_ms = 0.0
    s.screen_exact_hits = 0
    s.flush_count = 0
    s.flush_wall_ms = 0.0
    s.dirty_union_ms = 0.0
    s.work_rect_ms = 0.0
    s.seam_select_ms = 0.0
    s.undo_touch_ms = 0.0
    s.undo_commit_ms = 0.0
    s.pen_up_t = None
    s.smear_last_point = None
    s.dab_queue.append((x, y, pressure))


def move_stroke(x, y, pressure, radius_px, extras=None):
    s = _session
    if s is None or not s.stroke_active or s.error is not None:
        return
    x0, y0 = s.last_px
    pressure = sanitize_pressure(pressure, s.last_pressure)
    s.input_samples.append(
        _pointer_sample(s, x, y, pressure, False, extras))
    stamp = s.settings.get("brush_stamp")
    if stamp is not None:
        mean_pressure = (s.last_pressure + pressure) * 0.5
        pressure_radius, _opacity = stamp.values_at_pressure(mean_pressure)
        spacing = max(MIN_DAB_SPACING_PX,
                      2.0 * pressure_radius * stamp.spacing_ratio)
    else:
        spacing = dab_spacing(radius_px)
    positions, s.leftover = interpolate_dabs(
        x0, y0, x, y, spacing, s.leftover)
    for px, py, t in positions:
        sample_pressure = (s.last_pressure
                           + (pressure - s.last_pressure) * t)
        s.dab_queue.append((px, py, sample_pressure))
    s.last_px = (x, y)
    s.last_pressure = pressure


def end_stroke():
    s = _session
    if s is None or not s.stroke_active:
        return
    s.stroke_active = False
    s.pen_up_t = time.perf_counter()
    # Pen-up is GPU-only. The draw callback drains any queued dabs and records
    # the stroke boundary, but performs no texture readback or Image writes.
    s.pending_finalize = True
    _notify_completed_stroke(s)


def request_flush():
    """Queue a GPU->Image synchronization at the next owning-viewport draw.

    This is deliberately explicit/deferred: normal pen-up never calls it.
    Session exit and the panel's Flush button are the safe boundaries.
    """
    s = _session
    if s is None or s.error is not None:
        return False
    s.pending_flush = True
    return True


def request_history_action(action):
    """Queue atomic GPU tile undo/redo for the owning viewport draw."""
    s = _session
    action = str(action).upper()
    if (s is None or s.stroke_active or s.pending_finalize
            or action not in {'UNDO', 'REDO'}):
        return False
    s.pending_history_action = action
    return True


def history_counts():
    s = _session
    if s is None:
        return (0, 0)
    return (s.history.undo_count, s.history.redo_count)


def has_unflushed_changes():
    s = _session
    return bool(s is not None and (s.session_dirty is not None
                                   or s.session_dirty_full
                                   or s.stroke_dirty is not None
                                   or s.stroke_dirty_full
                                   or s.dab_queue))


def set_cursor(x, y):
    """Update the owning viewport's radius-scaled GPU brush reticle."""
    s = _session
    if s is not None:
        s.cursor = (float(x), float(y))


def cursor_position():
    """Current session reticle center, primarily a headless-test seam."""
    return _session.cursor if _session is not None else None


def update_stroke_settings(payloads, radius=None, hardness=None, opacity=None,
                           brush_mode=None,
                           brush_target_channel_keys=None,
                           erase_channel_keys=None,
                           stamp=None, stencil_settings=None,
                           caliper_settings=None):
    """Refresh values sampled at the next pen-down without restarting.

    The target images and channel order are fixed for a session, but brush
    values are not. This lets the N-panel remain useful between strokes.
    """
    s = _session
    if s is None or s.stroke_active:
        return False
    refreshed = list(payloads)
    if len(refreshed) != s.channels:
        raise ValueError("payload count must match session channels")
    changed = False
    if refreshed != s.payloads:
        s.payloads = refreshed
        s.target_batches = plan_target_batches(s.payloads)
        changed = True
    if radius is not None:
        value = float(radius)
        changed |= s.settings.get("radius") != value
        s.settings["radius"] = value
    if hardness is not None:
        value = float(hardness)
        changed |= s.settings.get("hardness") != value
        s.settings["hardness"] = value
    if opacity is not None:
        value = max(0.0, min(1.0, float(opacity)))
        changed |= s.settings.get("opacity") != value
        s.settings["opacity"] = value
    if brush_mode is not None:
        value = str(brush_mode)
        changed |= s.settings.get("brush_mode") != value
        s.settings["brush_mode"] = value
    if brush_target_channel_keys is not None:
        value = tuple(brush_target_channel_keys)
        changed |= s.settings.get("brush_target_channel_keys") != value
        s.settings["brush_target_channel_keys"] = value
    if erase_channel_keys is not None:
        value = tuple(erase_channel_keys)
        changed |= s.settings.get("erase_channel_keys") != value
        s.settings["erase_channel_keys"] = value
    changed |= s.settings.get("brush_stamp") != stamp
    s.settings["brush_stamp"] = stamp
    if stencil_settings is not None:
        for key, value in dict(stencil_settings).items():
            changed |= s.settings.get(key) != value
            s.settings[key] = value
    if caliper_settings is not None:
        for key, value in dict(caliper_settings).items():
            changed |= s.settings.get(key) != value
            s.settings[key] = value
    return changed


def update_stencil_settings(stencil_settings):
    """Refresh the resident stencil and overlay without restarting.

    Stencil properties belong to viewport/session state rather than to the
    fixed channel layout, so they may be refreshed independently between UI
    events.  GPU texture creation/replacement remains deferred to the owning
    draw callback.
    """
    s = _session
    if s is None:
        return False
    changed = False
    for key, value in dict(stencil_settings).items():
        changed |= s.settings.get(key) != value
        s.settings[key] = value
    return changed


def stroke_settings_snapshot():
    """Current payload/settings snapshot, primarily a headless-test seam."""
    if _session is None:
        return None
    return ([dict(item) for item in _session.payloads],
            dict(_session.settings))


def take_pending_pixels():
    """List of (numpy array, image_name) pairs — one per channel —
    awaiting the Image writes, or None. Called from the modal operator
    (never from a draw callback)."""
    s = _session
    if s is None or s.pending_pixels is None:
        return None
    pairs = s.pending_pixels
    s.pending_pixels = None
    s.flush_in_flight = False
    return pairs


def stats_log_path():
    """Stable per-user log file for the machine-readable stat lines.

    Console output is hidden by default on Windows, so every PROBE and
    STROKE line is also appended here for later collection.
    """
    import os
    return os.path.join(os.path.expanduser("~"), "gpu_paint_spike_stats.log")


def _log_line(text):
    print(text)
    try:
        import datetime
        with open(stats_log_path(), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.datetime.now().isoformat(
                timespec="seconds"), text))
    except OSError:
        pass  # logging must never break painting


def record_sync_stats(pixels_write_ms, image_update_ms):
    """Merge the CPU-side Image-write timings into the stroke stats and
    print the machine-readable per-stroke summary line."""
    global _last_stroke_stats
    s = _session
    if s is None or s.pending_gpu_stats is None:
        return
    stats = s.pending_gpu_stats
    s.pending_gpu_stats = None
    stats["pixels_write_ms"] = pixels_write_ms
    stats["image_update_ms"] = image_update_ms
    n = max(1, int(stats.get("channels", 1)))
    stats["pixels_write_avg_ch_ms"] = pixels_write_ms / n
    stats["syncback_total_ms"] = (stats.get("drain_ms", 0.0)
                                  + stats.get("fb_read_ms", 0.0)
                                  + stats.get("to_numpy_ms", 0.0)
                                  + pixels_write_ms + image_update_ms)
    _last_stroke_stats = stats
    _log_line("GPU_PAINT_SPIKE_STROKE "
              + " ".join("%s=%s" % (k, ("%.4f" % v) if isinstance(v, float)
                                    else v)
                         for k, v in sorted(stats.items())))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _add_handlers():
    global _handle_view, _handle_pixel
    try:
        if _handle_view is None:
            _handle_view = bpy.types.SpaceView3D.draw_handler_add(
                _draw_view, (), 'WINDOW', 'POST_VIEW')
        if _handle_pixel is None:
            _handle_pixel = bpy.types.SpaceView3D.draw_handler_add(
                _draw_pixel, (), 'WINDOW', 'POST_PIXEL')
    except Exception:
        # No viewport (background mode): the session still round-trips
        # logically; there is simply nothing to draw.
        _handle_view = None
        _handle_pixel = None


def _remove_handlers():
    global _handle_view, _handle_pixel
    for handle in (_handle_view, _handle_pixel):
        if handle is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(handle, 'WINDOW')
            except Exception:
                pass
    _handle_view = None
    _handle_pixel = None


@contextmanager
def _gpu_state_restored():
    """Save global gpu state, run the block, ALWAYS restore — draw
    callbacks and offscreen passes share global GPU state with all of
    Blender's own drawing (sibling-probed pattern). face_culling has no
    getter on 5.1.2; the documented default 'NONE' is restored. The
    viewport is captured too: GPUFrameBuffer binds do NOT save it
    (documented), so offscreen viewport_set calls would otherwise leak
    into the region's own drawing."""
    prior_blend = gpu.state.blend_get()
    prior_depth_test = gpu.state.depth_test_get()
    prior_depth_mask = gpu.state.depth_mask_get()
    prior_viewport = gpu.state.viewport_get()
    try:
        yield
    finally:
        gpu.state.blend_set(prior_blend)
        gpu.state.depth_test_set(prior_depth_test)
        gpu.state.depth_mask_set(prior_depth_mask)
        gpu.state.face_culling_set('NONE')
        gpu.state.viewport_set(*prior_viewport)


def _draw_view():
    s = _session
    if s is None or s.error is not None:
        return
    region = bpy.context.region
    rv3d = bpy.context.region_data
    if region is None or rv3d is None:
        return
    owning = (region.as_pointer() == s.region_ptr)
    passive = owning and _passive_hover(s)
    view_t0 = time.perf_counter() if passive else None
    try:
        with _gpu_state_restored():
            if owning:
                _ensure_gpu(s)
                prepass_elapsed = _update_prepass(s, region, rv3d)
                if passive and prepass_elapsed is not None:
                    s.hover_stats.add("prepass", prepass_elapsed)
                if s.dab_queue:
                    _flush_dabs(s, region)
                if s.pending_finalize and not s.dab_queue:
                    _finalize_stroke_gpu(s)
                if s.pending_flush and not s.dab_queue \
                        and not s.pending_finalize:
                    _flush_session_gpu(s)
                if s.pending_history_action is not None \
                        and not s.dab_queue and not s.pending_finalize:
                    _apply_history_action(s)
                if not material_inspect_active():
                    preview_t0 = time.perf_counter() if passive else None
                    _draw_composed_preview(s)
                    if passive:
                        s.hover_stats.add(
                            "preview", (time.perf_counter() - preview_t0)
                            * 1000.0)
    except Exception:
        s.latch("draw failed")
    finally:
        if passive:
            s.hover_stats.add("view", (time.perf_counter() - view_t0)
                              * 1000.0)


def _draw_pixel():
    s = _session
    if s is None:
        return
    region = bpy.context.region
    if region is None or region.as_pointer() != s.region_ptr:
        return
    passive = _passive_hover(s)
    pixel_t0 = time.perf_counter() if passive else None
    try:
        t0 = time.perf_counter() if passive else None
        _draw_stencil_preview(s, region)
        gpu_overlays.draw_stencil_sampling_hud(
            s, region, bpy.context.region_data)
        if passive:
            s.hover_stats.add("stencil", (time.perf_counter() - t0) * 1000.0)
            t0 = time.perf_counter()
        _draw_brush_reticle(s)
        if passive:
            s.hover_stats.add("reticle", (time.perf_counter() - t0) * 1000.0)
            t0 = time.perf_counter()
        _draw_sss_caliper(s, region, bpy.context.region_data)
        if passive:
            s.hover_stats.add("caliper", (time.perf_counter() - t0) * 1000.0)
            t0 = time.perf_counter()
        _draw_stats_overlay(s)
        if passive:
            s.hover_stats.add("stats_overlay",
                              (time.perf_counter() - t0) * 1000.0)
    except Exception:
        # Text overlay must never take the viewport down; latch quietly.
        if s.error is None:
            s.latch("stats overlay failed")
    finally:
        if passive:
            s.hover_stats.add("pixel", (time.perf_counter() - pixel_t0)
                              * 1000.0)


# ---------------------------------------------------------------------------
# GPU setup + runtime capability probes
# ---------------------------------------------------------------------------


def _channel_blend(s, index):
    """Blend class of channel ``index``: 'MIX' channels accumulate
    premultiplied alpha (convert at both CPU<->GPU boundaries); 'ADD'
    (Height) accumulates raw signed values on an opaque canvas and
    round-trips byte-identically."""
    if 0 <= index < len(s.payloads):
        return s.payloads[index].get("blend", "MIX")
    return "MIX"


class _GPUTileBackend:
    """GPU-only snapshot backend for tile_undo.TileHistory."""

    def __init__(self, session):
        self.session = session
        keys = tuple(session.settings.get("channel_keys", ()))
        self.index_by_channel = {str(key): i for i, key in enumerate(keys)}

    def _index(self, key):
        try:
            return self.index_by_channel[key.channel]
        except KeyError as exc:
            raise tile_undo.TileHistoryError(
                "unknown GPU paint channel %r" % key.channel) from exc

    def _draw_copy(self, source, framebuffer, viewport, origin, scale):
        s = self.session
        with framebuffer.bind():
            framebuffer.viewport_set(*viewport)
            gpu.state.blend_set('NONE')
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.face_culling_set('NONE')
            sh = s.copy_shader
            sh.bind()
            sh.uniform_float("uv_origin", origin)
            sh.uniform_float("uv_scale", scale)
            sh.uniform_sampler("source_tex", source)
            s.copy_batch.draw(sh)

    def capture_tile(self, key):
        s = self.session
        index = self._index(key)
        tex = gpu.types.GPUTexture((key.width, key.height), format='RGBA16F')
        fb = gpu.types.GPUFrameBuffer(color_slots=(tex,))
        size = float(s.size)
        self._draw_copy(
            s.paint_texs[index], fb, (0, 0, key.width, key.height),
            (key.x / size, key.y / size),
            (key.width / size, key.height / size))
        return tile_undo.TileSnapshot(
            payload=tex, byte_size=key.width * key.height * 8)

    @staticmethod
    def snapshot_byte_size(key):
        """Exact RGBA16F allocation used to reject impossible undo early."""
        return key.width * key.height * 8

    def restore_tile(self, key, snapshot):
        s = self.session
        index = self._index(key)
        self._draw_copy(
            snapshot.payload, s.single_fbs[index],
            (key.x, key.y, key.width, key.height),
            (0.0, 0.0), (1.0, 1.0))

    def release_tile(self, snapshot):
        # Releasing the TileSnapshot drops its last GPUTexture reference.
        return None


def _ensure_uv_gutter_map(s):
    """Build the optional diagnostic ownership map once for this session."""
    if not s.settings.get("experimental_uv_gutters", False):
        return False
    radius = int(s.settings.get(
        "uv_gutter_padding_px", uv_gutters.DEFAULT_PADDING_PX))
    key = uv_gutters.uv_seed_key(s.gutter_uvs, s.size, radius)
    if s.gutter_offset_map is not None and s.gutter_map_key == key:
        return True
    # Never retain a stale map when UVs, resolution, radius, or format differ.
    s.gutter_offset_map = None
    s.gutter_map_key = None
    s.gutter_diagnostics = None
    s.gutter_apply_shader = None
    s.gutter_apply_batch = None
    try:
        result, diagnostics = uv_gutters.build_compact_offset_map_from_uvs(
            s.gutter_uvs, s.size, radius)
        apply_shader, apply_batch = uv_gutters.create_gutter_apply_resources()
    except Exception as exc:
        # This experimental diagnostic must never prevent an ordinary paint
        # session. Exact duplicate UV triangles deliberately take this path.
        s.settings["experimental_uv_gutters"] = False
        _log_line("GPU_PAINT_UV_GUTTER status=disabled reason=%s"
                  % str(exc).replace("\n", " "))
        return False
    s.gutter_offset_map = result
    s.gutter_map_key = key
    s.gutter_diagnostics = diagnostics
    s.gutter_apply_shader = apply_shader
    s.gutter_apply_batch = apply_batch
    mib = 1024.0 * 1024.0
    _log_line(
        "GPU_PAINT_UV_GUTTER status=ready format=%s size=%d radius=%d "
        "persistent_mb=%.1f peak_gpu_build_mb=%.1f cpu_peak_mb=%.1f "
        "build_ms=%.3f triangles=%d subpixel=%d outside_01=%d "
        "exact_duplicates=%d partial_overlaps=not_checked propagation=approximate"
        % (uv_gutters.OFFSET_FORMAT, s.size, radius,
           result.persistent_bytes / mib, result.peak_gpu_build_bytes / mib,
           result.transient_cpu_bytes / mib, result.initialization_ms,
           diagnostics.triangle_count, diagnostics.subpixel_triangles,
           diagnostics.outside_unit_square,
           diagnostics.exact_duplicate_triangles))
    return True


def _apply_initial_uv_gutters(s):
    """Pad resident preview inputs once, before the first Lit PBR draw.

    Per-stroke padding only repairs pixels touched after session start.  Lit
    PBR also samples the pre-existing active canvases and resolved lower-stack
    baselines, so leaving those unpadded makes an enabled experiment appear to
    do nothing until a stroke happens to reach a seam.  Reuse the session's
    single soften scratch texture for every input; no per-channel scratch or
    retained preview copy is allocated.

    This is deliberately preview-resident initialization.  It does not mark
    the session dirty, create undo records, or write Blender Images.
    """
    if (s.gutter_offset_map is None or s.gutter_apply_shader is None
            or s.gutter_apply_batch is None or s.history_backend is None):
        return 0
    rect = (0, 0, s.size, s.size)
    origin = (0.0, 0.0)
    scale = (1.0, 1.0)
    targets = list(zip(s.paint_texs, s.single_fbs))
    targets.extend(
        (texture, gpu.types.GPUFrameBuffer(color_slots=(texture,)))
        for texture in s.baseline_texs.values())
    applied = 0
    for texture, framebuffer in targets:
        s.history_backend._draw_copy(
            texture, s.soften_scratch_fb, rect, origin, scale)
        s.gutter_apply_ms += uv_gutters.apply_gutters_into(
            s.soften_scratch, framebuffer, s.gutter_offset_map, rect,
            s.gutter_apply_shader, s.gutter_apply_batch)
        applied += 1
    if applied:
        _log_line("GPU_PAINT_UV_GUTTER status=initialized textures=%d" % applied)
    return applied


def _ensure_gpu(s):
    if s.gpu_ready:
        return
    ensure_t0 = time.perf_counter()
    import numpy as np
    from gpu_extras.batch import batch_for_shader

    startup_started = time.perf_counter()
    startup_phases = {}

    phase_started = time.perf_counter()
    if s.probe_lines is None:
        probe_t0 = time.perf_counter()
        s.probe_lines, probe_source = _cached_probe_capabilities()
        probe_ms = (time.perf_counter() - probe_t0) * 1000.0
        _log_line("GPU_PAINT_SPIKE_START_PHASE phase=capability_probe "
                  "source=%s ms=%.4f" % (probe_source, probe_ms))
        for line in s.probe_lines:
            _log_line("GPU_PAINT_SPIKE_PROBE %s" % line)
    startup_phases["probe"] = (time.perf_counter() - phase_started) * 1000.0

    phase_started = time.perf_counter()
    channel_keys = tuple(s.settings.get("channel_keys", ()))
    s.dab_shaders = [gpu.shader.create_from_info(
        dab_shader_create_info(
            len(indices), additive=(blend == 'ADD'),
            profile_enabled=True,
            profile_slots=tuple(
                index < len(channel_keys) and channel_keys[index] == 'normal'
                for index in indices)))
        for blend, indices in s.target_batches]
    s.dab_ubo_data = [np.zeros((DAB_UBO_VEC4_COUNT, 4), dtype=np.float32)
                      for _shader in s.dab_shaders]
    s.dab_ubos = [gpu.types.GPUUniformBuf(data)
                  for data in s.dab_ubo_data]
    s.prepass_shader = gpu.shader.create_from_info(
        prepass_shader_create_info())
    s.soften_shader = gpu.shader.create_from_info(soften_shader_create_info())
    s.smear_shader = gpu.shader.create_from_info(smear_shader_create_info())
    s.soften_ubo_data = np.zeros((DAB_UBO_VEC4_COUNT, 4), dtype=np.float32)
    s.soften_ubo = gpu.types.GPUUniformBuf(s.soften_ubo_data)
    s.preview_shader = gpu.shader.create_from_info(
        preview_shader_create_info())
    s.preview_ubo_data = np.zeros(
        (PREVIEW_UBO_VEC4_COUNT, 4), dtype=np.float32)
    s.preview_ubo = gpu.types.GPUUniformBuf(s.preview_ubo_data)
    s.copy_shader = gpu.shader.create_from_info(copy_shader_create_info())
    s.baseline_shader = gpu.shader.create_from_info(
        baseline_shader_create_info())
    s.upper_transform_shader = gpu.shader.create_from_info(
        upper_transform_shader_create_info())
    s.upper_reproject_shader = gpu.shader.create_from_info(
        upper_reproject_shader_create_info())
    seam_coverage_enabled = bool(
        s.settings.get("experimental_conservative_seams", False)
        and s.seam_correspondence and s.seam_correspondence.pairs
        and s.seam_channel_keys)
    if seam_coverage_enabled:
        s.seam_coverage_shader = gpu.shader.create_from_info(
            seam_coverage_shader_create_info())
        s.seam_boundary_shaders = [gpu.shader.create_from_info(
            seam_boundary_shader_create_info(
                len(indices), additive=(blend == 'ADD'),
                profile_slots=tuple(
                    index < len(channel_keys)
                    and channel_keys[index] == 'normal'
                    for index in indices)))
            for blend, indices in s.target_batches]
        s.seam_interior_shader = gpu.shader.create_from_info(
            seam_interior_shader_create_info())
    startup_phases["shaders_ubos"] = (
        time.perf_counter() - phase_started) * 1000.0

    # Blender does not expose Material Preview's prefiltered studio texture as
    # a public GPU handle. Upload Impasto's deterministic linear-HDR atlas;
    # shader-side fallback remains available if allocation fails.
    t_environment = time.perf_counter()
    try:
        atlas = ibl.build_environment_atlas()
        atlas_buffer = gpu.types.Buffer('FLOAT', atlas.shape, atlas)
        s.environment_tex = gpu.types.GPUTexture(
            (atlas.shape[1], atlas.shape[0]), format='RGBA16F',
            data=atlas_buffer)
    except Exception:
        s.environment_tex = None
        traceback.print_exc()
    s.environment_build_ms = (time.perf_counter() - t_environment) * 1000.0
    startup_phases["ibl"] = s.environment_build_ms
    _log_line("GPU_PAINT_IBL source=impasto_studio_atlas size=%dx%d "
              "upload_ms=%.3f ready=%s" % (
                  ibl.ATLAS_WIDTH, ibl.ATLAS_PANEL_HEIGHT * ibl.ATLAS_PANELS,
                  s.environment_build_ms,
                  "yes" if s.environment_tex is not None else "fallback"))

    # Paint textures: N RGBA16F accumulation targets on ONE framebuffer
    # (MRT). Readback goes through fb.read_color(..., slot=i, 'FLOAT')
    # which converts to float32 regardless of the attachment format.
    size = s.size
    n = s.channels
    phase_started = time.perf_counter()
    _ensure_uv_gutter_map(s)
    startup_phases["gutters"] = (
        time.perf_counter() - phase_started) * 1000.0

    # Every logical-layer binding owns an independent Blender Image. Seed all
    # GPU targets so an untouched channel round-trips losslessly.
    phase_started = time.perf_counter()
    seeded_count = 0
    s.paint_texs = []
    for i, image_name in enumerate(s.image_names[:n]):
        tex = None
        image = bpy.data.images.get(image_name)
        if (image is not None and image.size[0] == size
                and image.size[1] == size):
            try:
                arr = np.empty(size * size * 4, dtype=np.float32)
                image.pixels.foreach_get(arr)
                if _channel_blend(s, i) != "ADD":
                    # MIX targets accumulate premultiplied (see
                    # premultiply_canvas); canvases store straight.
                    key = (channel_keys[i]
                           if i < len(channel_keys) else "")
                    prepare_canvas_upload(
                        arr, opaque=key in set(s.settings.get(
                            "opaque_channel_keys", ())))
                buf = gpu.types.Buffer('FLOAT', (size, size, 4),
                                       arr.reshape(size, size, 4))
                tex = gpu.types.GPUTexture((size, size),
                                           format='RGBA16F', data=buf)
                seeded_count += 1
            except Exception:
                traceback.print_exc()
        if tex is None:
            tex = gpu.types.GPUTexture((size, size), format='RGBA16F')
            tex.clear(format='FLOAT', value=(0.0, 0.0, 0.0, 0.0))
        s.paint_texs.append(tex)
    s.paint_fbs = [gpu.types.GPUFrameBuffer(
        color_slots=tuple(s.paint_texs[i] for i in indices))
        for _blend, indices in s.target_batches]
    s.single_fbs = [gpu.types.GPUFrameBuffer(color_slots=(tex,))
                    for tex in s.paint_texs]
    s.soften_scratch = gpu.types.GPUTexture((size, size), format='RGBA16F')
    s.soften_scratch_fb = gpu.types.GPUFrameBuffer(
        color_slots=(s.soften_scratch,))
    if seam_coverage_enabled:
        # One scalar coverage plane, shared by every eligible channel. At 4K
        # R16F costs 32 MiB; no per-channel mask is retained.
        s.seam_coverage_tex = gpu.types.GPUTexture(
            (size, size), format='R16F')
        s.seam_coverage_tex.clear(
            format='FLOAT', value=(0.0, 0.0, 0.0, 0.0))
        s.seam_coverage_fb = gpu.types.GPUFrameBuffer(
            color_slots=(s.seam_coverage_tex,))
        s.seam_interior_tex = gpu.types.GPUTexture(
            (size, size), format='R16F')
        s.seam_interior_tex.clear(
            format='FLOAT', value=(0.0, 0.0, 0.0, 0.0))
        s.seam_interior_fb = gpu.types.GPUFrameBuffer(
            color_slots=(s.seam_interior_tex,))
        interior_batch = batch_for_shader(
            s.seam_interior_shader, 'TRIS',
            {"uv": s.gutter_uvs.reshape(-1, 2)})
        with s.seam_interior_fb.bind():
            s.seam_interior_fb.viewport_set(0, 0, size, size)
            gpu.state.blend_set('NONE')
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.face_culling_set('NONE')
            s.seam_interior_shader.bind()
            interior_batch.draw(s.seam_interior_shader)
    startup_phases["paint_textures"] = (
        time.perf_counter() - phase_started) * 1000.0
    seed_line = "paint_tex_seeded_images=%d/%d" % (seeded_count, n)
    s.probe_lines.append(seed_line)
    _log_line("GPU_PAINT_SPIKE_PROBE %s" % seed_line)

    # VRAM: analytic (gpu.capabilities exposes no memory getters — the
    # probe reports whatever it finds). RGBA16F = 8 bytes/texel.
    per_ch_mb = size * size * 8 / (1024.0 * 1024.0)
    _log_line("GPU_PAINT_SPIKE_VRAM channels=%d format=RGBA16F size=%d "
              "per_channel_mb=%.1f total_mb=%.1f"
              % (n, size, per_ch_mb, per_ch_mb * n))

    phase_started = time.perf_counter()
    s.batch_dabs = [batch_for_shader(
        shader, 'TRIS', {"pos": s.coords, "uv": s.uvs})
        for shader in s.dab_shaders]
    s.batch_soften = batch_for_shader(
        s.soften_shader, 'TRIS', {"pos": s.coords, "uv": s.uvs})
    s.batch_smear = batch_for_shader(
        s.smear_shader, 'TRIS', {"pos": s.coords, "uv": s.uvs})
    if seam_coverage_enabled:
        s.batch_seam_coverage = batch_for_shader(
            s.seam_coverage_shader, 'TRIS',
            {"pos": s.coords, "uv": s.uvs})
        s.seam_records = build_conservative_seam_records(
            s.seam_correspondence, s.gutter_uvs,
            s.coords.reshape(-1, 3, 3), size)
        if s.seam_records:
            s.seam_record_triangles = seam_record_triangle_index(
                s.seam_records)
            s.seam_strip_rects = tuple(record[3] for record in s.seam_records)
            # Selected records are concatenated into one transient batch per
            # target shader at flush time. Never allocate one GPUBatch per
            # seam side: production meshes can have tens of thousands.
            s.batch_seam_boundary = True
    s.batch_prepass = batch_for_shader(
        s.prepass_shader, 'TRIS', {"pos": s.coords})
    s.batch_preview = batch_for_shader(
        s.preview_shader, 'TRIS', {"pos": s.coords, "uv": s.uvs,
                                  "normal": s.normals,
                                  "base_uv": s.base_normal_uvs})
    s.copy_batch = batch_for_shader(
        s.copy_shader, 'TRI_FAN', {
            "pos": [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                    (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)],
            "uv": [(0.0, 0.0), (1.0, 0.0),
                   (1.0, 1.0), (0.0, 1.0)]})
    s.baseline_batch = batch_for_shader(
        s.baseline_shader, 'TRI_FAN', {
            "pos": [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                    (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)],
            "uv": [(0.0, 0.0), (1.0, 0.0),
                   (1.0, 1.0), (0.0, 1.0)]})
    s.upper_transform_batch = s.baseline_batch
    startup_phases["batches"] = (
        time.perf_counter() - phase_started) * 1000.0

    phase_started = time.perf_counter()
    _build_stack_baselines(s)
    _build_upper_transforms(s)
    _build_active_preview_textures(s)
    _ensure_base_normal_texture(s)
    startup_phases["stack_baselines"] = (
        time.perf_counter() - phase_started) * 1000.0

    phase_started = time.perf_counter()
    s.history_backend = _GPUTileBackend(s)
    _apply_initial_uv_gutters(s)
    startup_phases["remaining"] = (
        time.perf_counter() - phase_started) * 1000.0
    s.gpu_ready = True
    _log_line("GPU_PAINT_SPIKE_START_PHASE phase=first_draw_gpu_init "
              "ms=%.4f" % ((time.perf_counter() - ensure_t0) * 1000.0))
    startup_phases["total"] = (
        time.perf_counter() - startup_started) * 1000.0
    s.gpu_startup_phases_ms = dict(startup_phases)
    _log_line(
        "GPU_PAINT_STARTUP total_ms=%.3f probe_ms=%.3f "
        "shaders_ubos_ms=%.3f ibl_ms=%.3f gutters_ms=%.3f "
        "paint_textures_ms=%.3f batches_ms=%.3f "
        "stack_baselines_ms=%.3f remaining_ms=%.3f" % (
            startup_phases["total"], startup_phases["probe"],
            startup_phases["shaders_ubos"], startup_phases["ibl"],
            startup_phases["gutters"], startup_phases["paint_textures"],
            startup_phases["batches"], startup_phases["stack_baselines"],
            startup_phases["remaining"]))

    # The research spike's destructive readback characterization is not part
    # of the production Impasto session; normal stroke stats remain enabled.


def _vec4(value):
    if isinstance(value, (tuple, list)):
        values = tuple(float(v) for v in value)
        if len(values) >= 4:
            return values[:4]
        if len(values) == 3:
            return values + (1.0,)
        if len(values) == 1:
            return (values[0], values[0], values[0], 1.0)
    v = float(value)
    return (v, v, v, 1.0)


def pack_preview_ubo(records, globals=None, data=None):
    """Pack all preview parameters into std140-safe contiguous vec4s.

    ``data`` may be the session's existing array for allocation-free updates.
    Matrices are transposed into GLSL column-major vec4 columns.
    """
    import numpy as np
    if data is None:
        data = np.zeros((PREVIEW_UBO_VEC4_COUNT, 4), dtype=np.float32)
    else:
        data.fill(0.0)
    globals = globals or {}
    for offset, name in ((0, "model_matrix"), (4, "view_proj_matrix")):
        matrix = globals.get(name)
        if matrix is not None:
            data[offset:offset + 4] = np.asarray(
                matrix, dtype=np.float32).reshape(4, 4).T
    camera = tuple(globals.get("camera_position", (0.0, 0.0, 10.0)))
    data[8] = camera[:3] + (float(globals.get("preview_opacity", 1.0)),)
    data[9] = (float(globals.get("preview_mode", 0)),
               float(globals.get("environment_ready", 0.0)),
               float(globals.get("base_normal_enabled", 0.0)),
               float(globals.get("occluder_ready", 0.0)))
    data[10] = _vec4(globals.get("preview_lighting", (0.0, 0.0, 1.0, 0.0)))
    data[11] = _vec4(globals.get("preview_fill", (1.0, 0.0, 0.0, 0.0)))
    options = tuple(globals.get("base_normal_options", (1.0, 0.0)))
    data[12, :2] = options[:2]
    plane = globals.get("view_depth_plane")
    if plane is not None:
        data[PREVIEW_UBO_VIEW_DEPTH] = _vec4(plane)
    for i, key in enumerate(GPU_PAINT_CHANNEL_KEYS):
        record = records.get(key, {})
        n = PREVIEW_UBO_CHANNEL_BASE + i * PREVIEW_UBO_STRIDE
        data[n] = (float(record.get("has", 0.0)),
                   float(record.get("active", 0.0)),
                   float(record.get("active_factor", 1.0)),
                   float(record.get("active_blend", 0)))
        data[n + 1] = _vec4(record.get("baseline_value", 0.0))
        data[n + 2, 0] = float(record.get("baseline_is_texture", 0.0))
        # Affine coefficients are literal four-component vectors. _vec4's
        # scalar helper deliberately supplies alpha=1 for material values,
        # which is not the additive identity required by upper D.
        data[n + 3] = _vec4(record.get(
            "upper_c", (1.0, 1.0, 1.0, 1.0)))
        data[n + 4] = _vec4(record.get(
            "upper_d", (0.0, 0.0, 0.0, 0.0)))
        data[n + 5] = (float(record.get("upper_present", 0.0)),
                       float(record.get("upper_factor", 1.0)),
                       float(record.get("upper_blend", 0)), 0.0)
    return data


def _build_stack_baselines(s):
    """Resolve static lower layers without reading resident GPU pixels."""
    spec = s.stack_spec
    if not spec or not spec.get("enabled"):
        return
    t0 = time.perf_counter()
    size = s.size
    try:
        for key, channel in spec["channels"].items():
            steps = channel["lower_steps"]
            if not any(step["source"]["kind"] == "IMAGE" for step in steps):
                value = channel["seed"]
                for step in steps:
                    if key == "normal":
                        value = preview_stack.blend_tangent_normals_rnm(
                            value, step["source"]["value"], step["factor"])
                    else:
                        value = preview_stack.blend_value(
                            value, step["source"]["value"], step["factor"],
                            step["blend"])
                s.baseline_values[key] = _vec4(value)
                continue
            ping = gpu.types.GPUTexture((size, size), format='RGBA16F')
            pong = gpu.types.GPUTexture((size, size), format='RGBA16F')
            ping.clear(format='FLOAT', value=_vec4(channel["seed"]))
            current, target = ping, pong
            for step in steps:
                source = step["source"]
                source_tex = current
                if source["kind"] == "IMAGE":
                    image = bpy.data.images.get(source["image_name"])
                    if image is None:
                        raise RuntimeError("missing lower image %r" %
                                           source["image_name"])
                    if source.get("use_alpha"):
                        source_tex = gpu.texture.from_image(image)
                    else:
                        # Opaque channel images (notably Kiln normal bakes)
                        # may carry zero/non-authoritative alpha. Blender's
                        # cached image texture can premultiply that alpha,
                        # destroying RGB before the compositor gets a chance
                        # to ignore it. Upload raw pixels and establish opaque
                        # alpha at this boundary instead.
                        import numpy as np
                        width, height = (int(image.size[0]),
                                         int(image.size[1]))
                        pixels = np.empty(width * height * 4,
                                          dtype=np.float32)
                        image.pixels.foreach_get(pixels)
                        pixels.reshape(-1, 4)[:, 3] = 1.0
                        buffer = gpu.types.Buffer(
                            'FLOAT', (height, width, 4),
                            pixels.reshape(height, width, 4))
                        source_tex = gpu.types.GPUTexture(
                            (width, height), format='RGBA16F', data=buffer)
                    s.baseline_gpu_refs.append(source_tex)
                fb = gpu.types.GPUFrameBuffer(color_slots=(target,))
                with fb.bind():
                    gpu.state.viewport_set(0, 0, size, size)
                    gpu.state.blend_set('NONE')
                    shader = s.baseline_shader
                    shader.bind()
                    shader.uniform_float("uv_origin", (0.0, 0.0))
                    shader.uniform_float("uv_scale", (1.0, 1.0))
                    shader.uniform_float("source_value", _vec4(
                        source.get("value", 0.0)))
                    shader.uniform_float("source_is_texture",
                                         1.0 if source["kind"] == "IMAGE" else 0.0)
                    shader.uniform_float("source_uses_alpha",
                                         1.0 if source.get("use_alpha") else 0.0)
                    shader.uniform_float("factor", float(step["factor"]))
                    shader.uniform_float("is_normal",
                                         1.0 if key == "normal" else 0.0)
                    shader.uniform_int("blend_mode", _BLEND_INDEX.get(
                        step["blend"], 0))
                    shader.uniform_sampler("current_tex", current)
                    shader.uniform_sampler("source_tex", source_tex)
                    s.baseline_batch.draw(shader)
                current, target = target, current
            s.baseline_texs[key] = current
        s.baseline_build_ms = (time.perf_counter() - t0) * 1000.0
        _log_line("GPU_PAINT_STACK status=%s build_ms=%.3f textures=%d" % (
            spec["status"], s.baseline_build_ms, len(s.baseline_texs)))
    except Exception as exc:
        s.baseline_texs.clear()
        s.baseline_values.clear()
        spec["enabled"] = False
        spec["status"] = "fallback: lower baseline build failed: %s" % exc
        spec["safe_fallback"] = "MATERIAL_INSPECT"
        s.settings["material_inspect"] = True
        s.settings["input_paused"] = True
        traceback.print_exc()


def _build_upper_transforms(s):
    """Collapse arbitrary ordered upper steps into one C/D texture per channel.

    Each texel stores ``D.rgb`` and scalar ``C`` in alpha, representing the
    affine transform ``result.rgb = C * active.rgb + D``. This keeps preview
    sampler use constant regardless of layer count.
    """
    s.upper_transform_texs.clear()
    s.upper_transform_gpu_refs.clear()
    spec = s.stack_spec
    if not spec or not spec.get("enabled"):
        return
    size = s.size
    try:
        for key, channel_spec in spec["channels"].items():
            if key == "normal":
                continue
            steps = tuple(channel_spec.get("upper_steps", ()))
            if not steps:
                continue
            ping = gpu.types.GPUTexture((size, size), format='RGBA16F')
            pong = gpu.types.GPUTexture((size, size), format='RGBA16F')
            # D=0, C=1 is the identity transform.
            ping.clear(format='FLOAT', value=(0.0, 0.0, 0.0, 1.0))
            current, target = ping, pong
            for step in steps:
                source_tex = current
                mask_tex = current
                if step["kind"] == "IMAGE":
                    image = bpy.data.images.get(step["image_name"])
                    if image is None:
                        raise RuntimeError("missing upper image %r" %
                                           step["image_name"])
                    source_tex = gpu.texture.from_image(image)
                    s.upper_transform_gpu_refs.append(source_tex)
                mask = step.get("mask")
                if mask:
                    image = bpy.data.images.get(mask["image_name"])
                    if image is None:
                        raise RuntimeError("missing upper mask %r" %
                                           mask["image_name"])
                    mask_tex = gpu.texture.from_image(image)
                    s.upper_transform_gpu_refs.append(mask_tex)
                source_uv_map = str(step.get("uv_map", ""))
                active_uv_map = str(spec.get("active_uv_map", ""))
                reproject = (step["kind"] == "IMAGE"
                             and source_uv_map != active_uv_map)
                fb = gpu.types.GPUFrameBuffer(color_slots=(target,))
                with fb.bind():
                    gpu.state.viewport_set(0, 0, size, size)
                    gpu.state.blend_set('NONE')
                    if reproject:
                        # Preserve transform texels outside the mesh's active
                        # UV islands before overwriting covered fragments.
                        shader = s.copy_shader
                        shader.bind()
                        shader.uniform_float("uv_origin", (0.0, 0.0))
                        shader.uniform_float("uv_scale", (1.0, 1.0))
                        shader.uniform_sampler("source_tex", current)
                        s.copy_batch.draw(shader)
                        obj = bpy.data.objects.get(s.obj_name)
                        source_uvs = (build_uv_soup(obj, source_uv_map)
                                      if obj is not None else None)
                        if source_uvs is None:
                            raise RuntimeError(
                                "missing upper UV map %r" % source_uv_map)
                        batch = s.upper_reproject_batches.get(source_uv_map)
                        if batch is None:
                            from gpu_extras.batch import batch_for_shader
                            batch = batch_for_shader(
                                s.upper_reproject_shader, 'TRIS',
                                {"target_uv": s.uvs,
                                 "source_uv": source_uvs})
                            s.upper_reproject_batches[source_uv_map] = batch
                        shader = s.upper_reproject_shader
                    else:
                        shader = s.upper_transform_shader
                    shader.bind()
                    if reproject:
                        shader.uniform_float(
                            "source_uses_alpha",
                            1.0 if step.get("use_alpha") else 0.0)
                        shader.uniform_float("factor", float(step["factor"]))
                        shader.uniform_int(
                            "blend_mode", _BLEND_INDEX.get(step["blend"], 0))
                        shader.uniform_sampler("current_tex", current)
                        shader.uniform_sampler("source_tex", source_tex)
                        batch.draw(shader)
                        current, target = target, current
                        continue
                    shader.uniform_float("uv_origin", (0.0, 0.0))
                    shader.uniform_float("uv_scale", (1.0, 1.0))
                    shader.uniform_float(
                        "source_value", _vec4(step.get("value", 0.0)))
                    shader.uniform_float(
                        "source_is_texture",
                        1.0 if step["kind"] == "IMAGE" else 0.0)
                    shader.uniform_float(
                        "source_uses_alpha",
                        1.0 if step.get("use_alpha") else 0.0)
                    shader.uniform_float("factor", float(step["factor"]))
                    shader.uniform_float("mask_present",
                                         1.0 if mask else 0.0)
                    shader.uniform_float("mask_invert",
                                         1.0 if mask and mask["invert"]
                                         else 0.0)
                    shader.uniform_float("mask_opacity", float(
                        mask["opacity"] if mask else 1.0))
                    shader.uniform_int(
                        "blend_mode", _BLEND_INDEX.get(step["blend"], 0))
                    shader.uniform_sampler("current_tex", current)
                    shader.uniform_sampler("source_tex", source_tex)
                    shader.uniform_sampler("mask_tex", mask_tex)
                    s.upper_transform_batch.draw(shader)
                current, target = target, current
            s.upper_transform_texs[key] = current
            s.upper_transform_gpu_refs.append(current)
    except Exception as exc:
        s.upper_transform_texs.clear()
        s.upper_transform_gpu_refs.clear()
        spec["enabled"] = False
        spec["status"] = "fallback: upper transform build failed: %s" % exc
        spec["safe_fallback"] = "MATERIAL_INSPECT"
        s.settings["material_inspect"] = True
        s.settings["input_paused"] = True
        traceback.print_exc()


def _build_active_preview_textures(s):
    """Upload visible active-layer channels that are not writable targets.

    Brush targeting controls writes, not visibility. Resident writable
    channels already live in ``paint_texs``; the remaining active-layer
    images are read-only preview inputs.
    """
    s.active_preview_texs.clear()
    s.active_preview_gpu_refs.clear()
    spec = s.stack_spec
    if not spec or not spec.get("enabled"):
        return
    writable = set(s.settings.get("channel_keys", ()))
    for key, channel in spec["channels"].items():
        active = channel.get("active") or {}
        image_name = active.get("image_name")
        if key in writable or not image_name:
            continue
        image = bpy.data.images.get(image_name)
        if image is None:
            continue
        tex = gpu.texture.from_image(image)
        s.active_preview_texs[key] = tex
        s.active_preview_gpu_refs.append(tex)


def _ensure_base_normal_texture(s):
    """Upload an optional preview-only normal image with authoritative RGB."""
    s.base_normal_tex = None
    s.base_normal_gpu_ref = None
    name = str(s.settings.get("base_normal_image_name", "") or "")
    if not name:
        return None
    image = bpy.data.images.get(name)
    if image is None or image.size[0] < 1 or image.size[1] < 1:
        return None
    try:
        import numpy as np
        width, height = int(image.size[0]), int(image.size[1])
        values = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
        values.reshape(-1, 4)[:, 3] = 1.0
        buffer = gpu.types.Buffer(
            'FLOAT', (height, width, 4), values.reshape(height, width, 4))
        s.base_normal_tex = gpu.types.GPUTexture(
            (width, height), format='RGBA16F', data=buffer)
        s.base_normal_gpu_ref = s.base_normal_tex
    except Exception:
        traceback.print_exc()
        s.base_normal_tex = None
        s.base_normal_gpu_ref = None
    return s.base_normal_tex


def _refresh_base_normal_resources(s):
    """Rebuild preview-only image/UV GPU state in the owning draw context."""
    if not s.base_normal_resources_dirty:
        return
    s.base_normal_resources_dirty = False
    obj = bpy.data.objects.get(s.obj_name)
    if obj is None or obj.type != 'MESH':
        s.base_normal_tex = None
        return
    base_uvs = build_uv_soup(
        obj, str(s.settings.get("base_normal_uv_map", "") or ""))
    if base_uvs is None:
        s.base_normal_tex = None
        return
    s.base_normal_uvs = base_uvs
    _ensure_base_normal_texture(s)
    from gpu_extras.batch import batch_for_shader
    s.batch_preview = batch_for_shader(
        s.preview_shader, 'TRIS', {"pos": s.coords, "uv": s.uvs,
                                  "normal": s.normals,
                                  "base_uv": s.base_normal_uvs})


def _characterize_readback(s):
    """One-time per-session measurement: fb.read_color cost at 100% /
    25% / 5% of the texture AREA (slot 0), plus a full all-channels
    read. Logged as GPU_PAINT_SPIKE_READBACK_CHAR lines; best of 2."""
    import numpy as np
    size = s.size
    with s.paint_fb.bind():
        s.paint_fb.read_color(0, 0, 1, 1, 4, 0, 'FLOAT')   # warm/drain
        for frac_pct in (100, 25, 5):
            side = max(1, int(round(size * math.sqrt(frac_pct / 100.0))))
            best = None
            for _ in range(2):
                t0 = time.perf_counter()
                if _read_into_numpy:
                    arr = np.empty((side, side, 4), dtype=np.float32)
                    s.paint_fb.read_color(
                        0, 0, side, side, 4, 0, 'FLOAT',
                        data=gpu.types.Buffer('FLOAT', (side, side, 4), arr))
                else:
                    s.paint_fb.read_color(0, 0, side, side, 4, 0, 'FLOAT')
                dt = (time.perf_counter() - t0) * 1000.0
                best = dt if best is None else min(best, dt)
            _log_line("GPU_PAINT_SPIKE_READBACK_CHAR size=%d channels=1 "
                      "frac=%d%% rect=%dx%d ms=%.2f read_into_numpy=%s"
                      % (size, frac_pct, side, side, best,
                         "yes" if _read_into_numpy else "no"))
        if s.channels > 1:
            best = None
            for _ in range(2):
                t0 = time.perf_counter()
                for slot in range(s.channels):
                    if _read_into_numpy:
                        arr = np.empty((size, size, 4), dtype=np.float32)
                        s.paint_fb.read_color(
                            0, 0, size, size, 4, slot, 'FLOAT',
                            data=gpu.types.Buffer('FLOAT',
                                                  (size, size, 4), arr))
                    else:
                        s.paint_fb.read_color(0, 0, size, size, 4, slot,
                                              'FLOAT')
                dt = (time.perf_counter() - t0) * 1000.0
                best = dt if best is None else min(best, dt)
            _log_line("GPU_PAINT_SPIKE_READBACK_CHAR size=%d channels=%d "
                      "frac=100%% rect=%dx%d ms=%.2f read_into_numpy=%s "
                      "(all channels, full)"
                      % (size, s.channels, size, size, best,
                         "yes" if _read_into_numpy else "no"))


def _gpu_backend_identity():
    """Complete identity for process-local capability caching.

    An unavailable identity is intentionally not cacheable: sharing an
    ``unknown`` entry could reuse results after a backend/context change.
    """
    try:
        from gpu import platform
        return (_CAPABILITY_PROBE_CACHE_SCHEMA,
                platform.backend_type_get(), platform.vendor_get(),
                platform.renderer_get())
    except Exception:
        return None


def _cached_probe_capabilities():
    """Return ``(lines, source)`` and restore cached strategy latches."""
    global _buffer_numpy_path, _read_into_numpy
    identity = _gpu_backend_identity()
    cached = _capability_probe_cache.get(identity) if identity else None
    if cached is not None:
        _buffer_numpy_path = cached[1]
        _read_into_numpy = cached[2]
        return list(cached[0]), "cache"
    lines = _probe_capabilities()
    if identity is not None:
        _capability_probe_cache.clear()  # only the active backend is useful
        _capability_probe_cache[identity] = (
            tuple(lines), _buffer_numpy_path, _read_into_numpy)
    return lines, "runtime"


def _probe_capabilities():
    """Runtime probes of exactly the gpu features this spike leans on.
    Returns machine-readable "key=value" lines (also printed once with
    the GPU_PAINT_SPIKE_PROBE prefix). Runs inside the draw callback —
    a GPU context is guaranteed there."""
    import numpy as np
    from gpu_extras.batch import batch_for_shader
    from mathutils import Matrix

    lines = []

    try:
        from gpu import platform
        lines.append("backend=%s vendor=%s renderer=%s"
                     % (platform.backend_type_get(), platform.vendor_get(),
                        platform.renderer_get()))
    except Exception as e:
        lines.append("backend=unknown (%r)" % e)

    # RGBA16F color attachment on a custom framebuffer.
    try:
        tex = gpu.types.GPUTexture((8, 8), format='RGBA16F')
        fb = gpu.types.GPUFrameBuffer(color_slots=(tex,))
        lines.append("rgba16f_color_fb=yes")
    except Exception as e:
        lines.append("rgba16f_color_fb=NO (%r)" % e)
        return lines   # everything else depends on this

    # Fixed-function ALPHA blending INTO a custom framebuffer
    # attachment: draw an opaque red quad, then a half-alpha blue quad,
    # read the center pixel back. (0.5, 0, 0.5) proves blending works
    # and ping-pong is unnecessary.
    try:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        quad = batch_for_shader(
            shader, 'TRI_FAN',
            {"pos": [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                     (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]})
        ident = Matrix.Identity(4)
        with fb.bind():
            fb.viewport_set(0, 0, 8, 8)
            fb.clear(color=(0.0, 0.0, 0.0, 0.0))
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            with gpu.matrix.push_pop():
                with gpu.matrix.push_pop_projection():
                    gpu.matrix.load_matrix(ident)
                    gpu.matrix.load_projection_matrix(ident)
                    shader.bind()
                    shader.uniform_float("color", (1.0, 0.0, 0.0, 1.0))
                    quad.draw(shader)
                    shader.uniform_float("color", (0.0, 0.0, 1.0, 0.5))
                    quad.draw(shader)
            buf = fb.read_color(4, 4, 1, 1, 4, 0, 'FLOAT')
        px = np.asarray(buf, dtype=np.float32).ravel()
        ok = (abs(px[0] - 0.5) < 0.06 and abs(px[2] - 0.5) < 0.06)
        lines.append("blend_alpha_into_offscreen_attachment=%s (center=%s)"
                     % ("yes" if ok else "NO",
                        np.round(px, 3).tolist()))
    except Exception as e:
        lines.append("blend_alpha_into_offscreen_attachment=NO (%r)" % e)

    # Stroke-end readback strategy (the 0.1.0 bottleneck: ~1050 ms
    # Buffer->numpy at 4K). Two probes against the blended 8x8 content
    # above, so correctness is checked against non-trivial values:
    # 1. fastest zero-copy Buffer->numpy ladder rung;
    # 2. fb.read_color(..., data=Buffer-wrapping-numpy): if the Buffer
    #    references the numpy memory (rather than copying), the read
    #    lands directly in the array foreach_set consumes and the
    #    conversion step disappears entirely.
    global _buffer_numpy_path, _read_into_numpy
    ref = None
    try:
        with fb.bind():
            small = fb.read_color(0, 0, 8, 8, 4, 0, 'FLOAT')
        ref = np.asarray(small.to_list(), dtype=np.float32).ravel()
        _buffer_numpy_path = probe_buffer_to_numpy_path(small, ref)
        lines.append("buffer_to_numpy_path=%s" % _buffer_numpy_path)
    except Exception as e:
        _buffer_numpy_path = "to_list_fallback"
        lines.append("buffer_to_numpy_path=to_list_fallback (probe: %r)" % e)
    try:
        target = np.zeros((8, 8, 4), dtype=np.float32)
        tbuf = gpu.types.Buffer('FLOAT', (8, 8, 4), target)
        with fb.bind():
            fb.read_color(0, 0, 8, 8, 4, 0, 'FLOAT', data=tbuf)
        ok = (ref is not None and bool(ref.any())
              and bool(np.allclose(target.ravel(), ref, atol=1e-3)))
        _read_into_numpy = ok
        lines.append("fb_read_into_numpy_buffer=%s"
                     % ("yes" if ok else
                        "NO (Buffer copies instead of wrapping, or "
                        "values mismatched)"))
    except Exception as e:
        _read_into_numpy = False
        lines.append("fb_read_into_numpy_buffer=NO (%r)" % e)

    # GPUTexture.read() on RGBA16F (vs fb.read_color): does the Buffer
    # convert to numpy, what dtype/length comes back, and does it export
    # the C buffer protocol (memoryview) — decides whether a half-float
    # read + CPU astype could ever beat the 'FLOAT' read (headless
    # measurement says no: float16->float32 astype alone costs ~119 ms
    # at 4K on this machine).
    try:
        buf = tex.read()
        try:
            mv = memoryview(buf)
            mv_desc = "memoryview format=%s itemsize=%d" % (mv.format,
                                                            mv.itemsize)
        except Exception as e:
            mv_desc = "memoryview unsupported (%r)" % e
        arr = np.asarray(buf)
        lines.append("gputexture_read_rgba16f=yes (numpy dtype=%s shape=%s; "
                     "%s)" % (arr.dtype, arr.shape, mv_desc))
    except Exception as e:
        lines.append("gputexture_read_rgba16f=NO (%r)" % e)

    # R32F color attachment (the prepass NDC-depth target).
    try:
        r32 = gpu.types.GPUTexture((8, 8), format='R32F')
        gpu.types.GPUFrameBuffer(color_slots=(r32,))
        lines.append("r32f_color_fb=yes")
    except Exception as e:
        lines.append("r32f_color_fb=NO (%r)" % e)

    # DEPTH_COMPONENT32F texture attached as depth_slot + clear.
    try:
        dt = gpu.types.GPUTexture((8, 8), format='DEPTH_COMPONENT32F')
        r32 = gpu.types.GPUTexture((8, 8), format='R32F')
        fb2 = gpu.types.GPUFrameBuffer(depth_slot=dt, color_slots=(r32,))
        with fb2.bind():
            fb2.clear(color=(1.0, 0.0, 0.0, 1.0), depth=1.0)
            dbuf = fb2.read_depth(4, 4, 1, 1)
        dval = float(np.asarray(dbuf).ravel()[0])
        lines.append("depth32f_attach_clear_read=yes (cleared=%.3f)" % dval)
    except Exception as e:
        lines.append("depth32f_attach_clear_read=NO (%r)" % e)

    # ---- v0.3.0 multi-channel probes ----------------------------------

    # How many color_slots does GPUFrameBuffer accept? (Try 8 then 4
    # then 2 — the panel's channel counts.)
    max_slots = 0
    slots_err = ""
    for count in (8, 4, 2):
        try:
            texs = [gpu.types.GPUTexture((8, 8), format='RGBA16F')
                    for _ in range(count)]
            gpu.types.GPUFrameBuffer(color_slots=tuple(texs))
            max_slots = count
            break
        except Exception as e:
            if not slots_err:
                slots_err = repr(e)
    lines.append("fb_max_color_slots=%d (of 8/4/2 tried%s)"
                 % (max_slots,
                    "" if max_slots == 8 else "; first failure: %s"
                    % slots_err))

    # R16F color attachment (scalar channels) + mixed-format MRT
    # (RGBA16F slot 0 + R16F slot 1 on one framebuffer).
    try:
        r16 = gpu.types.GPUTexture((8, 8), format='R16F')
        gpu.types.GPUFrameBuffer(color_slots=(r16,))
        lines.append("r16f_color_fb=yes")
    except Exception as e:
        lines.append("r16f_color_fb=NO (%r)" % e)
    try:
        fbm = gpu.types.GPUFrameBuffer(color_slots=(
            gpu.types.GPUTexture((8, 8), format='RGBA16F'),
            gpu.types.GPUTexture((8, 8), format='R16F')))
        with fbm.bind():
            fbm.clear(color=(0.25, 0.0, 0.0, 1.0))
            v0 = np.asarray(fbm.read_color(4, 4, 1, 1, 4, 0,
                                           'FLOAT').to_list()).ravel()
            v1 = np.asarray(fbm.read_color(4, 4, 1, 1, 1, 1,
                                           'FLOAT').to_list()).ravel()
        ok = abs(v0[0] - 0.25) < 0.01 and abs(v1[0] - 0.25) < 0.01
        lines.append("mixed_format_mrt_rgba16f_r16f=%s (slot0=%.3f "
                     "slot1=%.3f)" % ("yes" if ok else "NO",
                                      float(v0[0]), float(v1[0])))
    except Exception as e:
        lines.append("mixed_format_mrt_rgba16f_r16f=NO (%r)" % e)

    # Does fixed-function ALPHA blend apply to ALL MRT attachments?
    # gpu.state.blend_set has no per-attachment form, so if attachment 1
    # blends too this is a design constraint: one blend mode per stroke
    # across every channel. Shader routes DISTINCT values (att1 = rgb *
    # 0.5) so this also proves output-slot routing. Draw opaque red then
    # half-alpha blue: att0 -> (0.5, 0, 0.5); att1 -> (0.25, 0, 0.25)
    # iff blending applied there as well.
    try:
        mrt_sh = gpu.shader.create_from_info(_mrt_probe_shader_create_info())
        mrt_quad = batch_for_shader(
            mrt_sh, 'TRI_FAN',
            {"pos": [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                     (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]})
        t0 = gpu.types.GPUTexture((8, 8), format='RGBA16F')
        t1 = gpu.types.GPUTexture((8, 8), format='RGBA16F')
        fb2 = gpu.types.GPUFrameBuffer(color_slots=(t0, t1))
        with fb2.bind():
            fb2.viewport_set(0, 0, 8, 8)
            fb2.clear(color=(0.0, 0.0, 0.0, 0.0))
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
            mrt_sh.bind()
            mrt_sh.uniform_float("color", (1.0, 0.0, 0.0, 1.0))
            mrt_quad.draw(mrt_sh)
            mrt_sh.uniform_float("color", (0.0, 0.0, 1.0, 0.5))
            mrt_quad.draw(mrt_sh)
            a0 = np.asarray(fb2.read_color(4, 4, 1, 1, 4, 0,
                                           'FLOAT').to_list()).ravel()
            a1 = np.asarray(fb2.read_color(4, 4, 1, 1, 4, 1,
                                           'FLOAT').to_list()).ravel()
        ok0 = abs(a0[0] - 0.5) < 0.06 and abs(a0[2] - 0.5) < 0.06
        ok1 = abs(a1[0] - 0.25) < 0.06 and abs(a1[2] - 0.25) < 0.06
        lines.append("mrt_blend_alpha_all_attachments=%s (att0=%s att1=%s; "
                     "per-attachment blend NOT exposed by gpu.state — "
                     "one blend mode per stroke)"
                     % ("yes" if (ok0 and ok1) else "NO",
                        np.round(a0, 3).tolist(), np.round(a1, 3).tolist()))
    except Exception as e:
        lines.append("mrt_blend_alpha_all_attachments=NO (%r)" % e)

    # Sub-rect fb.read_color: paint left half red / right half blue via
    # viewport_set, then check that x/y offsets land on the right texels
    # and that a non-square sub-rect read into a numpy-wrapping Buffer
    # comes back in (h, w, 4) row-major order.
    try:
        tex3 = gpu.types.GPUTexture((8, 8), format='RGBA16F')
        fb3 = gpu.types.GPUFrameBuffer(color_slots=(tex3,))
        with fb3.bind():
            fb3.viewport_set(0, 0, 8, 8)
            fb3.clear(color=(0.0, 0.0, 0.0, 0.0))
            gpu.state.blend_set('NONE')
            shader.bind()
            shader.uniform_float("color", (1.0, 0.0, 0.0, 1.0))
            with gpu.matrix.push_pop():
                with gpu.matrix.push_pop_projection():
                    gpu.matrix.load_matrix(ident)
                    gpu.matrix.load_projection_matrix(ident)
                    quad.draw(shader)
                    fb3.viewport_set(4, 0, 4, 8)
                    shader.uniform_float("color", (0.0, 0.0, 1.0, 1.0))
                    quad.draw(shader)
            left = np.asarray(fb3.read_color(1, 4, 1, 1, 4, 0,
                                             'FLOAT').to_list()).ravel()
            right = np.asarray(fb3.read_color(6, 4, 1, 1, 4, 0,
                                              'FLOAT').to_list()).ravel()
            sub = np.zeros((8, 4, 4), dtype=np.float32)   # h=8, w=4
            fb3.read_color(2, 0, 4, 8, 4, 0, 'FLOAT',
                           data=gpu.types.Buffer('FLOAT', (8, 4, 4), sub))
        ok_off = left[0] > 0.9 and left[2] < 0.1 \
            and right[2] > 0.9 and right[0] < 0.1
        # columns 2,3 red / 4,5 blue -> sub[:, :2] red, sub[:, 2:] blue
        ok_sub = (bool((sub[:, :2, 0] > 0.9).all())
                  and bool((sub[:, 2:, 2] > 0.9).all()))
        lines.append("fb_read_color_subrect=%s (offsets=%s; "
                     "into_numpy_hw4_order=%s)"
                     % ("yes" if (ok_off and ok_sub) else "NO",
                        "ok" if ok_off else "WRONG",
                        "ok" if ok_sub else "WRONG"))
    except Exception as e:
        lines.append("fb_read_color_subrect=NO (%r)" % e)

    # Queryable GPU memory info? (Expected absent; analytic VRAM
    # numbers are logged separately.)
    try:
        mem = []
        for name in dir(gpu.capabilities):
            if "mem" in name.lower() and name.endswith("_get"):
                try:
                    mem.append("%s=%r" % (name,
                                          getattr(gpu.capabilities, name)()))
                except Exception:
                    mem.append("%s=raises" % name)
        lines.append("gpu_capabilities_memory=%s"
                     % ("; ".join(mem) if mem else "none_exposed"))
    except Exception as e:
        lines.append("gpu_capabilities_memory=probe_failed (%r)" % e)

    # Direct sampling of a DEPTH texture through a FLOAT_2D sampler is
    # deliberately NOT relied on (backend convention minefield); the
    # spike samples its own R32F NDC-depth instead. Informational only.
    lines.append("depth_texture_direct_sampling=not_relied_on "
                 "(spike stores NDC depth in R32F color)")
    return lines


def _mrt_probe_shader_create_info():
    """Two-attachment MRT probe shader: slot 0 gets ``color``, slot 1
    gets ``color`` with rgb halved (distinct, so slot routing is
    proven). Positions are already NDC — no matrices."""
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('VEC4', "color")
    info.vertex_in(0, 'VEC3', "pos")
    info.fragment_out(0, 'VEC4', "fragColor")
    info.fragment_out(1, 'VEC4', "fragColor1")
    info.vertex_source("void main() { gl_Position = vec4(pos, 1.0); }\n")
    info.fragment_source(
        "void main()\n"
        "{\n"
        "    fragColor = color;\n"
        "    fragColor1 = vec4(color.rgb * 0.5, color.a);\n"
        "}\n")
    return info


# ---------------------------------------------------------------------------
# Depth prepass (per view change, NOT per dab)
# ---------------------------------------------------------------------------


def _visible_occluder_objects(s):
    context = bpy.context
    view_layer = getattr(context, "view_layer", None)
    if view_layer is None:
        return ()
    visible = []
    for obj in iter_preview_occluders(s.obj_name, view_layer.objects):
        try:
            if not obj.visible_get():
                continue
        except Exception:
            continue
        visible.append(obj)
    return tuple(visible)


def _occluder_view_key(s):
    parts = []
    for obj in _visible_occluder_objects(s):
        mw = obj.matrix_world
        parts.append((
            obj.name,
            tuple(round(v, 5) for row in mw for v in row),
        ))
    return tuple(parts)


def _occluder_mesh_signature(s, objects):
    parts = []
    for obj in objects:
        mesh = getattr(obj, "data", None)
        parts.append((
            obj.name,
            mesh.as_pointer() if mesh is not None else 0,
            len(mesh.vertices) if mesh is not None else 0,
        ))
    return tuple(parts)


def _ensure_occluder_batches(s, objects):
    """Rebuild other-mesh position batches when their topology changes."""
    signature = _occluder_mesh_signature(s, objects)
    if (signature == s.occluder_mesh_signature
            and s.occluder_batches is not None):
        return
    from gpu_extras.batch import batch_for_shader
    batches = []
    shader = s.prepass_shader
    if shader is None:
        s.occluder_batches = ()
        s.occluder_mesh_signature = signature
        return
    for obj in objects:
        coords = build_position_soup(obj)
        if coords is None:
            continue
        batches.append((obj.name, batch_for_shader(
            shader, 'TRIS', {"pos": coords})))
    s.occluder_batches = tuple(batches)
    s.occluder_mesh_signature = signature


def _prepass_state_key(s, region, rv3d, obj):
    m = rv3d.perspective_matrix
    mw = obj.matrix_world
    return (region.width, region.height,
            tuple(round(v, 6) for row in m for v in row),
            tuple(round(v, 6) for row in mw for v in row),
            _occluder_view_key(s))


def _update_prepass(s, region, rv3d):
    obj = bpy.data.objects.get(s.obj_name)
    if obj is None:
        return None
    key = _prepass_state_key(s, region, rv3d, obj)
    if key == s.prepass_key:
        return None
    t0 = time.perf_counter()

    w = max(int(region.width), 8)
    h = max(int(region.height), 8)
    if s.depth_fb_size != (w, h):
        # Release the old attachment graph before replacing it.  Framebuffers
        # retain their attachments, so merely overwriting the textures can
        # otherwise defer a viewport-sized allocation until GC.
        s.depth_fb = None
        s.depth_color_tex = None
        s.depth_depth_tex = None
        s.depth_color_tex = gpu.types.GPUTexture((w, h), format='R32F')
        s.depth_depth_tex = gpu.types.GPUTexture(
            (w, h), format='DEPTH_COMPONENT32F')
        s.depth_fb = gpu.types.GPUFrameBuffer(
            depth_slot=s.depth_depth_tex, color_slots=(s.depth_color_tex,))
        s.occluder_fb = None
        s.occluder_tex = None
        s.occluder_depth_tex = None
        s.occluder_tex = gpu.types.GPUTexture((w, h), format='R32F')
        s.occluder_depth_tex = gpu.types.GPUTexture(
            (w, h), format='DEPTH_COMPONENT32F')
        s.occluder_fb = gpu.types.GPUFrameBuffer(
            depth_slot=s.occluder_depth_tex, color_slots=(s.occluder_tex,))
        s.depth_fb_size = (w, h)

    # Capture the matrices the prepass renders with: the dab shader must
    # use the SAME values or the occlusion comparison is meaningless.
    s.view_proj = rv3d.perspective_matrix.copy()
    s.view = rv3d.view_matrix.copy()
    s.view_depth_plane = tuple(-s.view[2][i] for i in range(4))
    s.model = obj.matrix_world.copy()
    # Dirty/Undo projection bounds are needed by dabs, not by navigation.
    # Invalidate them here and rebuild lazily at the first paint flush after
    # the view settles. Projecting/clipping 150k+ triangles on every orbit
    # frame made otherwise-cheap Lit PBR navigation visibly choppy.
    s.tri_screen_bboxes = None
    s.tri_unprojectable = None
    s.hover_stats.geometry(len(s.coords) // 3, 0)

    with s.depth_fb.bind():
        s.depth_fb.viewport_set(0, 0, w, h)
        # The dab shader samples only pixels covered by this same mesh; a
        # large linear value remains a safe uncovered sentinel.
        s.depth_fb.clear(color=(1e30, 0.0, 0.0, 1.0), depth=1.0)
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('LESS_EQUAL')
        gpu.state.depth_mask_set(True)
        gpu.state.face_culling_set('NONE')
        sh = s.prepass_shader
        sh.bind()
        sh.uniform_float("model_matrix", s.model)
        sh.uniform_float("view_proj_matrix", s.view_proj)
        sh.uniform_float("view_depth_plane", s.view_depth_plane)
        s.batch_prepass.draw(sh)
        # Do not read the framebuffer here. A readback forces the CPU to wait
        # for the complete depth raster on every orbit/zoom frame. GPU command
        # ordering already makes later consumers observe this pass.
    if s.occluder_fb is not None and s.prepass_shader is not None:
        occluders = _visible_occluder_objects(s)
        _ensure_occluder_batches(s, occluders)
        with s.occluder_fb.bind():
            s.occluder_fb.viewport_set(0, 0, w, h)
            s.occluder_fb.clear(color=(1e30, 0.0, 0.0, 1.0), depth=1.0)
            gpu.state.blend_set('NONE')
            gpu.state.depth_test_set('LESS_EQUAL')
            gpu.state.depth_mask_set(True)
            gpu.state.face_culling_set('NONE')
            sh = s.prepass_shader
            sh.bind()
            sh.uniform_float("view_proj_matrix", s.view_proj)
            sh.uniform_float("view_depth_plane", s.view_depth_plane)
            by_name = {obj.name: obj for obj in occluders}
            for name, batch in (s.occluder_batches or ()):
                other = by_name.get(name)
                if other is None:
                    continue
                sh.uniform_float("model_matrix", other.matrix_world)
                batch.draw(sh)
    s.prepass_ms = (time.perf_counter() - t0) * 1000.0
    s.prepass_key = key
    return s.prepass_ms


def _ensure_projection_bounds(s, region):
    """Build dab dirty/Undo bounds lazily for the current prepass matrices."""
    if s.tri_screen_bboxes is not None:
        return 0.0
    if s.coords is None or s.view_proj is None or s.model is None:
        return None
    started = time.perf_counter()
    try:
        import numpy as np
        mvp = np.array(s.view_proj @ s.model, dtype=np.float32)
        s.tri_screen_bboxes, s.tri_unprojectable = triangle_screen_bboxes(
            s.coords, mvp, region.width, region.height)
        s.hover_stats.geometry(len(s.tri_screen_bboxes),
                               int(s.tri_unprojectable.sum()))
    except Exception:
        s.tri_screen_bboxes = None
        s.tri_unprojectable = None
        return None
    elapsed = (time.perf_counter() - started) * 1000.0
    s.projection_bounds_ms += elapsed
    return elapsed


# ---------------------------------------------------------------------------
# Dab dispatch
# ---------------------------------------------------------------------------


def _ensure_stencil_texture(s):
    """Resolve a Blender Image to a shared GPU texture on demand.

    Called only from the owning draw context. Changing/disabling the stencil
    drops the old reference; ordinary dabs reuse the same texture handle.
    """
    enabled = bool(s.settings.get("stencil_enabled", False))
    image_name = str(s.settings.get("stencil_image_name", ""))
    if not enabled or not image_name:
        s.stencil_tex = None
        s.stencil_image_name = ""
        return None
    if s.stencil_tex is not None and s.stencil_image_name == image_name:
        return s.stencil_tex
    s.stencil_tex = None
    s.stencil_image_name = ""
    image = bpy.data.images.get(image_name)
    if image is None:
        return None
    try:
        s.stencil_tex = gpu.texture.from_image(image)
        s.stencil_image_name = image_name
    except Exception:
        traceback.print_exc()
    return s.stencil_tex


def _flush_dabs(s, region):
    if s.view_proj is None:
        return
    flush_started = time.perf_counter()
    radius = float(s.settings.get("radius", 50.0))
    hardness = float(s.settings.get("hardness", 0.5))
    occlusion = bool(s.settings.get("occlusion", True))
    stamp = s.settings.get("brush_stamp")
    stencil_tex = _ensure_stencil_texture(s)
    use_stencil = stencil_tex is not None

    _ensure_projection_bounds(s, region)

    queue = s.dab_queue
    s.dab_queue = []
    # Never reuse a previous flush's seam subset when projection data is
    # temporarily unavailable or the current queue selects no seam face.
    s.seam_flush_indices = ()
    now = time.perf_counter()
    if s.first_dab_t is None:
        s.first_dab_t = now

    # Conservative dirty-rect accumulation (pure numpy; timed so the
    # stats show what the tracking itself costs).
    t_dirty = time.perf_counter()
    dab_work_rects = None
    undo_dirty_rects = None
    undo_recording = (s.stroke_transaction is None
                      or s.stroke_transaction.is_recording)
    if (s.tri_screen_bboxes is not None and s.tri_uv_bboxes is not None
            and queue):
        rect = dab_rect_union(queue, radius)
        hit_indices = _screen_bbox_hit_indices(
            s.tri_screen_bboxes, s.tri_unprojectable, rect)
        s.screen_exact_hits = getattr(s, "screen_exact_hits", 0) + len(
            hit_indices)
        if s.seam_records:
            seam_started = time.perf_counter()
            s.seam_flush_indices = touched_seam_record_indices(
                s.seam_records, s.tri_screen_bboxes, rect,
                s.seam_record_triangles, hit_indices)
            s.seam_selected_indices.update(s.seam_flush_indices)
            s.seam_select_ms += (time.perf_counter() - seam_started) * 1000.0
        union_started = time.perf_counter()
        bb = dirty_uv_bbox(s.tri_screen_bboxes, s.tri_unprojectable,
                           s.tri_uv_bboxes, rect,
                           hit_indices=hit_indices)
        s.stroke_dirty = union_bbox(s.stroke_dirty, bb)
        if (undo_recording
                and s.settings.get("brush_mode", "PAINT")
                in {"PAINT", "ERASE"}):
            undo_dirty_rects = dirty_uv_pixel_rects(
                s.tri_screen_bboxes, s.tri_unprojectable,
                s.tri_uv_bboxes, rect, s.size,
                hit_indices=hit_indices)
        s.dirty_union_ms += (time.perf_counter() - union_started) * 1000.0
        work_started = time.perf_counter()
        dab_work_rects = detailed_dab_work_rects(
            s.settings.get("brush_mode", "PAINT"),
            s.tri_screen_bboxes, s.tri_unprojectable, s.tri_uv_bboxes,
            queue, radius, s.size)
        s.work_rect_ms += (time.perf_counter() - work_started) * 1000.0
    elif queue:
        s.stroke_dirty_full = True   # no projection cache: full read
    s.dirty_ms += time.perf_counter() - t_dirty

    # Capture every touched channel tile once, immediately before its first
    # modification. Both before/after snapshots stay GPU-resident.
    if queue and s.history_backend is not None and undo_recording:
        undo_started = time.perf_counter()
        dirty_rect = (uv_bbox_to_pixel_rect(bb, s.size)
                      if not s.stroke_dirty_full else None)
        if dirty_rect is not None or s.stroke_dirty_full:
            if s.stroke_transaction is None:
                s.stroke_transaction = s.history.begin_stroke(
                    s.history_backend, "GPU multi-channel stroke")
            rect = dirty_rect or (0, 0, s.size, s.size)
            sparse_rects = (undo_dirty_rects
                            if undo_dirty_rects is not None else (rect,))
            keys = tuple(s.settings.get("channel_keys", ()))
            target_keys = set(s.settings.get(
                "brush_target_channel_keys", keys))
            if s.gutter_offset_map is not None:
                sparse_rects = tuple(
                    uv_gutters.expand_pixel_rect(
                        item, s.gutter_offset_map.radius, s.size)
                    for item in sparse_rects)
            undo_channels = []
            for i in range(s.channels):
                channel = keys[i] if i < len(keys) else str(i)
                if channel not in target_keys:
                    continue
                undo_channels.append(channel)
                if s.gutter_offset_map is not None:
                    for item in sparse_rects:
                        append_sparse_pixel_rect(
                            s.stroke_gutter_rects, channel, item)
            s.stroke_transaction.touch_channel_rects(
                undo_channels, sparse_rects, (s.size, s.size), 128)
            if (s.stroke_transaction.is_recording
                    and s.batch_seam_boundary is not None):
                eligible = set(s.seam_channel_keys) & target_keys
                new_seams = (s.seam_selected_indices
                             - s.seam_history_touched)
                for channel in eligible:
                    for seam_index in sorted(new_seams):
                        seam_rect = s.seam_records[seam_index][3]
                        undo_rect = seam_rect
                        if s.gutter_offset_map is not None:
                            undo_rect = uv_gutters.expand_pixel_rect(
                                seam_rect, s.gutter_offset_map.radius, s.size)
                            append_sparse_pixel_rect(
                                s.stroke_gutter_rects, channel, undo_rect)
                        s.stroke_transaction.touch_rect(
                            channel, undo_rect, (s.size, s.size))
                s.seam_history_touched.update(new_seams)
        s.undo_touch_ms += (time.perf_counter() - undo_started) * 1000.0

    # Gutter propagation remains part of painting even when an oversized
    # stroke can no longer be made undoable. Use one conservative union rect
    # instead of rebuilding its expensive per-triangle sparse undo request.
    if (queue and s.gutter_offset_map is not None
            and s.stroke_transaction is not None
            and not s.stroke_transaction.is_recording):
        keys = tuple(s.settings.get("channel_keys", ()))
        target_keys = set(s.settings.get(
            "brush_target_channel_keys", keys))
        gutter_rect = (uv_bbox_to_pixel_rect(bb, s.size)
                       if not s.stroke_dirty_full else
                       (0, 0, s.size, s.size))
        if gutter_rect is not None:
            gutter_rect = uv_gutters.expand_pixel_rect(
                gutter_rect, s.gutter_offset_map.radius, s.size)
            for channel in target_keys:
                append_sparse_pixel_rect(
                    s.stroke_gutter_rects, channel, gutter_rect)
        if s.batch_seam_boundary is not None:
            eligible = set(s.seam_channel_keys) & target_keys
            new_seams = s.seam_selected_indices - s.seam_history_touched
            for channel in eligible:
                for seam_index in sorted(new_seams):
                    seam_rect = uv_gutters.expand_pixel_rect(
                        s.seam_records[seam_index][3],
                        s.gutter_offset_map.radius, s.size)
                    append_sparse_pixel_rect(
                        s.stroke_gutter_rects, channel, seam_rect)
            s.seam_history_touched.update(new_seams)

    if s.settings.get("brush_mode", "PAINT") == "SOFTEN":
        _flush_soften_dabs(s, region, queue, radius, hardness, occlusion,
                           stamp, stencil_tex, use_stencil, dab_work_rects)
        s.dab_count += len(queue)
        s.last_dab_t = time.perf_counter()
        s.flush_count += 1
        s.flush_wall_ms += (time.perf_counter() - flush_started) * 1000.0
        return
    if s.settings.get("brush_mode", "PAINT") == "SMEAR":
        _flush_smear_dabs(s, region, queue, radius, hardness, occlusion,
                          stamp, stencil_tex, use_stencil, dab_work_rects)
        s.dab_count += len(queue)
        s.last_dab_t = time.perf_counter()
        s.flush_count += 1
        s.flush_wall_ms += (time.perf_counter() - flush_started) * 1000.0
        return

    for batch_index, ((blend, indices), fb, sh, ubo, ubo_data,
                      draw_batch) in enumerate(
            zip(s.target_batches, s.paint_fbs, s.dab_shaders,
                s.dab_ubos, s.dab_ubo_data, s.batch_dabs)):
        with fb.bind():
            fb.viewport_set(0, 0, s.size, s.size)
            erase = s.settings.get("brush_mode", 'PAINT') == 'ERASE'
            channel_keys = tuple(s.settings.get("channel_keys", ()))
            target_keys = set(s.settings.get(
                "brush_target_channel_keys",
                s.settings.get("erase_channel_keys", channel_keys)))
            gpu.state.blend_set(
                'MULTIPLY' if erase else
                ('ADDITIVE' if blend == 'ADD' else 'ALPHA'))
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.face_culling_set('NONE')
            sh.bind()
            brush_values = []
            for local, target_index in enumerate(indices):
                payload = s.payloads[target_index]
                value = tuple(payload.get("value", (0.0, 0.0, 0.0)))[:3]
                brush_values.append((
                    value[0], value[1], value[2],
                    ((1.0 if erase else float(
                        payload.get("strength", 1.0)))
                     if (target_index < len(channel_keys)
                         and channel_keys[target_index] in target_keys)
                     else 0.0)))
            stencil_projection = (
                s.settings.get("stencil_projection") == 'BRUSH_ALPHA')
            stencil_interpretation = (
                s.settings.get("stencil_interpretation") == 'LUMINANCE')
            stencil_position = tuple(
                s.settings.get("stencil_position", (0.5, 0.5)))
            stencil_scale = tuple(
                s.settings.get("stencil_scale", (0.35, 0.35)))
            stencil_opacity = float(
                s.settings.get("stencil_opacity", 1.0))
            stencil_rotation = float(
                s.settings.get("stencil_rotation", 0.0))
            profile_usage = (
                s.settings.get("stencil_usage") == 'NORMAL_PROFILE')
            profile_strength = float(
                s.settings.get("stencil_profile_strength", 1.0))
            profile_invert = bool(
                s.settings.get("stencil_profile_invert", False))
            dab_uniform_data(
                s.model, s.view_proj, s.view_depth_plane,
                (region.width, region.height), (0.0, 0.0), radius,
                hardness, DEPTH_EPSILON,
                visibility.DEFAULT_POLICY.relative_epsilon, occlusion, 0.0,
                use_stencil, stencil_projection, stencil_interpretation,
                stencil_opacity, stencil_position, stencil_scale,
                stencil_rotation, brush_values, profile_usage,
                profile_strength, profile_invert, data=ubo_data)
            ubo_data[DAB_UBO_STENCIL_FLAGS, 3] = (
                1.0 if s.settings.get("stencil_coverage", True) else 0.0)
            ubo_data[DAB_UBO_PROFILE_FLAGS, 3] = 1.0 if erase else 0.0
            ubo.update(ubo_data)
            sh.uniform_block(DAB_UBO_NAME, ubo)
            sh.uniform_sampler("scene_depth_tex", s.depth_color_tex)
            # All declared samplers must be bound even when the branch is off.
            sh.uniform_sampler("stencil_tex", stencil_tex if use_stencil
                               else s.depth_color_tex)
            for (x, y, pressure) in queue:
                t0 = time.perf_counter()
                dab_radius, dab_opacity = (
                    stamp.values_at_pressure(pressure)
                    if stamp is not None else (radius, pressure))
                stroke_opacity = float(s.settings.get("opacity", 1.0))
                effective_opacity = max(
                    0.0, min(1.0, dab_opacity * stroke_opacity))
                if stamp is not None and stamp.use_pressure_strength:
                    effective_opacity = overlap_compensated_opacity(
                        effective_opacity, stamp.spacing_ratio)
                ubo_data[DAB_UBO_REGION_CENTER, 2:4] = (float(x), float(y))
                ubo_data[DAB_UBO_BRUSH_DEPTH, 0] = dab_radius
                ubo_data[DAB_UBO_PAINT_FLAGS, 1] = effective_opacity
                ubo.update(ubo_data)
                draw_batch.draw(sh)
                s.submit_times.append(time.perf_counter() - t0)
    _flush_seam_coverage_dabs(
        s, queue, radius, stamp, stencil_tex, use_stencil)
    _flush_seam_boundary_dabs(
        s, queue, radius, stamp, stencil_tex, use_stencil)
    s.dab_count += len(queue)
    s.last_dab_t = time.perf_counter()
    s.flush_count += 1
    s.flush_wall_ms += (time.perf_counter() - flush_started) * 1000.0


def _flush_seam_coverage_dabs(s, queue, radius, stamp, stencil_tex,
                              use_stencil):
    """Record actual visible/stencilled dab coverage for later seam transfer.

    This pass shares the already-packed first dab UBO, and therefore exactly
    matches paint projection, falloff, stencil, and occlusion. It records one
    scalar mask for the whole stroke; no seam pixels are transported here.
    """
    if (not queue or s.seam_coverage_shader is None
            or s.seam_coverage_fb is None
            or s.batch_seam_coverage is None or not s.dab_ubos):
        return
    if s.seam_coverage_needs_clear:
        s.seam_coverage_tex.clear(
            format='FLOAT', value=(0.0, 0.0, 0.0, 0.0))
        s.seam_coverage_needs_clear = False
    shader = s.seam_coverage_shader
    ubo = s.dab_ubos[0]
    data = s.dab_ubo_data[0]
    with s.seam_coverage_fb.bind():
        s.seam_coverage_fb.viewport_set(0, 0, s.size, s.size)
        gpu.state.blend_set('ADDITIVE')
        gpu.state.depth_test_set('NONE')
        gpu.state.depth_mask_set(False)
        gpu.state.face_culling_set('NONE')
        shader.bind()
        shader.uniform_block(DAB_UBO_NAME, ubo)
        shader.uniform_sampler("scene_depth_tex", s.depth_color_tex)
        shader.uniform_sampler("stencil_tex", stencil_tex if use_stencil
                               else s.depth_color_tex)
        for x, y, pressure in queue:
            dab_radius, dab_opacity = (
                stamp.values_at_pressure(pressure)
                if stamp is not None else (radius, pressure))
            effective_opacity = max(0.0, min(
                1.0, dab_opacity * float(s.settings.get("opacity", 1.0))))
            if stamp is not None and stamp.use_pressure_strength:
                effective_opacity = overlap_compensated_opacity(
                    effective_opacity, stamp.spacing_ratio)
            data[DAB_UBO_REGION_CENTER, 2:4] = (float(x), float(y))
            data[DAB_UBO_BRUSH_DEPTH, 0] = dab_radius
            data[DAB_UBO_PAINT_FLAGS, 1] = effective_opacity
            ubo.update(data)
            s.batch_seam_coverage.draw(shader)


def _flush_seam_boundary_dabs(s, queue, radius, stamp, stencil_tex,
                              use_stencil):
    """Paint the conservative half-texel strip using clamped face points."""
    if (not queue or not s.batch_seam_boundary
            or not s.seam_flush_indices
            or s.seam_coverage_tex is None):
        return
    keys = tuple(s.settings.get("channel_keys", ()))
    targets = set(s.settings.get("brush_target_channel_keys", keys))
    erase = s.settings.get("brush_mode", "PAINT") == "ERASE"
    if s.settings.get("brush_mode", "PAINT") not in {"PAINT", "ERASE"}:
        return
    import numpy as np
    from gpu_extras.batch import batch_for_shader
    selected = [s.seam_records[index] for index in s.seam_flush_indices]
    positions = np.concatenate([record[1] for record in selected], axis=0)
    uvs = np.concatenate([record[2] for record in selected], axis=0)
    for (blend, indices), fb, shader, ubo, data in zip(
            s.target_batches, s.paint_fbs, s.seam_boundary_shaders,
            s.dab_ubos, s.dab_ubo_data):
        batch = batch_for_shader(
            shader, 'TRIS', {"pos": positions, "uv": uvs})
        # Tangent normals remain intentionally gated until a dedicated
        # face-basis validation exists. Other channels preserve normal target
        # toggles and their ordinary payloads.
        for local, index in enumerate(indices):
            key = keys[index] if index < len(keys) else str(index)
            if key == "normal" or key not in targets:
                data[DAB_UBO_BRUSH_VALUES + local, 3] = 0.0
        ubo.update(data)
        with fb.bind():
            fb.viewport_set(0, 0, s.size, s.size)
            gpu.state.blend_set(
                'MULTIPLY' if erase else
                ('ADDITIVE' if blend == 'ADD' else 'ALPHA'))
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.face_culling_set('NONE')
            shader.bind()
            shader.uniform_block(DAB_UBO_NAME, ubo)
            shader.uniform_sampler("scene_depth_tex", s.depth_color_tex)
            shader.uniform_sampler("stencil_tex", stencil_tex if use_stencil
                                   else s.depth_color_tex)
            shader.uniform_sampler("coverage_tex", s.seam_coverage_tex)
            shader.uniform_sampler("interior_tex", s.seam_interior_tex)
            for x, y, pressure in queue:
                dab_radius, dab_opacity = (
                    stamp.values_at_pressure(pressure)
                    if stamp is not None else (radius, pressure))
                effective_opacity = max(0.0, min(
                    1.0, dab_opacity * float(s.settings.get("opacity", 1.0))))
                if stamp is not None and stamp.use_pressure_strength:
                    effective_opacity = overlap_compensated_opacity(
                        effective_opacity, stamp.spacing_ratio)
                data[DAB_UBO_REGION_CENTER, 2:4] = (float(x), float(y))
                data[DAB_UBO_BRUSH_DEPTH, 0] = dab_radius
                data[DAB_UBO_PAINT_FLAGS, 1] = effective_opacity
                ubo.update(data)
                batch.draw(shader)
    for seam_index in s.seam_flush_indices:
        x, y, width, height = s.seam_records[seam_index][3]
        s.stroke_dirty = union_bbox(s.stroke_dirty, (
            x / s.size, y / s.size,
            (x + width) / s.size, (y + height) / s.size))


def _flush_soften_dabs(s, region, queue, radius, hardness, occlusion, stamp,
                       stencil_tex, use_stencil, dirty_rects=None):
    """Blur enabled resident targets without sampling a render attachment.

    Each dab copies a target into the spare texture, overwrites only covered
    mesh fragments there from the old target, then swaps the handles. No
    Blender Image access or GPU-to-CPU synchronization occurs.
    """
    sh = s.soften_shader
    ubo = s.soften_ubo
    data = s.soften_ubo_data
    stencil_projection = s.settings.get("stencil_projection") == 'BRUSH_ALPHA'
    stencil_interpretation = (
        s.settings.get("stencil_interpretation") == 'LUMINANCE')
    stencil_position = tuple(s.settings.get("stencil_position", (0.5, 0.5)))
    stencil_scale = tuple(s.settings.get("stencil_scale", (0.35, 0.35)))
    stencil_opacity = float(s.settings.get("stencil_opacity", 1.0))
    stencil_rotation = float(s.settings.get("stencil_rotation", 0.0))
    work_rects = (dirty_rects if dirty_rects is not None else
                  ((0, 0, s.size, s.size),) * len(queue))
    for (x, y, pressure), work_rect in zip(queue, work_rects):
        if work_rect is None:
            continue
        dab_radius, dab_opacity = (stamp.values_at_pressure(pressure)
                                   if stamp is not None
                                   else (radius, pressure))
        strength = max(0.0, min(1.0, dab_opacity * float(
            s.settings.get("opacity", 1.0))))
        if stamp is not None and stamp.use_pressure_strength:
            strength = overlap_compensated_opacity(
                strength, stamp.spacing_ratio)
        dab_uniform_data(
            s.model, s.view_proj, s.view_depth_plane,
            (region.width, region.height), (x, y), dab_radius, hardness,
            DEPTH_EPSILON, visibility.DEFAULT_POLICY.relative_epsilon,
            occlusion, strength, use_stencil, stencil_projection,
            stencil_interpretation, stencil_opacity, stencil_position,
            stencil_scale, stencil_rotation, (), data=data)
        data[DAB_UBO_STENCIL_FLAGS, 3] = (
            1.0 if s.settings.get("stencil_coverage", True) else 0.0)
        ubo.update(data)
        channel_keys = tuple(s.settings.get("channel_keys", ()))
        target_keys = set(s.settings.get(
            "brush_target_channel_keys", channel_keys))
        for index in range(s.channels):
            if (index >= len(channel_keys)
                    or channel_keys[index] not in target_keys):
                continue
            source = s.paint_texs[index]
            target = s.soften_scratch
            s.history_backend._draw_copy(
                source, s.soften_scratch_fb, work_rect,
                (work_rect[0] / s.size, work_rect[1] / s.size),
                (work_rect[2] / s.size, work_rect[3] / s.size))
            with s.single_fbs[index].bind():
                s.single_fbs[index].viewport_set(0, 0, s.size, s.size)
                gpu.state.blend_set('NONE')
                gpu.state.depth_test_set('NONE')
                gpu.state.depth_mask_set(False)
                gpu.state.face_culling_set('NONE')
                gpu.state.scissor_set(*work_rect)
                gpu.state.scissor_test_set(True)
                try:
                    sh.bind()
                    sh.uniform_block(DAB_UBO_NAME, ubo)
                    sh.uniform_sampler("scene_depth_tex", s.depth_color_tex)
                    sh.uniform_sampler(
                        "stencil_tex",
                        stencil_tex if use_stencil else s.depth_color_tex)
                    sh.uniform_sampler("source_tex", target)
                    s.batch_soften.draw(sh)
                finally:
                    gpu.state.scissor_test_set(False)


def _flush_smear_dabs(s, region, queue, radius, hardness, occlusion, stamp,
                      stencil_tex, use_stencil, dirty_rects=None):
    """Transport resident pixels in stroke direction without readback.

    This first usable version maps screen direction onto texture axes. It is
    deterministic and useful on ordinary UV islands; projection-aware
    transport across rotated islands and seams remains future work.
    """
    sh = s.smear_shader
    data = s.soften_ubo_data
    ubo = s.soften_ubo
    previous = s.smear_last_point
    work_rects = (dirty_rects if dirty_rects is not None else
                  ((0, 0, s.size, s.size),) * len(queue))
    for (x, y, pressure), work_rect in zip(queue, work_rects):
        if previous is None:
            previous = (x, y)
            continue
        if work_rect is None:
            previous = (x, y)
            continue
        dx, dy = float(x - previous[0]), float(y - previous[1])
        length = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        # Sample behind the dab, capped to keep transport stable on sparse
        # tablet events. Direction is inverted because GLSL samples source.
        travel = min(length, max(1.0, radius * 0.35)) / float(s.size)
        offset = (-dx / length * travel, -dy / length * travel)
        dab_radius, dab_opacity = (stamp.values_at_pressure(pressure)
                                   if stamp is not None
                                   else (radius, pressure))
        strength = max(0.0, min(1.0, dab_opacity * float(
            s.settings.get("opacity", 1.0))))
        dab_uniform_data(
            s.model, s.view_proj, s.view_depth_plane,
            (region.width, region.height), (x, y), dab_radius, hardness,
            DEPTH_EPSILON, visibility.DEFAULT_POLICY.relative_epsilon,
            occlusion, strength, use_stencil,
            s.settings.get("stencil_projection") == 'BRUSH_ALPHA',
            s.settings.get("stencil_interpretation") == 'LUMINANCE',
            float(s.settings.get("stencil_opacity", 1.0)),
            tuple(s.settings.get("stencil_position", (0.5, 0.5))),
            tuple(s.settings.get("stencil_scale", (0.35, 0.35))),
            float(s.settings.get("stencil_rotation", 0.0)), (), data=data)
        data[DAB_UBO_PROFILE_FLAGS, 2:4] = offset
        ubo.update(data)
        channel_keys = tuple(s.settings.get("channel_keys", ()))
        target_keys = set(s.settings.get(
            "brush_target_channel_keys", channel_keys))
        for index in range(s.channels):
            if (index >= len(channel_keys)
                    or channel_keys[index] not in target_keys):
                continue
            source, target = s.paint_texs[index], s.soften_scratch
            s.history_backend._draw_copy(source, s.soften_scratch_fb,
                                         work_rect,
                                         (work_rect[0] / s.size,
                                          work_rect[1] / s.size),
                                         (work_rect[2] / s.size,
                                          work_rect[3] / s.size))
            with s.single_fbs[index].bind():
                s.single_fbs[index].viewport_set(0, 0, s.size, s.size)
                gpu.state.blend_set('NONE')
                gpu.state.depth_test_set('NONE')
                gpu.state.depth_mask_set(False)
                gpu.state.face_culling_set('NONE')
                gpu.state.scissor_set(*work_rect)
                gpu.state.scissor_test_set(True)
                try:
                    sh.bind()
                    sh.uniform_block(DAB_UBO_NAME, ubo)
                    sh.uniform_sampler("scene_depth_tex", s.depth_color_tex)
                    sh.uniform_sampler(
                        "stencil_tex",
                        stencil_tex if use_stencil else s.depth_color_tex)
                    sh.uniform_sampler("source_tex", target)
                    s.batch_smear.draw(sh)
                finally:
                    gpu.state.scissor_test_set(False)
        previous = (x, y)
    s.smear_last_point = previous


# ---------------------------------------------------------------------------
# Stroke-end readback (GPU side; the Image write happens in the modal op)
# ---------------------------------------------------------------------------


def _stroke_stats(s):
    """Cheap GPU-submission statistics; never forces GPU completion."""
    stats = {
        "mode": s.settings.get("brush_mode", "PAINT"),
        "size": s.size,
        "dabs": s.dab_count,
        "channels": s.channels,
        "target_channels": len(set(s.settings.get(
            "brush_target_channel_keys",
            s.settings.get("channel_keys", ())))),
        "prepass_ms": s.prepass_ms,
        "projection_bounds_ms": s.projection_bounds_ms,
        "screen_exact_hits": s.screen_exact_hits,
        "dirty_ms": s.dirty_ms * 1000.0,
        "flush_count": s.flush_count,
        "flush_wall_ms": s.flush_wall_ms,
        "dirty_union_ms": s.dirty_union_ms,
        "work_rect_ms": s.work_rect_ms,
        "seam_select_ms": s.seam_select_ms,
        "undo_touch_ms": s.undo_touch_ms,
        "undo_commit_ms": s.undo_commit_ms,
        "deferred": 1,
    }
    if s.stroke_t0 is not None:
        stats["stroke_s"] = time.perf_counter() - s.stroke_t0
        stats["input_active_s"] = max(
            0.0, (s.pen_up_t or time.perf_counter()) - s.stroke_t0)
    if s.pen_up_t is not None:
        stats["finalize_delay_ms"] = max(
            0.0, (time.perf_counter() - s.pen_up_t) * 1000.0)
    if s.submit_times:
        stats["submit_avg_ms"] = (sum(s.submit_times)
                                  / len(s.submit_times)) * 1000.0
        stats["submit_max_ms"] = max(s.submit_times) * 1000.0
        stats["submit_total_ms"] = sum(s.submit_times) * 1000.0
    if (s.first_dab_t is not None and s.last_dab_t is not None
            and s.last_dab_t > s.first_dab_t and s.dab_count > 1):
        stats["dabs_per_s"] = ((s.dab_count - 1)
                               / (s.last_dab_t - s.first_dab_t))
    return stats


def _apply_seam_transport(s):
    """Obsolete topology texel-center transport; intentionally disabled."""
    return 0
    # Retained temporarily below only to ease comparison while the
    # conservative boundary experiment is validated; it is unreachable.
    if (getattr(s, "batch_seam_transfer", None) is None
            or getattr(s, "seam_coverage_tex", None) is None
            or s.history_backend is None
            or s.settings.get("brush_mode", "PAINT") not in {"PAINT", "ERASE"}):
        return 0
    keys = tuple(s.settings.get("channel_keys", ()))
    targets = set(s.settings.get("brush_target_channel_keys", keys))
    eligible = set(s.seam_channel_keys) & targets
    count = 0
    started = time.perf_counter()
    for index, texture in enumerate(s.paint_texs):
        channel = keys[index] if index < len(keys) else str(index)
        if channel not in eligible:
            continue
        # The seam batch can sample distant atlas locations. Seed only its
        # sparse source/destination strips into the shared absolute-coordinate
        # scratch texture instead of copying the complete canvas.
        for x, y, width, height in s.seam_strip_rects:
            s.history_backend._draw_copy(
                texture, s.soften_scratch_fb, (x, y, width, height),
                (x / s.size, y / s.size),
                (width / s.size, height / s.size))
        with s.single_fbs[index].bind():
            s.single_fbs[index].viewport_set(0, 0, s.size, s.size)
            gpu.state.blend_set('NONE')
            gpu.state.depth_test_set('NONE')
            gpu.state.depth_mask_set(False)
            gpu.state.face_culling_set('NONE')
            shader = s.seam_transfer_shader
            shader.bind()
            shader.uniform_sampler("source_pixels", s.soften_scratch)
            shader.uniform_sampler("coverage_tex", s.seam_coverage_tex)
            s.batch_seam_transfer.draw(shader)
        count += 1
    if count:
        for rect in s.seam_strip_rects:
            x, y, width, height = rect
            s.stroke_dirty = union_bbox(s.stroke_dirty, (
                x / s.size, y / s.size,
                (x + width) / s.size, (y + height) / s.size))
        elapsed = (time.perf_counter() - started) * 1000.0
        s.seam_transfer_ms += elapsed
        _log_line("GPU_PAINT_UV_SEAM status=transported channels=%d "
                  "strips=%d transfer_ms=%.3f" % (
                      count, len(s.seam_strip_rects), elapsed))
    return count


def _finalize_stroke_gpu(s):
    """Close one stroke without readback, drain, or Blender Image writes."""
    global _last_stroke_stats
    s.pending_finalize = False
    gutter_rects = s.stroke_gutter_rects
    if (gutter_rects and s.gutter_offset_map is not None
            and s.gutter_apply_shader is not None
            and s.history_backend is not None):
        keys = tuple(s.settings.get("channel_keys", ()))
        for index in range(s.channels):
            channel = keys[index] if index < len(keys) else str(index)
            for gutter_rect in sparse_pixel_rects(gutter_rects.get(channel)):
                x, y, width, height = gutter_rect
                s.history_backend._draw_copy(
                    s.paint_texs[index], s.soften_scratch_fb, gutter_rect,
                    (x / s.size, y / s.size),
                    (width / s.size, height / s.size))
                s.gutter_apply_ms += uv_gutters.apply_gutters_into(
                    s.soften_scratch, s.single_fbs[index],
                    s.gutter_offset_map, gutter_rect,
                    s.gutter_apply_shader, s.gutter_apply_batch)
                s.stroke_dirty = union_bbox(s.stroke_dirty, (
                    x / s.size, y / s.size,
                    (x + width) / s.size, (y + height) / s.size))
    if s.stroke_dirty_full:
        s.session_dirty_full = True
    else:
        s.session_dirty = union_bbox(s.session_dirty, s.stroke_dirty)
    s.stroke_dirty = None
    s.stroke_dirty_full = False
    s.stroke_gutter_rects = {}
    if s.stroke_transaction is not None:
        commit_started = time.perf_counter()
        s.stroke_transaction.commit()
        s.undo_commit_ms = getattr(s, "undo_commit_ms", 0.0) + (
            time.perf_counter() - commit_started) * 1000.0
        s.stroke_transaction = None
    stats = _stroke_stats(s)
    _last_stroke_stats = stats
    _log_line("GPU_PAINT_SPIKE_STROKE "
              + " ".join("%s=%s" % (
                  k, ("%.4f" % v) if isinstance(v, float) else v)
                  for k, v in sorted(stats.items())))


def _apply_history_action(s):
    action = s.pending_history_action
    s.pending_history_action = None
    if s.history_backend is None:
        return
    if action == 'UNDO':
        record = s.history.undo(s.history_backend)
    else:
        record = s.history.redo(s.history_backend)
    if record is not None:
        # A later explicit/session-exit flush must persist the restored state.
        s.session_dirty_full = True


def _flush_session_gpu(s):
    """Read resident textures only at an explicit/session-exit boundary."""
    import numpy as np

    s.pending_flush = False
    if s.session_dirty is None and not s.session_dirty_full:
        return
    s.flush_in_flight = True
    size = s.size
    n = s.channels
    stats = _stroke_stats(s)
    stats["deferred"] = 0

    # Sub-rect decision: the stroke's accumulated conservative UV bbox,
    # unless tracking was unavailable, the user disabled it, or the
    # rect is no smaller than the full texture.
    use_subrect = bool(s.settings.get("subrect", True))
    rect = None
    if use_subrect and not s.session_dirty_full:
        rect = uv_bbox_to_pixel_rect(s.session_dirty, size)
    if rect is not None and rect[2] * rect[3] >= size * size:
        rect = None
    rx, ry, rw, rh = rect if rect is not None else (0, 0, size, size)
    stats["readback_rect"] = ("%dx%d" % (rw, rh)) if rect is not None \
        else "full"

    # Full-size CPU mirrors: sub-rect reads scatter into them and
    # foreach_set consumes them whole (Image.pixels has no partial write).
    # Every mirror starts from its binding-owned Image.
    if s.cpu_mirrors is None:
        s.cpu_mirrors = []
        for i in range(n):
            mirror = np.zeros(size * size * 4, dtype=np.float32)
            image = bpy.data.images.get(s.image_names[i])
            if (image is not None and image.size[0] == size
                    and image.size[1] == size):
                try:
                    image.pixels.foreach_get(mirror)
                    if _channel_blend(s, i) != "ADD":
                        # Mirrors live in framebuffer space: sub-rect
                        # reads scatter premultiplied MIX pixels into
                        # them, so the straight canvas seed converts up
                        # front (zeros are identical in both spaces).
                        premultiply_canvas(mirror)
                except Exception:
                    pass
            s.cpu_mirrors.append(mirror)

    t_read = 0.0
    t_conv = 0.0
    path = None
    alpha_max = [0.0] * n
    # 1x1 reads drain every batch before the atomic all-image finalize.
    t0 = time.perf_counter()
    for fb in s.paint_fbs:
        with fb.bind():
            fb.read_color(0, 0, 1, 1, 4, 0, 'FLOAT')
    stats["drain_ms"] = (time.perf_counter() - t0) * 1000.0

    for (_blend, indices), fb in zip(s.target_batches, s.paint_fbs):
        with fb.bind():
            for slot, target_index in enumerate(indices):
                view = s.cpu_mirrors[target_index].reshape(size, size, 4)
                sub = None
                direct = False
                t0 = time.perf_counter()
                if _read_into_numpy:
                    try:
                        if rect is not None:
                            sub = np.empty((rh, rw, 4), dtype=np.float32)
                            fb.read_color(
                            rx, ry, rw, rh, 4, slot, 'FLOAT',
                            data=gpu.types.Buffer('FLOAT', (rh, rw, 4),
                                                  sub))
                        else:
                            fb.read_color(
                            0, 0, size, size, 4, slot, 'FLOAT',
                            data=gpu.types.Buffer('FLOAT',
                                                  (size, size, 4), view))
                        direct = True
                        path = path or "read_into_numpy"
                    except Exception:
                        sub = None
                        direct = False
                if not direct:
                    buf = fb.read_color(rx, ry, rw, rh, 4, slot, 'FLOAT')
                    t_read += time.perf_counter() - t0
                    path = path or _buffer_numpy_path
                    t0 = time.perf_counter()
                    flat = buffer_to_numpy(buf, _buffer_numpy_path)
                    if rect is not None:
                        view[ry:ry + rh, rx:rx + rw] = flat.reshape(
                            rh, rw, 4)
                    else:
                        s.cpu_mirrors[target_index][:] = flat
                    t_conv += time.perf_counter() - t0
                else:
                    t_read += time.perf_counter() - t0
                    if sub is not None:
                        t0 = time.perf_counter()
                        view[ry:ry + rh, rx:rx + rw] = sub
                        t_conv += time.perf_counter() - t0
                # Diagnose the actual dirty-region attachment content, not
                # merely whether an Image.pixels write was attempted.
                region_pixels = (view[ry:ry + rh, rx:rx + rw]
                                 if rect is not None else view)
                if region_pixels.size:
                    alpha_max[target_index] = float(
                        region_pixels[..., 3].max())

    stats["fb_read_ms"] = t_read * 1000.0
    stats["fb_read_avg_ch_ms"] = t_read * 1000.0 / n
    stats["readback_path"] = path or "none"
    stats["to_numpy_ms"] = t_conv * 1000.0
    stats["alpha_max"] = ",".join("%.4f" % value
                                   for value in alpha_max)

    if DEBUG_COMPARE_READS:
        # 0.1.0 A/B probe: GPUTexture.read() (returns the attachment's
        # own format — half floats for RGBA16F). Costs a second full
        # GPU->CPU transfer; never part of the production path.
        try:
            t0 = time.perf_counter()
            s.paint_texs[0].read()
            stats["tex_read_ms"] = (time.perf_counter() - t0) * 1000.0
        except Exception:
            stats["tex_read_ms"] = float("nan")

    # Canvases store STRAIGHT alpha: MIX mirrors (premultiplied
    # framebuffer space) divide back out on a copy; ADD (Height)
    # mirrors sync raw, exactly as before.
    s.pending_pixels = [
        (mirror if _channel_blend(s, i) == "ADD"
         else unpremultiply_readback(mirror), name)
        for i, (mirror, name)
        in enumerate(zip(s.cpu_mirrors, s.image_names))]
    s.pending_gpu_stats = stats
    s.session_dirty = None
    s.session_dirty_full = False


# ---------------------------------------------------------------------------
# Viewport preview + stats overlay
# ---------------------------------------------------------------------------


def preview_framebuffer_view_proj(fallback=None):
    """Return the clip matrix that matches POST_VIEW framebuffer depth.

    Builtin overlay shaders consume ``gpu.matrix`` (projection @ view).
    ``RegionView3D.perspective_matrix`` is the 3D view's classic
    win@view product and is correct for dab/prepass math against the
    private linear-depth target, but it can disagree with the overlay
    framebuffer's clip-space Z. Using it for the opaque Lit PBR coat
    makes ``LESS_EQUAL`` pass for fragments that other meshes already
    covered, so intersecting objects in front of the paint target
    disappear.

    Falls back to ``fallback`` when ``gpu.matrix`` is unavailable
    (headless) or raises.
    """
    try:
        return (gpu.matrix.get_projection_matrix()
                @ gpu.matrix.get_model_view_matrix())
    except Exception:
        return fallback


def _draw_composed_preview(s):
    """Draw a low-cost PBR approximation from all resident textures."""
    if s is not None and s.gpu_ready:
        _refresh_base_normal_resources(s)
    if (not s.gpu_ready or s.preview_shader is None
            or s.batch_preview is None or s.view_proj is None
            or not s.paint_texs):
        return
    t0 = time.perf_counter()
    keys = tuple(s.settings.get("channel_keys", ()))
    by_key = {key: s.paint_texs[i] for i, key in enumerate(keys)
              if i < len(s.paint_texs)}
    fallback = s.paint_texs[0]
    shader = s.preview_shader
    shader.bind()
    try:
        camera_position = s.view.inverted().translation
    except Exception:
        camera_position = (0.0, 0.0, 10.0)
    preview_globals = {
        "model_matrix": s.model,
        "view_proj_matrix": preview_framebuffer_view_proj(s.view_proj),
        "camera_position": tuple(camera_position),
        "preview_opacity": 1.0,
        "preview_mode": preview_mode_index(s.settings.get("preview_mode")),
        "environment_ready": 1.0 if s.environment_tex is not None else 0.0,
        "preview_lighting": (
        float(s.settings.get("preview_environment_exposure", 0.0)),
        float(s.settings.get("preview_environment_rotation", 0.0)),
        float(s.settings.get("preview_key_strength", 1.0)),
        float(s.settings.get("preview_key_rotation", 0.0))),
        "preview_fill": (
            float(s.settings.get("preview_fill_strength", 1.0)),
            float(s.settings.get("preview_roughness_readability", 0.0)),
            0.0, 0.0),
    }
    resolved = bool(s.stack_spec and s.stack_spec.get("enabled"))
    base_normal_enabled = s.base_normal_tex is not None
    preview_globals["base_normal_enabled"] = (
        1.0 if base_normal_enabled else 0.0)
    occluder_ready = (s.occluder_tex is not None
                      and s.view_depth_plane is not None)
    preview_globals["occluder_ready"] = 1.0 if occluder_ready else 0.0
    if s.view_depth_plane is not None:
        preview_globals["view_depth_plane"] = s.view_depth_plane
    preview_globals["base_normal_options"] = (
        max(0.0, float(s.settings.get("base_normal_strength", 1.0))),
        1.0 if s.settings.get("base_normal_invert_green", False) else 0.0)
    preview_records = {}
    for key in GPU_PAINT_CHANNEL_KEYS:
        channel_spec = (s.stack_spec.get("channels", {}).get(key, {})
                        if resolved else {})
        active_spec = channel_spec.get("active") or {}
        active_tex = by_key.get(key) or s.active_preview_texs.get(key)
        active = bool(active_tex) and bool(active_spec or not resolved)
        baseline_value = (s.baseline_values.get(key)
                          or _vec4(channel_spec.get(
                              "seed", model.seed_native(
                                  model.CHANNEL_MAP[key]))))
        baseline_tex = s.baseline_texs.get(key)
        record = {
            "has": 1.0 if (resolved or active or (
                key == "normal" and base_normal_enabled)) else 0.0,
            "active": 1.0 if active else 0.0,
            "active_factor": float(active_spec.get("factor", 1.0)),
            "active_blend": _BLEND_INDEX.get(
                active_spec.get("blend", "MIX"), 0),
            "baseline_value": baseline_value,
            "baseline_is_texture": 1.0 if baseline_tex is not None else 0.0,
        }
        if key != "normal":
            upper_tex = s.upper_transform_texs.get(key)
            record.update({
                "upper_c": _vec4(1.0), "upper_d": _vec4(0.0),
                "upper_present": 1.0 if upper_tex is not None else 0.0,
                "upper_factor": 1.0,
                "upper_blend": 0,
            })
            shader.uniform_sampler(
                "upper_" + key + "_tex", upper_tex or fallback)
        preview_records[key] = record
        shader.uniform_sampler(key + "_tex", active_tex or fallback)
        shader.uniform_sampler("baseline_" + key + "_tex",
                               baseline_tex or fallback)
    pack_preview_ubo(preview_records, preview_globals, s.preview_ubo_data)
    s.preview_ubo.update(s.preview_ubo_data)
    shader.uniform_block(PREVIEW_UBO_NAME, s.preview_ubo)
    shader.uniform_sampler("environment_atlas",
                           (s.environment_tex
                            if s.environment_tex is not None else fallback))
    shader.uniform_sampler("base_normal_tex", s.base_normal_tex or fallback)
    shader.uniform_sampler("occluder_depth_tex",
                           s.occluder_tex if occluder_ready else fallback)
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    # The preview is a second draw of the complete mesh. It must write depth
    # while drawing so its front triangles reject its own nearly coincident
    # rear triangles; testing only against Blender's earlier mesh depth lets
    # every bias-shifted shell pass independently.
    gpu.state.depth_mask_set(True)
    gpu.state.face_culling_set('BACK')
    s.batch_preview.draw(shader)
    elapsed = (time.perf_counter() - t0) * 1000.0
    s.preview_submit_ms = elapsed
    s.preview_submit_count += 1
    if s.preview_submit_count == 1:
        s.preview_submit_avg_ms = elapsed
    else:
        s.preview_submit_avg_ms += (
            elapsed - s.preview_submit_avg_ms) / s.preview_submit_count


def stencil_preview_quad(region_size, cursor, radius, settings):
    """Return POST_PIXEL quad corners using the dab shader's transform.

    Corners are ordered counter-clockwise from lower-left. ``None`` means
    there is no visible preview (disabled/missing stencil or Brush Alpha has
    no cursor yet). This pure seam keeps projection and preview testable in
    background Blender.
    """
    return gpu_overlays.stencil_preview_quad(
        region_size, cursor, radius, settings)


def _draw_stencil_preview(s, region):
    """Draw the active GPU stencil and a crisp projection boundary."""
    gpu_overlays.draw_stencil_preview(
        s, region,
        lambda: material_inspect_active() or material_inspect_requested(),
        _ensure_stencil_texture, stencil_preview_shader_create_info)


def _draw_brush_reticle(s):
    """Draw a screen-space circle using the exact GPU dab radius."""
    gpu_overlays.draw_brush_reticle(
        s, lambda: material_inspect_active()
        or material_inspect_requested())


def _format_scene_length(value, scene_unit_scale=1.0):
    """Compact metric label for a Blender-unit distance."""
    return gpu_overlays.format_scene_length(value, scene_unit_scale)


def _sss_cursor_surface(s, region, rv3d):
    """Return (world hit, pixels/world-unit, bbox diagonal), or None.

    The ray hit is deliberate: a screen radius calculated at the object
    origin is misleading on meshes with meaningful depth variation.
    """
    return gpu_overlays._sss_cursor_surface(s, region, rv3d)


def _ensure_overlay_circle(s):
    """One immutable unit-circle batch per session, not per draw frame."""
    gpu_overlays._ensure_overlay_circle(s)


def _draw_sss_caliper(s, region, rv3d):
    gpu_overlays.draw_sss_caliper(
        s, region, rv3d,
        lambda: material_inspect_active() or material_inspect_requested())


def _overlay_text_lines(s):
    dirty = " | unsaved GPU changes" if has_unflushed_changes() else ""
    lines = ["Impasto GPU paint — LMB paints  (RMB / Esc flushes + stops)"
             + dirty]
    if s.error is not None:
        lines.append("ERROR (see console): %s"
                     % s.error.strip().splitlines()[-1][:80])
        return lines
    if s.stack_spec:
        lines.append("stack preview: %s" % s.stack_spec.get(
            "status", "active layer only"))
    if s.stroke_active:
        n = s.dab_count + len(s.dab_queue)
        line = "stroke: %d dabs" % n
        if s.submit_times:
            avg = sum(s.submit_times) / len(s.submit_times) * 1000.0
            line += "  |  submit avg %.3f ms" % avg
        lines.append(line)
    st = _last_stroke_stats
    if st:
        lines.append(
            "last stroke: %d dabs | %.1f dabs/s | submit avg %.3f ms "
            "(max %.3f)" % (st.get("dabs", 0), st.get("dabs_per_s", 0.0),
                            st.get("submit_avg_ms", 0.0),
                            st.get("submit_max_ms", 0.0)))
        if st.get("deferred", 0):
            lines.append("ch=%d | prepass %.2f ms | pen-up sync: deferred"
                         % (st.get("channels", 1),
                            st.get("prepass_ms", 0.0)))
        else:
            lines.append(
                "flush: ch=%d rect=%s | readback %.2f ms | pixels %.2f ms"
                % (st.get("channels", 1), st.get("readback_rect", "full"),
                   st.get("fb_read_ms", 0.0),
                   st.get("pixels_write_ms", 0.0)))
    return lines


def _draw_stats_overlay(s):
    gpu_overlays.draw_text_lines(_overlay_text_lines(s))
