# SPDX-License-Identifier: GPL-2.0-or-later
"""Blender native texture-paint target activation (design section 5).

This module is the single owner of Blender paint state.  Keeping canvas
selection here prevents layer UI callbacks and operators from gradually
growing different paint-slot behaviour.
"""

import bpy

from . import compat
from . import model


class PaintTargetError(RuntimeError):
    """The active Impasto layer cannot be used as a paint canvas."""


def maybe_switch_material_preview(context, enabled=None):
    """Switch only the invoking VIEW_3D from Solid to Material Preview.

    Solid shading displays the active paint canvas rather than Impasto's
    composed shader and a transparent canvas can make the object disappear.
    Never touch other areas/viewports; return whether a switch occurred.
    """
    area = getattr(context, "area", None)
    if area is None or area.type != 'VIEW_3D':
        return False
    if enabled is None:
        addon = context.preferences.addons.get(__package__)
        enabled = (addon is None
                   or getattr(addon.preferences,
                              "auto_material_preview", True))
    if not enabled:
        return False
    space = getattr(area.spaces, "active", None)
    shading = getattr(space, "shading", None)
    if shading is None or shading.type != 'SOLID':
        return False
    shading.type = 'MATERIAL'
    return True


def activate_brush_tool(context):
    """Select Blender 5.1's main Texture Paint brush tool.

    Mode switching and tool switching are independent in Blender: entering
    Texture Paint can leave the remembered Select tool active, which gives no
    brush cursor and looks as if activation failed. Use the invoking 3D view,
    or another 3D view in the current screen for F3/menu invocations. Quietly
    return False in background mode or when no viewport exists.
    """
    area = getattr(context, "area", None)
    if area is None or area.type != 'VIEW_3D':
        screen = getattr(context, "screen", None)
        area = next((a for a in screen.areas if a.type == 'VIEW_3D'),
                    None) if screen is not None else None
    if area is None:
        return False
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    if region is None:
        return False
    try:
        with context.temp_override(area=area, region=region,
                                   space_data=area.spaces.active):
            result = bpy.ops.wm.tool_set_by_id(
                name="builtin.brush", space_type='VIEW_3D')
        return 'FINISHED' in result
    except (RuntimeError, TypeError):
        return False


def paint_binding(layer, channel_key=""):
    """The binding whose canvas native painting edits: the named
    channel, or the first enabled SHARED binding in registry order.
    Returns None when the layer has no paintable binding."""
    candidates = [b for b in layer.bindings
                  if b.enabled and b.mode == 'SHARED'
                  and b.name in model.CHANNEL_MAP]
    if channel_key:
        for b in candidates:
            if b.name == channel_key:
                return b
        return None
    candidates.sort(key=lambda b: model.CHANNEL_ORDER[b.name])
    return candidates[0] if candidates else None


def activate_paint_target(context, layer, channel_key=""):
    """Make one of ``layer``'s channel canvases Blender's image-mode
    texture-paint canvas (``channel_key`` selects which; empty picks
    the first enabled SHARED binding in registry order).

    This deliberately does not enter Texture Paint mode.  Layer selection is
    safe in Object/Edit mode; mode changes happen only through the explicit
    operator.  Returns whether the image colorspace needed repair.
    """
    obj = context.object
    if obj is None or obj.type != 'MESH':
        raise PaintTargetError("Select the mesh that owns this Impasto stack")
    if layer is None or layer.layer_type != 'PAINT':
        raise PaintTargetError("The active Impasto layer is not a paint layer")
    binding = paint_binding(layer, channel_key)
    if binding is None:
        raise PaintTargetError(
            "The active paint layer has no paintable %s binding"
            % (model.CHANNEL_MAP[channel_key].label if channel_key
               in model.CHANNEL_MAP else "channel"))
    channel = model.CHANNEL_MAP[binding.name]
    image = bpy.data.images.get(binding.image_name or layer.image_name)
    if image is None:
        raise PaintTargetError("The %s canvas of the active paint layer "
                               "is missing" % channel.label)

    uv_layers = obj.data.uv_layers
    if not uv_layers:
        raise PaintTargetError("The mesh needs a UV map before texture painting")
    uv_name = layer.uv_map
    uv = uv_layers.get(uv_name) if uv_name else uv_layers.active
    if uv is None:
        raise PaintTargetError("Paint layer UV map %r is missing" % uv_name)
    uv_layers.active = uv

    wanted = compat.resolve_colorspace(image, channel.colorspace)
    repaired = image.colorspace_settings.name != wanted
    if repaired:
        image.colorspace_settings.name = wanted

    image_paint = context.scene.tool_settings.image_paint
    image_paint.mode = 'IMAGE'
    image_paint.canvas = image

    # Keep Blender's conventional active image-node target aligned too.  The
    # explicit canvas above is authoritative, but selecting the generated
    # source node makes Image Editor/tool integrations show the same image.
    source_name = (model.n_binding_src(layer.name, binding.name)
                   if binding.image_name else model.n_src(layer.name))
    layer_tree = bpy.data.node_groups.get(model.layer_tree_name(layer.name))
    source = (layer_tree.nodes.get(source_name)
              if layer_tree is not None else None)
    if source is not None:
        for node in layer_tree.nodes:
            node.select = False
        source.select = True
        layer_tree.nodes.active = source
    return repaired


