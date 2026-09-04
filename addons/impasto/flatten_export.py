# SPDX-License-Identifier: GPL-2.0-or-later
"""Non-destructive, image-datablock export of an Impasto stack."""

import os
import re

import bpy

from . import compat, model, snapshot


def _np():
    try:
        import numpy
        return numpy
    except ImportError as exc:
        raise RuntimeError("Flatten requires Blender's NumPy module") from exc


def _pixels(image):
    np = _np()
    width, height = image.size[:]
    data = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(data)
    return data.reshape((height, width, 4))


def _resize(data, width, height):
    """Bilinear resize in the image's data domain (linear or Non-Color)."""
    np = _np()
    sh, sw = data.shape[:2]
    if (sw, sh) == (width, height):
        return data
    x = np.linspace(0, sw - 1, width, dtype=np.float32)
    y = np.linspace(0, sh - 1, height, dtype=np.float32)
    x0 = np.floor(x).astype(np.int32); x1 = np.minimum(x0 + 1, sw - 1)
    y0 = np.floor(y).astype(np.int32); y1 = np.minimum(y0 + 1, sh - 1)
    wx = (x - x0)[None, :, None]
    wy = (y - y0)[:, None, None]
    top = data[y0[:, None], x0[None, :]] * (1.0 - wx) \
        + data[y0[:, None], x1[None, :]] * wx
    bottom = data[y1[:, None], x0[None, :]] * (1.0 - wx) \
        + data[y1[:, None], x1[None, :]] * wx
    return top * (1.0 - wy) + bottom * wy


def _blend(a, b, mode):
    np = _np()
    if mode == 'MULTIPLY': return a * b
    if mode == 'SCREEN': return 1.0 - (1.0 - a) * (1.0 - b)
    if mode == 'ADD': return a + b
    if mode == 'SUBTRACT': return a - b
    if mode == 'OVERLAY':
        return np.where(a <= 0.5, 2.0 * a * b,
                        1.0 - 2.0 * (1.0 - a) * (1.0 - b))
    return b


def _luminance(rgba):
    return (rgba[..., 0] * 0.2126 + rgba[..., 1] * 0.7152
            + rgba[..., 2] * 0.0722)


def _normalize_normal(vector):
    """Normalize a tangent-space vector array, safely falling back to +Z."""
    np = _np()
    length = np.linalg.norm(vector, axis=-1, keepdims=True)
    fallback = np.zeros_like(vector)
    fallback[..., 2] = 1.0
    return np.where(length > 1e-8, vector / np.maximum(length, 1e-8),
                    fallback)


def _decode_normal(rgb):
    return _normalize_normal(rgb[..., :3] * 2.0 - 1.0)


def _encode_normal(vector):
    np = _np()
    return np.clip(_normalize_normal(vector) * 0.5 + 0.5, 0.0, 1.0)


def _rnm(base, detail):
    """Reoriented Normal Mapping, with ``base`` below ``detail``.

    Inputs and output are normalized tangent-space vectors.  This is the
    shortest form from Hill/Barré-Brisebois, preserving the base orientation
    while reorienting the upper layer's detail around it.
    """
    np = _np()
    t = base + np.array((0.0, 0.0, 1.0), dtype=np.float32)
    u = detail * np.array((-1.0, -1.0, 1.0), dtype=np.float32)
    combined = (t * np.sum(t * u, axis=-1, keepdims=True)
                / np.maximum(t[..., 2:3], 1e-8) - u)
    return _normalize_normal(combined)


def _image(name):
    image = bpy.data.images.get(name)
    if image is None:
        raise RuntimeError("Missing source image: %s" % name)
    if image.source == 'TILED':
        raise RuntimeError("UDIM sources are not supported by Flatten yet")
    return image


def validate_export(stack_model):
    uv_maps = {layer.uv_map for layer in stack_model.layers
               if layer.layer_type == 'PAINT' and layer.uv_map}
    uv_maps.update(mask.uv_map for layer in stack_model.layers
                   for mask in layer.masks if mask.visible and mask.uv_map)
    if len(uv_maps) > 1:
        raise RuntimeError("Flatten requires one shared UV map; found: %s"
                           % ", ".join(sorted(uv_maps)))


def composite_channel(stack_model, channel_key, width, height):
    """Evaluate one compiled channel chain into a straight, opaque RGBA array."""
    np = _np()
    ch = model.CHANNEL_MAP[channel_key]
    seed = model.seed_rgba(ch)
    result = np.empty((height, width, 4), dtype=np.float32)
    result[:] = seed
    for layer in reversed(stack_model.layers):
        binding = next((b for b in layer.bindings
                        if b.key == channel_key), None)
        if binding is None or layer.layer_type == 'GROUP':
            continue
        factor = model.const_factor(stack_model, layer, binding)
        if factor <= 0.0:
            continue
        source = None
        gate = np.ones((height, width), dtype=np.float32)
        image_name = model.binding_image(layer, binding)
        if binding.mode == 'SHARED' and image_name:
            source = _resize(_pixels(_image(image_name)), width, height)
            if binding.use_masks:
                gate *= source[..., 3]
        elif binding.mode == 'COLOR':
            source = np.empty_like(result); source[:] = tuple(binding.color)
        elif binding.mode == 'VALUE':
            source = np.empty_like(result); source[:] = (binding.value,) * 3 + (1.0,)
        else:
            source = np.empty_like(result); source[:] = seed
        if binding.use_masks:
            for mask in layer.masks:
                if not (mask.visible and mask.image_name
                        and (not mask.channels
                             or channel_key in mask.channels)):
                    continue
                mask_data = _resize(_pixels(_image(mask.image_name)), width, height)
                value = _luminance(mask_data)
                if mask.invert: value = 1.0 - value
                gate *= (1.0 - mask.opacity) + mask.opacity * value
        fac = np.clip(gate * factor, 0.0, 1.0)[..., None]
        if channel_key == 'normal':
            base = _decode_normal(result)
            detail = _decode_normal(source)
            neutral = np.zeros_like(detail)
            neutral[..., 2] = 1.0
            # Alpha, masks, and layer/binding opacity attenuate the detail
            # normal toward neutral before it is reoriented over the base.
            detail = _normalize_normal(neutral * (1.0 - fac) + detail * fac)
            result[..., :3] = _encode_normal(_rnm(base, detail))
        else:
            mixed = _blend(result, source,
                           model.effective_blend(layer, binding))
            result = result * (1.0 - fac) + mixed * fac
    result[..., 3] = 1.0
    return result


