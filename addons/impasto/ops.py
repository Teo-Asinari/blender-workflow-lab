# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto operators. Every operator is discoverable three ways —
sidebar panel, menu entry, F3 search (labels carry the add-on prefix);
all are REGISTER | UNDO. Bulk edits run inside stack_edit_session so a
whole operator costs exactly one compile+reconcile (design §4.7)."""

import json
import os
import time
from dataclasses import replace

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

from . import compat
from . import brush_adapter
from . import channel_paint
from . import engine
from . import flatten_export
from . import gpu_engine
from . import model
from . import operator_support
from . import paint
from . import props
from . import snapshot
from . import stencil

DEFAULT_IMAGE_SIZE = operator_support.DEFAULT_IMAGE_SIZE

# Compatibility aliases: these helpers historically lived in ``ops`` and are
# intentionally kept importable there for integrations and tests.
_context_material = operator_support.context_material
_context_stack = operator_support.context_stack
_unique_uid = operator_support.unique_uid
_active_uv_map = operator_support.active_uv_map
new_layer_image = operator_support.new_layer_image
_channel_canvas_seed = operator_support.channel_canvas_seed
_layer_canvas_size = operator_support.layer_canvas_size
ensure_stack_channel = operator_support.ensure_stack_channel
ensure_layer_binding = operator_support.ensure_layer_binding
_remember_displaced_channel_link = operator_support.remember_displaced_channel_link

TEMPLATE_IDS = {
    'PRINCIPLED_STANDARD': "Principled — Standard",
    'PRINCIPLED_FULL': "Principled — Full",
    'EMISSIVE_PROP': "Emissive prop",
    'SKIN_ORGANIC': "Skin / organic",
}
TEMPLATE_ITEMS = tuple(
    (tid, label, "Channels: " + ", ".join(
        model.CHANNEL_MAP[k].label for k in model.TEMPLATES[label]))
    for tid, label in TEMPLATE_IDS.items())


def import_normal_baseline(mat, image, uv_map="", fallback_node_name=""):
    """Insert or update an external tangent normal image at stack bottom.

    Public soft-integration seam used by Kiln. The image participates in the
    same encoded-RGB normal chain as painted Normal layers, so the stack keeps
    sole ownership of Principled Normal instead of two add-ons overwriting the
    same socket. Returns the imported layer, or raises ValueError.

    ``fallback_node_name`` is the material Normal Map node to restore if the
    user later removes Impasto; a newer external baseline supersedes whatever
    Normal link was displaced when the stack was first created.
    """
    tree = engine.find_stack_for_material(mat)
    if tree is None:
        raise ValueError("Material has no Impasto stack")
    if image is None:
        raise ValueError("Normal baseline image is missing")
    compat.set_image_colorspace(image, "Non-Color")
    image.use_fake_user = True
    state = tree.impasto
    active_uid = state.active_layer_uid
    imported = None
    with engine.stack_edit_session(tree):
        ensure_stack_channel(state, "normal")
        for layer in state.layers:
            for binding in layer.bindings:
                if (binding.name == "normal"
                        and layer.label == "Kiln Baked Normal"):
                    imported = layer
                    binding.image_name = image.name
                    break
            if imported is not None:
                break
        if imported is None:
            imported = state.layers.add()  # append = bottom of UI/stack
            imported.name = _unique_uid(state)
            imported.label = "Kiln Baked Normal"
            imported.layer_type = 'PAINT'
            binding = imported.bindings.add()
            binding.name = "normal"
            binding.mode = 'SHARED'
        imported.image_name = image.name
        imported.uv_map = uv_map
        binding = next(b for b in imported.bindings if b.name == "normal")
        binding.image_name = image.name
        binding.enabled = True
        # A baked tangent-normal target is an opaque material channel.  Its
        # alpha is not a paint/mask coverage channel (some bakers leave it at
        # zero), so allowing it to gate the layer can erase the Kiln baseline
        # in the resident preview and compiled material alike.
        binding.use_masks = False
        # Keep/re-establish it as the bottom baseline on repeated bakes.
        index = next(i for i, layer in enumerate(state.layers)
                     if layer.name == imported.name)
        if index != len(state.layers) - 1:
            state.layers.move(index, len(state.layers) - 1)

        if fallback_node_name:
            try:
                displaced = json.loads(mat.impasto_mat.displaced_links
                                       or "[]")
            except Exception:
                displaced = []
            displaced = [entry for entry in displaced
                         if entry.get("to_socket") != "Normal"]
            displaced.append({
                "from_node": fallback_node_name,
                "from_socket": "Normal",
                "to_socket": "Normal",
            })
            mat.impasto_mat.displaced_links = json.dumps(displaced)

    if active_uid:
        state.active_layer_uid = active_uid
        state.active_index = next(
            (i for i, layer in enumerate(state.layers)
             if layer.name == active_uid), -1)
    return imported


class IMPASTO_OT_stack_init(bpy.types.Operator):
    """Create an Impasto layer stack on the active material (a new
    material is created if the object has none)"""
    bl_idname = "impasto.stack_init"
    bl_label = "Impasto: New Layer Stack"
    bl_options = {'REGISTER', 'UNDO'}

    template: EnumProperty(name="Template", items=TEMPLATE_ITEMS,
                           default='PRINCIPLED_STANDARD')

    @classmethod
    def poll(cls, context):
        return context.object is not None and context.object.type == 'MESH'

    def execute(self, context):
        obj = context.object
        mat = obj.active_material
        if mat is None:
            mat = bpy.data.materials.new(obj.name + " Material")
            if obj.material_slots:
                obj.material_slots[obj.active_material_index].material \
                    = mat
            else:
                obj.data.materials.append(mat)
        mat.use_nodes = True
        if engine.find_stack_for_material(mat) is not None:
            self.report({'WARNING'},
                        "Material %r already has an Impasto stack"
                        % mat.name)
            return {'CANCELLED'}
        principled = compat.find_principled(mat.node_tree)
        if principled is None:
            self.report({'ERROR'},
                        "Material %r has no Principled BSDF node"
                        % mat.name)
            return {'CANCELLED'}

        keys = model.TEMPLATES[TEMPLATE_IDS[self.template]]
        tree = bpy.data.node_groups.new("Impasto Stack (%s)" % mat.name,
                                        "ShaderNodeTree")
        # Attach the root tree before the edit session exits. This makes
        # material_for_stack() able to discover the material, allowing the
        # compiler's material TreeSpec to create and wire the generated node.
        group_node = mat.node_tree.nodes.new("ShaderNodeGroup")
        group_node.name = model.n_material_stack()
        group_node.label = "Impasto (generated)"
        group_node.node_tree = tree
        with engine.stack_edit_session(tree):
            state = tree.impasto
            state.is_stack = True
            state.schema_version = model.SCHEMA_VERSION
            state.blender_version = bpy.app.version
            for key in keys:
                ch = state.channels.add()
                ch.name = key
            preferences = impasto_preferences(context)
            if preferences is not None and preferences.global_material_presets:
                load_global_material_presets(state)
            # remember the Principled links we are about to displace so
            # Remove Stack can restore the material (ori_bsdf pattern).
            displaced = []
            targets = [model.CHANNEL_MAP[k].socket for k in keys
                       if k != "height"]
            if "height" in keys:
                targets.append("Normal")
            # Normal and Height converge on the same Principled socket.
            targets = list(dict.fromkeys(targets))
            for sock_name in targets:
                sock = compat.find_socket(principled.inputs, sock_name)
                if sock is None:
                    continue
                for link in sock.links:
                    displaced.append({
                        "from_node": link.from_node.name,
                        "from_socket": link.from_socket.identifier,
                        "to_socket": sock_name,
                    })
            mstate = mat.impasto_mat
            mstate.displaced_links = json.dumps(displaced)
            mstate.stack_tree = tree.name
        self.report({'INFO'}, "Impasto stack created on %r (%s)"
                    % (mat.name, ", ".join(keys)))
        return {'FINISHED'}


class IMPASTO_OT_stack_remove(bpy.types.Operator):
    """Remove the Impasto stack from the active material and restore
    the Principled links it displaced (images are kept)"""
    bl_idname = "impasto.stack_remove"
    bl_label = "Impasto: Remove Layer Stack"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _context_stack(context)[1] is not None

    def execute(self, context):
        mat, tree = _context_stack(context)
        nt = mat.node_tree
        group_node = nt.nodes.get(model.n_material_stack())
        if group_node is not None:
            nt.nodes.remove(group_node)
        principled = compat.find_principled(nt)
        restored = 0
        try:
            displaced = json.loads(mat.impasto_mat.displaced_links
                                   or "[]")
        except Exception:
            displaced = []
        if principled is not None:
            for entry in displaced:
                src_node = nt.nodes.get(entry.get("from_node", ""))
                dst = compat.find_socket(principled.inputs,
                                         entry.get("to_socket", ""))
                if src_node is None or dst is None:
                    continue
                src = compat.find_socket(src_node.outputs,
                                         entry.get("from_socket", ""))
                if src is None:
                    continue
                nt.links.new(src, dst)
                restored += 1
        mat.impasto_mat.displaced_links = ""
        mat.impasto_mat.stack_tree = ""
        # drop generated trees (images are deliberately kept)
        for ly in tree.impasto.layers:
            lt = bpy.data.node_groups.get(model.layer_tree_name(ly.name))
            if lt is not None:
                bpy.data.node_groups.remove(lt)
        bpy.data.node_groups.remove(tree)
        self.report({'INFO'},
                    "Impasto stack removed (%d link(s) restored, "
                    "images kept)" % restored)
        return {'FINISHED'}


class IMPASTO_OT_layer_add(bpy.types.Operator):
    """Add a layer to the top of the stack (paint layers get a fresh
    transparent canvas bound to Base Color)"""
    bl_idname = "impasto.layer_add"
    bl_label = "Impasto: Add Layer"
    bl_options = {'REGISTER', 'UNDO'}

    layer_type: EnumProperty(
        name="Type",
        items=(('PAINT', "Paint", "Painted image layer"),
               ('FILL', "Fill", "Constant color/value layer"),
               ('GROUP', "Group", "Organizational group")),
        default='PAINT')
    channel_key: EnumProperty(
        name="Initial Channel",
        items=tuple((ch.key, ch.label, "Paint a dedicated %s image" % ch.label)
                    for ch in model.CHANNELS),
        default="base_color",
        description="Channel initially bound to a new Paint layer")
    canvas_size: EnumProperty(
        name="Canvas Size",
        items=(('DEFAULT', "Stack / Channel Default",
                "Use the stack default or this channel's override"),
               ('1024', "1024 (1K)", ""),
               ('2048', "2048 (2K)", "Default; interactive strokes remain "
                "GPU-resident and explicit flush is comparatively quick"),
               ('4096', "4096 (4K)", "Four-channel GPU flush/exit sync "
                "can exceed 400 ms (Image.pixels writes; see README)"),
               ('8192', "8192 (8K)", "Experimental; very high VRAM use")),
        default='DEFAULT',
        description="Resolution of every canvas this layer creates "
                    "(later channel canvases inherit it)")

    @classmethod
    def description(cls, context, properties):
        layer_type = getattr(properties, "layer_type", 'PAINT')
        if layer_type == 'FILL':
            return ("Add a Fill layer whose channels use uniform color or "
                    "numeric values across the material")
        if layer_type == 'GROUP':
            return "Add an organizational layer group"
        return ("Add a Paint layer with image canvases that store brush "
                "strokes for its enabled material channels")

    @classmethod
    def poll(cls, context):
        return _context_stack(context)[1] is not None

    def execute(self, context):
        mat, tree = _context_stack(context)
        state = tree.impasto
        uid = _unique_uid(state)
        count = sum(1 for ly in state.layers
                    if ly.layer_type == self.layer_type) + 1
        initial = model.CHANNEL_MAP.get(self.channel_key,
                                        model.CHANNEL_MAP["base_color"])
        base = {'PAINT': ("Height Detail" if initial.key == "height"
                          else "Paint Layer"), 'FILL': "Fill Layer",
                'GROUP': "Group"}[self.layer_type]
        with engine.stack_edit_session(tree):
            ly = state.layers.add()
            ly.name = uid
            ly.label = "%s %d" % (base, count)
            ly.layer_type = self.layer_type
            if self.layer_type == 'PAINT':
                ch = initial
                img = new_layer_image("Impasto %s %s" % (ly.label, uid),
                                      ch.colorspace,
                                      size=(int(self.canvas_size)
                                            if self.canvas_size != 'DEFAULT'
                                            else operator_support
                                            .stack_canvas_size(state)),
                                      generated_color=_channel_canvas_seed(ch))
                # Explicit per-binding canvas (schema 2). The layer slot
                # mirrors the primary canvas for legacy compatibility.
                ly.image_name = img.name
                ly.uv_map = _active_uv_map(context)
                b = ly.bindings.add()
                b.name = ch.key
                b.image_name = img.name
            elif self.layer_type == 'FILL':
                ch = model.CHANNEL_MAP["base_color"]
                b = ly.bindings.add()
                b.name = "base_color"
                b.mode = 'COLOR'
                b.color = model.seed_rgba(ch)
            state.layers.move(len(state.layers) - 1, 0)
            state.active_index = 0
        self.report({'INFO'}, "%s added" % state.layers[0].label)
        return {'FINISHED'}


class IMPASTO_OT_layer_remove(bpy.types.Operator):
    """Delete the active layer (its image, if any, is kept)"""
    bl_idname = "impasto.layer_remove"
    bl_label = "Impasto: Delete Layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        return (tree is not None
                and tree.impasto.active_layer() is not None)

    def execute(self, context):
        _, tree = _context_stack(context)
        state = tree.impasto
        idx = next((i for i, ly in enumerate(state.layers)
                    if ly.name == state.active_layer_uid), -1)
        if idx < 0:
            return {'CANCELLED'}
        label = state.layers[idx].label
        with engine.stack_edit_session(tree):
            state.layers.remove(idx)
            state.active_index = min(idx, len(state.layers) - 1)
        self.report({'INFO'}, "%s deleted (image kept)" % label)
        return {'FINISHED'}


class IMPASTO_OT_layer_move(bpy.types.Operator):
    """Move the active layer up or down the stack (uids, bindings and
    animation paths are untouched — order is presentation-only)"""
    bl_idname = "impasto.layer_move"
    bl_label = "Impasto: Move Layer"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(items=(('UP', "Up", ""),
                                   ('DOWN', "Down", "")),
                            default='UP')

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        return (tree is not None
                and tree.impasto.active_layer() is not None)

    def execute(self, context):
        _, tree = _context_stack(context)
        state = tree.impasto
        idx = next((i for i, ly in enumerate(state.layers)
                    if ly.name == state.active_layer_uid), -1)
        dst = idx + (-1 if self.direction == 'UP' else 1)
        if idx < 0 or not (0 <= dst < len(state.layers)):
            return {'CANCELLED'}
        with engine.stack_edit_session(tree):
            state.layers.move(idx, dst)
            state.active_index = dst
        return {'FINISHED'}


class IMPASTO_OT_channel_add(bpy.types.Operator):
    """Register a missing stack channel and optionally bind the active layer."""
    bl_idname = "impasto.channel_add"
    bl_label = "Impasto: Add Material Channel"
    bl_options = {'REGISTER', 'UNDO'}

    channel_key: StringProperty()
    bind_active_layer: BoolProperty(
        name="Bind Active Layer", default=True,
        description="Also create this channel on the selected Paint/Fill layer")

    @classmethod
    def poll(cls, context):
        return _context_stack(context)[1] is not None

    def execute(self, context):
        mat, tree = _context_stack(context)
        state = tree.impasto
        ch = model.CHANNEL_MAP.get(self.channel_key)
        if ch is None:
            return {'CANCELLED'}
        layer = state.active_layer()
        can_bind = layer is not None and layer.layer_type != 'GROUP'
        registered = state.channels.get(ch.key) is not None
        bound = can_bind and layer.bindings.get(ch.key) is not None
        if registered and (not self.bind_active_layer or bound or not can_bind):
            self.report({'INFO'}, "%s is already available" % ch.label)
            return {'FINISHED'}
        _remember_displaced_channel_link(mat, ch.key)
        with engine.stack_edit_session(tree):
            ensure_stack_channel(state, ch.key)
            if self.bind_active_layer and can_bind:
                ensure_layer_binding(layer, ch.key)
        message = "%s added to stack" % ch.label
        if self.bind_active_layer and can_bind:
            message += " and selected layer"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class IMPASTO_OT_binding_add(bpy.types.Operator):
    """Bind the active layer to a channel (paint layers get a dedicated
    per-channel canvas; fill layers deposit the channel's default
    constant)"""
    bl_idname = "impasto.binding_add"
    bl_label = "Impasto: Bind Channel"
    bl_options = {'REGISTER', 'UNDO'}

    channel_key: StringProperty()

    @classmethod
    def poll(cls, context):
        mat, tree = _context_stack(context)
        ly = tree.impasto.active_layer() if tree else None
        return ly is not None and ly.layer_type != 'GROUP'

    def execute(self, context):
        mat, tree = _context_stack(context)
        state = tree.impasto
        ly = state.active_layer()
        ch = model.CHANNEL_MAP.get(self.channel_key)
        if ly is None or ch is None:
            return {'CANCELLED'}
        _remember_displaced_channel_link(mat, ch.key)
        with engine.stack_edit_session(tree):
            ensure_stack_channel(state, ch.key)
            ensure_layer_binding(ly, ch.key)
        return {'FINISHED'}


class IMPASTO_OT_binding_remove(bpy.types.Operator):
    """Unbind a channel from the active layer"""
    bl_idname = "impasto.binding_remove"
    bl_label = "Impasto: Unbind Channel"
    bl_options = {'REGISTER', 'UNDO'}

    channel_key: StringProperty()

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        return tree is not None and tree.impasto.active_layer()

    def execute(self, context):
        _, tree = _context_stack(context)
        ly = tree.impasto.active_layer()
        idx = next((i for i, b in enumerate(ly.bindings)
                    if b.name == self.channel_key), -1)
        if idx < 0:
            return {'CANCELLED'}
        with engine.stack_edit_session(tree):
            ly.bindings.remove(idx)
        return {'FINISHED'}


class IMPASTO_OT_mask_add(bpy.types.Operator):
    """Add a paintable grayscale image mask to the active layer."""
    bl_idname = "impasto.mask_add"
    bl_label = "Impasto: Add Layer Mask"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return layer is not None and layer.layer_type != 'GROUP'

    def execute(self, context):
        _, tree = _context_stack(context)
        state, layer = tree.impasto, tree.impasto.active_layer()
        size = _layer_canvas_size(layer)
        image = new_layer_image(
            "Impasto %s Mask %s" % (layer.label, len(layer.masks) + 1),
            "Non-Color", size=size,
            generated_color=(1.0, 1.0, 1.0, 1.0))
        with engine.stack_edit_session(tree):
            mask = layer.masks.add()
            mask.name = _unique_uid(state)
            mask.label = "Mask %d" % (len(layer.masks))
            mask.image_name = image.name
            mask.uv_map = layer.uv_map or _active_uv_map(context)
            layer.active_mask_index = len(layer.masks) - 1
        self.report({'INFO'}, "Added white layer mask (image kept on removal)")
        return {'FINISHED'}


class IMPASTO_OT_mask_remove(bpy.types.Operator):
    """Remove the selected layer mask while retaining its image."""
    bl_idname = "impasto.mask_remove"
    bl_label = "Impasto: Remove Layer Mask"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return layer is not None and len(layer.masks) > 0

    def execute(self, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer()
        index = min(max(0, layer.active_mask_index), len(layer.masks) - 1)
        with engine.stack_edit_session(tree):
            layer.masks.remove(index)
            layer.active_mask_index = min(index, len(layer.masks) - 1)
        return {'FINISHED'}


class IMPASTO_OT_mask_select(bpy.types.Operator):
    """Select a layer mask for editing/removal."""
    bl_idname = "impasto.mask_select"
    bl_label = "Impasto: Select Layer Mask"
    bl_options = {'INTERNAL'}
    index: IntProperty(default=0, min=0)

    def execute(self, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        if layer is None or self.index >= len(layer.masks):
            return {'CANCELLED'}
        layer.active_mask_index = self.index
        return {'FINISHED'}


class IMPASTO_OT_mask_paint(bpy.types.Operator):
    """Paint the selected layer mask with Blender's native image brush."""
    bl_idname = "impasto.mask_paint"
    bl_label = "Impasto: Paint Layer Mask"
    bl_options = {'REGISTER'}

    def execute(self, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        if layer is None or not layer.masks:
            return {'CANCELLED'}
        index = min(max(0, layer.active_mask_index), len(layer.masks) - 1)
        try:
            paint.activate_mask_target(context, layer, layer.masks[index])
            if context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
        except (paint.PaintTargetError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        paint.activate_brush_tool(context)
        paint.maybe_switch_material_preview(context)
        self.report({'INFO'}, "Painting mask %s" % layer.masks[index].label)
        return {'FINISHED'}


def impasto_preferences(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon is not None else None


class IMPASTO_OT_stencil_image_open(bpy.types.Operator):
    """Load a stencil from the persistent thumbnail browser."""
    bl_idname = "impasto.stencil_image_open"
    bl_label = "Impasto: Load Stencil Image"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(
        default="*.bmp;*.cin;*.dds;*.dpx;*.exr;*.hdr;*.jpeg;*.jpg;*.png;"
                "*.psd;*.rgb;*.sgi;*.tga;*.tif;*.tiff;*.webp",
        options={'HIDDEN'})
    @classmethod
    def poll(cls, context):
        _mat, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return layer is not None and layer.layer_type == 'PAINT'

    def invoke(self, context, event):
        preferences = impasto_preferences(context)
        directory = (bpy.path.abspath(preferences.stencil_directory)
                     if preferences and preferences.stencil_directory else "")
        if directory and os.path.isdir(directory):
            self.filepath = os.path.join(directory, "")
        window = context.window
        context.window_manager.fileselect_add(self)
        # The File Browser replaces the invoking area after this method
        # returns. Set its view on the next UI tick rather than relying on the
        # user's last global browser mode.
        def thumbnail_view():
            if window is not None and window.screen is not None:
                for area in window.screen.areas:
                    if area.type == 'FILE_BROWSER':
                        space = area.spaces.active
                        if space.params is not None:
                            space.params.display_type = 'THUMBNAIL'
                        break
            return None
        bpy.app.timers.register(thumbnail_view, first_interval=0.0)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        _mat, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        path = bpy.path.abspath(self.filepath)
        if layer is None or not path or not os.path.isfile(path):
            self.report({'ERROR'}, "Choose an existing stencil image")
            return {'CANCELLED'}
        try:
            image = bpy.data.images.load(path, check_existing=True)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        layer.brush_stencil_image = image
        preferences = impasto_preferences(context)
        if preferences is not None:
            preferences.stencil_directory = os.path.join(
                os.path.dirname(path), "")
        self.report({'INFO'}, "Loaded stencil %s" % image.name)
        return {'FINISHED'}


class IMPASTO_OT_stack_rebuild(bpy.types.Operator):
    """Drop caches and rebuild the stack's node trees from scratch —
    the one repair operator for tampered or stale graphs"""
    bl_idname = "impasto.stack_rebuild"
    bl_label = "Impasto: Rebuild Stack"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _context_stack(context)[1] is not None

    def execute(self, context):
        mat, tree = _context_stack(context)
        kiln_tex = (mat.node_tree.nodes.get("Kiln Bake Target")
                    if mat is not None and mat.use_nodes else None)
        imported = None
        if (kiln_tex is not None
                and kiln_tex.bl_idname == 'ShaderNodeTexImage'
                and kiln_tex.image is not None):
            imported = import_normal_baseline(
                mat, kiln_tex.image, uv_map=_active_uv_map(context),
                fallback_node_name="Kiln Normal Map")
        deltas = engine.rebuild(tree)
        suffix = ("; imported Kiln Bake Target"
                  if imported is not None else "")
        self.report({'INFO'}, "Impasto rebuild: %s%s" % (deltas, suffix))
        return {'FINISHED'}


class IMPASTO_OT_import_kiln_normal(bpy.types.Operator):
    """Repair/import an existing Kiln bake beneath this Impasto stack"""
    bl_idname = "impasto.import_kiln_normal"
    bl_label = "Impasto: Import/Repair Kiln Normal"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        mat, tree = _context_stack(context)
        if mat is None or tree is None or not mat.use_nodes:
            return False
        tex = mat.node_tree.nodes.get("Kiln Bake Target")
        return (tex is not None and tex.bl_idname == 'ShaderNodeTexImage'
                and tex.image is not None)

    def execute(self, context):
        mat, _tree = _context_stack(context)
        tex = mat.node_tree.nodes.get("Kiln Bake Target")
        uv_map = _active_uv_map(context)
        try:
            layer = import_normal_baseline(
                mat, tex.image, uv_map=uv_map,
                fallback_node_name="Kiln Normal Map")
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "%s imported beneath the Impasto stack"
                    % layer.label)
        return {'FINISHED'}


class IMPASTO_OT_paint_activate(bpy.types.Operator):
    """Use one of the active Impasto paint layer's channel canvases as
    Blender's native brush canvas and enter Texture Paint mode"""
    bl_idname = "impasto.paint_activate"
    bl_label = "Impasto: Paint Active Layer"
    bl_options = {'REGISTER'}

    channel_key: StringProperty(
        name="Channel",
        description="Channel canvas to paint natively; empty picks the "
                    "layer's first enabled painted channel",
        default="")

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return layer is not None and layer.layer_type == 'PAINT'

    def execute(self, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer()
        try:
            repaired = paint.activate_paint_target(context, layer,
                                                   self.channel_key)
        except paint.PaintTargetError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        obj = context.object
        try:
            if obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
        except RuntimeError as exc:
            self.report({'ERROR'}, "Could not enter Texture Paint mode: %s" % exc)
            return {'CANCELLED'}
        brush_selected = paint.activate_brush_tool(context)
        paint.maybe_switch_material_preview(context)
        binding = paint.paint_binding(layer, self.channel_key)
        message = "Painting %s" % layer.label
        if binding is not None:
            message += " (%s)" % model.CHANNEL_MAP[binding.name].label
        if repaired:
            message += " (image colorspace repaired)"
        if not brush_selected and not bpy.app.background:
            message += " (Texture Paint active; select Paint tool if hidden)"
        self.report({'INFO'}, message)
        return {'FINISHED'}


class IMPASTO_OT_detail_paint(bpy.types.Operator):
    """Activate a Height Detail canvas and configure an accumulating native
    brush. Raise adds white; Lower subtracts white around neutral gray."""
    bl_idname = "impasto.detail_paint"
    bl_label = "Impasto: Paint Height Detail"
    bl_options = {'REGISTER'}

    direction: EnumProperty(
        items=(('RAISE', "Raise", "Repeated strokes build raised detail"),
               ('LOWER', "Lower", "Repeated strokes build recessed detail")),
        default='RAISE')

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return (layer is not None and layer.layer_type == 'PAINT'
                and any(b.enabled and b.name == 'height'
                        for b in layer.bindings))

    def execute(self, context):
        result = bpy.ops.impasto.paint_activate(channel_key='height')
        if result != {'FINISHED'}:
            return result
        brush = context.scene.tool_settings.image_paint.brush
        if brush is None:
            self.report({'ERROR'}, "No Texture Paint brush is active")
            return {'CANCELLED'}
        brush.blend = 'ADD' if self.direction == 'RAISE' else 'SUB'
        brush.color = (1.0, 1.0, 1.0)
        self.report({'INFO'}, "%s height detail; repeated strokes accumulate"
                    % self.direction.title())
        return {'FINISHED'}


def gpu_paint_targets(layer):
    """Ordered (channel key, Image) pairs one GPU stroke deposits into:
    the layer's enabled SHARED bindings of GPU-paintable channels whose
    canvas exists, in registry order (payloads align by index)."""
    pairs = []
    for ch in model.CHANNELS:
        if ch.key not in gpu_engine.GPU_PAINT_CHANNEL_KEYS:
            continue
        b = layer.bindings.get(ch.key)
        if b is None or not b.enabled or b.mode != 'SHARED':
            continue
        image = bpy.data.images.get(b.image_name or layer.image_name)
        if image is not None:
            pairs.append((ch.key, image))
    return pairs


_BRUSH_CHANNEL_PROPERTIES = {
    'PAINT': "paint_channels",
    'SOFTEN': "soften_channels",
    'SMEAR': "smear_channels",
    'ERASE': "erase_channels",
}


def gpu_brush_target_keys(layer, keys=None):
    """Enabled channel keys selected for the layer's current GPU brush."""
    keys = tuple(keys if keys is not None
                 else (key for key, _image in gpu_paint_targets(layer)))
    values = getattr(layer, _BRUSH_CHANNEL_PROPERTIES[layer.brush_mode])
    return tuple(key for key in keys
                 if values[model.CHANNEL_ORDER[key]])


def _gpu_brush(layer):
    """Snapshot the layer's brush PropertyGroup values into the plain
    dict gpu_engine.stroke_payloads consumes (pure seam)."""
    return {"color": tuple(layer.paint_color),
            "roughness": layer.paint_roughness,
            "metallic": layer.paint_metallic,
            "normal": tuple(layer.paint_normal),
            "height_strength": layer.paint_height_strength,
            "height_direction": layer.paint_height_direction,
            "emission_color": tuple(layer.paint_emission_color),
            "emission_strength": layer.paint_emission_strength,
            "sss_weight": layer.paint_sss_weight,
            "sss_radius": tuple(layer.paint_sss_radius),
            "sss_scale": layer.paint_sss_scale}


RECENT_COLOR_LIMIT = 8


def _remember_color(colors, value):
    """Deduplicate and cap one Blender collection; exposed for tests."""
    value = tuple(value)
    epsilon = 1.0 / 255.0
    duplicate = next((i for i, item in enumerate(colors)
                      if max(abs(float(a) - float(b))
                             for a, b in zip(item.color, value)) <= epsilon),
                     None)
    if duplicate is not None:
        colors.remove(duplicate)
    item = colors.add()
    item.color = value
    while len(colors) > RECENT_COLOR_LIMIT:
        colors.remove(0)
    return len(colors) - 1


def remember_recent_color(layer, channel_key, color=None):
    """Remember a color that was actually used, newest last.

    Near-identical picker results collapse to one entry so small UI drags do
    not fill the history with visual duplicates.
    """
    stack = layer.id_data.impasto
    if channel_key == 'base_color':
        colors = stack.recent_base_colors
        value = tuple(color or layer.paint_color)
    elif channel_key == 'emission_color':
        colors = stack.recent_emission_colors
        value = tuple(color or layer.paint_emission_color)
    else:
        raise ValueError("Recent colors only support color channels")
    return _remember_color(colors, value)


class IMPASTO_OT_recent_color_apply(bpy.types.Operator):
    """Reuse a color previously painted on this layer"""
    bl_idname = "impasto.recent_color_apply"
    bl_label = "Use Recent Color"
    bl_options = {'UNDO', 'INTERNAL'}

    channel_key: EnumProperty(items=(
        ('base_color', "Base Color", "Apply to the Base Color brush value"),
        ('emission_color', "Emission Color",
         "Apply to the Emission Color brush value")))
    index: IntProperty(default=-1)

    @classmethod
    def description(cls, context, properties):
        label = ("Base Color" if properties.channel_key == 'base_color'
                 else "Emission Color")
        return "Reuse this recent color as the %s brush value" % label

    def execute(self, context):
        _mat, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        if layer is None:
            return {'CANCELLED'}
        colors = (tree.impasto.recent_base_colors
                  if self.channel_key == 'base_color'
                  else tree.impasto.recent_emission_colors)
        if not 0 <= self.index < len(colors):
            return {'CANCELLED'}
        value = tuple(colors[self.index].color)
        if self.channel_key == 'base_color':
            layer.paint_color = value
        else:
            layer.paint_emission_color = value
        remember_recent_color(layer, self.channel_key, value)
        return {'FINISHED'}


_MATERIAL_PRESET_FIELDS = (
    ("paint_color", "base_color"),
    ("paint_roughness", "roughness"),
    ("paint_metallic", "metallic"),
    ("paint_normal", "normal"),
    ("paint_height_strength", "height_strength"),
    ("paint_height_direction", "height_direction"),
    ("paint_emission_color", "emission_color"),
    ("paint_emission_strength", "emission_strength"),
    ("paint_sss_weight", "sss_weight"),
    ("paint_sss_radius", "sss_radius"),
    ("paint_sss_scale", "sss_scale"),
)


def _global_material_preset_path():
    directory = bpy.utils.user_resource('CONFIG', path="impasto", create=True)
    return os.path.join(directory, "material_presets.json")


def _preset_dict(preset):
    data = {"label": preset.label}
    for _layer_name, preset_name in _MATERIAL_PRESET_FIELDS:
        value = getattr(preset, preset_name)
        data[preset_name] = (list(value) if not isinstance(value, str)
                             and hasattr(value, "__len__") else value)
    return data


def _read_global_material_presets():
    try:
        with open(_global_material_preset_path(), "r", encoding="utf-8") as stream:
            data = json.load(stream)
        return list(data.get("presets", ()))
    except (OSError, ValueError, TypeError):
        return []


def _write_global_material_preset(preset):
    records = _read_global_material_presets()
    record = _preset_dict(preset)
    records = [item for item in records if item.get("label") != preset.label]
    records.append(record)
    path = _global_material_preset_path()
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump({"schema": 1, "presets": records}, stream,
                  separators=(",", ":"))
    os.replace(temporary, path)


def load_global_material_presets(stack):
    """Seed a new stack from the cross-project preset library."""
    existing = {preset.label for preset in stack.material_presets}
    for record in _read_global_material_presets():
        label = str(record.get("label") or "Material")
        if label in existing:
            continue
        preset = stack.material_presets.add()
        preset.label = label
        for _layer_name, preset_name in _MATERIAL_PRESET_FIELDS:
            if preset_name in record:
                setattr(preset, preset_name, record[preset_name])
        existing.add(label)


def capture_material_preset(preset, layer):
    """Copy every brush-material value into a persistent preset."""
    for layer_name, preset_name in _MATERIAL_PRESET_FIELDS:
        value = getattr(layer, layer_name)
        setattr(preset, preset_name,
                tuple(value) if not isinstance(value, str)
                and hasattr(value, "__len__") else value)


def apply_material_preset(layer, preset):
    """Apply material values without changing channel selection/ownership."""
    for layer_name, preset_name in _MATERIAL_PRESET_FIELDS:
        value = getattr(preset, preset_name)
        setattr(layer, layer_name,
                tuple(value) if not isinstance(value, str)
                and hasattr(value, "__len__") else value)


def material_preset_tooltip(preset):
    rgb = lambda value: ", ".join("%.2g" % x for x in value)
    height = ("+%.3g" if preset.height_direction == 'RAISE' else "-%.3g") \
        % preset.height_strength
    return (
        "%s — Base (%s); Roughness %.3g; Metallic %.3g; Normal (%s); "
        "Height %s; Emission (%s) × %.3g; SSS Weight %.3g, Radius (%s), "
        "Scale %.3g"
        % (preset.label, rgb(preset.base_color), preset.roughness,
           preset.metallic, rgb(preset.normal), height,
           rgb(preset.emission_color), preset.emission_strength,
           preset.sss_weight, rgb(preset.sss_radius), preset.sss_scale))


class IMPASTO_OT_material_preset_capture(bpy.types.Operator):
    """Save the active layer's brush material values as a palette preset"""
    bl_idname = "impasto.material_preset_capture"
    bl_label = "Save Brush Material Preset"
    bl_options = {'UNDO'}

    label: StringProperty(name="Name", default="Material")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        _mat, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        if layer is None or layer.layer_type != 'PAINT':
            return {'CANCELLED'}
        preset = tree.impasto.material_presets.add()
        preset.label = self.label.strip() or "Material"
        capture_material_preset(preset, layer)
        preferences = impasto_preferences(context)
        if preferences is not None and preferences.global_material_presets:
            try:
                _write_global_material_preset(preset)
            except OSError as exc:
                self.report({'WARNING'}, "Saved in blend, not global library: %s"
                            % exc)
        return {'FINISHED'}


class IMPASTO_OT_material_preset_apply(bpy.types.Operator):
    """Apply a saved brush material without changing painted channels"""
    bl_idname = "impasto.material_preset_apply"
    bl_label = "Apply Brush Material Preset"
    bl_options = {'UNDO', 'INTERNAL'}

    index: IntProperty(default=-1)

    @classmethod
    def description(cls, context, properties):
        _mat, tree = _context_stack(context)
        presets = tree.impasto.material_presets if tree else ()
        if 0 <= properties.index < len(presets):
            return material_preset_tooltip(presets[properties.index])
        return "Apply the saved brush material values"

    def execute(self, context):
        _mat, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        presets = tree.impasto.material_presets if tree else ()
        if layer is None or not 0 <= self.index < len(presets):
            return {'CANCELLED'}
        apply_material_preset(layer, presets[self.index])
        return {'FINISHED'}


class IMPASTO_OT_material_preset_remove(bpy.types.Operator):
    """Remove a saved brush material preset"""
    bl_idname = "impasto.material_preset_remove"
    bl_label = "Remove Brush Material Preset"
    bl_options = {'UNDO', 'INTERNAL'}

    index: IntProperty(default=-1)

    def execute(self, context):
        _mat, tree = _context_stack(context)
        presets = tree.impasto.material_presets if tree else ()
        if not 0 <= self.index < len(presets):
            return {'CANCELLED'}
        presets.remove(self.index)
        return {'FINISHED'}


def gpu_preview_mode(layer):
    """Persistent layer preview mode, sanitized for runtime consumers."""
    mode = getattr(layer, "gpu_preview_mode", 'LIT_PBR')
    return mode if mode in props.GPU_PREVIEW_MODE_IDS else 'LIT_PBR'


def gpu_preview_lighting(layer):
    """Persistent display-only controls for the resident PBR preview."""
    return {
        "preview_environment_exposure": layer.preview_environment_exposure,
        "preview_environment_rotation": layer.preview_environment_rotation,
        "preview_key_strength": layer.preview_key_strength,
        "preview_key_rotation": layer.preview_key_rotation,
        "preview_fill_strength": layer.preview_fill_strength,
        "preview_roughness_readability":
            layer.preview_roughness_readability,
    }


def gpu_sss_caliper(layer, scene):
    """Plain runtime settings for the GPU paint cursor overlay."""
    return {
        "sss_caliper_enabled": bool(layer.show_sss_caliper),
        "sss_caliper_scale": float(layer.paint_sss_scale),
        "sss_caliper_radius": tuple(layer.paint_sss_radius),
        "scene_unit_scale": float(scene.unit_settings.scale_length or 1.0),
    }


def gpu_preview_base_normal(layer):
    """Persistent display-only base-normal settings for the live preview."""
    image = getattr(layer, "preview_base_normal_image", None)
    return {
        "base_normal_image_name": getattr(image, "name", "") if image else "",
        "base_normal_uv_map": layer.preview_base_normal_uv_map,
        "base_normal_strength": layer.preview_base_normal_strength,
        "base_normal_invert_green": layer.preview_base_normal_invert_green,
    }


def gpu_stencil_settings(layer):
    """Persistent layer stencil state as an immutable runtime contract."""
    image = getattr(layer, "brush_stencil_image", None)
    projection = getattr(
        layer, "brush_stencil_projection", 'VIEW_STENCIL')
    scale = (getattr(layer, "brush_stencil_brush_scale", (1.0, 1.0))
             if projection == 'BRUSH_ALPHA'
             else getattr(layer, "brush_stencil_scale", (0.35, 0.35)))
    return stencil.normalized(
        enabled=getattr(layer, "brush_stencil_enabled", False),
        image_name=getattr(image, "name", ""),
        projection=projection,
        interpretation=getattr(
            layer, "brush_stencil_interpretation", 'ALPHA'),
        usage=getattr(layer, "brush_stencil_usage", 'COVERAGE'),
        coverage=getattr(layer, "brush_stencil_coverage", True),
        opacity=getattr(layer, "brush_stencil_opacity", 1.0),
        preview_opacity=getattr(
            layer, "brush_stencil_preview_opacity", 0.38),
        position=getattr(layer, "brush_stencil_position", (0.5, 0.5)),
        scale=scale,
        rotation=getattr(layer, "brush_stencil_rotation", 0.0),
        profile_strength=getattr(
            layer, "brush_stencil_profile_strength", 1.0),
        profile_invert=getattr(
            layer, "brush_stencil_profile_invert", False))


def _gpu_stamp(context, radius=None, pressure_opacity=None,
               pressure_size=None):
    settings = context.scene.tool_settings.image_paint
    brush = settings.brush
    if brush is None:
        return None
    tool_id = ""
    try:
        tool = context.workspace.tools.from_space_view3d_mode(
            'PAINT_TEXTURE', create=False)
        tool_id = tool.idname if tool is not None else ""
    except (AttributeError, TypeError):
        pass
    stamp = brush_adapter.brush_to_gpu_stamp(
        brush, paint.unified_paint_settings(context), tool_id)
    # Impasto exposes a persistent Radius beside its GPU button. Preserve the
    # Blender asset's spacing/strength/pressure/falloff semantics, but make
    # that visible control authoritative for GPU dab size.
    if radius is not None and stamp.supported:
        stamp = replace(stamp, radius_px=max(0.5, float(radius)))
    if stamp.supported:
        overrides = {}
        if pressure_opacity is not None:
            overrides["use_pressure_strength"] = bool(pressure_opacity)
        if pressure_size is not None:
            overrides["use_pressure_size"] = bool(pressure_size)
        if overrides:
            stamp = replace(stamp, **overrides)
    return stamp


def native_replay_targets(layer):
    """Ordered native-replay targets for one logical PBR stroke.

    This deliberately shares the same registry/canvas eligibility as the GPU
    path, while remaining a separate execution path.
    """
    return gpu_paint_targets(layer)


def native_channel_style(layer, channel_key):
    """Return ``(brush_color, blend)`` for a replay target (pure seam)."""
    return channel_paint.native_style(channel_key, _gpu_brush(layer))


def replay_native_stroke(context, layer, stroke, area=None, region=None):
    """Replay one captured Blender stroke into every enabled PBR canvas.

    Blender 5.1 exposes ``paint.image_paint(stroke=...)`` but only one canvas
    per invocation.  We therefore keep the user's active Brush asset and its
    stroke mechanics, invoke it once per image, and restore all temporary
    canvas/color/blend changes even if one replay fails.
    """
    targets = native_replay_targets(layer)
    if not targets:
        raise paint.PaintTargetError(
            "The active layer has no native-replay channel canvas")
    settings = context.scene.tool_settings.image_paint
    state = paint.capture_native_state(context)
    results = []
    try:
        paint.configure_front_surface_paint(context)
        brush = settings.brush
        if brush is None:
            raise paint.PaintTargetError(
                "Select a Blender Texture Paint brush asset first")
        for channel_key, image in targets:
            paint.activate_paint_target(context, layer, channel_key)
            color, blend = native_channel_style(layer, channel_key)
            brush.blend = blend
            unified = paint.unified_paint_settings(context)
            paint.configure_native_replay_color(
                brush, unified, color, state.get("unified_use_color"))
            if area is not None and region is not None:
                with context.temp_override(area=area, region=region,
                                           space_data=area.spaces.active):
                    result = bpy.ops.paint.image_paint(
                        stroke=stroke, mode='NORMAL')
            else:
                result = bpy.ops.paint.image_paint(stroke=stroke,
                                                   mode='NORMAL')
            if 'FINISHED' not in result:
                raise paint.PaintTargetError(
                    "Blender declined the %s stroke replay (%s)"
                    % (model.CHANNEL_MAP[channel_key].label, result))
            image.update()
            results.append(channel_key)
    finally:
        paint.restore_native_state(context, state)
    return tuple(results)


class IMPASTO_OT_native_multichannel_paint(bpy.types.Operator):
    """Capture a stroke and replay the active Blender brush asset into every
    enabled image channel of the active Impasto paint layer"""
    bl_idname = "impasto.native_multichannel_paint"
    bl_label = "Impasto: Blender Brush — All Channels"
    bl_options = {'REGISTER', 'UNDO', 'UNDO_GROUPED', 'BLOCKING'}

    @classmethod
    def poll(cls, context):
        if gpu_engine.session_active():
            return False
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return (context.object is not None
                and context.object.type == 'MESH'
                and layer is not None and layer.layer_type == 'PAINT'
                and bool(native_replay_targets(layer)))

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Run from a 3D Viewport")
            return {'CANCELLED'}
        region = next((r for r in context.area.regions
                       if r.type == 'WINDOW'), None)
        if region is None:
            self.report({'ERROR'}, "No drawable viewport region found")
            return {'CANCELLED'}

        _, tree = _context_stack(context)
        self._layer_uid = tree.impasto.active_layer_uid
        self._tree_name = tree.name
        self._area = context.area
        self._region = region
        self._original_mode = context.object.mode
        self._original_state = paint.capture_native_state(context)
        self._stroke = []
        self._stroke_t0 = 0.0
        self._painting = False
        try:
            layer = tree.impasto.active_layer()
            first_key = native_replay_targets(layer)[0][0]
            paint.activate_paint_target(context, layer, first_key)
            if context.object.mode != 'TEXTURE_PAINT':
                if context.object.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
        except (RuntimeError, paint.PaintTargetError) as exc:
            paint.restore_native_state(context, self._original_state)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        if context.scene.tool_settings.image_paint.brush is None:
            paint.restore_native_state(context, self._original_state)
            self.report({'ERROR'}, "Select a Blender Texture Paint brush first")
            return {'CANCELLED'}

        paint.maybe_switch_material_preview(context)
        context.window_manager.modal_handler_add(self)
        region.tag_redraw()
        self.report({'INFO'},
                    "Blender brush multi-channel — LMB stroke, RMB/Esc stops")
        return {'RUNNING_MODAL'}

    def _point(self, context, event, is_start=False):
        brush = context.scene.tool_settings.image_paint.brush
        unified = paint.unified_paint_settings(context)
        size = (unified.size if unified is not None
                and unified.use_unified_size else brush.size)
        pressure = float(getattr(event, "pressure", 1.0))
        if pressure <= 0.0:
            pressure = 1.0
        return paint.native_stroke_point(
            event.mouse_x - self._region.x,
            event.mouse_y - self._region.y,
            pressure, size, time.perf_counter() - self._stroke_t0,
            is_start=is_start,
            x_tilt=getattr(event, "x_tilt", 0.0),
            y_tilt=getattr(event, "y_tilt", 0.0))

    def _replay(self, context):
        tree = bpy.data.node_groups.get(self._tree_name)
        layer = (tree.impasto.layers.get(self._layer_uid)
                 if tree is not None else None)
        if layer is None:
            raise paint.PaintTargetError("The active paint layer disappeared")
        return replay_native_stroke(context, layer, self._stroke,
                                    self._area, self._region)

    def _restore(self, context):
        paint.restore_native_state(context, self._original_state)
        obj = context.object
        if (obj is not None and obj.mode != self._original_mode
                and self._original_mode == 'OBJECT'):
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                pass
        self._region.tag_redraw()

    def modal(self, context, event):
        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS' and self._inside(event):
                self._painting = True
                self._stroke_t0 = time.perf_counter()
                self._stroke = [self._point(context, event, True)]
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and self._painting:
                self._stroke.append(self._point(context, event))
                self._painting = False
                try:
                    replayed = self._replay(context)
                except (RuntimeError, paint.PaintTargetError) as exc:
                    self.report({'ERROR'}, str(exc))
                    self._restore(context)
                    return {'CANCELLED'}
                for area in context.screen.areas:
                    if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                        area.tag_redraw()
                self.report({'INFO'}, "Stroke replayed to %d channels"
                            % len(replayed))
                return {'RUNNING_MODAL'}
        elif event.type == 'MOUSEMOVE' and self._painting:
            self._stroke.append(self._point(context, event))
            return {'RUNNING_MODAL'}
        elif event.type in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            self._restore(context)
            return {'FINISHED'}
        return {'PASS_THROUGH'}

    def _inside(self, event):
        x = event.mouse_x - self._region.x
        y = event.mouse_y - self._region.y
        return 0 <= x < self._region.width and 0 <= y < self._region.height

    def cancel(self, context):
        self._restore(context)


def _stroke_pointer_extras(event):
    """Tablet tilt for GPU stroke recording; missing Event fields stay 0."""
    tilt = getattr(event, "tilt", (0.0, 0.0))
    try:
        x_tilt = float(tilt[0])
        y_tilt = float(tilt[1])
    except (TypeError, ValueError, IndexError):
        x_tilt = y_tilt = 0.0
    return {"x_tilt": x_tilt, "y_tilt": y_tilt}


class IMPASTO_OT_gpu_paint(bpy.types.Operator):
    """Paint every bound channel of the active Impasto paint layer with
    one GPU-resident stroke (LMB paints, RMB or Esc flushes and stops).
    Ctrl-Z / Ctrl-Shift-Z apply atomic GPU tile undo / redo."""
    bl_idname = "impasto.gpu_paint"
    bl_label = "Impasto: GPU Paint All Channels"
    bl_options = {'REGISTER'}

    # The modal loop never touches gpu: it enqueues dabs / tags redraws
    # and writes explicitly flushed readbacks into the Image datablocks. All GPU
    # work happens in gpu_engine's POST_VIEW draw callback.

    @classmethod
    def poll(cls, context):
        if gpu_engine.session_active():
            return False   # one session at a time
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        obj = context.object
        return (obj is not None and obj.type == 'MESH'
                and layer is not None and layer.layer_type == 'PAINT')

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'},
                        "Run from a 3D Viewport (Impasto sidebar)")
            return {'CANCELLED'}
        # The modal loop needs region-relative mouse coordinates; the
        # panel button invokes from the UI region, so find WINDOW.
        region = next((r for r in context.area.regions
                       if r.type == 'WINDOW'), None)
        if region is None:
            self.report({'ERROR'}, "No drawable viewport region found")
            return {'CANCELLED'}

        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer()
        targets = gpu_paint_targets(layer)
        if not targets:
            self.report({'ERROR'},
                        "The active layer has no paintable channel canvas")
            return {'CANCELLED'}
        sizes = {tuple(img.size) for _key, img in targets}
        if len(sizes) != 1 or any(w != h for w, h in sizes):
            self.report({'ERROR'},
                        "Channel canvases must share one square "
                        "resolution; got %s"
                        % sorted("%dx%d" % s for s in sizes))
            return {'CANCELLED'}

        obj = context.object
        if obj.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError as exc:
                self.report({'ERROR'},
                            "Could not enter Object Mode: %s" % exc)
                return {'CANCELLED'}
        if layer.uv_map:
            uv = obj.data.uv_layers.get(layer.uv_map)
            if uv is None:
                self.report({'ERROR'}, "Paint layer UV map %r is missing"
                            % layer.uv_map)
                return {'CANCELLED'}
            obj.data.uv_layers.active = uv

        keys = [key for key, _img in targets]
        images = [img for _key, img in targets]
        payloads = gpu_engine.stroke_payloads(keys, _gpu_brush(layer))
        stamp = _gpu_stamp(
            context, layer.brush_radius,
            layer.brush_pressure_opacity, layer.brush_pressure_size)
        settings = {
            "radius": (stamp.radius_px if stamp is not None
                       and stamp.supported else layer.brush_radius),
            "hardness": layer.brush_hardness,
            "occlusion": True,
            "subrect": True,
            "channel_keys": tuple(keys),
            "brush_stamp": (stamp if stamp is not None
                            and stamp.supported else None),
            "opacity": layer.brush_opacity,
            "brush_mode": layer.brush_mode,
            "brush_target_channel_keys": gpu_brush_target_keys(layer, keys),
            "experimental_uv_gutters": bool(
                layer.experimental_uv_gutters),
            "experimental_conservative_seams": bool(
                layer.experimental_conservative_seams),
            "erase_channel_keys": tuple(
                key for key in keys
                if layer.erase_channels[model.CHANNEL_ORDER[key]]),
            "preview_mode": gpu_preview_mode(layer),
            "stack_model": snapshot.snapshot(
                tree, _context_material(context)),
            "active_layer_uid": layer.name,
            # Kiln bake alpha predates Impasto's paint-coverage contract and
            # is not authoritative. Preserve its RGB when Kiln is selected as
            # the active canvas, as the lower-baseline uploader already does.
            "opaque_channel_keys": (
                ("normal",) if layer.label == "Kiln Baked Normal"
                and "normal" in keys else ()),
        }
        settings.update(gpu_preview_lighting(layer))
        settings.update(gpu_sss_caliper(layer, context.scene))
        settings.update(gpu_preview_base_normal(layer))
        settings.update(gpu_stencil_settings(layer).as_gpu_settings())
        if not gpu_engine.start_session(obj, images, region,
                                        payloads=payloads,
                                        settings=settings):
            self.report({'ERROR'}, "Mesh has no UV map or faces")
            return {'CANCELLED'}
        paint.maybe_switch_material_preview(context)

        self._focused_ui_space = None
        self._focused_ui_state = {}
        if layer.focused_paint_ui:
            from . import focused_ui
            self._focused_ui_space = context.space_data
            self._focused_ui_state = focused_ui.hide(
                self._focused_ui_space)

        self._region = region
        self._area = context.area
        self._tree_name = tree.name
        self._layer_uid = layer.name
        self._channel_keys = tuple(keys)
        self._preview_mode = gpu_preview_mode(layer)
        self._radius = layer.brush_radius
        self._auto_inspect_delay = (layer.auto_material_preview_delay
                                    if layer.auto_material_preview else None)
        self._auto_inspect_deadline = None
        self._pending_save_as = None
        self._stopping = False
        self._stencil_drag = None
        gpu_engine.set_cursor(event.mouse_x - region.x,
                              event.mouse_y - region.y)
        self._timer = context.window_manager.event_timer_add(
            0.05, window=context.window)
        context.window_manager.modal_handler_add(self)
        region.tag_redraw()
        self.report({'INFO'},
                    "GPU painting %s — LMB paints, RMB/Esc flushes and stops"
                    % ", ".join(model.CHANNEL_MAP[k].label for k in keys))
        if stamp is not None and not stamp.supported:
            self.report({'WARNING'},
                        "GPU brush uses the basic Impasto stamp: %s"
                        % stamp.unsupported_reason)
        return {'RUNNING_MODAL'}

    # -- helpers -----------------------------------------------------------

    def _mouse_region(self, event):
        return (event.mouse_x - self._region.x,
                event.mouse_y - self._region.y)

    def _inside_region(self, event):
        rx, ry = self._mouse_region(event)
        return (0 <= rx < self._region.width
                and 0 <= ry < self._region.height)

    def _over_interface_region(self, event):
        """Whether a window-space event belongs to an overlapping Blender UI.

        The N-panel may overlap the VIEW_3D WINDOW region instead of reducing
        its rectangle, so ``_inside_region`` alone cannot distinguish painting
        from editing a sidebar property.
        """
        area = getattr(self, "_area", None)
        if area is None:
            return False
        for region in area.regions:
            if region.type == 'WINDOW' or region.width <= 1 or region.height <= 1:
                continue
            if (region.x <= event.mouse_x < region.x + region.width
                    and region.y <= event.mouse_y < region.y + region.height):
                return True
        return False

    def _refresh_stroke_settings(self, context):
        tree = bpy.data.node_groups.get(self._tree_name)
        layer = (tree.impasto.layers.get(self._layer_uid)
                 if tree is not None else None)
        if layer is None:
            raise paint.PaintTargetError("The active paint layer disappeared")
        payloads = gpu_engine.stroke_payloads(
            self._channel_keys, _gpu_brush(layer))
        stamp = _gpu_stamp(
            context, layer.brush_radius,
            layer.brush_pressure_opacity, layer.brush_pressure_size)
        supported_stamp = (stamp if stamp is not None
                           and stamp.supported else None)
        self._radius = (supported_stamp.radius_px if supported_stamp
                        else layer.brush_radius)
        changed = gpu_engine.update_stroke_settings(
            payloads, radius=self._radius,
            hardness=layer.brush_hardness, opacity=layer.brush_opacity,
            brush_mode=layer.brush_mode,
            brush_target_channel_keys=gpu_brush_target_keys(
                layer, self._channel_keys),
            erase_channel_keys=tuple(
                key for key in self._channel_keys
                if layer.erase_channels[model.CHANNEL_ORDER[key]]),
            stamp=supported_stamp,
            stencil_settings=gpu_stencil_settings(layer).as_gpu_settings(),
            caliper_settings=gpu_sss_caliper(layer, context.scene))
        if changed:
            self._region.tag_redraw()
        return changed

    def _refresh_preview_mode(self):
        """Apply a sidebar preview change without restarting the session."""
        tree = bpy.data.node_groups.get(self._tree_name)
        layer = (tree.impasto.layers.get(self._layer_uid)
                 if tree is not None else None)
        if layer is None:
            return False
        mode = gpu_preview_mode(layer)
        if mode == self._preview_mode:
            return False
        if not gpu_engine.set_preview_mode(mode):
            return False
        self._preview_mode = mode
        self._region.tag_redraw()
        return True

    def _refresh_stencil_settings(self):
        """Keep the resident stencil mask and placement overlay live."""
        tree = bpy.data.node_groups.get(self._tree_name)
        layer = (tree.impasto.layers.get(self._layer_uid)
                 if tree is not None else None)
        if layer is None:
            return False
        changed = gpu_engine.update_stencil_settings(
            gpu_stencil_settings(layer).as_gpu_settings())
        if changed:
            self._region.tag_redraw()
        return changed

    def _stencil_layer(self):
        tree = bpy.data.node_groups.get(self._tree_name)
        if tree is None:
            return None
        return tree.impasto.layers.get(self._layer_uid)

    def _consume_stencil_transform(self, context, event):
        """Scale/rotate a Planar Viewport stencil instead of painting."""
        etype = event.type
        drag = getattr(self, "_stencil_drag", None)
        if drag is not None:
            if etype == 'MOUSEMOVE' or (
                    etype == 'LEFTMOUSE' and event.value == 'PRESS'):
                self._apply_stencil_drag(event)
                return True
            if etype == 'LEFTMOUSE' and event.value == 'RELEASE':
                self._apply_stencil_drag(event)
                self._stencil_drag = None
                self._region.tag_redraw()
                return True
            if etype in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
                self._revert_stencil_drag()
                self._stencil_drag = None
                self._region.tag_redraw()
                return True
            return True
        if etype != 'LEFTMOUSE' or event.value != 'PRESS':
            return False
        if gpu_engine.stroke_active() \
                or gpu_engine.material_inspect_active() \
                or gpu_engine.material_inspect_requested():
            return False
        if self._over_interface_region(event):
            return False
        layer = self._stencil_layer()
        if layer is None:
            return False
        settings = gpu_stencil_settings(layer)
        if settings.projection != 'VIEW_STENCIL' or not settings.active:
            return False
        rx, ry = self._mouse_region(event)
        handle = stencil.hit_planar_handle(
            (rx, ry), (self._region.width, self._region.height), settings)
        if handle is None:
            return False
        self._stencil_drag = {
            "handle": handle,
            "start": settings,
            "preserve": bool(event.shift),
        }
        self._apply_stencil_drag(event)
        return True

    def _apply_stencil_drag(self, event):
        drag = self._stencil_drag
        layer = self._stencil_layer()
        if drag is None or layer is None:
            return
        rx, ry = self._mouse_region(event)
        gpu_engine.set_cursor(rx, ry)
        scale, rotation = stencil.apply_planar_handle_drag(
            drag["handle"], (rx, ry),
            (self._region.width, self._region.height),
            drag["start"], preserve_aspect=drag["preserve"] or event.shift)
        layer.brush_stencil_scale = scale
        layer.brush_stencil_rotation = rotation
        self._refresh_stencil_settings()

    def _revert_stencil_drag(self):
        drag = self._stencil_drag
        layer = self._stencil_layer()
        if drag is None or layer is None:
            return
        start = drag["start"]
        layer.brush_stencil_scale = start.scale
        layer.brush_stencil_rotation = start.rotation
        self._refresh_stencil_settings()

    def _reset_stencil_transform(self, event):
        """R restores Planar Viewport position, scale, and rotation."""
        if event.type != 'R' or event.value != 'PRESS' or event.ctrl \
                or event.alt or event.shift:
            return False
        if gpu_engine.input_paused() or gpu_engine.stroke_active() \
                or getattr(self, "_stencil_drag", None) is not None:
            return False
        if gpu_engine.material_inspect_active() \
                or gpu_engine.material_inspect_requested():
            return False
        layer = self._stencil_layer()
        if layer is None:
            return False
        settings = gpu_stencil_settings(layer)
        if not settings.active or settings.projection != 'VIEW_STENCIL':
            return False
        position, scale, rotation = stencil.default_planar_transform()
        layer.brush_stencil_position = position
        layer.brush_stencil_scale = scale
        layer.brush_stencil_rotation = rotation
        self._refresh_stencil_settings()
        self.report({'INFO'}, "Stencil placement reset")
        return True

    def _refresh_preview_lighting(self):
        """Apply sidebar lighting edits without restarting or synchronizing."""
        tree = bpy.data.node_groups.get(self._tree_name)
        layer = (tree.impasto.layers.get(self._layer_uid)
                 if tree is not None else None)
        if layer is None:
            return False
        changed = gpu_engine.set_preview_lighting(gpu_preview_lighting(layer))
        if changed:
            self._region.tag_redraw()
        return changed

    def _refresh_preview_base_normal(self):
        """Apply base-normal preview edits without restart or image readback."""
        tree = bpy.data.node_groups.get(self._tree_name)
        layer = (tree.impasto.layers.get(self._layer_uid)
                 if tree is not None else None)
        if layer is None:
            return False
        changed = gpu_engine.set_preview_base_normal(
            gpu_preview_base_normal(layer))
        if changed:
            self._region.tag_redraw()
        return changed

    def _refresh_sss_caliper(self, context):
        """Apply SSS caliper toggles/values without restarting painting."""
        tree = bpy.data.node_groups.get(self._tree_name)
        layer = (tree.impasto.layers.get(self._layer_uid)
                 if tree is not None else None)
        if layer is None:
            return False
        changed = gpu_engine.set_sss_caliper(
            gpu_sss_caliper(layer, context.scene))
        if changed:
            self._region.tag_redraw()
        return changed

    def _apply_pending_sync(self):
        """Write an explicitly flushed readback into the channel Image
        datablocks — always here, never in a draw callback."""
        pending = gpu_engine.take_pending_pixels()
        if pending is None:
            return
        write_ms = 0.0
        for arr, image_name in pending:
            image = bpy.data.images.get(image_name)
            if image is None:
                continue
            t0 = time.perf_counter()
            paint.write_flushed_image_pixels(image, arr)
            write_ms += (time.perf_counter() - t0) * 1000.0
        gpu_engine.record_sync_stats(write_ms, 0.0)
        gpu_engine.complete_material_inspect()
        # Repaint the composed material and any Image editors.
        for area in bpy.context.screen.areas:
            if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                area.tag_redraw()
        return True

    def _perform_deferred_save(self):
        """Save only after resident textures reached Blender Images."""
        save_as = self._pending_save_as
        self._pending_save_as = None
        if save_as or not bpy.data.filepath:
            bpy.ops.wm.save_as_mainfile('INVOKE_DEFAULT')
        else:
            bpy.ops.wm.save_as_mainfile(
                filepath=bpy.data.filepath, check_existing=False)

    def _request_save_boundary(self, save_as=False):
        """Queue a draw-context flush; the modal saves when it completes."""
        if gpu_engine.stroke_active():
            gpu_engine.end_stroke()
        if not gpu_engine.has_unflushed_changes() and not gpu_engine.busy():
            return False
        self._pending_save_as = bool(save_as)
        gpu_engine.request_flush()
        self._region.tag_redraw()
        self.report({'INFO'}, "Synchronizing resident GPU paint before save")
        return True

    def _finish(self, context):
        finish_t0 = time.perf_counter()
        stats = gpu_engine.stop_session(log_summary=False)
        self._restore_focused_ui()
        timer_t0 = time.perf_counter()
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        stats["timer_remove_ms"] = ((time.perf_counter() - timer_t0)
                                    * 1000.0)
        redraw_t0 = time.perf_counter()
        if self._region is not None:
            self._region.tag_redraw()
        stats["redraw_ms"] = (time.perf_counter() - redraw_t0) * 1000.0
        stats["operator_finish_ms"] = ((time.perf_counter() - finish_t0)
                                       * 1000.0)
        stats["total_ms"] = stats["operator_finish_ms"]
        if stats.get("had_session", False):
            gpu_engine.log_stop_telemetry(stats)
        return {'FINISHED'}

    def _restore_focused_ui(self):
        from . import focused_ui
        focused_ui.restore(getattr(self, "_focused_ui_space", None),
                           getattr(self, "_focused_ui_state", None))
        self._focused_ui_space = None
        self._focused_ui_state = {}

    # -- modal loop ----------------------------------------------------------

    def modal(self, context, event):
        if not gpu_engine.session_active():
            return self._finish(context)

        self._apply_pending_sync()

        if (getattr(self, "_pending_save_as", None) is not None
                and not gpu_engine.busy()):
            if gpu_engine.has_unflushed_changes():
                gpu_engine.request_flush()
                self._region.tag_redraw()
            else:
                self._perform_deferred_save()

        if self._stopping:
            if not gpu_engine.busy():
                return self._finish(context)
            self._region.tag_redraw()   # keep draw callbacks pumping
            return {'RUNNING_MODAL'}

        if gpu_engine.last_error() is not None:
            # Latched GPU failure: stop cleanly; the console holds the
            # traceback. Native painting stays fully available.
            self.report({'WARNING'},
                        "Impasto GPU paint failed — see console; "
                        "native painting is unaffected")
            return self._finish(context)

        etype = event.type
        if etype == 'S' and event.value == 'PRESS' and event.ctrl:
            if self._request_save_boundary(save_as=event.shift):
                return {'RUNNING_MODAL'}
            # No resident changes: let Blender's normal Save/Save As shortcut
            # run without ending the GPU session.
            return {'PASS_THROUGH'}
        if etype == 'V' and event.value == 'PRESS':
            if (gpu_engine.material_inspect_active()
                    or gpu_engine.material_inspect_requested()):
                gpu_engine.leave_material_inspect()
                self.report({'INFO'}, "GPU paint preview resumed")
            else:
                if gpu_engine.stroke_active():
                    gpu_engine.end_stroke()
                gpu_engine.request_material_inspect()
                self.report({'INFO'}, "Syncing Blender material preview")
            self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        if self._consume_stencil_transform(context, event):
            return {'RUNNING_MODAL'}
        if self._reset_stencil_transform(event):
            return {'RUNNING_MODAL'}
        if etype == 'P' and event.value == 'PRESS':
            if (gpu_engine.material_inspect_active()
                    or gpu_engine.material_inspect_requested()):
                self.report({'INFO'}, "Press V to return to GPU painting")
                return {'RUNNING_MODAL'}
            if gpu_engine.stroke_active():
                gpu_engine.end_stroke()
            paused = not gpu_engine.input_paused()
            gpu_engine.set_input_paused(paused)
            self._region.tag_redraw()
            self.report({'INFO'}, "GPU paint input %s — resident data preserved"
                        % ("paused for settings" if paused else "resumed"))
            return {'RUNNING_MODAL'}
        if (gpu_engine.input_paused() and etype != 'TIMER'
                and not gpu_engine.material_inspect_active()
                and not gpu_engine.material_inspect_requested()):
            # A deliberate, backend-independent editing state: no pointer event
            # can become a dab, while all Blender UI continues receiving it.
            return {'PASS_THROUGH'}
        if etype == 'LEFTMOUSE':
            if event.value == 'PRESS' and self._over_interface_region(event):
                return {'PASS_THROUGH'}
            if event.value == 'PRESS' and self._inside_region(event):
                self._auto_inspect_deadline = None
                if (gpu_engine.material_inspect_active()
                        or gpu_engine.material_inspect_requested()):
                    gpu_engine.leave_material_inspect()
                try:
                    self._refresh_stroke_settings(context)
                except (ValueError, paint.PaintTargetError) as exc:
                    self.report({'ERROR'}, str(exc))
                    return self._finish(context)
                tree = bpy.data.node_groups.get(self._tree_name)
                layer = (tree.impasto.layers.get(self._layer_uid)
                         if tree is not None else None)
                if layer is not None and layer.brush_mode == 'PAINT':
                    for key in ('base_color', 'emission_color'):
                        if key in self._channel_keys:
                            remember_recent_color(layer, key)
                rx, ry = self._mouse_region(event)
                gpu_engine.begin_stroke(rx, ry, event.pressure,
                                        _stroke_pointer_extras(event))
                self._region.tag_redraw()
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and gpu_engine.stroke_active():
                gpu_engine.end_stroke()
                if self._auto_inspect_delay is not None:
                    self._auto_inspect_deadline = (
                        time.monotonic() + self._auto_inspect_delay)
                self._region.tag_redraw()
                return {'RUNNING_MODAL'}
        elif etype == 'MOUSEMOVE':
            rx, ry = self._mouse_region(event)
            gpu_engine.set_cursor(rx, ry)
            if gpu_engine.stroke_active():
                gpu_engine.move_stroke(rx, ry, event.pressure, self._radius,
                                       _stroke_pointer_extras(event))
                self._region.tag_redraw()
                return {'RUNNING_MODAL'}
            self._region.tag_redraw()
            return {'PASS_THROUGH'}
        elif etype in {'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            if gpu_engine.stroke_active():
                gpu_engine.end_stroke()
            gpu_engine.request_flush()
            self._stopping = True
            self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        elif etype == 'Z' and event.value == 'PRESS' and event.ctrl:
            action = 'REDO' if event.shift else 'UNDO'
            if gpu_engine.request_history_action(action):
                self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        elif etype == 'Y' and event.value == 'PRESS' and event.ctrl:
            if gpu_engine.request_history_action('REDO'):
                self._region.tag_redraw()
            return {'RUNNING_MODAL'}
        elif etype == 'TIMER':
            if not gpu_engine.stroke_active():
                self._refresh_stroke_settings(context)
            self._refresh_preview_mode()
            self._refresh_preview_lighting()
            self._refresh_preview_base_normal()
            self._refresh_sss_caliper(context)
            if (self._auto_inspect_deadline is not None
                    and time.monotonic() >= self._auto_inspect_deadline
                    and not gpu_engine.busy()
                    and not gpu_engine.stroke_active()):
                self._auto_inspect_deadline = None
                gpu_engine.request_material_inspect()
                self._region.tag_redraw()
            return {'RUNNING_MODAL'}

        # Orbit/zoom/pan and UI clicks pass through; the depth prepass
        # re-renders itself at the next draw after any view change.
        return {'PASS_THROUGH'}

    def cancel(self, context):
        # Cancellation is an emergency lifecycle path (window loss/reload),
        # where no owning viewport draw is guaranteed. Normal RMB/Esc exit
        # requests and completes a flush before resources are released.
        gpu_engine.stop_session()
        self._restore_focused_ui()
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None


class IMPASTO_OT_gpu_flush(bpy.types.Operator):
    """Synchronize the active GPU-resident canvases to Blender Images"""
    bl_idname = "impasto.gpu_flush"
    bl_label = "Impasto: Flush GPU Paint to Images"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (gpu_engine.session_active()
                and not gpu_engine.stroke_active())

    def execute(self, context):
        if not gpu_engine.request_flush():
            return {'CANCELLED'}
        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                area.tag_redraw()
        self.report({'INFO'}, "GPU paint flush queued")
        return {'FINISHED'}


class IMPASTO_OT_gpu_material_inspect_toggle(bpy.types.Operator):
    """Inspect Blender's synchronized material, or resume resident preview"""
    bl_idname = "impasto.gpu_material_inspect_toggle"
    bl_label = "Impasto: Toggle Blender Material Inspection"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (gpu_engine.session_active()
                and not gpu_engine.stroke_active())

    def execute(self, context):
        if (gpu_engine.material_inspect_active()
                or gpu_engine.material_inspect_requested()):
            if gpu_engine.leave_material_inspect():
                message = "Resident GPU preview resumed"
            else:
                message = ("Blender material inspection retained: this "
                           "stack cannot be represented safely by resident "
                           "preview")
        elif not gpu_engine.request_material_inspect():
            return {'CANCELLED'}
        else:
            message = "Blender material inspection requested"
        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                area.tag_redraw()
        self.report({'INFO'}, message)
        return {'FINISHED'}


class IMPASTO_OT_brush_mode_set(bpy.types.Operator):
    """Select an Impasto GPU brush mode"""
    bl_idname = "impasto.brush_mode_set"
    bl_label = "Impasto: Set Brush Mode"
    bl_options = {'INTERNAL'}

    mode: EnumProperty(items=(
        ('PAINT', "Paint", "Paint configured values into enabled channels"),
        ('SOFTEN', "Soften", "Diffuse detail across selected channels"),
        ('SMEAR', "Smear", "Transport active-layer pixels along the stroke"),
        ('ERASE', "Erase", "Remove active-layer coverage"),
    ))

    @classmethod
    def description(cls, context, properties):
        return {
            'PAINT': "Paint configured values into every enabled channel",
            'SOFTEN': "Soften detail in selected channels; pressure can "
                      "control strength",
            'SMEAR': "Smear active-layer detail along the stroke direction; "
                     "pressure can control strength",
            'ERASE': "Erase active-layer coverage to reveal the layers below",
        }.get(properties.mode, "Select the GPU brush mode")

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return layer is not None and layer.layer_type == 'PAINT'

    def execute(self, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        if layer is None:
            return {'CANCELLED'}
        layer.brush_mode = self.mode
        return {'FINISHED'}


class IMPASTO_OT_erase_channels_set(bpy.types.Operator):
    """Select or clear every currently enabled Erase channel"""
    bl_idname = "impasto.erase_channels_set"
    bl_label = "Impasto: Set Erase Channels"
    bl_options = {'INTERNAL', 'UNDO'}

    selected: BoolProperty(default=True)

    @classmethod
    def description(cls, context, properties):
        return ("Select every enabled channel for erasing" if
                properties.selected else
                "Clear every enabled channel from the eraser")

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return layer is not None and layer.layer_type == 'PAINT'

    def execute(self, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        if layer is None:
            return {'CANCELLED'}
        for key, _image in gpu_paint_targets(layer):
            layer.erase_channels[model.CHANNEL_ORDER[key]] = self.selected
        return {'FINISHED'}


class IMPASTO_OT_brush_channels_set(bpy.types.Operator):
    """Select or clear every enabled channel for one GPU brush mode"""
    bl_idname = "impasto.brush_channels_set"
    bl_label = "Impasto: Set Brush Channels"
    bl_options = {'INTERNAL', 'UNDO'}

    mode: EnumProperty(items=(
        ('PAINT', "Paint", ""),
        ('SOFTEN', "Soften", ""),
        ('SMEAR', "Smear", ""),
        ('ERASE', "Erase", ""),
    ))
    selected: BoolProperty(default=True)

    @classmethod
    def description(cls, context, properties):
        action = "Select every enabled channel for" if properties.selected \
            else "Clear every enabled channel from"
        return "%s the %s brush" % (action, properties.mode.title())

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        return layer is not None and layer.layer_type == 'PAINT'

    def execute(self, context):
        _, tree = _context_stack(context)
        layer = tree.impasto.active_layer() if tree else None
        if layer is None:
            return {'CANCELLED'}
        values = getattr(layer, _BRUSH_CHANNEL_PROPERTIES[self.mode])
        for key, _image in gpu_paint_targets(layer):
            values[model.CHANNEL_ORDER[key]] = self.selected
        return {'FINISHED'}


class IMPASTO_OT_flatten_export(bpy.types.Operator):
    """Composite the visible stack into new, packed channel images; source
    layers are preserved unchanged"""
    bl_idname = "impasto.flatten_export"
    bl_label = "Impasto: Flatten to Channel Images"
    bl_options = {'REGISTER', 'UNDO'}

    resolution: EnumProperty(name="Output Resolution", items=(
        ('1024', "1K", "1024 x 1024 pixels"),
        ('2048', "2K", "2048 x 2048 pixels"),
        ('4096', "4K", "4096 x 4096 pixels"),
    ), default='2048')
    pack_images: BoolProperty(
        name="Pack Images in .blend", default=True,
        description="Pack generated images into the blend file; no files are written")
    prepare_gltf: BoolProperty(
        name="Prepare glTF Material", default=True,
        description="Save PNGs and create a simple Principled material that glTF can export")
    output_directory: StringProperty(
        name="Texture Directory", default="//textures",
        subtype='DIR_PATH', options={'PATH_SUPPORTS_BLEND_RELATIVE'},
        description="Directory for flattened PNG textures")
    assign_material: BoolProperty(
        name="Assign to Active Object", default=True,
        description="Assign the generated glTF material; the editable Impasto material remains intact")

    @classmethod
    def poll(cls, context):
        _, tree = _context_stack(context)
        return tree is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        mat, tree = _context_stack(context)
        if gpu_engine.has_unflushed_changes():
            self.report({'ERROR'}, "Flush GPU paint before flattening")
            return {'CANCELLED'}
        size = int(self.resolution)
        try:
            images = flatten_export.flatten_stack(
                tree, mat.name, size, size, self.pack_images)
            if self.prepare_gltf:
                by_channel = flatten_export.channel_images(
                    tree, mat.name, images)
                flatten_export.save_channel_images(
                    by_channel, self.output_directory)
                export_material = flatten_export.build_gltf_material(
                    mat, by_channel)
                if self.assign_material:
                    obj = context.object
                    obj.material_slots[obj.active_material_index].material = export_material
        except (RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        message = "Created %d non-destructive %s channel images" % (
            len(images), self.resolution)
        if self.prepare_gltf:
            message += " and glTF material"
        self.report({'INFO'}, message)
        return {'FINISHED'}


_classes = (
    IMPASTO_OT_stack_init,
    IMPASTO_OT_stack_remove,
    IMPASTO_OT_layer_add,
    IMPASTO_OT_layer_remove,
    IMPASTO_OT_layer_move,
    IMPASTO_OT_channel_add,
    IMPASTO_OT_binding_add,
    IMPASTO_OT_binding_remove,
    IMPASTO_OT_mask_add,
    IMPASTO_OT_mask_remove,
    IMPASTO_OT_mask_select,
    IMPASTO_OT_mask_paint,
    IMPASTO_OT_stencil_image_open,
    IMPASTO_OT_stack_rebuild,
    IMPASTO_OT_import_kiln_normal,
    IMPASTO_OT_paint_activate,
    IMPASTO_OT_detail_paint,
    IMPASTO_OT_recent_color_apply,
    IMPASTO_OT_material_preset_capture,
    IMPASTO_OT_material_preset_apply,
    IMPASTO_OT_material_preset_remove,
    IMPASTO_OT_native_multichannel_paint,
    IMPASTO_OT_gpu_paint,
    IMPASTO_OT_gpu_flush,
    IMPASTO_OT_gpu_material_inspect_toggle,
    IMPASTO_OT_brush_mode_set,
    IMPASTO_OT_erase_channels_set,
    IMPASTO_OT_brush_channels_set,
    IMPASTO_OT_flatten_export,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    # A modal GPU session owns viewport draw handlers and GPU resources.
    # Tear it down before unregistering its operator classes so an add-on
    # reload cannot leave a stale, invisible session behind.
    gpu_engine.stop_session()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
