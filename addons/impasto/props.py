# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto stack state: PropertyGroups stored on the generated root
node group's ShaderNodeTree (A4 — free .blend persistence, append/link
portability, undo integration).

Stable-ID rules (A3, design §2.2):

- every Layer/Mask gets an immutable 8-hex uid at creation and
  ``PropertyGroup.name`` IS the uid (collections keyed by name), so
  F-Curve data_paths use the string-key form and survive reorder;
- ``ChannelState.name`` / ``ChannelBinding.name`` are registry channel
  keys, not uids;
- collection indices are presentation order ONLY; every cross-reference
  is by uid.

Every ``update=`` callback routes to engine.py's trigger classifier
(UNIFORM / TOGGLE / STRUCTURAL) — no callback mutates the graph (A1).
"""

import re

import bpy
from bpy.props import (BoolProperty, BoolVectorProperty, CollectionProperty,
                       EnumProperty, FloatProperty, FloatVectorProperty,
                       IntProperty, IntVectorProperty, PointerProperty,
                       StringProperty)

from . import engine
from . import model

CANVAS_SIZE_ITEMS = (
    ('1024', "1K", "1024 × 1024 pixels"),
    ('2048', "2K", "2048 × 2048 pixels"),
    ('4096', "4K", "4096 × 4096 pixels"),
    ('8192', "8K", "8192 × 8192 pixels; experimental and VRAM intensive"),
)
from . import stencil

_LAYER_UID_RE = re.compile(r'layers\["([^"]+)"\]')

GPU_PREVIEW_MODE_ITEMS = (
    ('LIT_PBR', "Lit PBR",
     "Live approximation of the combined Base Color, Metallic, Roughness, "
     "Tangent Normal, and Height channels"),
    ('RAW_TANGENT_NORMAL', "Raw Tangent Normal",
     "Display the encoded tangent-normal RGB channel without lighting"),
    ('NEUTRAL_NORMAL_LIGHTING', "Neutral Normal Lighting",
     "Inspect painted normal direction under neutral lighting without Base "
     "Color, Metallic, or Roughness distracting from the result"),
    ('HEIGHT_GRAYSCALE', "Height Grayscale",
     "Display the painted Height channel directly as grayscale"),
)
GPU_PREVIEW_MODE_IDS = frozenset(item[0] for item in
                                 GPU_PREVIEW_MODE_ITEMS)


def _owner_layer_uid(pg):
    """Owning layer uid parsed from the PropertyGroup's RNA path, e.g.
    impasto.layers["c3a91f02"].bindings["roughness"] -> c3a91f02."""
    try:
        m = _LAYER_UID_RE.search(pg.path_from_id())
    except Exception:
        return ""
    return m.group(1) if m else ""


def _uniform(self, context):
    engine.on_uniform(self.id_data)


def _structural(self, context):
    engine.on_structural(self.id_data)


def _toggle(self, context):
    engine.on_toggle(self.id_data, _owner_layer_uid(self) or
                     getattr(self, "name", ""))


def _sss_caliper_overlay_update(self, context):
    from .gpu import sss_caliper_overlay
    sss_caliper_overlay.sync(context)


def _blend_items():
    labels = {"MIX": "Mix", "MULTIPLY": "Multiply", "SCREEN": "Screen",
              "ADD": "Add", "SUBTRACT": "Subtract",
              "OVERLAY": "Overlay"}
    return tuple((m, labels.get(m, m.title()), "") for m in
                 model.BLEND_MODES)


class ImpastoBinding(bpy.types.PropertyGroup):
    """Per-layer-per-channel participation (R1). SPARSE: a layer only
    has bindings for channels it touches. name = channel key."""
    image_name: StringProperty(
        name="Image",
        description="Paint canvas for this channel; empty uses the layer's "
                    "legacy shared canvas",
        update=_structural)
    enabled: BoolProperty(
        name="Enabled",
        description="Whether this layer deposits into this channel "
                    "(instant to flip; the shader slims after a pause)",
        default=True, update=_toggle)
    mode: EnumProperty(
        name="Mode",
        items=(('SHARED', "Shared",
                "Consume the layer's painted source"),
               ('VALUE', "Value", "Deposit a constant value"),
               ('COLOR', "Color", "Deposit a constant color")),
        default='SHARED', update=_structural)
    value: FloatProperty(
        name="Value", default=0.0, soft_min=0.0, soft_max=1.0,
        update=_uniform)
    color: FloatVectorProperty(
        name="Color", subtype='COLOR', size=4, min=0.0, max=1.0,
        default=(0.8, 0.8, 0.8, 1.0), update=_uniform)
    blend_mode: EnumProperty(
        name="Blend",
        items=(('LAYER', "Layer default", "Inherit the layer's blend "
                "mode"),) + _blend_items(),
        default='LAYER', update=_structural)
    opacity: FloatProperty(
        name="Channel Influence",
        description="Layer-compositing influence for this channel; this "
                    "does not change the value painted by the brush",
        default=1.0, min=0.0, max=1.0,
        subtype='FACTOR', update=_uniform)
    use_masks: BoolProperty(
        name="Use Masks",
        description="Gate this channel's deposit by the layer's mask "
                    "chain (and paint alpha)",
        default=True, update=_structural)


class ImpastoMask(bpy.types.PropertyGroup):
    """Mask state (compiled in phase 1; UI arrives with phase 3).
    name = uid."""
    label: StringProperty(name="Name", default="Mask")
    mask_type: EnumProperty(
        items=(('IMAGE', "Image", "Painted grayscale image mask"),),
        default='IMAGE')
    image_name: StringProperty(update=_structural)
    uv_map: StringProperty(update=_structural)
    blend: EnumProperty(
        items=(('MULTIPLY', "Multiply", ""),),
        default='MULTIPLY', update=_structural)
    invert: BoolProperty(default=False, update=_uniform)
    opacity: FloatProperty(default=1.0, min=0.0, max=1.0,
                           subtype='FACTOR', update=_uniform)
    visible: BoolProperty(default=True, update=_structural)


class ImpastoRecentColor(bpy.types.PropertyGroup):
    """A color deliberately used by a layer brush, persisted in the .blend."""
    color: FloatVectorProperty(
        name="Recent Color", subtype='COLOR', size=3,
        min=0.0, max=1.0, default=(0.8, 0.2, 0.1))


class ImpastoLayer(bpy.types.PropertyGroup):
    """One stack layer. name = uid; label is the user-facing name."""
    label: StringProperty(name="Name", default="Layer")
    layer_type: EnumProperty(
        items=(('PAINT', "Paint", "Painted image layer"),
               ('FILL', "Fill", "Constant color/value layer"),
               ('GROUP', "Group", "Organizational group "
                "(pass-through)")),
        default='PAINT')
    parent_uid: StringProperty(update=_structural)
    visible: BoolProperty(
        name="Visible", default=True, update=_toggle)
    opacity: FloatProperty(
        name="Opacity", default=1.0, min=0.0, max=1.0,
        subtype='FACTOR', update=_uniform)
    blend_mode: EnumProperty(
        name="Blend", items=_blend_items(), default='MIX',
        update=_structural)
    image_name: StringProperty(update=_structural)
    uv_map: StringProperty(update=_structural)
    bindings: CollectionProperty(type=ImpastoBinding)
    masks: CollectionProperty(type=ImpastoMask)
    active_mask_index: IntProperty(name="Active Mask", default=-1)
    paint_color: FloatVectorProperty(
        name="Base Color", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(0.8, 0.2, 0.1))
    paint_roughness: FloatProperty(
        name="Stroke Roughness",
        description="Grayscale roughness value written into this layer's "
                    "Roughness image by GPU strokes",
        default=0.5, min=0.0, max=1.0,
        subtype='FACTOR')
    paint_metallic: FloatProperty(
        name="Stroke Metallic",
        description="Grayscale metallic value written into this layer's "
                    "Metallic image by GPU strokes",
        default=0.0, min=0.0, max=1.0,
        subtype='FACTOR')
    paint_normal: FloatVectorProperty(
        name="Tangent Normal", subtype='COLOR', size=3,
        min=0.0, max=1.0, default=(0.5, 0.5, 1.0))
    paint_height_strength: FloatProperty(
        name="Height Step", default=0.05, min=0.0, soft_max=0.25)
    paint_height_direction: EnumProperty(
        name="Height", items=(('RAISE', "Raise", "Add height"),
                              ('LOWER', "Lower", "Subtract height")),
        default='RAISE')
    paint_emission_color: FloatVectorProperty(
        name="Emission Color", subtype='COLOR', size=3,
        min=0.0, max=1.0, default=(1.0, 1.0, 1.0))
    paint_emission_strength: FloatProperty(
        name="Emission Strength",
        description="HDR luminosity written independently of emission color",
        default=0.0, min=0.0, soft_max=20.0)
    ui_show_recent_colors: BoolProperty(
        name="Recent Colors",
        description="Show colors recently used for painting on this layer",
        default=False)
    ui_show_material_presets: BoolProperty(
        name="Material Presets",
        description="Show the compact brush-material preset palette",
        default=False)
    paint_sss_weight: FloatProperty(
        name="Subsurface Weight",
        description="How much subsurface scattering contributes; 0 disables "
                    "the effect and 1 gives its full contribution",
        default=0.0, min=0.0, max=1.0,
        subtype='FACTOR')
    paint_sss_radius: FloatVectorProperty(
        name="Subsurface Radius",
        description="Relative scattering distance for red, green, and blue; "
                    "larger values let that color travel farther",
        size=3, min=0.0, soft_max=4.0, default=(1.0, 0.2, 0.1))
    paint_sss_scale: FloatProperty(
        name="Subsurface Scale",
        description="Overall distance light travels beneath the surface in "
                    "scene units; unlike Weight, this controls depth",
        default=0.05, min=0.0, soft_max=1.0,
        subtype='DISTANCE', unit='LENGTH')
    show_sss_caliper: BoolProperty(
        name="Show SSS Caliper",
        description="Show red, green, and blue rings for the effective "
                    "Subsurface distances (Scale multiplied by Radius RGB) "
                    "while the cursor is over the mesh; the separate white "
                    "circle is the GPU-paint brush radius and appears only "
                    "during GPU painting",
        default=False,
        update=_sss_caliper_overlay_update)
    brush_radius: FloatProperty(
        name="Brush Radius", default=50.0, min=1.0, soft_max=500.0,
        subtype='PIXEL')
    brush_hardness: FloatProperty(
        name="Brush Hardness", default=0.5, min=0.0, max=0.999,
        subtype='FACTOR')
    brush_opacity: FloatProperty(
        name="Stroke Opacity",
        description="Additional opacity multiplier applied to every channel "
                    "of a GPU stroke",
        default=1.0, min=0.0, max=1.0, subtype='FACTOR')
    brush_mode: EnumProperty(
        name="Brush Mode",
        description="Paint, soften, smear, or erase active-layer detail",
        items=(('PAINT', "Paint", "Paint configured values into selected "
                                  "channels"),
               ('SOFTEN', "Soften", "Blur detail in selected channels "
                                    "using pressure-scaled soften strength"),
               ('SMEAR', "Smear", "Transport active-layer pixels along the "
                                   "stroke direction"),
               ('ERASE', "Erase", "Erase active-layer coverage to reveal "
                                   "the layers below")),
        default='PAINT')
    erase_channels: BoolVectorProperty(
        name="Erase Channels",
        description="Choose which enabled layer channels the Erase brush "
                    "removes; newly created and existing layers default to "
                    "all channels",
        size=len(model.CHANNELS),
        default=tuple(True for _channel in model.CHANNELS))
    paint_channels: BoolVectorProperty(
        name="Paint Channels",
        description="Choose which enabled layer channels receive Paint "
                    "brush values",
        size=len(model.CHANNELS),
        default=tuple(True for _channel in model.CHANNELS))
    soften_channels: BoolVectorProperty(
        name="Soften Channels",
        description="Choose which enabled layer channels the Soften brush "
                    "blurs",
        size=len(model.CHANNELS),
        default=tuple(True for _channel in model.CHANNELS))
    smear_channels: BoolVectorProperty(
        name="Smear Channels",
        description="Choose which enabled layer channels the Smear brush "
                    "transports",
        size=len(model.CHANNELS),
        default=tuple(True for _channel in model.CHANNELS))
    brush_pressure_opacity: BoolProperty(
        name="Opacity",
        description="Use tablet pressure to control GPU stroke opacity",
        default=True)
    brush_pressure_size: BoolProperty(
        name="Size",
        description="Use tablet pressure to control GPU brush size",
        default=True)
    brush_stencil_enabled: BoolProperty(
        name="Image Stencil", description="Modulate every enabled GPU paint "
        "channel with one shared image mask", default=False)
    brush_stencil_image: PointerProperty(
        name="Stencil Image", type=bpy.types.Image,
        description="Alpha or luminance image used by the GPU brush")
    brush_stencil_projection: EnumProperty(
        name="Projection", items=stencil.PROJECTION_ITEMS,
        default='VIEW_STENCIL')
    brush_stencil_interpretation: EnumProperty(
        name="Mask From", items=stencil.INTERPRETATION_ITEMS,
        default='ALPHA')
    brush_stencil_usage: EnumProperty(
        name="Usage", items=stencil.USAGE_ITEMS, default='COVERAGE')
    brush_stencil_coverage: BoolProperty(
        name="Paint Coverage",
        description="Use stencil intensity to mask every enabled painted "
                    "channel; can be combined with Normal Relief",
        default=True)
    brush_stencil_normal_relief: BoolProperty(
        name="Normal Relief",
        description="Derive tangent-space Normal detail from stencil "
                    "brightness gradients; can be combined with Paint "
                    "Coverage",
        get=lambda self: self.brush_stencil_usage == 'NORMAL_PROFILE',
        set=lambda self, value: setattr(
            self, "brush_stencil_usage",
            'NORMAL_PROFILE' if value else 'COVERAGE'))
    brush_stencil_opacity: FloatProperty(
        name="Stencil Opacity", default=1.0, min=0.0, max=1.0,
        subtype='FACTOR')
    brush_stencil_position: FloatVectorProperty(
        name="Position", description="Viewport-normalized stencil center",
        size=2, default=(0.5, 0.5), soft_min=0.0, soft_max=1.0)
    brush_stencil_scale: FloatVectorProperty(
        name="Viewport Scale",
        description="Normalized viewport width and height", size=2,
        default=(0.35, 0.35), min=0.001, soft_max=2.0)
    brush_stencil_brush_scale: FloatVectorProperty(
        name="Brush Scale",
        description="Brush-diameter multiplier; (1, 1) maps the image "
        "across the full brush footprint", size=2,
        default=(1.0, 1.0), min=0.001, soft_max=2.0)
    brush_stencil_rotation: FloatProperty(
        name="Rotation", default=0.0, subtype='ANGLE')
    brush_stencil_profile_strength: FloatProperty(
        name="Relief Strength",
        description="Strength of tangent-normal detail derived from image "
                    "intensity gradients",
        default=1.0, min=0.0, soft_max=8.0)
    brush_stencil_profile_invert: BoolProperty(
        name="Invert Relief",
        description="Reverse raised and recessed normal-profile detail",
        default=False)
    auto_material_preview: BoolProperty(
        name="Idle Material Synchronization",
        description="After a pause, read GPU textures back to Blender Images "
                    "and show the authoritative material; leave disabled for "
                    "the lowest painting latency",
        default=False)
    auto_material_preview_delay: FloatProperty(
        name="Feedback Delay", description="Idle time before authoritative "
        "material feedback", default=0.35, min=0.1, max=2.0,
        subtype='TIME', unit='TIME')
    gpu_preview_mode: EnumProperty(
        name="Live Preview",
        description="How the GPU-resident paint overlay is visualized; this "
                    "does not alter painted channel data",
        items=GPU_PREVIEW_MODE_ITEMS,
        default='LIT_PBR')
    preview_environment_exposure: FloatProperty(
        name="Environment Exposure",
        description="Preview-only environment brightness in exposure stops",
        default=0.0, min=-4.0, max=4.0)
    preview_environment_rotation: FloatProperty(
        name="Environment Rotation",
        description="Rotate the preview environment around the model",
        default=0.0, subtype='ANGLE')
    preview_key_strength: FloatProperty(
        name="Key Strength",
        description="Brightness of the main preview studio light",
        default=1.0, min=0.0, soft_max=8.0)
    preview_key_rotation: FloatProperty(
        name="Key Rotation",
        description="Rotate the main preview light around the model",
        default=0.0, subtype='ANGLE')
    preview_fill_strength: FloatProperty(
        name="Fill Strength",
        description="Brightness of the broad light revealing recessed areas",
        default=1.0, min=0.0, soft_max=8.0)
    preview_roughness_readability: FloatProperty(
        name="Roughness Readability",
        description="Preview-only supplemental studio reflection that makes "
                    "low and medium roughness easier to distinguish; does "
                    "not change painted roughness or the material",
        default=0.0, min=0.0, soft_max=4.0)
    preview_base_normal_image: PointerProperty(
        name="Base Normal Map", type=bpy.types.Image,
        description="Optional normal map used only by Impasto's live preview")
    preview_base_normal_uv_map: StringProperty(
        name="UV Map",
        description="Mesh UV map used by the preview-only base normal")
    preview_base_normal_strength: FloatProperty(
        name="Strength",
        description="Strength of the preview-only base normal map",
        default=1.0, min=0.0, soft_max=4.0)
    preview_base_normal_invert_green: BoolProperty(
        name="Invert Green",
        description="Invert the normal map's green channel (DirectX/OpenGL convention)",
        default=False)
    paint_workflow: EnumProperty(
        name="Paint Engine",
        items=(('GPU', "GPU Multi-Channel",
                "Resident multi-channel painting with live preview"),
               ('BLENDER', "Blender Brush Replay (Prototype)",
                "Embryonic compatibility demo; fundamentally non-performant "
                "and not intended for serious painting")),
        default='GPU')
    ui_show_channels: BoolProperty(name="Channels", default=True)
    ui_show_emission_channels: BoolProperty(name="Emission", default=False)
    ui_show_subsurface_channels: BoolProperty(name="Subsurface", default=False)
    ui_show_emission_paint: BoolProperty(
        name="Emission Brush Values", default=False,
        description="Show Emission color and strength brush controls")
    ui_show_subsurface_paint: BoolProperty(
        name="Subsurface Brush Values", default=False,
        description="Show Subsurface brush controls and the SSS caliper")
    ui_show_advanced: BoolProperty(name="Advanced", default=False)
    experimental_uv_gutters: BoolProperty(
        name="Experimental Seam Padding",
        description="Pad painted UV-island edges after each GPU stroke to "
                    "reduce filtering gaps; exact duplicates disable it",
        default=False)
    experimental_conservative_seams: BoolProperty(
        name="Conservative UV Seam Paint",
        description="Experimental: paint a narrow face-clamped boundary "
                    "around touched UV seams to prevent texel-center gaps",
        default=False)


class ImpastoChannel(bpy.types.PropertyGroup):
    """A registry channel enabled on this stack. name = channel key."""
    enabled: BoolProperty(default=True, update=_structural)


def _active_index_update(self, context):
    if 0 <= self.active_index < len(self.layers):
        self.active_layer_uid = self.layers[self.active_index].name
    else:
        self.active_layer_uid = ""
    # Selection switches the native canvas but never forces a mode change.
    # A callback cannot report errors usefully, so invalid/non-paint targets
    # are left for the explicit paint operator to explain.
    layer = self.active_layer()
    if layer is not None and layer.layer_type == 'PAINT':
        try:
            from . import paint
            paint.activate_paint_target(context, layer)
        except paint.PaintTargetError:
            pass
    from .gpu import sss_caliper_overlay
    sss_caliper_overlay.sync(context)


class ImpastoMaterialPreset(bpy.types.PropertyGroup):
    """One persistent snapshot of brush material values."""
    label: StringProperty(name="Name", default="Material")
    base_color: FloatVectorProperty(
        name="Base Color", subtype='COLOR', size=3,
        min=0.0, max=1.0, default=(0.8, 0.2, 0.1))
    roughness: FloatProperty(default=0.5, min=0.0, max=1.0)
    metallic: FloatProperty(default=0.0, min=0.0, max=1.0)
    normal: FloatVectorProperty(
        size=3, min=0.0, max=1.0, default=(0.5, 0.5, 1.0))
    height_strength: FloatProperty(default=0.05, min=0.0)
    height_direction: EnumProperty(
        items=(('RAISE', "Raise", ""), ('LOWER', "Lower", "")),
        default='RAISE')
    emission_color: FloatVectorProperty(
        subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0))
    emission_strength: FloatProperty(default=0.0, min=0.0)
    sss_weight: FloatProperty(default=0.0, min=0.0, max=1.0)
    sss_radius: FloatVectorProperty(
        size=3, min=0.0, default=(1.0, 0.2, 0.1))
    sss_scale: FloatProperty(default=0.05, min=0.0)


class ImpastoStack(bpy.types.PropertyGroup):
    """Root stack state, stored on the generated root node group's
    tree. NO halt/batch flags here — batching is runtime-only
    (design §4.7)."""
    is_stack: BoolProperty(default=False)
    schema_version: IntProperty(default=model.SCHEMA_VERSION)
    blender_version: IntVectorProperty(size=3)
    channels: CollectionProperty(type=ImpastoChannel)
    layers: CollectionProperty(type=ImpastoLayer)
    # Material-stack palette shared by all of its paint layers.
    recent_base_colors: CollectionProperty(type=ImpastoRecentColor)
    recent_emission_colors: CollectionProperty(type=ImpastoRecentColor)
    material_presets: CollectionProperty(type=ImpastoMaterialPreset)
    active_layer_uid: StringProperty()
    # presentation-order UI slot for template_list; the uid above is
    # the source of truth for every cross-reference.
    active_index: IntProperty(default=-1, update=_active_index_update)
    default_canvas_size: EnumProperty(
        name="Canvas Resolution", items=CANVAS_SIZE_ITEMS, default='2048',
        description="Resolution for newly created Paint canvases; existing "
                    "images are not resized")

    def active_layer(self):
        for ly in self.layers:
            if ly.name == self.active_layer_uid:
                return ly
        return None


class ImpastoMaterialState(bpy.types.PropertyGroup):
    """Minimal per-material bookkeeping: only the Principled links we
    displaced (so removing the stack restores the material) and the
    stack tree name."""
    displaced_links: StringProperty(default="")   # JSON
    stack_tree: StringProperty(default="")


class ImpastoPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    auto_material_preview: BoolProperty(
        name="Switch Active Viewport to Material Preview",
        description="When Impasto painting starts from Solid shading, switch "
                    "only the invoking 3D Viewport to Material Preview so the "
                    "composed PBR material is visible",
        default=True)
    stencil_directory: StringProperty(
        name="Default Stencil Directory",
        description="Folder opened by Impasto's stencil image browser; the "
                    "last successfully loaded stencil folder is remembered",
        subtype='DIR_PATH',
        default="")

    def draw(self, context):
        self.layout.prop(self, "auto_material_preview")
        self.layout.prop(self, "stencil_directory")


_classes = (
    ImpastoBinding,
    ImpastoMask,
    ImpastoRecentColor,
    ImpastoLayer,
    ImpastoChannel,
    ImpastoMaterialPreset,
    ImpastoStack,
    ImpastoMaterialState,
    ImpastoPreferences,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.ShaderNodeTree.impasto = PointerProperty(type=ImpastoStack)
    bpy.types.Material.impasto_mat = PointerProperty(
        type=ImpastoMaterialState)


def unregister():
    del bpy.types.Material.impasto_mat
    del bpy.types.ShaderNodeTree.impasto
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
