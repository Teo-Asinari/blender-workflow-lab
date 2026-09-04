# SPDX-License-Identifier: GPL-2.0-or-later
"""Public API guards for mechanical package reorganizations."""

import importlib
import sys
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto


PUBLIC_MODULES = (
    "model", "channel_paint", "debounce", "compat", "reconcile",
    "snapshot", "engine", "visibility", "brush_adapter", "tile_undo",
    "ibl", "preview_stack", "stencil", "gpu_engine", "props", "paint",
    "focused_ui",
    "flatten_export", "ops", "ui_icons", "ui",
)

OPERATOR_IDS = {
    "IMPASTO_OT_stack_init": "impasto.stack_init",
    "IMPASTO_OT_stack_remove": "impasto.stack_remove",
    "IMPASTO_OT_layer_add": "impasto.layer_add",
    "IMPASTO_OT_layer_remove": "impasto.layer_remove",
    "IMPASTO_OT_layer_move": "impasto.layer_move",
    "IMPASTO_OT_channel_add": "impasto.channel_add",
    "IMPASTO_OT_binding_add": "impasto.binding_add",
    "IMPASTO_OT_binding_remove": "impasto.binding_remove",
    "IMPASTO_OT_mask_add": "impasto.mask_add",
    "IMPASTO_OT_mask_remove": "impasto.mask_remove",
    "IMPASTO_OT_mask_select": "impasto.mask_select",
    "IMPASTO_OT_mask_paint": "impasto.mask_paint",
    "IMPASTO_OT_mask_asset_link": "impasto.mask_asset_link",
    "IMPASTO_OT_mask_asset_delete": "impasto.mask_asset_delete",
    "IMPASTO_OT_stack_rebuild": "impasto.stack_rebuild",
    "IMPASTO_OT_import_kiln_normal": "impasto.import_kiln_normal",
    "IMPASTO_OT_paint_activate": "impasto.paint_activate",
    "IMPASTO_OT_detail_paint": "impasto.detail_paint",
    "IMPASTO_OT_recent_color_apply": "impasto.recent_color_apply",
    "IMPASTO_OT_material_preset_capture": "impasto.material_preset_capture",
    "IMPASTO_OT_material_preset_apply": "impasto.material_preset_apply",
    "IMPASTO_OT_material_preset_remove": "impasto.material_preset_remove",
    "IMPASTO_OT_stencil_image_open": "impasto.stencil_image_open",
    "IMPASTO_OT_brush_mode_set": "impasto.brush_mode_set",
    "IMPASTO_OT_erase_channels_set": "impasto.erase_channels_set",
    "IMPASTO_OT_brush_channels_set": "impasto.brush_channels_set",
    "IMPASTO_OT_flatten_export": "impasto.flatten_export",
    "IMPASTO_OT_native_multichannel_paint":
        "impasto.native_multichannel_paint",
    "IMPASTO_OT_gpu_paint": "impasto.gpu_paint",
    "IMPASTO_OT_gpu_flush": "impasto.gpu_flush",
    "IMPASTO_OT_gpu_material_inspect_toggle":
        "impasto.gpu_material_inspect_toggle",
}

