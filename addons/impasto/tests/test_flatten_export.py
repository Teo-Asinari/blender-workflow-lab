import os
import sys

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
ADDONS = os.path.dirname(os.path.dirname(HERE))
if ADDONS not in sys.path:
    sys.path.insert(0, ADDONS)

from impasto import flatten_export, model


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print("PASS:", label)


def rgba_image(name, color):
    image = bpy.data.images.new(name, 2, 2, alpha=True)
    image.pixels.foreach_set(list(color) * 4)
    return image


source = rgba_image("Flatten Source", (0.0, 1.0, 0.0, 0.5))
bottom = model.LayerModel(
    uid="bottom", label="Bottom", layer_type="FILL",
    bindings=(model.BindingModel("base_color", mode="COLOR",
                                 color=(1.0, 0.0, 0.0, 1.0)),))
top = model.LayerModel(
    uid="top", label="Top", layer_type="PAINT",
    bindings=(model.BindingModel("base_color", image_name=source.name),))
stack = model.StackModel("Test", ("base_color",), (top, bottom))
before = tuple(source.pixels[:])
result = flatten_export.composite_channel(stack, "base_color", 2, 2)
stored_alpha = float(flatten_export._pixels(source)[0, 0, 3])
check("paint alpha gates the source over the fill",
      all(abs(float(result[0, 0, i]) - v) < 1e-5
          for i, v in enumerate((1.0 - stored_alpha, stored_alpha,
                                 0.0, 1.0))))
check("source image is unchanged", tuple(source.pixels[:]) == before)

scalar = rgba_image("Flatten Scalar", (0.25, 0.25, 0.25, 1.0))
layer = model.LayerModel(
    uid="paint", layer_type="PAINT",
    bindings=(model.BindingModel("roughness", image_name=scalar.name),))
stack = model.StackModel("Test", ("roughness",), (layer,))
result = flatten_export.composite_channel(stack, "roughness", 3, 5)
check("explicit output dimensions are honored", result.shape == (5, 3, 4))
check("material export is opaque", bool((result[..., 3] == 1.0).all()))


def normal_image(name, normal, alpha=1.0):
    encoded = tuple(component * 0.5 + 0.5 for component in normal)
    return rgba_image(name, encoded + (alpha,))


base_normal = normal_image("Flatten Base Normal", (0.6, 0.0, 0.8))
detail_normal = normal_image("Flatten Detail Normal", (0.0, 0.6, 0.8))
normal_bottom = model.LayerModel(
    uid="normal_bottom", layer_type="PAINT",
    bindings=(model.BindingModel("normal", image_name=base_normal.name),))
normal_top = model.LayerModel(
    uid="normal_top", layer_type="PAINT",
    bindings=(model.BindingModel("normal", image_name=detail_normal.name),))
normal_stack = model.StackModel(
    "Normals", ("normal",), (normal_top, normal_bottom))
normal_result = flatten_export.composite_channel(
    normal_stack, "normal", 2, 2)
np = flatten_export._np()
base_vector = flatten_export._decode_normal(
    flatten_export._pixels(base_normal)[0, 0])
detail_vector = flatten_export._decode_normal(
    flatten_export._pixels(detail_normal)[0, 0])
expected = flatten_export._encode_normal(
    flatten_export._rnm(base_vector, detail_vector))
check("normal layers compose bottom-up with RNM",
      np.allclose(normal_result[0, 0, :3], expected, atol=1e-5))
check("RNM upper detail augments rather than replaces the base",
      not np.allclose(normal_result[0, 0, :3],
                      detail_normal.pixels[:3], atol=1e-5))

half_detail = normal_image(
    "Flatten Half Detail Normal", (0.0, 0.6, 0.8), alpha=0.5)
half_top = model.LayerModel(
    uid="half_top", layer_type="PAINT",
    bindings=(model.BindingModel("normal", image_name=half_detail.name),))
half_stack = model.StackModel(
    "Half Normals", ("normal",), (half_top, normal_bottom))
half_result = flatten_export.composite_channel(
    half_stack, "normal", 2, 2)
neutral = np.array((0.0, 0.0, 1.0), dtype=np.float32)
half_pixels = flatten_export._pixels(half_detail)[0, 0]
half_vector = flatten_export._decode_normal(half_pixels)
half_alpha = float(half_pixels[3])
attenuated_detail = (neutral * (1.0 - half_alpha)
                     + half_vector * half_alpha)
attenuated_detail /= np.linalg.norm(attenuated_detail)
half_expected = flatten_export._encode_normal(
    flatten_export._rnm(base_vector, attenuated_detail))
check("normal source alpha attenuates RNM detail toward neutral",
      np.allclose(half_result[0, 0, :3], half_expected, atol=1e-5))

mask_image = rgba_image("Flatten Normal Mask", (0.5, 0.5, 0.5, 1.0))
masked_top = model.LayerModel(
    uid="masked_top", layer_type="PAINT",
    masks=(model.MaskModel(
        uid="mask", image_name=mask_image.name, opacity=1.0),),
    bindings=(model.BindingModel("normal", image_name=detail_normal.name),))
masked_stack = model.StackModel(
    "Masked Normals", ("normal",), (masked_top, normal_bottom))
masked_result = flatten_export.composite_channel(
    masked_stack, "normal", 2, 2)
check("normal image masks attenuate RNM detail",
      np.allclose(masked_result[0, 0, :3], half_expected, atol=1e-5))

source_material = bpy.data.materials.new("Flatten Source Material")
source_material.use_nodes = True
base_export = rgba_image("Flatten Export Base", (0.2, 0.3, 0.4, 1.0))
rough_export = rgba_image("Flatten Export Rough", (0.7, 0.7, 0.7, 1.0))
normal_export = normal_image("Flatten Export Normal", (0.0, 0.0, 1.0))
gltf_material = flatten_export.build_gltf_material(source_material, {
    "base_color": base_export,
    "roughness": rough_export,
    "normal": normal_export,
})
check("glTF preparation preserves source material",
      bpy.data.materials.get(source_material.name) == source_material)
check("glTF material is separately named",
      gltf_material.name == source_material.name + " glTF")
principled = next(node for node in gltf_material.node_tree.nodes
                  if node.bl_idname == 'ShaderNodeBsdfPrincipled')
check("base color image is wired directly",
      principled.inputs["Base Color"].is_linked)
check("roughness image is wired directly",
      principled.inputs["Roughness"].is_linked)
check("normal image uses a Normal Map node",
      principled.inputs["Normal"].links[0].from_node.bl_idname
      == 'ShaderNodeNormalMap')
zero_emission = rgba_image("Flatten Zero Emission", (0.0, 0.0, 0.0, 1.0))
white_emission = rgba_image("Flatten White Emission", (1.0, 1.0, 1.0, 1.0))
no_glow = flatten_export.build_gltf_material(source_material, {
    "base_color": base_export,
    "emission_color": white_emission,
    "emission_strength": zero_emission,
})
no_glow_principled = next(node for node in no_glow.node_tree.nodes
                          if node.bl_idname == 'ShaderNodeBsdfPrincipled')
check("zero emission strength does not export a white glow",
      not no_glow_principled.inputs["Emission Color"].is_linked)
print("flatten export tests passed")
