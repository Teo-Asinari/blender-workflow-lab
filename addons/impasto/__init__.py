# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto — non-destructive PBR layer stacks for Blender."""

bl_info = {
    "name": "Impasto",
    "author": "Teo Asinari",
    "version": (0, 15, 36),
    "blender": (5, 1, 0),
    "location": "3D Viewport > Sidebar (N) > Impasto tab",
    "description": "Non-destructive PBR material layer stacks",
    "category": "Paint",
}

if "model" in locals():
    import importlib
    model = importlib.reload(model)
    if "channel_paint" in locals():
        channel_paint = importlib.reload(channel_paint)
    else:
        from . import channel_paint
    debounce = importlib.reload(debounce)
    compat = importlib.reload(compat)
    reconcile = importlib.reload(reconcile)
    snapshot = importlib.reload(snapshot)
    engine = importlib.reload(engine)
    if "visibility" in locals():
        visibility = importlib.reload(visibility)
    else:
        from . import visibility
    if "brush_adapter" in locals():
        brush_adapter = importlib.reload(brush_adapter)
        tile_undo = importlib.reload(tile_undo)
    else:
        from . import brush_adapter
        from . import tile_undo
    if "ibl" in locals():
        ibl = importlib.reload(ibl)
    else:
        from . import ibl
    if "preview_stack" in locals():
        preview_stack = importlib.reload(preview_stack)
    else:
        from . import preview_stack
    if "stencil" in locals():
        stencil = importlib.reload(stencil)
    else:
        from . import stencil
    if "gpu_brush_math" in locals():
        gpu_brush_math = importlib.reload(gpu_brush_math)
        gpu_caliper = importlib.reload(gpu_caliper)
        gpu_overlays = importlib.reload(gpu_overlays)
        if "gpu_sss_caliper_overlay" in locals():
            gpu_sss_caliper_overlay = importlib.reload(gpu_sss_caliper_overlay)
        else:
            from .gpu import sss_caliper_overlay as gpu_sss_caliper_overlay
    else:
        from .gpu import brush_math as gpu_brush_math
        from .gpu import caliper as gpu_caliper
        from .gpu import overlays as gpu_overlays
        from .gpu import sss_caliper_overlay as gpu_sss_caliper_overlay
    gpu_engine = importlib.reload(gpu_engine)
    props = importlib.reload(props)
    if "paint" in locals():
        paint = importlib.reload(paint)
    else:
        from . import paint
    if "operator_support" in locals():
        operator_support = importlib.reload(operator_support)
    else:
        from . import operator_support
    if "flatten_export" in locals():
        flatten_export = importlib.reload(flatten_export)
    else:
        from . import flatten_export
    ops = importlib.reload(ops)
    if "ui_channels" in locals():
        ui_channels = importlib.reload(ui_channels)
    else:
        from . import ui_channels
    if "ui_paint" in locals():
        ui_paint = importlib.reload(ui_paint)
    else:
        from . import ui_paint
    if "ui_icons" in locals():
        ui_icons = importlib.reload(ui_icons)
    else:
        from . import ui_icons
    ui = importlib.reload(ui)
else:
    from . import model
    from . import channel_paint
    from . import debounce
    from . import compat
    from . import reconcile
    from . import snapshot
    from . import engine
    from . import visibility
    from . import brush_adapter
    from . import tile_undo
    from . import ibl
    from . import preview_stack
    from . import stencil
    from .gpu import brush_math as gpu_brush_math
    from .gpu import caliper as gpu_caliper
    from .gpu import overlays as gpu_overlays
    from .gpu import sss_caliper_overlay as gpu_sss_caliper_overlay
    from . import gpu_engine
    from . import props
    from . import paint
    from . import operator_support
    from . import flatten_export
    from . import ops
    from . import ui_channels
    from . import ui_paint
    from . import ui_icons
    from . import ui


def register():
    props.register()
    ops.register()
    ui.register()
    engine.register()
    gpu_sss_caliper_overlay.register()


def unregister():
    gpu_sss_caliper_overlay.unregister()
    engine.unregister()
    ui.unregister()
    ops.unregister()
    props.unregister()


if __name__ == "__main__":
    register()
