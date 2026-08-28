# SPDX-License-Identifier: GPL-2.0-or-later
"""Viewport overlay geometry and drawing for GPU paint and the SSS caliper."""

import math

import bpy
import gpu

from .. import stencil
from .caliper import sss_caliper_layout


def stencil_preview_quad(region_size, cursor, radius, settings):
    """Return POST_PIXEL stencil corners, or ``None`` when hidden."""
    if not settings.get("stencil_enabled", False) \
            or not settings.get("stencil_image_name", ""):
        return None
    projection = settings.get("stencil_projection", "VIEW_STENCIL")
    scale = tuple(settings.get("stencil_scale", (0.35, 0.35)))
    if projection == "BRUSH_ALPHA":
        if cursor is None:
            return None
        center = (float(cursor[0]), float(cursor[1]))
        half_extent = (float(radius) * float(scale[0]),
                       float(radius) * float(scale[1]))
    else:
        position = tuple(settings.get("stencil_position", (0.5, 0.5)))
        center = (float(position[0]) * float(region_size[0]),
                  float(position[1]) * float(region_size[1]))
        half_extent = (0.5 * float(scale[0]) * float(region_size[0]),
                       0.5 * float(scale[1]) * float(region_size[1]))
    angle = float(settings.get("stencil_rotation", 0.0))
    cs, sn = math.cos(angle), math.sin(angle)
    points = []
    for sx, sy in ((-1.0, -1.0), (1.0, -1.0),
                   (1.0, 1.0), (-1.0, 1.0)):
        lx, ly = sx * half_extent[0], sy * half_extent[1]
        points.append((center[0] + cs * lx - sn * ly,
                       center[1] + sn * lx + cs * ly))
    return tuple(points)


def draw_stencil_preview(session, region, inspect_active,
                         ensure_texture, shader_create_info):
    """Draw the active stencil image and its projection boundary."""
    if inspect_active():
        return
    points = stencil_preview_quad(
        (region.width, region.height), session.cursor,
        max(1.0, float(session.settings.get("radius", 50.0))),
        session.settings)
    if points is None:
        return
    stencil_tex = ensure_texture(session)
    if stencil_tex is None:
        return
    from gpu_extras.batch import batch_for_shader
    if session.stencil_preview_shader is None:
        session.stencil_preview_shader = gpu.shader.create_from_info(
            shader_create_info())
    clip = [((point[0] / region.width) * 2.0 - 1.0,
             (point[1] / region.height) * 2.0 - 1.0) for point in points]
    uv = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    batch = batch_for_shader(session.stencil_preview_shader, "TRI_FAN",
                             {"pos": clip, "uv": uv})
    outline_shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    outline = batch_for_shader(outline_shader, "LINE_LOOP", {"pos": points})
    prior_blend = gpu.state.blend_get()
    try:
        gpu.state.blend_set("ALPHA")
        session.stencil_preview_shader.bind()
        session.stencil_preview_shader.uniform_float(
            "stencil_preview_opacity", 0.38)
        session.stencil_preview_shader.uniform_sampler(
            "stencil_preview_tex", stencil_tex)
        batch.draw(session.stencil_preview_shader)
        outline_shader.bind()
        outline_shader.uniform_float("color", (1.0, 0.72, 0.18, 0.95))
        outline.draw(outline_shader)
        _draw_stencil_handles(session.settings,
                              (region.width, region.height))
    finally:
        gpu.state.blend_set(prior_blend)


# Dark, cool, saturated: green scales, cyan rotates. One fill each, no rim.
_SCALE_FILL = (0.04, 0.58, 0.32, 0.96)
_ROTATE_FILL = (0.00, 0.56, 0.68, 0.96)
_HINT_COLOR = (0.70, 0.76, 0.78, 0.40)
_SCALE_HALF = 13.0
_ROTATE_RADIUS = 15.6
_HANDLE_PAD = 4.0
_handle_shader = None

HANDLE_VERT_SRC = """
void main()
{
    gl_Position = vec4(pos, 0.0, 1.0);
    handleUV = uv;
}
"""

HANDLE_FRAG_SRC = """
void main()
{
    vec2 p = handleUV;
    float dist;
    if (handle_shape < 0.5) {
        dist = length(p) - handle_radius;
    } else {
        vec2 q = abs(p) - vec2(handle_radius);
        dist = max(q.x, q.y);
    }
    float aa = max(fwidth(dist), 1.0e-4);
    float fill_a = 1.0 - smoothstep(-aa, aa, dist);
    if (fill_a < 0.02) {
        discard;
    }
    fragColor = handle_fill * fill_a;
}
"""


