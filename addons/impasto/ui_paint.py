# SPDX-License-Identifier: GPL-2.0-or-later
"""Paint-control and layer-binding rendering for the Impasto sidebar.

Kept as a mixin so the registered panel and its public Blender identifier stay
in ``ui``, while the substantial paint workflow UI evolves independently.
"""

import bpy

from . import gpu_engine
from . import model
from . import ops
from . import ui_icons
from .ui_channels import format_image_dimensions, paint_layer_image_sizes


def draw_brush_mode(layout, layer):
    """Draw a compact, explicit GPU brush-mode selector."""
    box = layout.box()
    box.label(text="Brush Mode")
    row = box.row(align=True)
    row.scale_y = 1.2
    op = row.operator(ops.IMPASTO_OT_brush_mode_set.bl_idname, text="Paint",
                      icon='BRUSH_DATA', depress=layer.brush_mode == 'PAINT')
    op.mode = 'PAINT'
    op = row.operator(ops.IMPASTO_OT_brush_mode_set.bl_idname, text="Soften",
                      icon_value=ui_icons.icon_value('soften'),
                      depress=layer.brush_mode == 'SOFTEN')
    op.mode = 'SOFTEN'
    op = row.operator(ops.IMPASTO_OT_brush_mode_set.bl_idname, text="Smear",
                      icon='FORCE_VORTEX',
                      depress=layer.brush_mode == 'SMEAR')
    op.mode = 'SMEAR'
    op = row.operator(ops.IMPASTO_OT_brush_mode_set.bl_idname, text="Erase",
                      icon_value=ui_icons.icon_value('erase'),
                      depress=layer.brush_mode == 'ERASE')
    op.mode = 'ERASE'


def draw_brush_channels(layout, layer, channel_keys):
    """Draw compact, mode-specific GPU brush channel targeting."""
    mode = layer.brush_mode
    property_name = ops._BRUSH_CHANNEL_PROPERTIES[mode]
    label = "%s Channels" % mode.title()
    box = layout.box()
    header = box.row(align=True)
    header.label(text=label, icon='IMAGE_DATA')
    op = header.operator(ops.IMPASTO_OT_brush_channels_set.bl_idname,
                         text="All")
    op.mode = mode
    op.selected = True
    op = header.operator(ops.IMPASTO_OT_brush_channels_set.bl_idname,
                         text="None")
    op.mode = mode
    op.selected = False
    grid = box.grid_flow(row_major=True, columns=2, even_columns=True,
                         even_rows=False, align=True)
    selected = 0
    for key in channel_keys:
        index = model.CHANNEL_ORDER[key]
        grid.prop(layer, property_name, index=index,
                  text=model.CHANNEL_MAP[key].label, toggle=True)
        selected += bool(getattr(layer, property_name)[index])
    if not selected:
        warning = box.row()
        warning.alert = True
        warning.label(text="Select at least one channel", icon='ERROR')


def draw_recent_colors(layout, layer, channel_keys):
    """Draw a collapsed-by-default, per-layer color recall menu."""
    if not any(key in channel_keys
               for key in ('base_color', 'emission_color')):
        return
    box = layout.box()
    row = box.row(align=True)
    row.prop(layer, "ui_show_recent_colors", text="Recent Colors",
             icon=('TRIA_DOWN' if layer.ui_show_recent_colors
                   else 'TRIA_RIGHT'), emboss=False)
    if not layer.ui_show_recent_colors:
        return
    stack = layer.id_data.impasto
    groups = (
        ('base_color', "Base", stack.recent_base_colors),
        ('emission_color', "Emission", stack.recent_emission_colors),
    )
    shown = False
    for key, label, colors in groups:
        if key not in channel_keys:
            continue
        shown = True
        box.label(text=label)
        if not colors:
            row = box.row()
            row.label(text="Paint to add colors", icon='INFO')
            continue
        box.label(text="Click the arrow to reuse", icon='INFO')
        # Newest first. A read-only, color-managed socket is the swatch; the
        # adjacent operator recalls it without making history itself editable.
        grid = box.grid_flow(row_major=True, columns=4, even_columns=True,
                             even_rows=True, align=True)
        for index in reversed(range(len(colors))):
            swatch = grid.row(align=True)
            swatch.template_node_socket(
                color=tuple(colors[index].color) + (1.0,))
            op = swatch.operator(ops.IMPASTO_OT_recent_color_apply.bl_idname,
                                 text="", icon='IMPORT')
            op.channel_key = key
            op.index = index
    if not shown:
        box.label(text="Enable Base or Emission Color", icon='INFO')