PROPERTY_IDS = {
    "ImpastoRecentColor": {"color"},
    "ImpastoMaterialPreset": {
        "label", "base_color", "roughness", "metallic", "normal",
        "height_strength", "height_direction", "emission_color",
        "emission_strength", "sss_weight", "sss_radius", "sss_scale",
    },
    "ImpastoBinding": {
        "image_name", "enabled", "mode", "value", "color", "blend_mode",
        "opacity", "use_masks",
    },
    "ImpastoMask": {
        "label", "mask_type", "image_name", "uv_map", "blend", "invert",
        "opacity", "visible", "asset_uid", "channels",
    },
    "ImpastoMaskAsset": {"label", "image_name", "uv_map"},
    "ImpastoLayer": {
        "label", "layer_type", "parent_uid", "visible", "opacity",
        "blend_mode", "image_name", "uv_map", "bindings", "masks",
        "active_mask_index",
        "paint_color", "paint_roughness", "paint_metallic", "paint_normal",
        "paint_height_strength", "paint_height_direction",
        "paint_emission_color", "paint_emission_strength",
        "paint_sss_weight", "paint_sss_radius", "paint_sss_scale",
        "show_sss_caliper", "brush_radius", "brush_hardness",
        "brush_opacity", "brush_mode", "paint_channels", "soften_channels",
        "smear_channels", "erase_channels",
        "brush_pressure_opacity",
        "brush_pressure_size",
        "brush_stencil_enabled", "brush_stencil_image",
        "brush_stencil_projection", "brush_stencil_interpretation",
        "brush_stencil_usage", "brush_stencil_coverage",
        "brush_stencil_normal_relief", "brush_stencil_opacity",
        "brush_stencil_preview_opacity",
        "brush_stencil_position", "brush_stencil_scale",
        "brush_stencil_brush_scale", "brush_stencil_rotation",
        "brush_stencil_profile_strength", "brush_stencil_profile_invert",
        "auto_material_preview", "auto_material_preview_delay",
        "gpu_preview_mode", "preview_environment_exposure",
        "preview_environment_rotation", "preview_key_strength",
        "preview_key_rotation", "preview_fill_strength",
        "preview_roughness_readability",
        "preview_base_normal_image", "preview_base_normal_uv_map",
        "preview_base_normal_strength", "preview_base_normal_invert_green",
        "experimental_uv_gutters", "experimental_conservative_seams",
        "focused_paint_ui",
        "paint_workflow", "ui_show_channels", "ui_show_emission_channels",
        "ui_show_subsurface_channels", "ui_show_recent_colors",
        "ui_show_material_presets",
        "ui_show_emission_paint", "ui_show_subsurface_paint",
        "ui_show_advanced",
    },
    "ImpastoChannel": {"enabled"},
    "ImpastoStack": {
        "is_stack", "schema_version", "blender_version", "channels",
        "layers", "active_layer_uid", "active_index",
        "recent_base_colors", "recent_emission_colors",
        "material_presets", "mask_assets", "default_canvas_size",
    },
    "ImpastoMaterialState": {"displaced_links", "stack_tree"},
}


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


registered = False
try:
    for module_name in PUBLIC_MODULES:
        module = importlib.import_module("impasto." + module_name)
        check("public import impasto." + module_name,
              getattr(impasto, module_name, None) is module)

    for class_name, operator_id in OPERATOR_IDS.items():
        operator_type = getattr(impasto.ops, class_name)
        check("operator id " + class_name,
              operator_type.bl_idname == operator_id)

    impasto.register()
    registered = True
    check("ShaderNodeTree property", hasattr(bpy.types.ShaderNodeTree,
                                               "impasto"))
    check("Material property", hasattr(bpy.types.Material, "impasto_mat"))

    for class_name, expected in PROPERTY_IDS.items():
        prop_group = getattr(impasto.props, class_name)
        # ``name`` is inherited by every PropertyGroup and is not part of
        # Impasto's declared persistence schema.
        actual = set(prop_group.bl_rna.properties.keys()) - {"rna_type", "name"}
        check("property identifiers " + class_name, actual == expected,
              "missing=%s extra=%s" %
              (sorted(expected - actual), sorted(actual - expected)))

    for operator_id in OPERATOR_IDS.values():
        namespace, name = operator_id.split(".")
        check("registered operator " + operator_id,
              hasattr(getattr(bpy.ops, namespace), name))

    impasto.unregister()
    registered = False
    check("ShaderNodeTree property removed",
          not hasattr(bpy.types.ShaderNodeTree, "impasto"))
    check("Material property removed",
          not hasattr(bpy.types.Material, "impasto_mat"))
    print("IMPASTO_PACKAGE_CONTRACT_PASSED")
except Exception:
    if registered:
        try:
            impasto.unregister()
        except Exception:
            pass
    traceback.print_exc()
    print("IMPASTO_PACKAGE_CONTRACT_FAILED")
