# SPDX-License-Identifier: GPL-2.0-or-later
"""UV Island Overlay — per-island UV coloring and texel-density
checkerboard in the 3D viewport.

Three display modes: each UV island's faces tinted with a distinct
color (island boundaries at a glance), a checkerboard mapped through
the mesh's actual UVs (texel-density mismatches show as different
checker scales on the surface), or — the default since v1.4.0 — both
combined: the checker multiplied by each island's identity color, so
hue reads island membership while checker scale reads texel density.
"""

bl_info = {
    "name": "UV Island Overlay",
    "author": "Teo Asinari",
    "version": (1, 5, 2),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > UV Islands tab; also in the "
                "Overlays popover",
    "description": "Color each UV island distinctly, visualize texel "
                   "density as a UV checkerboard, or both combined, in "
                   "the 3D viewport",
    "category": "UV",
}

import bpy
from bpy.app.handlers import persistent
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       IntProperty)

if "overlay" in locals():
    import importlib
    density = importlib.reload(density)
    health = importlib.reload(health)
    islands = importlib.reload(islands)
    live = importlib.reload(live)
    overlay = importlib.reload(overlay)
else:
    from . import density
    from . import health
    from . import islands
    from . import live
    from . import overlay


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class UV_OT_island_overlay_toggle(bpy.types.Operator):
    """Toggle the UV island color overlay for the active mesh object"""
    bl_idname = "uv.island_overlay_toggle"
    bl_label = "Toggle UV Island Overlay"

    @classmethod
    def poll(cls, context):
        if overlay.is_enabled():
            return True  # always allow toggling OFF
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        wm = context.window_manager
        want = not wm.uv_island_overlay
        wm.uv_island_overlay = want
        if want and not wm.uv_island_overlay:
            # enable() failed and the update callback reverted the
            # property — tell the user instead of failing silently.
            self.report({'WARNING'},
                        "No active mesh object — overlay not enabled")
            return {'CANCELLED'}
        if wm.uv_island_overlay:
            self.report({'INFO'},
                        "UV island overlay on (%d islands)"
                        % overlay.island_count())
        return {'FINISHED'}


class UV_OT_island_overlay_refresh(bpy.types.Operator):
    """Recompute UV islands for the overlaid mesh (use after re-unwrapping)"""
    bl_idname = "uv.island_overlay_refresh"
    bl_label = "Refresh UV Island Overlay"

    @classmethod
    def poll(cls, context):
        return overlay.is_enabled()

    def execute(self, context):
        overlay.refresh(context)
        self.report({'INFO'},
                    "UV islands recomputed (%d islands)"
                    % overlay.island_count())
        return {'FINISHED'}


class UV_OT_health_analyze(bpy.types.Operator):
    """Analyze UV mappings and texel-density risks on the active mesh"""
    bl_idname = "uv.island_health_analyze"
    bl_label = "Analyze UV Health"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        wm = context.window_manager
        try:
            result = health.analyze_object(
                context.active_object,
                texture_size=wm.uv_health_texture_size,
                low_density_ratio=wm.uv_health_low_density_ratio,
                minimum_island_span_px=wm.uv_health_minimum_span_px)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "%d islands; %d duplicate UV triangles; "
                    "%d low-density islands; %d tiny islands" % (
                        result.island_count, result.duplicate_triangles,
                        len(result.low_density_islands),
                        len(result.tiny_islands)))
        return {'FINISHED'}


class UV_OT_health_select(bpy.types.Operator):
    """Select faces belonging to one UV-health issue category"""
    bl_idname = "uv.island_health_select"
    bl_label = "Select UV Health Issues"
    bl_options = {'REGISTER', 'UNDO'}

    issue: EnumProperty(items=(
        ('ZERO', "Collapsed UVs", "Faces with zero UV or 3D area"),
        ('DUPLICATE', "Duplicate Mappings",
         "Faces whose triangulated UV coordinates duplicate another triangle"),
        ('LOW_DENSITY', "Low Density", "Faces in below-threshold islands"),
        ('TINY', "Tiny Islands", "Faces in islands narrower than the pixel threshold"),
        ('OUTSIDE', "Outside 0-1", "Faces with UV coordinates outside the main tile"),
    ))

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH'

    def execute(self, context):
        result = health.last_result(context.active_object.name)
        if result is None:
            self.report({'ERROR'}, "Run Analyze UV Health first")
            return {'CANCELLED'}
        faces = {
            'ZERO': result.zero_area_faces,
            'DUPLICATE': result.duplicate_faces,
            'LOW_DENSITY': result.low_density_faces,
            'TINY': result.tiny_island_faces,
            'OUTSIDE': result.out_of_bounds_faces,
        }[self.issue]
        health.select_faces(context.active_object, faces)
        self.report({'INFO'}, "Selected %d face(s)" % len(faces))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