def _handle_shader_create_info():
    iface = gpu.types.GPUStageInterfaceInfo("impasto_stencil_handle_iface")
    iface.smooth('VEC2', "handleUV")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('VEC4', "handle_fill")
    info.push_constant('FLOAT', "handle_shape")
    info.push_constant('FLOAT', "handle_radius")
    info.vertex_in(0, 'VEC2', "pos")
    info.vertex_in(1, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(HANDLE_VERT_SRC)
    info.fragment_source(HANDLE_FRAG_SRC)
    return info


def _ensure_handle_shader():
    global _handle_shader
    if _handle_shader is not None:
        return _handle_shader
    try:
        _handle_shader = gpu.shader.create_from_info(_handle_shader_create_info())
    except Exception:
        _handle_shader = False
    return _handle_shader or None


def _px_to_ndc(x, y, width, height):
    return ((x / max(width, 1.0)) * 2.0 - 1.0,
            (y / max(height, 1.0)) * 2.0 - 1.0)


def _draw_poly(shader, mode, points, color):
    from gpu_extras.batch import batch_for_shader
    batch = batch_for_shader(shader, mode, {"pos": points})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _oriented_quad(cx, cy, half, rotation):
    cosine, sine = math.cos(rotation), math.sin(rotation)
    local = ((-half, -half), (half, -half), (half, half), (-half, half))
    uv = ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0))
    corners = []
    for lx, ly in local:
        corners.append((cx + cosine * lx - sine * ly,
                        cy + sine * lx + cosine * ly))
    return corners, uv


def _draw_sdf_knob(cx, cy, pixel_radius, region_size, shape, fill,
                   rotation=0.0):
    shader = _ensure_handle_shader()
    if shader is None:
        return
    from gpu_extras.batch import batch_for_shader
    width, height = region_size
    half = pixel_radius + _HANDLE_PAD
    corners, uv = _oriented_quad(cx, cy, half, rotation)
    ndc = [_px_to_ndc(x, y, width, height) for x, y in corners]
    batch = batch_for_shader(shader, "TRI_FAN", {"pos": ndc, "uv": uv})
    shader.bind()
    shader.uniform_float("handle_fill", fill)
    shader.uniform_float("handle_shape", shape)
    shader.uniform_float("handle_radius", pixel_radius / half)
    batch.draw(shader)


def _unit(dx, dy):
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return (1.0, 0.0)
    return (dx / length, dy / length)


def _arrowhead(tip, direction, size=8.4):
    dx, dy = _unit(*direction)
    px, py = -dy, dx
    back = (tip[0] - dx * size, tip[1] - dy * size)
    return (
        tip,
        (back[0] + px * size * 0.55, back[1] + py * size * 0.55),
        (back[0] - px * size * 0.55, back[1] - py * size * 0.55),
    )


def _capsule_tris(p0, p1, width):
    dx, dy = _unit(p1[0] - p0[0], p1[1] - p0[1])
    nx, ny = -dy * width * 0.5, dx * width * 0.5
    a = (p0[0] + nx, p0[1] + ny)
    b = (p0[0] - nx, p0[1] - ny)
    c = (p1[0] - nx, p1[1] - ny)
    d = (p1[0] + nx, p1[1] + ny)
    return (a, b, c, a, c, d)


def _thick_arc_tris(cx, cy, radius, width, start, end, steps=36):
    inner = radius - width * 0.5
    outer = radius + width * 0.5
    tris = []
    for i in range(steps):
        t0 = start + (end - start) * (i / steps)
        t1 = start + (end - start) * ((i + 1) / steps)
        a0 = (cx + math.cos(t0) * inner, cy + math.sin(t0) * inner)
        b0 = (cx + math.cos(t0) * outer, cy + math.sin(t0) * outer)
        a1 = (cx + math.cos(t1) * inner, cy + math.sin(t1) * inner)
        b1 = (cx + math.cos(t1) * outer, cy + math.sin(t1) * outer)
        tris.extend((a0, b0, b1, a0, b1, a1))
    return tris


def _draw_scale_hints(shader, origin, axes):
    for axis in axes:
        dx, dy = _unit(*axis)
        length = 22.0
        p0 = (origin[0] - dx * length, origin[1] - dy * length)
        p1 = (origin[0] + dx * length, origin[1] + dy * length)
        _draw_poly(shader, "TRIS", _capsule_tris(p0, p1, 3.2), _HINT_COLOR)
        _draw_poly(shader, "TRIS", _arrowhead(p1, axis, 9.0), _HINT_COLOR)
        _draw_poly(shader, "TRIS", _arrowhead(p0, (-axis[0], -axis[1]), 9.0),
                   _HINT_COLOR)