def flatten_stack(tree, material_name, width, height, pack=True):
    """Create/update ``Impasto Export <material> <channel>`` images.

    Source layers are never changed. RGB/scalars remain in their registry
    domain, normal is encoded tangent RGB, height remains signed/encoded as
    stored, and output alpha is deliberately opaque because the channel seed
    defines the complete material surface.
    """
    stack_model = snapshot.snapshot(tree)
    validate_export(stack_model)
    outputs = []
    for key in stack_model.channels:
        if key not in model.CHANNEL_MAP:
            continue
        ch = model.CHANNEL_MAP[key]
        name = "Impasto Export %s %s" % (material_name, ch.label)
        old = bpy.data.images.get(name)
        if old is not None and tuple(old.size[:]) != (width, height):
            bpy.data.images.remove(old)
            old = None
        image = old or bpy.data.images.new(name, width, height, alpha=True)
        compat.set_image_colorspace(image, ch.colorspace)
        data = composite_channel(stack_model, key, width, height)
        # Some blend expressions promote NumPy intermediates to float64. Blender's
        # image pixel bulk setter requires contiguous float32 data on 5.1.x.
        pixels = _np().ascontiguousarray(data, dtype=_np().float32).ravel()
        image.pixels.foreach_set(pixels)
        image.update()
        image.use_fake_user = True
        if pack:
            image.pack()
        outputs.append(image)
    return outputs


def channel_images(tree, material_name, images):
    """Return flattened images by stable Impasto channel key."""
    stack_model = snapshot.snapshot(tree)
    keys = [key for key in stack_model.channels if key in model.CHANNEL_MAP]
    return dict(zip(keys, images))


def save_channel_images(images_by_channel, directory):
    """Save flattened channels as PNG files and return their absolute paths."""
    if not directory:
        raise RuntimeError("Choose an export texture directory")
    if directory.startswith("//") and not bpy.data.filepath:
        raise RuntimeError("Save the .blend before using a // relative texture path")
    absolute = bpy.path.abspath(directory)
    os.makedirs(absolute, exist_ok=True)
    paths = {}
    for key, image in images_by_channel.items():
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", image.name).strip("_")
        path = os.path.join(absolute, safe_name + ".png")
        image.file_format = 'PNG'
        image.filepath_raw = path
        image.save()
        paths[key] = path
    return paths


def _image_has_positive_rgb(image, threshold=1e-6):
    """Whether an image contains a meaningful positive RGB value."""
    pixels = _pixels(image)
    return bool(_np().max(pixels[..., :3]) > threshold)


def build_gltf_material(source_material, images_by_channel):
    """Create/update a simple exporter-recognizable Principled material.

    The editable Impasto material and its stack are never modified.
    """
    name = source_material.name + " glTF"
    material = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (420.0, 0.0)
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (100.0, 0.0)
    links.new(principled.outputs["BSDF"], output.inputs["Surface"])

    y = 300.0
    for key in ("base_color", "metallic", "roughness", "alpha"):
        image = images_by_channel.get(key)
        channel = model.CHANNEL_MAP.get(key)
        if image is None or channel is None or channel.socket not in principled.inputs:
            continue
        texture = nodes.new("ShaderNodeTexImage")
        texture.name = "Impasto glTF " + key
        texture.label = channel.label
        texture.image = image
        texture.location = (-500.0, y)
        y -= 230.0
        links.new(texture.outputs["Color"], principled.inputs[channel.socket])

    # A default-white Emission Color with a zero Strength is non-emissive in
    # Impasto. glTF cannot represent a per-pixel strength graph directly, so
    # never wire that white color unless the flattened strength is nonzero.
    emission = images_by_channel.get("emission_color")
    strength = images_by_channel.get("emission_strength")
    if emission is not None and strength is not None and _image_has_positive_rgb(strength):
        texture = nodes.new("ShaderNodeTexImage")
        texture.name = "Impasto glTF emission_color"
        texture.label = "Emission Color"
        texture.image = emission
        texture.location = (-500.0, y)
        links.new(texture.outputs["Color"], principled.inputs["Emission Color"])

    normal_image = images_by_channel.get("normal")
    if normal_image is not None:
        texture = nodes.new("ShaderNodeTexImage")
        texture.name = "Impasto glTF normal"
        texture.label = "Tangent Normal"
        texture.image = normal_image
        texture.location = (-500.0, y)
        normal = nodes.new("ShaderNodeNormalMap")
        normal.space = 'TANGENT'
        normal.location = (-180.0, y)
        links.new(texture.outputs["Color"], normal.inputs["Color"])
        links.new(normal.outputs["Normal"], principled.inputs["Normal"])
    return material