#
# Two surfaces for the same controls, so the feature is discoverable:
# - The Overlays popover (probed on Blender 5.1.2: bpy.types.
#   VIEW3D_PT_overlay accepts draw-function append()), where overlay
#   toggles conventionally live.
# - A sidebar (N-panel) tab "UV Islands", which is far easier to find.
# Plus menu entries (View menu, Edit Mode UV menu) so F3 menu search
# finds the toggle operator.
# ---------------------------------------------------------------------------

def _draw_overlay_controls(layout, context, show_health=True):
    """Shared body: checkbox + display mode + per-mode settings +
    refresh button + status labels + a loud error row if the draw
    handler ever failed (see overlay._draw)."""
    obj = context.active_object
    have_mesh = obj is not None and obj.type == 'MESH'
    wm = context.window_manager
    interactive = have_mesh or overlay.is_enabled()
    col = layout.column()
    row = col.row(align=True)
    row.enabled = interactive
    row.prop(wm, "uv_island_overlay", text="UV Island Overlay")
    sub = row.row(align=True)
    sub.enabled = overlay.is_enabled()
    sub.operator(UV_OT_island_overlay_refresh.bl_idname,
                 text="", icon='FILE_REFRESH')
    # Display mode + island source as dropdowns (full item labels stay
    # readable in the narrow Overlays popover, unlike expanded rows).
    mode_row = col.row(align=True)
    mode_row.enabled = interactive
    mode_row.prop(wm, "uv_island_overlay_mode", text="Mode")
    mode = wm.uv_island_overlay_mode
    if mode in ('ISLANDS', 'COMBINED'):
        # Island colors follow the source in both of these modes; the
        # COMBINED checker always samples actual UVs regardless.
        src_row = col.row(align=True)
        src_row.enabled = interactive
        src_row.prop(wm, "uv_island_overlay_source", text="Source")
    if mode == 'ISLANDS':
        op_row = col.row(align=True)
        op_row.enabled = interactive
        op_row.prop(wm, "uv_island_overlay_opacity")
    else:
        den = col.column(align=True)
        den.enabled = interactive
        den.prop(wm, "uv_island_overlay_checker_size")
        # COMBINED shares the Checker Opacity property with DENSITY:
        # the combined overlay is checker-like paint, so the
        # near-opaque default is right; Tint Opacity is ISLANDS-only.
        den.prop(wm, "uv_island_overlay_density_opacity")
        den.prop(wm, "uv_island_overlay_texture_size")
        if mode == 'DENSITY':
            # The deviation tint is DENSITY-only: in COMBINED the
            # checker tint IS the island color, and island hue x
            # checker x deviation would be unreadable.
            tint_row = col.row(align=True)
            tint_row.enabled = interactive
            tint_row.prop(wm, "uv_island_overlay_density_tint")
    if overlay.is_enabled():
        active = overlay.active_mode()
        if active != 'ISLANDS' and overlay.has_no_uvs():
            # A state, not an error: the checker modes need a UV layer
            # (COMBINED deliberately draws nothing rather than
            # degrading to islands-only without the checker).
            col.label(text="Mesh has no UVs", icon='INFO')
        else:
            if active == 'DENSITY':
                col.label(text="%d island%s (actual UVs)"
                          % (overlay.island_count(),
                             "" if overlay.island_count() == 1 else "s"))
            else:
                col.label(text="%d island%s (%s)"
                          % (overlay.island_count(),
                             "" if overlay.island_count() == 1 else "s",
                             "predicted"
                             if overlay.active_source() == 'SEAM'
                             else "actual"))
            if active != 'ISLANDS':
                med = overlay.median_density()
                if med is not None:
                    tex = wm.uv_island_overlay_texture_size
                    col.label(text="Median: %.1f px/unit @ %d px"
                              % (med * tex, tex))
                else:
                    col.label(text="Median: undefined (degenerate UVs)")
        if overlay.last_draw_error() is not None:
            col.label(text="Draw failed - see system console",
                      icon='ERROR')
    elif not have_mesh:
        col.label(text="Select a mesh object", icon='INFO')
    elif wm.uv_island_overlay_mode != 'ISLANDS' \
            and not getattr(obj.data, "uv_layers", None):
        col.label(text="Mesh has no UVs", icon='INFO')

    if have_mesh and show_health:
        health_box = layout.box()
        health_box.label(text="UV Health", icon='VIEWZOOM')
        health_box.prop(wm, "uv_health_texture_size")
        health_box.prop(wm, "uv_health_low_density_ratio")
        health_box.prop(wm, "uv_health_minimum_span_px")
        health_box.operator(UV_OT_health_analyze.bl_idname,
                            icon='FILE_REFRESH')
        result = health.last_result(obj.name)
        if result is not None:
            health_box.label(text="%d islands, median %.1f px/unit" % (
                result.island_count,
                (result.median_density or 0.0) * wm.uv_health_texture_size))
            rows = (
                ('ZERO', "Collapsed UV faces", len(result.zero_area_faces)),
                ('DUPLICATE', "Duplicate-mapped faces",
                 len(result.duplicate_faces)),
                ('LOW_DENSITY', "Low-density islands",
                 len(result.low_density_islands)),
                ('TINY', "Tiny islands", len(result.tiny_islands)),
                ('OUTSIDE', "Faces outside 0-1",
                 len(result.out_of_bounds_faces)),
            )
            for issue, label, count in rows:
                row = health_box.row(align=True)
                row.label(text=f"{label}: {count}")
                op = row.operator(UV_OT_health_select.bl_idname,
                                  text="Select")
                op.issue = issue