def draw_material_presets(layout, layer):
    """Compact persistent palette; node sockets provide sphere thumbnails."""
    box = layout.box()
    header = box.row(align=True)
    header.prop(
        layer, "ui_show_material_presets", text="Material Presets",
        icon=('TRIA_DOWN' if layer.ui_show_material_presets
              else 'TRIA_RIGHT'), emboss=False)
    header.operator(
        ops.IMPASTO_OT_material_preset_capture.bl_idname,
        text="", icon='ADD')
    if not layer.ui_show_material_presets:
        return
    presets = layer.id_data.impasto.material_presets
    if not presets:
        box.label(text="Save the current brush material", icon='INFO')
        return
    grid = box.grid_flow(row_major=True, columns=2, even_columns=True,
                         even_rows=True, align=True)
    for index, preset in enumerate(presets):
        card = grid.box()
        row = card.row(align=True)
        row.template_node_socket(
            color=tuple(preset.base_color) + (1.0,))
        apply = row.operator(
            ops.IMPASTO_OT_material_preset_apply.bl_idname,
            text=preset.label, emboss=False)
        apply.index = index
        remove = row.operator(
            ops.IMPASTO_OT_material_preset_remove.bl_idname,
            text="", icon='X', emboss=False)
        remove.index = index


class PaintPanelMixin:
    def _draw_paint_tools(self, context, box, layer):
        keys = [key for key, _image in ops.gpu_paint_targets(layer)]
        paint = box.box()
        row = paint.row(align=True)
        row.label(text="Brush Controls", icon='BRUSH_DATA')
        row.label(text="%d channel%s" %
                  (len(keys), "s" if len(keys) != 1 else ""))
        if not keys:
            paint.label(text="Enable a painted channel above", icon='INFO')
            return

        row = paint.row(align=True)
        row.label(text="Painting Engine")
        row.prop(layer, "paint_workflow", text="")
        if layer.paint_workflow == 'BLENDER':
            warning = paint.column(align=True)
            warning.alert = True
            warning.label(text="Prototype demo — fundamentally slow",
                          icon='ERROR')
            warning.label(text="Not intended for serious painting")

        if layer.paint_workflow == 'GPU':
            brush = paint.column(align=True)
            draw_brush_mode(brush, layer)
            draw_brush_channels(brush, layer, keys)
            brush.separator()
            brush.label(text="Brush Shape & Input", icon='BRUSH_DATA')
            brush.prop(layer, "brush_radius", text="Brush Radius")
            brush.prop(layer, "brush_hardness", text="Brush Hardness",
                       slider=True)
            brush.prop(layer, "brush_opacity",
                       text=("Effect Strength"
                             if layer.brush_mode in {'SOFTEN', 'SMEAR'}
                             else "Brush Opacity"),
                       slider=True)
            row = brush.row(align=True)
            row.label(text="Pressure")
            row.prop(layer, "brush_pressure_opacity", toggle=True)
            row.prop(layer, "brush_pressure_size", toggle=True)

        paint.separator()
        erasing = (layer.paint_workflow == 'GPU'
                   and layer.brush_mode == 'ERASE')
        softening = (layer.paint_workflow == 'GPU'
                     and layer.brush_mode == 'SOFTEN')
        smearing = (layer.paint_workflow == 'GPU'
                    and layer.brush_mode == 'SMEAR')
        paint.label(text=("Values ignored while erasing" if erasing else
                          "Softens selected layer channels" if softening
                          else "Smears selected layer channels" if smearing
                          else "Painted Channel Values"), icon='MATERIAL')
        if softening or smearing:
            paint.label(text="Pressure controls strength when enabled",
                        icon='INFO')
        values = paint.column(align=True)
        values.enabled = not (erasing or softening or smearing)
        if 'base_color' in keys:
            values.prop(layer, "paint_color", text="Base Color")
        if 'roughness' in keys:
            values.prop(layer, "paint_roughness", text="Roughness", slider=True)
        if 'metallic' in keys:
            values.prop(layer, "paint_metallic", text="Metallic", slider=True)
        if 'normal' in keys:
            values.prop(layer, "paint_normal", text="Normal")
        if 'height' in keys:
            row = values.row(align=True)
            row.prop(layer, "paint_height_direction", expand=True)
            row.prop(layer, "paint_height_strength", text="Step")
        if 'emission_color' in keys or 'emission_strength' in keys:
            emission = paint.box()
            row = emission.row(align=True)
            row.prop(layer, "ui_show_emission_paint", text="Emission",
                     icon=('TRIA_DOWN' if layer.ui_show_emission_paint
                           else 'TRIA_RIGHT'), emboss=False)
            if layer.ui_show_emission_paint:
                if 'emission_color' in keys:
                    emission.prop(layer, "paint_emission_color", text="Color")
                if 'emission_strength' in keys:
                    emission.prop(layer, "paint_emission_strength",
                                  text="Strength")
        draw_recent_colors(paint, layer, keys)
        draw_material_presets(paint, layer)
        if any(k in keys for k in
               ('sss_weight', 'sss_radius', 'sss_scale')):
            subsurface = paint.box()
            row = subsurface.row(align=True)
            row.prop(layer, "ui_show_subsurface_paint", text="Subsurface",
                     icon=('TRIA_DOWN' if layer.ui_show_subsurface_paint
                           else 'TRIA_RIGHT'), emboss=False)
            if layer.ui_show_subsurface_paint:
                if 'sss_weight' in keys:
                    subsurface.prop(layer, "paint_sss_weight", text="Weight",
                                    slider=True)
                if 'sss_radius' in keys:
                    subsurface.prop(layer, "paint_sss_radius",
                                    text="Radius RGB")
                if 'sss_scale' in keys:
                    subsurface.prop(layer, "paint_sss_scale", text="Scale")
                subsurface.prop(layer, "show_sss_caliper",
                                text="Show SSS Caliper", toggle=True,
                                icon='DRIVER_DISTANCE')
                subsurface.label(
                    text="Weight = amount; Scale = travel distance",
                    icon='INFO')
                subsurface.label(text="Radius sets relative RGB travel",
                                 icon='INFO')

        row = paint.row()
        row.scale_y = 1.35
        row.enabled = not gpu_engine.session_active()
        if layer.paint_workflow == 'GPU':
            row.operator(ops.IMPASTO_OT_gpu_paint.bl_idname,
                         text="Start GPU Painting", icon='BRUSH_DATA')
        else:
            row.operator(ops.IMPASTO_OT_native_multichannel_paint.bl_idname,
                         text="Start Blender Brush Replay", icon='TPAINT_HLT')

        row = paint.row(align=True)
        row.prop(layer, "ui_show_advanced", text="Advanced",
                 icon='TRIA_DOWN' if layer.ui_show_advanced
                 else 'TRIA_RIGHT', emboss=False)
        if layer.ui_show_advanced:
            self._draw_advanced_paint(paint, layer)
        if gpu_engine.session_active():
            self._draw_gpu_session(paint)
        if gpu_engine.last_error():
            paint.label(text="GPU paint failed — see console", icon='ERROR')

    def _draw_advanced_paint(self, box, layer):
        col = box.column(align=True)
        row = col.row(align=True)
        row.prop(layer, "gpu_preview_mode", text="Live Preview")
        row.popover(panel="IMPASTO_PT_preview_lighting", text="", icon='LIGHT')
        stencil_box = col.box()
        stencil_box.prop(layer, "brush_stencil_enabled")
        controls = stencil_box.column(align=True)
        controls.enabled = layer.brush_stencil_enabled
        self._draw_stencil_controls(controls, layer)
        row = col.row(align=True)
        row.prop(layer, "auto_material_preview", text="Idle Sync")
        delay = row.row(align=True)
        delay.enabled = layer.auto_material_preview
        delay.prop(layer, "auto_material_preview_delay", text="Delay")
        col.separator()
        col.prop(layer, "experimental_uv_gutters",
                 text="Legacy Seam Padding")
        col.prop(layer, "experimental_conservative_seams",
                 text="Conservative UV Seam Paint")
        if layer.experimental_uv_gutters:
            warning = col.column(align=True)
            warning.alert = True
            warning.label(text="Experimental — adds GPU memory and pen-up work",
                          icon='ERROR')

    def _draw_stencil_controls(self, col, layer):
        """Present projection, source data, and effect as distinct choices."""
        # Blender owns the thumbnail cache, so this remains cheap and updates
        # when the selected Image datablock changes without an Impasto GPU
        # allocation or pixel readback.
        col.template_ID_preview(
            layer, "brush_stencil_image",
            open=ops.IMPASTO_OT_stencil_image_open.bl_idname,
            rows=3, cols=4, hide_buttons=False)

        col.separator()
        col.label(text="Placement", icon='VIEW_CAMERA')
        col.prop(layer, "brush_stencil_projection", expand=True)
        if layer.brush_stencil_projection == 'VIEW_STENCIL':
            col.label(text="Fixed camera-facing image", icon='INFO')
            col.prop(layer, "brush_stencil_position", text="Position")
            col.prop(layer, "brush_stencil_scale", text="Viewport Scale")
            col.label(text="Amber boxes scale; cyan knob rotates",
                      icon='INFO')
            col.label(text="Shift keeps aspect  ·  R resets placement")
        else:
            col.label(text="Image follows every brush dab", icon='INFO')
            col.prop(layer, "brush_stencil_brush_scale", text="Brush Scale")
        col.prop(layer, "brush_stencil_rotation", text="Rotation")

        col.separator()
        col.label(text="Read Image From", icon='IMAGE_DATA')
        col.prop(layer, "brush_stencil_interpretation", expand=True)

        col.separator()
        effects = col.box()
        effects.label(text="Stencil Effects", icon='MODIFIER')
        effects.prop(layer, "brush_stencil_coverage", toggle=True)
        effects.prop(layer, "brush_stencil_normal_relief", toggle=True)
        effects.label(text="Coverage affects enabled Layer Channels only",
                      icon='INFO')

        controls = col.box()
        controls.label(text="Effect Controls", icon='SETTINGS')
        controls.prop(layer, "brush_stencil_opacity", text="Stamp Opacity",
                      slider=True)
        if layer.brush_stencil_normal_relief:
            controls.prop(layer, "brush_stencil_profile_strength",
                          text="Relief Strength", slider=True)
            controls.prop(layer, "brush_stencil_profile_invert",
                          text="Invert Relief")
            if layer.brush_stencil_interpretation == 'ALPHA':
                warning = controls.box().column(align=True)
                warning.label(text="Alpha requires varying transparency",
                              icon='ERROR')
                warning.label(text="Opaque grayscale image? Choose Grayscale")
        if (not layer.brush_stencil_coverage
                and not layer.brush_stencil_normal_relief):
            effects.label(text="Enable at least one stencil effect",
                          icon='ERROR')

    def _draw_gpu_session(self, box):
        if gpu_engine.material_inspect_requested():
            box.label(text="Synchronizing material…", icon='FILE_REFRESH')
        elif gpu_engine.material_inspect_active():
            box.label(text="Inspecting Blender material", icon='SHADING_RENDERED')
        elif gpu_engine.input_paused():
            box.label(text="Painting paused — P to resume", icon='PAUSE')
        else:
            box.label(text="GPU painting active — P to pause", icon='REC')
        if (not gpu_engine.material_inspect_active()
                and not gpu_engine.material_inspect_requested()):
            row = box.row()
            row.enabled = not gpu_engine.stroke_active()
            row.operator(ops.IMPASTO_OT_gpu_material_inspect_toggle.bl_idname,
                         text="Inspect Material", icon='SHADING_RENDERED')
        elif gpu_engine.material_inspect_active():
            box.operator(ops.IMPASTO_OT_gpu_material_inspect_toggle.bl_idname,
                         text="Resume Painting", icon='PLAY')
        row = box.row()
        row.enabled = not gpu_engine.stroke_active()
        row.operator(ops.IMPASTO_OT_gpu_flush.bl_idname,
                     text="Flush for Save / Export", icon='FILE_REFRESH')

    def _draw_legacy_paint_tools(self, context, box, layer):
        bound = [b.name for b in layer.bindings
                 if b.enabled and b.mode == 'SHARED'
                 and b.name in model.CHANNEL_MAP]
        replay_keys = [key for key, _image in ops.gpu_paint_targets(layer)]
        image = bpy.data.images.get(layer.image_name)
        row = box.row(align=True)
        row.label(text=image.name if image else "Missing image",
                  icon='IMAGE_DATA' if image else 'ERROR')
        painting = (context.object is not None
                    and context.object.mode == 'TEXTURE_PAINT')
        paint_status = ("Texture Paint active"
                        if painting else "Object Mode")
        if len(replay_keys) > 1:
            paint_status += " — use multi-channel control below"
        elif not painting:
            paint_status += " — press Start Painting"
        box.label(text=paint_status,
                  icon='CHECKMARK' if painting else 'INFO')
        if 'normal' in bound:
            box.label(text="RGB normal: absolute direction", icon='INFO')
        if 'height' in bound:
            box.label(text="Height: repeated strokes accumulate",
                      icon='INFO')
            row = box.row(align=True)
            op = row.operator(ops.IMPASTO_OT_detail_paint.bl_idname,
                              text="Raise", icon='TRIA_UP')
            op.direction = 'RAISE'
            op = row.operator(ops.IMPASTO_OT_detail_paint.bl_idname,
                              text="Lower", icon='TRIA_DOWN')
            op.direction = 'LOWER'
        if len(replay_keys) <= 1 and (any(k != 'height' for k in bound)
                                     or not bound):
            row = box.row()
            row.scale_y = 1.25
            op = row.operator(ops.IMPASTO_OT_paint_activate.bl_idname,
                              text="Start Painting",
                              icon='TPAINT_HLT')
            op.channel_key = ""

        gpu_keys = replay_keys
        if gpu_keys:
            col = box.column(align=True)
            col.label(text="Shared Multi-Channel Values", icon='BRUSH_DATA')
            col.label(text="Used by Blender replay and GPU paint",
                      icon='INFO')
            col.label(text="Targets (%d): %s" % (
                len(gpu_keys), ", ".join(
                    model.CHANNEL_MAP[k].label for k in gpu_keys)))
            if len(gpu_keys) == 1:
                col.label(text="Add channels below for simultaneous paint",
                          icon='INFO')
            if 'base_color' in gpu_keys:
                col.prop(layer, "paint_color", text="Stroke Base Color")
            if 'roughness' in gpu_keys:
                col.prop(layer, "paint_roughness", text="Stroke Roughness",
                         slider=True)
            if 'metallic' in gpu_keys:
                col.prop(layer, "paint_metallic", text="Stroke Metallic",
                         slider=True)
            if 'normal' in gpu_keys:
                col.prop(layer, "paint_normal", text="Normal")
            if 'height' in gpu_keys:
                row = col.row(align=True)
                row.prop(layer, "paint_height_direction", expand=True)
                col.prop(layer, "paint_height_strength")
            if ('emission_color' in gpu_keys
                    or 'emission_strength' in gpu_keys):
                emission = col.box()
                emission.label(text="Emission", icon='LIGHT')
                if 'emission_color' in gpu_keys:
                    emission.prop(layer, "paint_emission_color",
                                  text="Color")
                if 'emission_strength' in gpu_keys:
                    emission.prop(layer, "paint_emission_strength",
                                  text="Strength")
                    emission.label(text="HDR strength may exceed 1",
                                   icon='INFO')
            if any(k in gpu_keys for k in
                   ('sss_weight', 'sss_radius', 'sss_scale')):
                subsurface = col.box()
                subsurface.label(text="Subsurface", icon='SHADING_RENDERED')
                if 'sss_weight' in gpu_keys:
                    subsurface.prop(layer, "paint_sss_weight",
                                    text="Weight", slider=True)
                if 'sss_radius' in gpu_keys:
                    subsurface.prop(layer, "paint_sss_radius",
                                    text="Radius RGB")
                if 'sss_scale' in gpu_keys:
                    subsurface.prop(layer, "paint_sss_scale", text="Scale")
                subsurface.prop(layer, "show_sss_caliper",
                                text="Show SSS Caliper", toggle=True,
                                icon='DRIVER_DISTANCE')
                subsurface.label(
                    text="Weight = amount; Scale = travel distance",
                    icon='INFO')
                subsurface.label(
                    text="Radius sets relative RGB travel",
                    icon='INFO')
            row = box.row()
            row.scale_y = 1.25
            row.enabled = not gpu_engine.session_active()
            row.operator(
                ops.IMPASTO_OT_native_multichannel_paint.bl_idname,
                text=("Blender Brush → %d Channels" % len(gpu_keys)),
                icon='TPAINT_HLT')
            box.label(text="Uses the active Blender brush asset; applies at pen-up",
                      icon='INFO')

            box.separator()
            box.label(text="Experimental GPU Brush", icon='BRUSH_DATA')
            gpu_col = box.column(align=True)
            draw_brush_mode(gpu_col, layer)
            draw_brush_channels(gpu_col, layer, gpu_keys)
            gpu_col.prop(layer, "gpu_preview_mode", text="Live Preview")
            gpu_col.label(text="Display only — painted channels are unchanged",
                          icon='INFO')
            row = gpu_col.row(align=True)
            row.prop(layer, "brush_radius")
            row.prop(layer, "brush_hardness", slider=True)
            gpu_col.prop(layer, "brush_opacity", slider=True)
            row = gpu_col.row(align=True)
            row.label(text="Pressure")
            row.prop(layer, "brush_pressure_opacity", toggle=True)
            row.prop(layer, "brush_pressure_size", toggle=True)
            stencil_box = gpu_col.box()
            stencil_box.prop(layer, "brush_stencil_enabled")
            stencil_controls = stencil_box.column(align=True)
            stencil_controls.enabled = layer.brush_stencil_enabled
            self._draw_stencil_controls(stencil_controls, layer)
            row = gpu_col.row(align=True)
            row.prop(layer, "auto_material_preview", text="Idle Material Sync")
            sub = row.row(align=True)
            sub.enabled = layer.auto_material_preview
            sub.prop(layer, "auto_material_preview_delay", text="Delay")
            if layer.auto_material_preview:
                gpu_col.label(text="Idle sync adds GPU readback; disable for "
                                   "lowest latency",
                              icon='ERROR')
            row = box.row()
            row.scale_y = 1.25
            row.enabled = not gpu_engine.session_active()
            row.operator(
                ops.IMPASTO_OT_gpu_paint.bl_idname,
                text=("GPU Paint %d Channel%s" %
                      (len(gpu_keys), "s" if len(gpu_keys) != 1 else "")),
                icon='BRUSH_DATA')
            if gpu_engine.session_active():
                box.label(text=("Syncing Blender material…"
                                if gpu_engine.material_inspect_requested()
                                else "Blender material inspection"
                                if gpu_engine.material_inspect_active()
                                else "GPU input paused — edit settings"
                                if gpu_engine.input_paused()
                                else "GPU painting… live GPU preview"),
                          icon='BRUSH_DATA')
                box.label(text=("Please wait — resident session preserved"
                                if gpu_engine.material_inspect_requested()
                                else "Press V to return to GPU painting"
                                if gpu_engine.material_inspect_active()
                                else "Press P to resume painting"
                                if gpu_engine.input_paused()
                                else "Press P to pause and edit settings"),
                          icon='INFO')
                if (not gpu_engine.material_inspect_active()
                        and not gpu_engine.material_inspect_requested()):
                    row = box.row()
                    row.enabled = not gpu_engine.stroke_active()
                    row.operator(
                        ops.IMPASTO_OT_gpu_material_inspect_toggle.bl_idname,
                        text="Inspect Blender Material",
                        icon='SHADING_RENDERED')
                elif gpu_engine.material_inspect_active():
                    box.operator(
                        ops.IMPASTO_OT_gpu_material_inspect_toggle.bl_idname,
                        text="Resume GPU Preview",
                        icon='PLAY')
                undo_count, redo_count = gpu_engine.history_counts()
                box.label(text="GPU Undo %d / Redo %d (Ctrl-Z / Ctrl-Shift-Z)"
                          % (undo_count, redo_count), icon='LOOP_BACK')
                row = box.row()
                row.enabled = not gpu_engine.stroke_active()
                row.operator(
                    ops.IMPASTO_OT_gpu_flush.bl_idname,
                    text="Flush for Save / Export",
                    icon='FILE_REFRESH')
                box.label(text="Ctrl-S flushes before saving; use Flush before "
                               "menu Save/Export",
                          icon='INFO')
                box.label(text="RMB/Esc flushes and stops",
                          icon='INFO')
            else:
                box.label(text="Draw brushes reuse Blender spacing, strength, "
                               "pressure and falloff",
                          icon='INFO')
                box.label(text="Radius and Hardness use the controls above",
                          icon='INFO')
            err = gpu_engine.last_error()
            if err:
                box.label(text="GPU paint failed — see console",
                          icon='ERROR')

    def _draw_bindings(self, box, state, layer):
        col = box.column(align=True)
        col.label(text="Channel Images / Layer Influence")
        image_sizes = (paint_layer_image_sizes(layer)
                       if layer.layer_type == 'PAINT' else {})
        sizes_mismatch = len(set(image_sizes.values())) > 1

        def draw_channel(parent, c):
            ch = model.CHANNEL_MAP.get(c.name)
            if ch is None or not c.enabled:
                return
            row = parent.row(align=True)
            binding = None
            for b in layer.bindings:
                if b.name == c.name:
                    binding = b
                    break
            if binding is None:
                op = row.operator(ops.IMPASTO_OT_binding_add.bl_idname,
                                  text=ch.label, icon='ADD',
                                  emboss=False)
                op.channel_key = c.name
                return
            row.prop(binding, "enabled", text=ch.label)
            sub = row.row(align=True)
            sub.enabled = binding.enabled
            if binding.mode == 'COLOR':
                sub.prop(binding, "color", text="")
            elif binding.mode == 'VALUE':
                sub.prop(binding, "value", text="")
            elif layer.layer_type == 'PAINT':
                # Native single-channel edit of this channel's canvas.
                op = sub.operator(ops.IMPASTO_OT_paint_activate.bl_idname,
                                  text="", icon='TPAINT_HLT', emboss=False)
                op.channel_key = c.name
            else:
                sub.label(text="painted")
            sub.prop(binding, "opacity", text="Influence", slider=True)
            op = sub.operator(ops.IMPASTO_OT_binding_remove.bl_idname,
                              text="", icon='X', emboss=False)
            op.channel_key = c.name
            if layer.layer_type == 'PAINT':
                image = bpy.data.images.get(binding.image_name
                                            or layer.image_name)
                detail = parent.row()
                detail.enabled = False
                detail.label(text=("Image: %s  ·  %s" %
                                   (image.name, format_image_dimensions(image))
                                   if image else "Image: missing"),
                             icon='IMAGE_DATA' if image else 'ERROR')

        grouped = {'Core': [], 'Emission': [], 'Subsurface': []}
        for c in state.channels:
            ch = model.CHANNEL_MAP.get(c.name)
            if ch is not None and c.enabled:
                grouped.setdefault(ch.panel_group, []).append(c)

        for c in grouped.get('Core', []):
            draw_channel(col, c)

        sections = (
            ('Emission', 'ui_show_emission_channels', 'LIGHT'),
            ('Subsurface', 'ui_show_subsurface_channels', 'SHADING_RENDERED'),
        )
        for group_name, prop_name, group_icon in sections:
            channels = grouped.get(group_name, [])
            if not channels:
                continue
            section = col.box()
            expanded = getattr(layer, prop_name)
            row = section.row(align=True)
            row.prop(layer, prop_name, text=group_name,
                     icon='TRIA_DOWN' if expanded else 'TRIA_RIGHT',
                     emboss=False)
            row.label(text="%d" % len(channels), icon=group_icon)
            if expanded:
                for c in channels:
                    draw_channel(section, c)

        if sizes_mismatch:
            warning = col.row()
            warning.alert = True
            warning.label(text="Channel image sizes differ", icon='ERROR')
