# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto's registered panels and layer-list UI.

Paint controls and channel menus live in focused helper modules while this
module retains the stable Blender registration surface.
"""

import bpy

from . import bl_info as _BL_INFO
from . import engine
from . import gpu_engine
from . import model
from . import ops
from . import ui_icons
from .ui_channels import (
    IMPASTO_MT_add_channel,
    IMPASTO_MT_add_channel_register,
    draw_missing_channels as _draw_missing_channels,
    layer_channel_summary as _layer_channel_summary,
    missing_channels as _missing_channels,
)
from .ui_paint import PaintPanelMixin

_TYPE_ICONS = {'PAINT': 'BRUSH_DATA', 'FILL': 'SNAP_FACE',
               'GROUP': 'FILE_FOLDER'}
_VERSION_LABEL = "Impasto %s" % ".".join(
    str(part) for part in _BL_INFO["version"])


class IMPASTO_UL_layers(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname):
        row = layout.row(align=True)
        row.prop(item, "visible", text="", emboss=False,
                 icon='HIDE_OFF' if item.visible else 'HIDE_ON')
        row.label(icon=_TYPE_ICONS.get(item.layer_type, 'BLANK1'))
        row.prop(item, "label", text="", emboss=False)
        sub = row.row(align=True)
        sub.alignment = 'RIGHT'
        keys = [b.name for b in item.bindings if b.enabled]
        if keys:
            sub.label(text=_layer_channel_summary(keys))


class IMPASTO_PT_main(PaintPanelMixin, bpy.types.Panel):
    """Sidebar home for the layer stack (always visible in the Impasto
    tab so the feature is discoverable)"""
    bl_idname = "IMPASTO_PT_main"
    bl_label = "Layer Stack"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Impasto"

    def draw(self, context):
        layout = self.layout
        header = layout.row()
        header.alignment = 'RIGHT'
        header.label(text=_VERSION_LABEL, icon='BRUSH_DATA')
        obj = context.object
        if obj is None or obj.type != 'MESH':
            layout.label(text="Select a mesh object", icon='INFO')
            return
        mat = obj.active_material
        tree = engine.find_stack_for_material(mat)
        if tree is None:
            if mat is not None:
                layout.label(text="Material: %s" % mat.name)
            layout.operator_menu_enum(ops.IMPASTO_OT_stack_init.bl_idname,
                                      "template",
                                      text="New Layer Stack",
                                      icon='ADD')
            return

        state = tree.impasto
        row = layout.row(align=True)
        row.label(text="Stack: %s" % mat.name, icon='NODETREE')
        chan_labels = ", ".join(
            model.CHANNEL_MAP[c.name].label for c in state.channels
            if c.enabled and c.name in model.CHANNEL_MAP)
        layout.label(text=chan_labels, icon='MATERIAL')
        resolution = layout.box()
        row = resolution.row(align=True)
        row.label(text="Canvas Resolution", icon='IMAGE_DATA')
        row.prop(state, "default_canvas_size", text="")
        resolution.label(
            text="New images only; channels within a layer stay uniform",
            icon='INFO')
        kiln_tex = (mat.node_tree.nodes.get("Kiln Bake Target")
                    if mat.use_nodes else None)
        if (kiln_tex is not None
                and kiln_tex.bl_idname == 'ShaderNodeTexImage'
                and kiln_tex.image is not None):
            layout.operator(
                ops.IMPASTO_OT_import_kiln_normal.bl_idname,
                text="Import / Repair Kiln Normal",
                icon='NORMALS_FACE')

        row = layout.row()
        row.template_list("IMPASTO_UL_layers", "", state, "layers",
                          state, "active_index", rows=4)
        col = row.column(align=True)
        op = col.operator(ops.IMPASTO_OT_layer_add.bl_idname, text="",
                          icon='BRUSH_DATA')
        op.layer_type = 'PAINT'
        op.channel_key = 'base_color'
        op = col.operator(ops.IMPASTO_OT_layer_add.bl_idname, text="",
                          icon='SNAP_FACE')
        op.layer_type = 'FILL'
        col.operator(ops.IMPASTO_OT_layer_remove.bl_idname, text="",
                     icon='TRASH')
        col.separator()
        op = col.operator(ops.IMPASTO_OT_layer_move.bl_idname, text="",
                          icon='TRIA_UP')
        op.direction = 'UP'
        op = col.operator(ops.IMPASTO_OT_layer_move.bl_idname, text="",
                          icon='TRIA_DOWN')
        op.direction = 'DOWN'

        layer = state.active_layer()
        if layer is not None:
            box = layout.box()
            box.label(text=layer.label,
                      icon=_TYPE_ICONS.get(layer.layer_type, 'BLANK1'))
            if layer.layer_type != 'GROUP':
                row = box.row(align=True)
                row.prop(layer, "blend_mode", text="")
                row.prop(layer, "opacity", text="Layer Opacity", slider=True)
                channels_box = box.box()
                row = channels_box.row(align=True)
                row.prop(layer, "ui_show_channels", text="Layer Channels",
                         icon='TRIA_DOWN' if layer.ui_show_channels
                         else 'TRIA_RIGHT', emboss=False)
                row.menu("IMPASTO_MT_add_channel", text="", icon='ADD')
                if layer.ui_show_channels:
                    self._draw_bindings(channels_box, state, layer)
                # Essential paint controls precede the extensible mask UI.
                # Blender stops drawing the rest of a panel after a layout
                # exception, so mask presentation must never hide painting.
                if layer.layer_type == 'PAINT':
                    self._draw_paint_tools(context, box, layer)
                masks_box = box.box()
                row = masks_box.row(align=True)
                row.label(text="Shared Masks", icon='MOD_MASK')
                row.operator(ops.IMPASTO_OT_mask_add.bl_idname, text="",
                             icon='FILE_NEW')
                row.operator(ops.IMPASTO_OT_mask_asset_link.bl_idname,
                             text="", icon='LINKED')
                row.operator(ops.IMPASTO_OT_mask_remove.bl_idname, text="",
                             icon='REMOVE')
                for index, mask in enumerate(layer.masks):
                    row = masks_box.row(align=True)
                    row.prop(mask, "visible", text="")
                    asset = state.mask_assets.get(mask.asset_uid)
                    row.label(text=(asset.label if asset is not None else
                                    "Missing shared mask"),
                              icon='IMAGE_DATA' if asset is not None
                              else 'ERROR')
                    row.prop(mask, "invert", text="Invert")
                    row.prop(mask, "opacity", text="", slider=True)
                    op = row.operator(
                        ops.IMPASTO_OT_mask_select.bl_idname, text="",
                        icon=('RADIOBUT_ON' if index
                              == layer.active_mask_index else 'RADIOBUT_OFF'))
                    op.index = index
                    scope = masks_box.row(align=True)
                    scope.label(text="Channels:")
                    for key in state.channels:
                        if key.enabled:
                            scope.prop(mask, "channels",
                                       index=model.CHANNEL_ORDER[key.name],
                                       text=model.CHANNEL_MAP[key.name].label,
                                       toggle=True)
                if layer.masks:
                    masks_box.operator(
                        ops.IMPASTO_OT_mask_paint.bl_idname,
                        text="Paint Selected Mask", icon='BRUSH_DATA')
                    row = masks_box.row(align=True)
                    row.label(text="Affect Channels:")
                    for binding in layer.bindings:
                        if binding.enabled:
                            row.prop(binding, "use_masks",
                                     text=model.CHANNEL_MAP[
                                         binding.name].label)
                if state.mask_assets:
                    assets = masks_box.box()
                    assets.label(text="Material Mask Library")
                    for asset in state.mask_assets:
                        card = assets.box()
                        row = card.row(align=True)
                        row.prop(asset, "label", text="")
                        op = row.operator(
                            ops.IMPASTO_OT_mask_asset_delete.bl_idname,
                            text="", icon='TRASH')
                        op.asset_uid = asset.name
                        card.prop(asset, "image_name", text="Image")
                        card.prop(asset, "uv_map", text="UV Map")
            else:
                box.prop(layer, "opacity", slider=True)

        layout.separator()
        export_box = layout.box()
        export_box.label(text="Flatten / Export", icon='IMAGE_DATA')
        export_box.label(text="Creates new images; layers stay editable")
        export_box.operator_context = 'INVOKE_DEFAULT'
        export_box.operator(ops.IMPASTO_OT_flatten_export.bl_idname,
                            text="Flatten / Prepare glTF", icon='RENDER_RESULT')
        layout.operator(ops.IMPASTO_OT_stack_rebuild.bl_idname,
                        text="Rebuild", icon='FILE_REFRESH')

class IMPASTO_PT_preview_lighting(bpy.types.Panel):
    """Compact popover; keeps five diagnostic controls out of the sidebar."""
    bl_idname = "IMPASTO_PT_preview_lighting"
    bl_label = "Preview Lighting"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'

    def draw(self, context):
        obj = context.object
        mat = obj.active_material if obj is not None else None
        tree = engine.find_stack_for_material(mat)
        layer = tree.impasto.active_layer() if tree is not None else None
        if layer is None:
            self.layout.label(text="No active Impasto layer", icon='INFO')
            return
        col = self.layout.column(align=True)
        mat = obj.active_material if obj is not None else None
        if mat is not None:
            col.label(text="Material Sphere", icon='MATERIAL')
            col.template_preview(mat, show_buttons=False)
            col.label(text="Shows the last synchronized material", icon='INFO')
            col.separator()
        col.label(text="Environment", icon='WORLD')
        col.prop(layer, "preview_environment_exposure", text="Exposure")
        col.prop(layer, "preview_environment_rotation", text="Rotation")
        col.separator()
        col.label(text="Studio Lights", icon='LIGHT')
        col.prop(layer, "preview_key_strength", text="Key")
        col.prop(layer, "preview_key_rotation", text="Key Rotation")
        col.prop(layer, "preview_fill_strength", text="Fill")
        col.prop(layer, "preview_roughness_readability",
                 text="Roughness Readability")
        col.separator()
        col.label(text="Base Normal Map", icon='NORMALS_FACE')
        col.template_ID(layer, "preview_base_normal_image", open="image.open")
        if obj is not None and getattr(obj, "data", None) is not None:
            col.prop_search(layer, "preview_base_normal_uv_map",
                            obj.data, "uv_layers", text="UV Map")
        else:
            col.prop(layer, "preview_base_normal_uv_map", text="UV Map")
        row = col.row(align=True)
        row.enabled = layer.preview_base_normal_image is not None
        row.prop(layer, "preview_base_normal_strength", text="Strength")
        row.prop(layer, "preview_base_normal_invert_green", text="Invert Green",
                 toggle=True)
        col.label(text="Clear the image field to disable", icon='INFO')
        col.label(text="Preview only — paint data is unchanged", icon='INFO')


class IMPASTO_MT_main(bpy.types.Menu):
    bl_idname = "IMPASTO_MT_main"
    bl_label = "Impasto"

    def draw(self, context):
        layout = self.layout
        layout.operator(ops.IMPASTO_OT_stack_init.bl_idname)
        op = layout.operator(ops.IMPASTO_OT_layer_add.bl_idname,
                             text="Impasto: Add Paint Layer")
        op.layer_type = 'PAINT'
        op = layout.operator(ops.IMPASTO_OT_layer_add.bl_idname,
                             text="Impasto: Add Fill Layer")
        op.layer_type = 'FILL'
        layout.operator(ops.IMPASTO_OT_stack_rebuild.bl_idname)
        layout.operator(ops.IMPASTO_OT_paint_activate.bl_idname)
        layout.operator(ops.IMPASTO_OT_native_multichannel_paint.bl_idname)
        layout.operator(ops.IMPASTO_OT_gpu_paint.bl_idname)
        layout.operator(ops.IMPASTO_OT_stack_remove.bl_idname)


def _menu_draw(self, context):
    self.layout.separator()
    self.layout.menu(IMPASTO_MT_main.bl_idname)


# Texture Paint mode has no Paint menu on 5.1.2 (probed: only
# vertex/weight/grease-pencil paint menus exist), so the Object menu +
# the always-visible sidebar tab + F3 cover discoverability.
_MENUS = ("VIEW3D_MT_object",)

_classes = (
    IMPASTO_UL_layers,
    IMPASTO_MT_add_channel_register,
    IMPASTO_MT_add_channel,
    IMPASTO_PT_main,
    IMPASTO_PT_preview_lighting,
    IMPASTO_MT_main,
)


def register():
    ui_icons.register()
    for cls in _classes:
        bpy.utils.register_class(cls)
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            menu.append(_menu_draw)


def unregister():
    for menu_name in _MENUS:
        menu = getattr(bpy.types, menu_name, None)
        if menu is not None:
            try:
                menu.remove(_menu_draw)
            except Exception:
                pass
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    ui_icons.unregister()