def _draw_rotate_hints(shader, cx, cy):
    radius = _ROTATE_RADIUS + 12.0
    for start, end in ((0.28, 2.35), (0.28 + math.pi, 2.35 + math.pi)):
        tris = _thick_arc_tris(cx, cy, radius, 3.4, start, end)
        _draw_poly(shader, "TRIS", tris, _HINT_COLOR)
        tip_t = end
        prev_t = end - 0.12
        tip = (cx + math.cos(tip_t) * radius, cy + math.sin(tip_t) * radius)
        prev = (cx + math.cos(prev_t) * radius, cy + math.sin(prev_t) * radius)
        _draw_poly(shader, "TRIS", _arrowhead(tip, (tip[0] - prev[0],
                                                    tip[1] - prev[1]), 9.0),
                   _HINT_COLOR)


def _scale_hint_axes(name, rotation):
    cosine, sine = math.cos(rotation), math.sin(rotation)
    local_x = (cosine, sine)
    local_y = (-sine, cosine)
    if name in ("scale_l", "scale_r"):
        return (local_x,)
    if name in ("scale_t", "scale_b"):
        return (local_y,)
    return (local_x, local_y)


def _draw_stencil_handles(settings, region_size):
    """Corner/edge scale boxes and a rotate knob in the view plane."""
    if settings.get("stencil_projection") != "VIEW_STENCIL":
        return
    handles = stencil.planar_handle_points(region_size, settings)
    if not handles:
        return
    rotation = float(settings.get("stencil_rotation", 0.0))
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    for name, (x, y) in handles:
        if name == "rotate":
            _draw_rotate_hints(shader, x, y)
            _draw_sdf_knob(x, y, _ROTATE_RADIUS, region_size, 0.0,
                           _ROTATE_FILL)
            continue
        _draw_scale_hints(shader, (x, y), _scale_hint_axes(name, rotation))
        _draw_sdf_knob(x, y, _SCALE_HALF, region_size, 1.0,
                       _SCALE_FILL, rotation)
    top = next((pos for name, pos in handles if name == "scale_t"), None)
    rotate = next((pos for name, pos in handles if name == "rotate"), None)
    if top is not None and rotate is not None:
        _draw_poly(shader, "TRIS", _capsule_tris(top, rotate, 3.0),
                   _ROTATE_FILL)


def draw_brush_reticle(session, inspect_active):
    """Draw a screen-space circle using the exact dab radius."""
    if session.cursor is None or inspect_active():
        return
    from gpu_extras.batch import batch_for_shader
    x, y = session.cursor
    radius = max(1.0, float(session.settings.get("radius", 50.0)))
    segments = max(32, min(128, int(radius * 0.8)))
    points = [(x + math.cos(i * math.tau / segments) * radius,
               y + math.sin(i * math.tau / segments) * radius)
              for i in range(segments)]
    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    batch = batch_for_shader(shader, "LINE_LOOP", {"pos": points})
    prior_blend = gpu.state.blend_get()
    try:
        gpu.state.blend_set("ALPHA")
        shader.bind()
        shader.uniform_float("color", (1.0, 1.0, 1.0, 0.9))
        batch.draw(shader)
    finally:
        gpu.state.blend_set(prior_blend)


def format_scene_length(value, scene_unit_scale=1.0):
    metres = abs(float(value)) * max(float(scene_unit_scale), 1e-12)
    if metres >= 1.0:
        return "%.3g m" % metres
    if metres >= 1e-3:
        return "%.3g mm" % (metres * 1e3)
    if metres >= 1e-6:
        return "%.3g um" % (metres * 1e6)
    return "%.3g nm" % (metres * 1e9)


class SSSCaliperSource:
    """Minimal duck-typed source for ``draw_sss_caliper``.

    A GPU paint session already exposes these attributes; idle overlay
    uses this adapter so ring drawing does not require a live session.
    """

    __slots__ = ("cursor", "obj_name", "settings",
                 "overlay_circle_batch", "overlay_color_shader")

    def __init__(self, cursor=None, obj_name="", settings=None):
        self.cursor = cursor
        self.obj_name = obj_name or ""
        self.settings = dict(settings or {})
        self.overlay_circle_batch = None
        self.overlay_color_shader = None


