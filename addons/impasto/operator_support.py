# SPDX-License-Identifier: GPL-2.0-or-later
"""Shared mechanics for Impasto operators.

This module deliberately contains no Blender operator classes.  ``ops`` keeps
the public registration/API surface while these reusable stack, image, and
channel operations live in one focused implementation module.
"""

import json

import bpy

from . import compat
from . import engine
from . import model

DEFAULT_IMAGE_SIZE = 2048


def context_material(context):
    obj = context.object
    if obj is None:
        return None
    return obj.active_material


def context_stack(context):
    """Return ``(material, root stack tree)`` or a pair of ``None`` values."""
    mat = context_material(context)
    if mat is None:
        return None, None
    return mat, engine.find_stack_for_material(mat)


def unique_uid(state):
    existing = ({ly.name for ly in state.layers}
                | {mask.name for ly in state.layers for mask in ly.masks}
                | {asset.name for asset in state.mask_assets})
    return model.new_uid(existing)


def active_uv_map(context):
    obj = context.object
    if obj is not None and obj.type == 'MESH':
        uvs = obj.data.uv_layers
        if uvs and uvs.active:
            return uvs.active.name
    return ""


def new_layer_image(name, colorspace, size=DEFAULT_IMAGE_SIZE,
                    generated_color=(0.0, 0.0, 0.0, 0.0)):
    """Create a protected, channel-colorspace-aware layer image."""
    image = bpy.data.images.new(name, size, size, alpha=True)
    image.generated_color = generated_color
    image.use_fake_user = True
    compat.set_image_colorspace(image, colorspace)
    return image


def channel_canvas_seed(channel):
    return ((0.5, 0.5, 0.5, 1.0) if channel.key == "height"
            else (0.0, 0.0, 0.0, 0.0))


def layer_canvas_size(layer):
    for binding in layer.bindings:
        image = bpy.data.images.get(binding.image_name or layer.image_name)
        if image is not None and image.size[0] > 0:
            return image.size[0]
    image = bpy.data.images.get(layer.image_name)
    if image is not None and image.size[0] > 0:
        return image.size[0]
    return DEFAULT_IMAGE_SIZE


def stack_canvas_size(state):
    """Resolve the stack-wide non-destructive canvas creation size."""
    if state is not None:
        return int(state.default_canvas_size)
    return DEFAULT_IMAGE_SIZE


def ensure_stack_channel(state, channel_key):
    """Idempotently register one model channel in registry order."""
    channel = model.CHANNEL_MAP.get(channel_key)
    if channel is None:
        raise ValueError("unknown Impasto channel %r" % channel_key)
    existing = state.channels.get(channel_key)
    if existing is not None:
        return existing, False
    item = state.channels.add()
    item.name = channel_key
    source_index = len(state.channels) - 1
    target_index = sum(
        1 for other in state.channels
        if other.name != channel_key
        and model.CHANNEL_ORDER.get(other.name, 10 ** 6)
        < model.CHANNEL_ORDER[channel_key])
    if source_index != target_index:
        state.channels.move(source_index, target_index)
    return state.channels.get(channel_key), True


def ensure_layer_binding(layer, channel_key):
    """Idempotently bind a registered channel to one non-group layer."""
    channel = model.CHANNEL_MAP.get(channel_key)
    if channel is None:
        raise ValueError("unknown Impasto channel %r" % channel_key)
    if layer is None or layer.layer_type == 'GROUP':
        raise ValueError("a non-group layer must be selected")
    binding = layer.bindings.get(channel_key)
    if binding is not None:
        binding.enabled = True
        return binding, False
    binding = layer.bindings.add()
    binding.name = channel_key
    if layer.layer_type == 'PAINT':
        binding.mode = 'SHARED'
        image = new_layer_image(
            "Impasto %s %s %s" % (layer.label, channel.label, layer.name),
            channel.colorspace, size=layer_canvas_size(layer),
            generated_color=channel_canvas_seed(channel))
        binding.image_name = image.name
    elif channel.kind == 'COLOR':
        binding.mode = 'COLOR'
        binding.color = model.seed_rgba(channel)
    else:
        binding.mode = 'VALUE'
        binding.value = float(channel.default_value[0])
    return binding, True


def remember_displaced_channel_link(mat, channel_key):
    """Preserve a pre-stack Principled link for Remove Stack restoration."""
    if mat is None or mat.node_tree is None:
        return
    channel = model.CHANNEL_MAP[channel_key]
    socket_name = "Normal" if channel_key == "height" else channel.socket
    if not socket_name:
        return
    principled = compat.find_principled(mat.node_tree)
    socket = (compat.find_socket(principled.inputs, socket_name)
              if principled is not None else None)
    if socket is None:
        return
    try:
        displaced = json.loads(mat.impasto_mat.displaced_links or "[]")
    except Exception:
        displaced = []
    if any(entry.get("to_socket") == socket_name for entry in displaced):
        return
    generated = mat.node_tree.nodes.get(model.n_material_stack())
    for link in tuple(socket.links):
        if generated is not None and link.from_node == generated:
            continue
        displaced.append({"from_node": link.from_node.name,
                          "from_socket": link.from_socket.identifier,
                          "to_socket": socket_name})
    mat.impasto_mat.displaced_links = json.dumps(displaced)
