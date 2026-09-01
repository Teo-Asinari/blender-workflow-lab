# SPDX-License-Identifier: GPL-2.0-or-later
"""Idle SSS caliper: colored Scale×Radius rings outside GPU painting.

GPU paint still draws the caliper from its POST_PIXEL handler, including
the white brush reticle. This module only draws the colored rings, and
no-ops while a GPU session is already drawing them.
"""

from bpy.app.handlers import persistent

import bpy

from .overlays import SSSCaliperSource, draw_sss_caliper

POLL_INTERVAL = 0.05


class _State:
    __slots__ = ("handle", "source", "registered", "wanted")

    def __init__(self):
        self.handle = None
        self.source = SSSCaliperSource()
        self.registered = False
        self.wanted = False


_state = _State()


def _gpu_session_active():
    from .. import gpu_engine
    return gpu_engine.session_active()


def _active_mesh(context):
    for ctx in (context, bpy.context):
        if ctx is None:
            continue
        obj = (getattr(ctx, "object", None)
               or getattr(ctx, "active_object", None))
        if obj is None:
            view_layer = getattr(ctx, "view_layer", None)
            if view_layer is not None:
                obj = view_layer.objects.active
        if obj is not None and obj.type == 'MESH':
            return obj
    return None


def _scene(context):
    for ctx in (context, bpy.context):
        if ctx is None:
            continue
        scene = getattr(ctx, "scene", None)
        if scene is not None:
            return scene
    return None


def _caliper_settings(layer, scene):
    scale_length = 1.0
    if scene is not None:
        scale_length = float(scene.unit_settings.scale_length or 1.0)
    return {
        "sss_caliper_enabled": True,
        "sss_caliper_scale": float(layer.paint_sss_scale),
        "sss_caliper_radius": tuple(layer.paint_sss_radius),
        "scene_unit_scale": scale_length,
    }


def resolve_target(context=None):
    """Active Paint layer that wants the idle caliper, or ``None``."""
    from .. import engine
    obj = _active_mesh(context)
    if obj is None:
        return None
    tree = engine.find_stack_for_material(obj.active_material)
    if tree is None:
        return None
    layer = tree.impasto.active_layer()
    if (layer is None or layer.layer_type != 'PAINT'
            or not layer.show_sss_caliper):
        return None
    return {
        "object": obj,
        "layer": layer,
        "scale": float(layer.paint_sss_scale),
        "radius": tuple(layer.paint_sss_radius),
        "settings": _caliper_settings(layer, _scene(context)),
    }


def any_paint_caliper_enabled():
    """True if any Impasto Paint layer has the caliper toggle on."""
    try:
        from .. import engine
        for tree in engine.iter_stack_trees():
            for layer in tree.impasto.layers:
                if layer.layer_type == 'PAINT' and layer.show_sss_caliper:
                    return True
    except Exception:
        return False
    return False


def should_draw(context=None):
    """Idle overlay draw contract, ignoring cursor/ray hit."""
    if _gpu_session_active():
        return False
    return resolve_target(context) is not None


def region_local_cursor(window, region):
    """Window mouse in region pixels; ``None`` when outside the region."""
    if window is None or region is None:
        return None
    x = window.mouse_x - region.x
    y = window.mouse_y - region.y
    if x < 0 or y < 0 or x >= region.width or y >= region.height:
        return None
    return (float(x), float(y))


def timer_is_registered():
    try:
        return bpy.app.timers.is_registered(_timer_cb)
    except Exception:
        return False


def draw_handler_registered():
    return _state.handle is not None


def _tag_redraw_view3d():
    try:
        wm = bpy.context.window_manager
    except Exception:
        return
    if wm is None:
        return
    try:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _ensure_timer():
    try:
        if not bpy.app.timers.is_registered(_timer_cb):
            bpy.app.timers.register(_timer_cb, first_interval=POLL_INTERVAL,
                                    persistent=True)
    except Exception:
        pass


def _stop_timer():
    try:
        if bpy.app.timers.is_registered(_timer_cb):
            bpy.app.timers.unregister(_timer_cb)
    except Exception:
        pass


def _timer_cb():
    try:
        if not _state.registered or not any_paint_caliper_enabled():
            _state.wanted = False
            _tag_redraw_view3d()
            return None
        _state.wanted = True
        if not _gpu_session_active():
            _tag_redraw_view3d()
        return POLL_INTERVAL
    except Exception:
        return POLL_INTERVAL


def _draw():
    try:
        _draw_caliper()
    except Exception:
        pass


def _draw_caliper():
    if not _state.wanted or _gpu_session_active():
        return
    context = bpy.context
    region = getattr(context, "region", None)
    cursor = region_local_cursor(getattr(context, "window", None), region)
    if cursor is None:
        return
    target = resolve_target(context)
    if target is None:
        return
    rv3d = getattr(context, "region_data", None)
    source = _state.source
    source.cursor = cursor
    source.obj_name = target["object"].name
    source.settings = target["settings"]
    draw_sss_caliper(source, region, rv3d, lambda: False,
                     brush_ring_hint=False)


def _add_draw_handler():
    if _state.handle is not None:
        return
    try:
        _state.handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw, (), 'WINDOW', 'POST_PIXEL')
    except Exception:
        # No viewport (background mode): stay registered logically.
        _state.handle = None


def _remove_draw_handler():
    if _state.handle is None:
        return
    try:
        bpy.types.SpaceView3D.draw_handler_remove(_state.handle, 'WINDOW')
    except Exception:
        pass
    _state.handle = None


def sync(context=None):
    """Start or stop the cursor-poll timer from a property/layer change."""
    if not _state.registered:
        return
    _state.wanted = any_paint_caliper_enabled()
    if _state.wanted:
        _ensure_timer()
    else:
        _stop_timer()
    _tag_redraw_view3d()


def _deferred_sync():
    """Run after enable: register() itself still has RestrictData."""
    if _state.registered:
        sync()
    return None


def _schedule_sync():
    try:
        if not bpy.app.timers.is_registered(_deferred_sync):
            bpy.app.timers.register(_deferred_sync, first_interval=0.0)
            return
    except Exception:
        pass
    try:
        sync()
    except Exception:
        pass


def _cancel_deferred_sync():
    try:
        if bpy.app.timers.is_registered(_deferred_sync):
            bpy.app.timers.unregister(_deferred_sync)
    except Exception:
        pass


@persistent
def _on_load_post(*_args):
    sync()


def register():
    _state.registered = True
    _add_draw_handler()
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    _schedule_sync()


def unregister():
    _state.registered = False
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    _cancel_deferred_sync()
    _stop_timer()
    _remove_draw_handler()
    _state.source = SSSCaliperSource()
    _state.wanted = False
    _tag_redraw_view3d()