def _overlay_popover_draw(self, context):
    obj = context.active_object
    # Keep showing while enabled even if a non-mesh became active, so the
    # off-switch never disappears from under the user.
    if not overlay.is_enabled() and (obj is None or obj.type != 'MESH'):
        return
    layout = self.layout
    layout.separator()
    _draw_overlay_controls(layout, context, show_health=False)


class VIEW3D_PT_uv_island_overlay(bpy.types.Panel):
    """Sidebar home for the overlay controls (always visible, so the
    feature is discoverable without knowing about the Overlays popover)"""
    bl_idname = "VIEW3D_PT_uv_island_overlay"
    bl_label = "UV Islands"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "UV Islands"

    def draw(self, context):
        _draw_overlay_controls(self.layout, context)


def _menu_draw(self, context):
    self.layout.separator()
    self.layout.operator(UV_OT_island_overlay_toggle.bl_idname)


# ---------------------------------------------------------------------------
# Property + handlers
# ---------------------------------------------------------------------------

def _on_enabled_update(self, context):
    if self.uv_island_overlay:
        if not overlay.enable(context):
            # No active mesh: revert so the checkbox never lies. NOTE:
            # this must be a real property assignment — writing the
            # idprop (self["uv_island_overlay"] = False) stopped working
            # in Blender 5.0 (bpy.props storage is no longer idprop-
            # accessible), leaving the checkbox stuck on True. The
            # re-entrant update this triggers is bounded: it takes the
            # else-branch below and stops.
            self.uv_island_overlay = False
    else:
        overlay.disable()


def _on_source_update(self, context):
    """Island source changed: invalidate and rebuild (overlay.set_source
    is a no-op when the value did not actually change)."""
    overlay.set_source(self.uv_island_overlay_source, context)


def _on_mode_update(self, context):
    """Display mode changed: invalidate and rebuild, exactly like the
    source switch (overlay.set_mode is a no-op when unchanged)."""
    overlay.set_mode(self.uv_island_overlay_mode, context)


def _on_checker_size_update(self, context):
    """Checker resolution is a shader push constant read at draw time
    (probed on 5.1.2: FLOAT push constants are supported), so a change
    only needs a repaint — never a rebuild."""
    overlay.on_checker_size_changed()


def _on_opacity_update(self, context):
    """Overlay opacity (either mode's property) is a shader push
    constant read at draw time — same mechanism as the checker size, so
    dragging the slider only repaints, never rebuilds."""
    overlay.on_opacity_changed()


def _on_density_tint_update(self, context):
    """The deviation tint is baked into the per-vertex color attribute,
    so toggling it rebuilds (DENSITY mode only; no-op otherwise)."""
    overlay.on_density_tint_changed(context)