def activate_mask_target(context, layer, mask):
    """Make a layer-mask image Blender's native grayscale paint canvas."""
    obj = context.object
    if obj is None or obj.type != 'MESH':
        raise PaintTargetError("Select the mesh that owns this Impasto stack")
    image = bpy.data.images.get(mask.image_name) if mask else None
    if image is None:
        raise PaintTargetError("The selected mask image is missing")
    uv_layers = obj.data.uv_layers
    uv = uv_layers.get(mask.uv_map) if mask.uv_map else uv_layers.active
    if uv is None:
        raise PaintTargetError("The mask UV map is missing")
    uv_layers.active = uv
    wanted = compat.resolve_colorspace(image, "Non-Color")
    repaired = image.colorspace_settings.name != wanted
    if repaired:
        image.colorspace_settings.name = wanted
    settings = context.scene.tool_settings.image_paint
    settings.mode = 'IMAGE'
    settings.canvas = image
    layer_tree = bpy.data.node_groups.get(model.layer_tree_name(layer.name))
    source = (layer_tree.nodes.get(model.n_mask_src(layer.name, mask.name))
              if layer_tree else None)
    if source is not None:
        for node in layer_tree.nodes:
            node.select = False
        source.select = True
        layer_tree.nodes.active = source
    return repaired


def native_stroke_point(x, y, pressure, size, elapsed, is_start=False,
                        x_tilt=0.0, y_tilt=0.0):
    """Return one ``paint.image_paint`` stroke element.

    Kept as a small pure seam so the replay contract is testable without a
    live viewport.  Blender consumes region-relative coordinates here.
    """
    mouse = (float(x), float(y))
    return {
        "name": "",
        "location": (0.0, 0.0, 0.0),
        "mouse": mouse,
        "mouse_event": mouse,
        "pressure": max(0.0, float(pressure)),
        "size": max(1.0, float(size)),
        "x_tilt": float(x_tilt),
        "y_tilt": float(y_tilt),
        "time": max(0.0, float(elapsed)),
        "is_start": bool(is_start),
    }


def capture_native_state(context):
    """Snapshot the native state Impasto temporarily changes for replay.

    The brush pointer/asset and active workspace tool are deliberately not
    changed at all.  Keeping their identity in this snapshot makes that
    invariant explicit and gives tests a stable restoration contract.
    """
    settings = context.scene.tool_settings.image_paint
    brush = settings.brush
    state = {
        "canvas": settings.canvas,
        "paint_mode": settings.mode,
        "use_occlude": settings.use_occlude,
        "use_backface_culling": settings.use_backface_culling,
        "brush": brush,
        "brush_asset": getattr(settings, "brush_asset_reference", None),
    }
    unified = unified_paint_settings(context)
    if unified is not None:
        state.update({
            "unified": unified,
            "unified_color": tuple(unified.color),
            "unified_secondary_color": tuple(unified.secondary_color),
            "unified_use_color": getattr(
                unified, "use_unified_color", None),
        })
    if brush is not None:
        state.update({
            "color": tuple(brush.color),
            "secondary_color": tuple(brush.secondary_color),
            "blend": brush.blend,
        })
    return state