def _sss_cursor_surface(overlay, region, rv3d):
    if overlay.cursor is None or region is None or rv3d is None:
        return None
    obj = bpy.data.objects.get(overlay.obj_name)
    if obj is None:
        return None
    from bpy_extras import view3d_utils
    from mathutils import Vector
    coord = Vector(overlay.cursor)
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    inv = obj.matrix_world.inverted_safe()
    local_origin = inv @ origin
    local_direction = (inv.to_3x3() @ direction).normalized()
    hit, location, _normal, _index = obj.ray_cast(local_origin,
                                                   local_direction)
    if not hit:
        return None
    world = obj.matrix_world @ location
    camera_right = (rv3d.view_matrix.inverted().to_3x3()
                    @ Vector((1.0, 0.0, 0.0))).normalized()
    p0 = view3d_utils.location_3d_to_region_2d(region, rv3d, world)
    p1 = view3d_utils.location_3d_to_region_2d(
        region, rv3d, world + camera_right)
    if p0 is None or p1 is None:
        return None
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    if not corners:
        return None
    xs, ys, zs = zip(*corners)
    diagonal = Vector((max(xs) - min(xs), max(ys) - min(ys),
                       max(zs) - min(zs))).length
    return world, (p1 - p0).length, diagonal


def _ensure_overlay_circle(overlay):
    if overlay.overlay_circle_batch is None:
        from gpu_extras.batch import batch_for_shader
        points = [(math.cos(i * math.tau / 96),
                   math.sin(i * math.tau / 96)) for i in range(96)]
        overlay.overlay_color_shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        overlay.overlay_circle_batch = batch_for_shader(
            overlay.overlay_color_shader, "LINE_LOOP", {"pos": points})


def draw_sss_caliper(overlay, region, rv3d, inspect_active,
                     brush_ring_hint=True):
    """Draw colored Scale×Radius rings.

    ``overlay`` needs ``cursor``, ``settings``, ``obj_name``, and the
    overlay batch/shader attributes. GPU sessions and the idle adapter
    both satisfy that contract.
    """
    if not overlay.settings.get("sss_caliper_enabled", False) \
            or inspect_active():
        return
    surface = _sss_cursor_surface(overlay, region, rv3d)
    if surface is None:
        return
    _world, pixels_per_unit, bbox_diagonal = surface
    scale = float(overlay.settings.get("sss_caliper_scale", 0.0))
    radius = tuple(overlay.settings.get(
        "sss_caliper_radius", (1.0, 0.2, 0.1)))
    effective, radii_px, percentages, too_small = sss_caliper_layout(
        scale, radius, pixels_per_unit, bbox_diagonal)
    if max(radii_px, default=0.0) <= 0.0:
        return
    _ensure_overlay_circle(overlay)
    colors = ((1.0, 0.16, 0.12, 0.9), (0.2, 1.0, 0.25, 0.9),
              (0.2, 0.45, 1.0, 0.9))
    prior_blend = gpu.state.blend_get()
    try:
        gpu.state.blend_set("ALPHA")
        for radius_px, color in zip(radii_px, colors):
            if radius_px < 0.5:
                continue
            with gpu.matrix.push_pop():
                gpu.matrix.translate((*overlay.cursor, 0.0))
                gpu.matrix.scale((radius_px, radius_px, 1.0))
                overlay.overlay_color_shader.bind()
                overlay.overlay_color_shader.uniform_float("color", color)
                overlay.overlay_circle_batch.draw(overlay.overlay_color_shader)
    finally:
        gpu.state.blend_set(prior_blend)
    import blf
    unit_scale = float(overlay.settings.get("scene_unit_scale", 1.0))
    labels = "  ".join("%s %s (%.2g%% mesh)" % (
        name, format_scene_length(distance, unit_scale), percentage)
        for name, distance, percentage in zip(
            ("R", "G", "B"), effective, percentages))
    lines = ["SSS CALIPER — colored rings = Scale x Radius RGB", labels]
    if brush_ring_hint:
        lines.append(
            "Colored rings zoom with mesh; white brush ring stays screen-sized")
    if too_small:
        lines.append("WARNING: SSS rings are very small relative to this mesh")
    blf.size(0, 11)
    for index, line in enumerate(lines):
        blf.position(0, overlay.cursor[0] + 14,
                     overlay.cursor[1] + 16 + index * 15, 0)
        blf.color(0, 1.0, 1.0, 1.0, 0.95)
        blf.draw(0, line)
    for name, radius_px, color, angle in zip(
            ("R", "G", "B"), radii_px, colors,
            (0.0, math.tau / 3.0, 2.0 * math.tau / 3.0)):
        if radius_px < 0.5:
            continue
        blf.position(0, overlay.cursor[0] + math.cos(angle) * radius_px + 3,
                     overlay.cursor[1] + math.sin(angle) * radius_px + 3, 0)
        blf.color(0, *color)
        blf.draw(0, name)


def draw_text_lines(lines):
    """Draw precomputed engine status lines in the viewport."""
    import blf
    blf.size(0, 12)
    blf.color(0, 1.0, 1.0, 1.0, 1.0)
    for index, line in enumerate(reversed(lines)):
        blf.position(0, 20, 60 + index * 18, 0)
        blf.draw(0, line)