@persistent
def _on_depsgraph_update(scene, depsgraph):
    """Cheap auto-refresh: when the overlaid object's geometry changes
    (mesh edits, seam marking, mode switches), notify the overlay. In UV
    source (any mode) and DENSITY mode that marks it dirty (recompute
    once at the next draw — never per frame); in SEAM source (Island
    Colors and Combined modes) it feeds the debounced live pipeline
    (O(1) here; checksum + rebuild happen after a quiet period, off the
    hot path). In Combined the debounce checksum also covers the UV
    layer, so seam edits and UV edits converge on the same SINGLE
    debounced rebuild — never a draw-path rebuild racing a debounced
    one. Probed on 5.1.2: seam-flag-only edits AND UV edits
    (foreach_set, edit-bmesh writes, uv.unwrap) DO report
    is_updated_geometry, selection-only changes do NOT — exactly the
    filter we want, and it means the checker modes catch re-unwraps."""
    if not overlay.is_enabled():
        return
    name = overlay.tracked_object_name()
    if name is None:
        return
    # Match the object OR its mesh datablock: depending on the edit,
    # the geometry update can be reported on either ID (and the two can
    # be named differently).
    names = {name}
    obj = bpy.data.objects.get(name)
    data = getattr(obj, "data", None)
    if data is not None:
        names.add(data.name)
    try:
        for update in depsgraph.updates:
            if update.is_updated_geometry and \
                    getattr(update.id, "name", None) in names:
                overlay.on_tracked_geometry_update()
                return
    except Exception:
        pass


@persistent
def _on_load_pre(*args):
    """Drop the overlay before loading a new file so no draw handler or
    object reference goes stale."""
    try:
        wm = bpy.context.window_manager
        if wm is not None and wm.uv_island_overlay:
            wm.uv_island_overlay = False  # update callback disables
        else:
            overlay.disable()
    except Exception:
        overlay.disable()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    UV_OT_island_overlay_toggle,
    UV_OT_island_overlay_refresh,
    UV_OT_health_analyze,
    UV_OT_health_select,
    VIEW3D_PT_uv_island_overlay,
)