def unified_paint_settings(context):
    """Return Blender's unified paint settings across API generations.

    Blender 5.1 moved this pointer from ``ToolSettings`` onto the active
    ``ImagePaint`` settings. Prefer the current location and retain the older
    fallback for compatibility.
    """
    tool_settings = context.scene.tool_settings
    settings = getattr(tool_settings.image_paint,
                       "unified_paint_settings", None)
    if settings is None:
        settings = getattr(tool_settings, "unified_paint_settings", None)
    return settings


def configure_front_surface_paint(context):
    """Prevent projected native strokes from reaching hidden surfaces."""
    settings = context.scene.tool_settings.image_paint
    settings.use_occlude = True
    settings.use_backface_culling = True


def configure_native_replay_color(brush, unified, color,
                                  preferred_use_unified_color=None):
    """Set a native replay color without clamping HDR channel values.

    Blender's Brush color accepts HDR values, while UnifiedPaintSettings.color
    is limited to [0, 1].  Unified color normally overrides the Brush during
    image-paint replay, so temporarily bypass it only when the requested value
    is outside its RNA range.  The caller supplies the user's captured unified
    preference so every replay target starts from that preference; the whole
    state is restored by :func:`restore_native_state` after the logical stroke.

    Return whether unified color can represent, and was assigned, ``color``.
    """
    color = tuple(float(component) for component in color)
    brush.color = color
    if unified is None:
        return False

    if (preferred_use_unified_color is not None
            and hasattr(unified, "use_unified_color")):
        unified.use_unified_color = preferred_use_unified_color

    hard_min, hard_max = 0.0, 1.0
    try:
        prop = unified.bl_rna.properties["color"]
        hard_min, hard_max = float(prop.hard_min), float(prop.hard_max)
    except (AttributeError, KeyError, TypeError):
        pass
    representable = all(hard_min <= value <= hard_max for value in color)
    if representable:
        unified.color = color
    elif hasattr(unified, "use_unified_color"):
        unified.use_unified_color = False
    return representable


def restore_native_state(context, state):
    """Restore a state from :func:`capture_native_state`."""
    settings = context.scene.tool_settings.image_paint
    settings.mode = state["paint_mode"]
    settings.canvas = state["canvas"]
    settings.use_occlude = state["use_occlude"]
    settings.use_backface_culling = state["use_backface_culling"]
    brush = state.get("brush")
    # Never restore settings.brush: it is a read-only asset-driven pointer in
    # Blender 5.1.  We never replace it, and only restore properties when the
    # exact same brush is still active.
    if brush is not None and settings.brush is brush:
        brush.color = state["color"]
        brush.secondary_color = state["secondary_color"]
        brush.blend = state["blend"]
    unified = state.get("unified")
    current_unified = unified_paint_settings(context)
    if unified is not None and current_unified is not None:
        current_unified.color = state["unified_color"]
        current_unified.secondary_color = state["unified_secondary_color"]
        use_color = state.get("unified_use_color")
        if (use_color is not None
                and hasattr(current_unified, "use_unified_color")):
            current_unified.use_unified_color = use_color


def write_flushed_image_pixels(image, arr):
    """Write GPU readback into a Blender Image and refresh packed bytes.

    ``foreach_set`` updates ``Image.pixels``, which the viewport shows.
    Packed FILE images also keep a separate PNG blob. File Save and
    ``image.reload()`` can persist or restore that blob unless ``pack()``
    runs after the write. Generated images store pixels in the .blend and
    are left unpacked.

    This stayed latent while most canvases were generated. Packed emission
    canvases on BlackBody_Low (2026-08-29) showed the viewport flush and
    then lost the marks on save/reload.
    """
    image.pixels.foreach_set(arr)
    image.update()
    image.update_tag()
    if image.packed_file is None:
        return
    try:
        image.pack()
    except Exception as exc:
        print("[impasto] GPU flush updated pixels but could not re-pack %r: %s"
              % (image.name, exc))
