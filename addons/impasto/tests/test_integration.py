# SPDX-License-Identifier: GPL-2.0-or-later
"""Real-Blender Phase 1 lifecycle checks."""

import inspect
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

ADDONS = str(Path(__file__).resolve().parents[2])
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

import impasto
from impasto import engine, model, snapshot


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (": " + detail if detail else ""))
    print("  ok  " + name)


try:
    impasto.register()
    check("package registration",
          hasattr(bpy.types.ShaderNodeTree, "impasto"))
    check("metadata", impasto.bl_info["version"] == (0, 16, 1))
    check("panel version label", impasto.ui._VERSION_LABEL == "Impasto 0.16.1")
    focused_prop = impasto.props.ImpastoLayer.bl_rna.properties[
        "focused_paint_ui"]
    check("focused painting UI is opt-in and clearly described",
          focused_prop.default is False
          and "restore" in focused_prop.description.lower())
    fake_space = type("Space", (), {
        "show_region_toolbar": True,
        "show_region_tool_header": False,
        "show_region_asset_shelf": True,
    })()
    prior_ui = impasto.focused_ui.hide(fake_space)
    check("focused UI hides supported paint chrome",
          not fake_space.show_region_toolbar
          and not fake_space.show_region_tool_header
          and not fake_space.show_region_asset_shelf)
    impasto.focused_ui.restore(fake_space, prior_ui)
    check("focused UI restores exact prior viewport state",
          fake_space.show_region_toolbar
          and not fake_space.show_region_tool_header
          and fake_space.show_region_asset_shelf)
    check("extended brush sections collapse by default",
          not impasto.props.ImpastoLayer.bl_rna.properties[
              "ui_show_emission_paint"].default
          and not impasto.props.ImpastoLayer.bl_rna.properties[
              "ui_show_subsurface_paint"].default)
    check("custom soften and erase icons loaded",
          impasto.ui_icons.is_loaded('soften')
          and impasto.ui_icons.is_loaded('erase'))
    check("brush modes use custom icon operators",
          all(token in inspect.getsource(impasto.ui_paint.draw_brush_mode)
              for token in ("'PAINT'", "'SOFTEN'", "'SMEAR'", "'ERASE'",
                            "icon_value", "brush_mode_set")))
    layer_rna = impasto.props.ImpastoLayer.bl_rna.properties
    check("brush-wide controls have explicit names",
          layer_rna["brush_radius"].name == "Brush Radius"
          and layer_rna["brush_hardness"].name == "Brush Hardness")
    check("SSS caliper is an opt-in persistent layer control",
          layer_rna["show_sss_caliper"].name == "Show SSS Caliper"
          and layer_rna["show_sss_caliper"].default is False)
    check("SSS caliper tooltip distinguishes its rings from the brush",
          "red, green, and blue" in
          layer_rna["show_sss_caliper"].description
          and "white" in layer_rna["show_sss_caliper"].description
          and "GPU-paint" in layer_rna["show_sss_caliper"].description)
    replay_item = impasto.props.ImpastoLayer.bl_rna.properties[
        "paint_workflow"].enum_items["BLENDER"]
    check("brush replay is explicitly marked as a prototype",
          "Prototype" in replay_item.name
          and "non-performant" in replay_item.description
          and "not intended for serious painting" in replay_item.description)
    check("layer channel summary groups extended channels",
          impasto.ui._layer_channel_summary((
              "base_color", "metallic", "roughness", "normal", "height",
              "emission_color", "emission_strength", "sss_weight",
              "sss_radius", "sss_scale")) == "BMRNH E(2) SS(3)")
    size_a = bpy.data.images.new("Impasto Size A", 1024, 512)
    size_b = bpy.data.images.new("Impasto Size B", 2048, 2048)
    check("channel UI reads actual image datablock dimensions",
          impasto.ui_channels.image_dimensions(size_a) == (1024, 512)
          and impasto.ui_channels.format_image_dimensions(size_a)
          == "1024 × 512")
    fake_layer = type("Layer", (), {
        "image_name": "",
        "bindings": (
            type("Binding", (), {"enabled": True, "name": "base_color",
                                  "image_name": size_a.name})(),
            type("Binding", (), {"enabled": True, "name": "roughness",
                                  "image_name": size_b.name})(),
            type("Binding", (), {"enabled": True, "name": "normal",
                                  "image_name": "Missing Image"})(),
        ),
    })()
    check("channel UI tolerates missing and mismatched imported images",
          impasto.ui_channels.paint_layer_image_sizes(fake_layer)
          == {"base_color": (1024, 512),
              "roughness": (2048, 2048)})
    bpy.data.images.remove(size_a)
    bpy.data.images.remove(size_b)
    paint_tip = impasto.ops.IMPASTO_OT_layer_add.description(
        None, type("Props", (), {"layer_type": "PAINT"})())
    fill_tip = impasto.ops.IMPASTO_OT_layer_add.description(
        None, type("Props", (), {"layer_type": "FILL"})())
    check("paint and fill tooltips are distinct",
          paint_tip != fill_tip and "brush strokes" in paint_tip
          and "uniform" in fill_tip)

    bpy.ops.mesh.primitive_cube_add()
    obj = bpy.context.object
    check("stack init",
          bpy.ops.impasto.stack_init(
              template="PRINCIPLED_STANDARD") == {"FINISHED"})
    mat = obj.active_material
    tree = engine.find_stack_for_material(mat)
    check("stack discoverable", tree is not None)
    for value in ((0.1, 0.2, 0.3), (0.1001, 0.2001, 0.3001),
                  (0.2, 0.3, 0.4), (0.3, 0.4, 0.5),
                  (0.4, 0.5, 0.6), (0.5, 0.6, 0.7),
                  (0.6, 0.7, 0.8), (0.7, 0.8, 0.9),
                  (0.8, 0.9, 1.0), (0.9, 0.8, 0.7)):
        impasto.ops._remember_color(tree.impasto.recent_base_colors, value)
    check("recent colors deduplicate near matches and cap history",
          len(tree.impasto.recent_base_colors)
          == impasto.ops.RECENT_COLOR_LIMIT == 8)
    impasto.ops._remember_color(
        tree.impasto.recent_emission_colors, (1.0, 0.25, 0.1))
    check("Base and Emission recent colors are independent",
          len(tree.impasto.recent_emission_colors) == 1
          and len(tree.impasto.recent_base_colors) == 8)
    check("material group exists",
          mat.node_tree.nodes.get(model.n_material_stack()) is not None)
    check("five standard channels", len(tree.impasto.channels) == 5)
    check("initial reconcile clean",
          engine._last_deltas is not None
          and not engine._last_deltas.errors,
          str(engine._last_deltas))
    tree.impasto.default_canvas_size = '1024'
    check("stack-wide creation resolution is configurable",
          tree.impasto.default_canvas_size == '1024'
          and impasto.operator_support.stack_canvas_size(
              tree.impasto) == 1024)

    check("add fill",
          bpy.ops.impasto.layer_add(layer_type="FILL") == {"FINISHED"})
    check("add paint",
          bpy.ops.impasto.layer_add(layer_type="PAINT") == {"FINISHED"})
    check("requested layer types",
          sorted(ly.layer_type for ly in tree.impasto.layers)
          == ["FILL", "PAINT"])
    check("layer reconcile clean", not engine._last_deltas.errors,
          str(engine._last_deltas))

    paint_layer = tree.impasto.active_layer()
    check("new layer uses the stack-wide resolution",
          tuple(bpy.data.images[paint_layer.image_name].size)
          == (1024, 1024))
    check("bind channel at the existing layer resolution",
          bpy.ops.impasto.binding_add(channel_key="roughness")
          == {"FINISHED"})
    roughness_image = bpy.data.images[
        paint_layer.bindings["roughness"].image_name]
    check("new channel canvas preserves uniform layer resolution",
          tuple(roughness_image.size) == (1024, 1024))
    check("resolution controls disclose non-destructive creation semantics",
          "existing canvases are unchanged" in
          impasto.props.ImpastoStack.bl_rna.properties[
              "default_canvas_size"].description.lower())
    stencil_path = Path(tempfile.gettempdir()) / "impasto_stencil_browser.png"
    stencil_source = bpy.data.images.new(
        "Impasto Stencil Browser Source", 4, 4)
    stencil_source.generated_color = (0.25, 0.5, 0.75, 1.0)
    stencil_source.save_render(str(stencil_path))
    bpy.data.images.remove(stencil_source)
    check("stencil browser defaults to thumbnail view",
          "display_type = 'THUMBNAIL'" in inspect.getsource(
              impasto.ops.IMPASTO_OT_stencil_image_open.invoke)
          and "IMPASTO_OT_stencil_image_open"
          in inspect.getsource(impasto.ui_paint.PaintPanelMixin))
    check("stencil loader selects the chosen image",
          bpy.ops.impasto.stencil_image_open(
              filepath=str(stencil_path)) == {"FINISHED"}
          and paint_layer.brush_stencil_image is not None)
    check("add-on preferences expose a persistent stencil directory",
          impasto.props.ImpastoPreferences.bl_rna.properties[
              "stencil_directory"].subtype == 'DIR_PATH')

    check("add production layer mask",
          bpy.ops.impasto.mask_add() == {"FINISHED"}
          and len(paint_layer.masks) == 1
          and paint_layer.active_mask_index == 0)
    mask = paint_layer.masks[0]
    asset = tree.impasto.mask_assets.get(mask.asset_uid)
    mask_image = bpy.data.images.get(mask.image_name)
    check("stack mask asset owns the original grayscale image and UV",
          asset is not None and asset.image_name == mask.image_name
          and mask_image is not None
          and tuple(mask_image.size) == tuple(
              bpy.data.images[paint_layer.image_name].size)
          and mask_image.colorspace_settings.name
          == impasto.compat.resolve_colorspace(mask_image, "Non-Color")
          and min(mask_image.generated_color) == 1.0)
    check("mask target activates as the native paint canvas",
          impasto.paint.activate_mask_target(
              bpy.context, paint_layer, mask) in {True, False}
          and bpy.context.scene.tool_settings.image_paint.canvas == mask_image)
    check("mask reference scope is independently configurable per channel",
          all(mask.channels) and all(binding.use_masks
                                     for binding in paint_layer.bindings))
    mask.channels[model.CHANNEL_ORDER["roughness"]] = False
    snap_mask = next(ly for ly in snapshot.snapshot(tree).layers
                     if ly.uid == paint_layer.name).masks[0]
    check("reference channel scope reaches compiled snapshots",
          "roughness" not in snap_mask.channels
          and "base_color" in snap_mask.channels)
    fill_layer = next(ly for ly in tree.impasto.layers
                      if ly.layer_type == 'FILL')
    shared_ref = fill_layer.masks.add()
    shared_ref.name = model.new_uid()
    shared_ref.asset_uid = asset.name
    asset.label = "Shared Dirt"
    snap = snapshot.snapshot(tree)
    check("Paint and Fill layers resolve edits to one shared asset",
          sum(m.image_name == asset.image_name for ly in snap.layers
              for m in ly.masks) == 2
          and all(m.label == "Shared Dirt" for ly in snap.layers
                  for m in ly.masks))
    check("referenced mask assets refuse deletion",
          bpy.ops.impasto.mask_asset_delete(asset_uid=asset.name)
          == {'CANCELLED'}
          and tree.impasto.mask_assets.get(asset.name) is not None)
    paint_layer.bindings[0].use_masks = False
    check("mask channel exclusion persists in layer state",
          not paint_layer.bindings[0].use_masks
          and all(binding.use_masks for binding in paint_layer.bindings[1:]))
    kept_mask_name = mask.image_name
    check("remove mask retains its source image",
          bpy.ops.impasto.mask_remove() == {"FINISHED"}
          and len(paint_layer.masks) == 0
          and bpy.data.images.get(kept_mask_name) is not None
          and tree.impasto.mask_assets.get(asset.name) is not None)
    # Simulate a schema-2 file: migration creates an asset but never an image.
    legacy = paint_layer.masks.add()
    legacy.name = model.new_uid()
    legacy.label = "Legacy"
    legacy.image_name = kept_mask_name
    legacy.uv_map = paint_layer.uv_map
    tree.impasto.schema_version = 2
    image_count = len(bpy.data.images)
    engine.run_migrations(tree)
    check("legacy masks migrate without image duplication",
          tree.impasto.schema_version == 3 and legacy.asset_uid
          and tree.impasto.mask_assets.get(legacy.asset_uid).image_name
          == kept_mask_name and len(bpy.data.images) == image_count)

    paint_layer.paint_color = (0.12, 0.34, 0.56)
    paint_layer.paint_roughness = 0.23
    paint_layer.paint_metallic = 0.78
    paint_layer.paint_emission_color = (1.0, 0.2, 0.05)
    paint_layer.paint_emission_strength = 7.5
    check("capture persistent brush material preset",
          bpy.ops.impasto.material_preset_capture(
              label="Warm Metal") == {"FINISHED"}
          and len(tree.impasto.material_presets) == 1)
    preset = tree.impasto.material_presets[0]
    paint_targets_before = tuple(paint_layer.paint_channels)
    paint_layer.paint_color = (0.0, 0.0, 0.0)
    paint_layer.paint_roughness = 1.0
    check("material preset restores channel values only",
          bpy.ops.impasto.material_preset_apply(index=0) == {"FINISHED"}
          and tuple(round(x, 3) for x in paint_layer.paint_color)
          == (0.12, 0.34, 0.56)
          and abs(paint_layer.paint_roughness - 0.23) < 1e-6
          and abs(paint_layer.paint_metallic - 0.78) < 1e-6
          and tuple(paint_layer.paint_channels) == paint_targets_before)
    tooltip = impasto.ops.material_preset_tooltip(preset)
    check("material preset tooltip identifies useful channel values",
          "Warm Metal" in tooltip and "Roughness 0.23" in tooltip
          and "Metallic 0.78" in tooltip and "Emission" in tooltip
          and "7.5" in tooltip, tooltip)
    global_path = str(Path(tempfile.gettempdir()) /
                      "impasto_global_material_presets_test.json")
    original_global_path = impasto.ops._global_material_preset_path
    impasto.ops._global_material_preset_path = lambda: global_path
    try:
        impasto.ops._write_global_material_preset(preset)
        library_tree = bpy.data.node_groups.new(
            "Impasto Global Preset Test", "ShaderNodeTree")
        impasto.ops.load_global_material_presets(library_tree.impasto)
        check("global material library round-trips preset bundles",
              len(library_tree.impasto.material_presets) == 1
              and library_tree.impasto.material_presets[0].label
              == "Warm Metal"
              and abs(library_tree.impasto.material_presets[0].roughness
                      - 0.23) < 1e-6)
    finally:
        impasto.ops._global_material_preset_path = original_global_path
        Path(global_path).unlink(missing_ok=True)

    erase_target_indices = [
        model.CHANNEL_ORDER[key]
        for key, _image in impasto.ops.gpu_paint_targets(paint_layer)
    ]
    check("paint layer has eraser targets", bool(erase_target_indices))
    check("clear all eraser targets",
          bpy.ops.impasto.erase_channels_set(selected=False) == {"FINISHED"}
          and not any(paint_layer.erase_channels[index]
                      for index in erase_target_indices))
    check("select all eraser targets",
          bpy.ops.impasto.erase_channels_set(selected=True) == {"FINISHED"}
          and all(paint_layer.erase_channels[index]
                  for index in erase_target_indices))
    for mode, property_name in (
            ('PAINT', "paint_channels"),
            ('SOFTEN', "soften_channels"),
            ('SMEAR', "smear_channels"),
            ('ERASE', "erase_channels")):
        values = getattr(paint_layer, property_name)
        check("clear all %s targets" % mode.lower(),
              bpy.ops.impasto.brush_channels_set(
                  mode=mode, selected=False) == {"FINISHED"}
              and not any(values[index] for index in erase_target_indices))
        check("select all %s targets" % mode.lower(),
              bpy.ops.impasto.brush_channels_set(
                  mode=mode, selected=True) == {"FINISHED"}
              and all(values[index] for index in erase_target_indices))

    d1 = engine.rebuild(tree)
    d2 = engine.reconcile_stack(tree)
    check("rebuild clean", not d1.errors, str(d1))
    check("idempotent second reconcile", d2.total() == 0, str(d2))

    check("remove stack",
          bpy.ops.impasto.stack_remove() == {"FINISHED"})
    check("stack removed", engine.find_stack_for_material(mat) is None)
    impasto.unregister()
    check("package unregistration",
          not hasattr(bpy.types.ShaderNodeTree, "impasto"))
    print("IMPASTO_INTEGRATION_PASSED")
except Exception:
    traceback.print_exc()
    print("IMPASTO_INTEGRATION_FAILED")