# Menus that get a "Toggle UV Island Overlay" entry. F3 search only finds
# operators that live in a menu, so these double as search keywords:
# View menu (all modes) and the Edit Mode UV menu (right where the user
# just ran Unwrap).
_MENUS = ("VIEW3D_MT_view", "VIEW3D_MT_uv_map")


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    bpy.types.WindowManager.uv_island_overlay = BoolProperty(
        name="UV Island Colors",
        description="Tint each UV island of the active mesh with a "
                    "distinct color in the 3D viewport",
        default=False,
        update=_on_enabled_update,
    )

    # Default 'SEAM': the primary workflow this overlay serves is
    # interactive seam marking — islands update live as
    # seams are added, no Unwrap needed, and the prediction equals what
    # the next seam-respecting Unwrap produces. It is also much cheaper
    # on large meshes. Switch to 'UV' to see the actual current unwrap
    # (e.g. Smart UV Project charts, which have no seam flags).
    bpy.types.WindowManager.uv_island_overlay_source = EnumProperty(
        name="Island Source",
        description="How islands are determined for the overlay colors "
                    "(Island Colors and Combined modes; the Combined "
                    "checker always samples actual UVs regardless)",
        items=(
            ('SEAM', "Seams (predicted)",
             "Regions bounded by seam edges — updates live while you "
             "mark seams and predicts the islands the next Unwrap will "
             "produce (no UV data needed)"),
            ('UV', "UVs (actual)",
             "True UV-space connectivity of the current unwrap (follows "
             "Smart UV Project / manual UV edits; needs a UV layer, "
             "updates after unwrapping)"),
        ),
        default='SEAM',
        update=_on_source_update,
    )

    # Default 'COMBINED' (v1.4.0): island hues AND the texel-density
    # checker at once — the most informative default. WindowManager
    # properties are runtime-only (never restored from .blend files —
    # a session always starts from the property default), so flipping
    # the default needs no migration for existing users; the headless
    # suite probes this by saving/reopening a file.
    bpy.types.WindowManager.uv_island_overlay_mode = EnumProperty(
        name="Display Mode",
        description="What the overlay visualizes",
        items=(
            ('ISLANDS', "Island Colors",
             "Tint each UV island with a distinct color — island "
             "boundaries at a glance"),
            ('DENSITY', "Texel Density",
             "Checkerboard mapped through the mesh's actual UVs — "
             "islands with mismatched texel density show different "
             "checker scales on the surface, tinted blue/red below/"
             "above the mesh's median density (needs a UV layer)"),
            ('COMBINED', "Islands + Density",
             "Both at once: the texel-density checkerboard multiplied "
             "by each island's identity color — hue reads island "
             "membership, checker scale reads texel density. Island "
             "colors follow the Source setting; the checker always "
             "samples the actual UVs (needs a UV layer)"),
        ),
        default='COMBINED',
        update=_on_mode_update,
    )

    bpy.types.WindowManager.uv_island_overlay_checker_size = IntProperty(
        name="Checker Size",
        description="Checkerboard resolution in checkers per UV unit: "
                    "at 32, each checker covers 32 px of a 1024 px "
                    "texture. Applied live (shader uniform, no rebuild)",
        default=32, min=1, soft_max=512,
        update=_on_checker_size_update,
    )

    # Two opacity properties, not one (v1.3.1): the right defaults
    # differ per mode — the island tint is a translucent color wash
    # (0.4) while the density checker should read like near-opaque
    # paint (0.9) — and separate properties mean switching modes never
    # drags one mode's slider value into the other. Both feed a FLOAT
    # push constant read at draw time (probed on 5.1.2, same mechanism
    # as the checker size), so dragging is live with zero rebuild.
    bpy.types.WindowManager.uv_island_overlay_opacity = FloatProperty(
        name="Tint Opacity",
        description="Opacity of the island-color tint in Island Colors "
                    "mode. Applied live (shader uniform, no rebuild)",
        default=overlay.ALPHA, min=0.0, max=1.0, subtype='FACTOR',
        update=_on_opacity_update,
    )

    bpy.types.WindowManager.uv_island_overlay_density_opacity = \
        FloatProperty(
            name="Checker Opacity",
            description="Opacity of the checker overlay in Texel "
                        "Density and Combined modes; near-opaque by "
                        "default so the checker reads like paint on "
                        "the surface. Applied live (shader uniform, "
                        "no rebuild)",
            default=overlay.DEFAULT_DENSITY_OPACITY, min=0.0, max=1.0,
            subtype='FACTOR',
            update=_on_opacity_update,
        )

    bpy.types.WindowManager.uv_island_overlay_texture_size = IntProperty(
        name="Texture Size",
        description="Assumed square texture edge in pixels, used only "
                    "to express the median density in px/unit",
        default=1024, min=1, soft_max=16384,
    )

    bpy.types.WindowManager.uv_island_overlay_density_tint = BoolProperty(
        name="Deviation Tint",
        description="Tint the checker per island by log2 deviation from "
                    "the mesh's median density: blue below the median, "
                    "neutral at it, red above (clamped at +/-2 octaves). "
                    "Texel Density mode only — Combined mode ignores it "
                    "(its checker tint is the island color)",
        default=True,
        update=_on_density_tint_update,
    )

    bpy.types.WindowManager.uv_health_texture_size = IntProperty(
        name="Health Texture Size",
        description="Texture resolution used to express texel density and "
                    "minimum island span in pixels",
        default=4096, min=1, soft_max=16384)
    bpy.types.WindowManager.uv_health_low_density_ratio = FloatProperty(
        name="Low Density Below",
        description="Flag islands below this fraction of the mesh median "
                    "texel density",
        default=0.5, min=0.01, max=1.0, subtype='FACTOR')
    bpy.types.WindowManager.uv_health_minimum_span_px = FloatProperty(
        name="Minimum Island Span",
        description="Flag islands whose UV bounding box is narrower than "
                    "this many pixels at the chosen texture size",
        default=8.0, min=0.0, soft_max=64.0, unit='NONE')

    if hasattr(bpy.types, "VIEW3D_PT_overlay"):
        bpy.types.VIEW3D_PT_overlay.append(_overlay_popover_draw)
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            menu.append(_menu_draw)

    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if _on_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_on_load_pre)


def unregister():
    overlay.disable()

    if _on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre)
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)

    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            try:
                menu.remove(_menu_draw)
            except Exception:
                pass
    if hasattr(bpy.types, "VIEW3D_PT_overlay"):
        try:
            bpy.types.VIEW3D_PT_overlay.remove(_overlay_popover_draw)
        except Exception:
            pass

    del bpy.types.WindowManager.uv_health_minimum_span_px
    del bpy.types.WindowManager.uv_health_low_density_ratio
    del bpy.types.WindowManager.uv_health_texture_size
    del bpy.types.WindowManager.uv_island_overlay_density_tint
    del bpy.types.WindowManager.uv_island_overlay_texture_size
    del bpy.types.WindowManager.uv_island_overlay_density_opacity
    del bpy.types.WindowManager.uv_island_overlay_opacity
    del bpy.types.WindowManager.uv_island_overlay_checker_size
    del bpy.types.WindowManager.uv_island_overlay_mode
    del bpy.types.WindowManager.uv_island_overlay_source
    del bpy.types.WindowManager.uv_island_overlay

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
