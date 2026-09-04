# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto pure core: channel registry, stack model, and the compiler.

This module must never depend on Blender's ``bpy`` API (enforced by the
test suite). It defines:

- the channel registry (channels are data, not code — A6),
- frozen dataclasses for the stack model (``StackModel``) and the
  compiled graph (``GraphSpec``),
- ``compile_stack(model) -> GraphSpec`` — a pure, deterministic function,
- deterministic node naming (the ``ps:`` role table), and
- spec hashing / JSON serialization (the incrementality + golden-test
  mechanism).

The node graph is a build artifact of this compiler, never a source of
truth (A1). ``reconcile.py`` is the only module that writes node trees.
"""

import hashlib
import json
import secrets
from dataclasses import dataclass, field

# 1: original single-canvas Paint layers (layer.image_name only).
# 2: per-binding canvases — every SHARED PAINT binding carries its own
#    image_name (engine.py migrates 1 -> 2 by copying the layer canvas
#    into bindings that lack one; compile keeps the legacy fallback).
# 3: stack-level reusable mask assets; layer masks become lightweight refs.
SCHEMA_VERSION = 3

NODE_PREFIX = "ps:"
LAYER_TREE_PREFIX = ".Impasto Layer "

# Phase-1 blend set. All values are ShaderNodeMix.blend_type identifiers
# (probed on Blender 5.1.2); blending happens in scene-linear, full stop
# (design §4.8) — Multiply/Screen/etc. are render-correct and may differ
# from display-referred Photoshop results.
BLEND_MODES = ("MIX", "MULTIPLY", "SCREEN", "ADD", "SUBTRACT", "OVERLAY")

LAYER_TYPES = ("PAINT", "FILL", "GROUP")
BINDING_MODES = ("SHARED", "VALUE", "COLOR")
MASK_BLENDS = ("MULTIPLY",)  # phase 1; ADD/SUBTRACT arrive with mask UI


# ---------------------------------------------------------------------------
# Channel registry (A6): channels are data. Keys are stable identifiers,
# snake_case, never renamed. ``socket`` strings are Principled BSDF input
# names probed verbatim on 5.1.2.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelDef:
    key: str            # stable identifier, never renamed
    label: str
    socket: str         # Principled BSDF input name ("" = special: height)
    kind: str           # 'COLOR' | 'SCALAR' | 'VECTOR'
    colorspace: str     # 'sRGB' | 'Non-Color' (resolved via RNA probe)
    default_value: tuple  # chain seed = the Principled default
    default_blend: str
    panel_group: str


def _c(key, label, socket, kind, colorspace, default, group):
    return ChannelDef(key, label, socket, kind, colorspace, default,
                      "MIX", group)


CHANNELS = (
    _c("base_color", "Base Color", "Base Color", "COLOR", "sRGB",
       (0.8, 0.8, 0.8, 1.0), "Core"),
    _c("metallic", "Metallic", "Metallic", "SCALAR", "Non-Color",
       (0.0,), "Core"),
    _c("roughness", "Roughness", "Roughness", "SCALAR", "Non-Color",
       (0.5,), "Core"),
    # Tangent-space normals are painted/stored in their conventional encoded
    # RGB form.  The root compiler composes participating layers bottom-up
    # with reoriented normal mapping (RNM), then decodes the result exactly
    # once with ShaderNodeNormalMap.
    _c("normal", "Tangent Normal (RGB)", "Normal", "COLOR", "Non-Color",
       (0.5, 0.5, 1.0, 1.0), "Core"),
    # height is the one special channel: its chain feeds a single Bump
    # node at chain end -> Principled Normal (A2).
    _c("height", "Height", "", "SCALAR", "Non-Color",
       (0.5,), "Core"),
    _c("alpha", "Alpha", "Alpha", "SCALAR", "Non-Color",
       (1.0,), "Core"),
    _c("emission_color", "Emission Color", "Emission Color", "COLOR",
       "sRGB", (1.0, 1.0, 1.0, 1.0), "Emission"),
    _c("emission_strength", "Emission Strength", "Emission Strength",
       "SCALAR", "Non-Color", (0.0,), "Emission"),
    _c("sss_weight", "Subsurface Weight", "Subsurface Weight", "SCALAR",
       "Non-Color", (0.0,), "Subsurface"),
    # A distance vector: painted as color, stored Non-Color (values are
    # metric, not perceptual).
    _c("sss_radius", "Subsurface Radius", "Subsurface Radius", "VECTOR",
       "Non-Color", (1.0, 0.2, 0.1), "Subsurface"),
    _c("sss_scale", "Subsurface Scale", "Subsurface Scale", "SCALAR",
       "Non-Color", (0.05,), "Subsurface"),
    _c("sss_ior", "Subsurface IOR", "Subsurface IOR", "SCALAR",
       "Non-Color", (1.4,), "Subsurface"),
    _c("sss_anisotropy", "Subsurface Anisotropy", "Subsurface Anisotropy",
       "SCALAR", "Non-Color", (0.0,), "Subsurface"),
)

CHANNEL_MAP = {c.key: c for c in CHANNELS}
CHANNEL_ORDER = {c.key: i for i, c in enumerate(CHANNELS)}

# A template is nothing but a named list of channel keys (design §3.2).
TEMPLATES = {
    "Principled — Standard": ("base_color", "metallic", "roughness",
                              "normal", "height"),
    "Principled — Full": ("base_color", "metallic", "roughness", "normal",
                          "height",
                          "alpha", "emission_color", "emission_strength",
                          "sss_weight", "sss_radius", "sss_scale"),
    "Emissive prop": ("base_color", "roughness", "emission_color",
                      "emission_strength"),
    "Skin / organic": ("base_color", "roughness", "height", "sss_weight",
                       "sss_radius", "sss_scale"),
}

_SOCKET_TYPE_BY_KIND = {
    "COLOR": "NodeSocketColor",
    "SCALAR": "NodeSocketFloat",
    "VECTOR": "NodeSocketVector",
}


def seed_rgba(ch):
    """Channel default as an RGBA 4-tuple (chains blend RGBA uniformly)."""
    v = ch.default_value
    if ch.kind == "COLOR":
        return tuple(float(x) for x in v)
    if ch.kind == "VECTOR":
        return (float(v[0]), float(v[1]), float(v[2]), 1.0)
    return (float(v[0]),) * 3 + (1.0,)


def seed_native(ch):
    """Channel default in the shape of its root output socket."""
    if ch.kind == "COLOR":
        return tuple(float(x) for x in ch.default_value)
    if ch.kind == "VECTOR":
        return tuple(float(x) for x in ch.default_value)
    return float(ch.default_value[0])


# ---------------------------------------------------------------------------
# Stack model (plain frozen dataclasses; produced by snapshot.py)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BindingModel:
    key: str                 # channel key
    image_name: str = ""     # PAINT: per-channel canvas; legacy fallback below
    enabled: bool = True
    mode: str = "SHARED"     # SHARED | VALUE | COLOR
    value: float = 0.0
    color: tuple = (0.0, 0.0, 0.0, 1.0)
    blend_mode: str = "LAYER"    # 'LAYER' = inherit the layer's blend
    opacity: float = 1.0
    use_masks: bool = True


@dataclass(frozen=True)
class MaskModel:
    uid: str
    label: str = ""
    mask_type: str = "IMAGE"
    image_name: str = ""
    uv_map: str = ""
    blend: str = "MULTIPLY"
    invert: bool = False
    opacity: float = 1.0
    visible: bool = True
    channels: tuple = ()


@dataclass(frozen=True)
class LayerModel:
    uid: str
    label: str = ""
    layer_type: str = "PAINT"    # PAINT | FILL | GROUP
    parent_uid: str = ""         # "" = root
    visible: bool = True
    opacity: float = 1.0
    blend_mode: str = "MIX"
    image_name: str = ""         # PAINT: the canvas image (by name)
    uv_map: str = ""
    bindings: tuple = ()         # tuple[BindingModel] — SPARSE
    masks: tuple = ()            # tuple[MaskModel]


@dataclass(frozen=True)
class MaterialModel:
    principled_node_name: str


@dataclass(frozen=True)
class StackModel:
    root_tree_name: str
    channels: tuple = ()         # enabled channel keys, registry order
    layers: tuple = ()           # display order: index 0 = TOP
    material: MaterialModel = None


# ---------------------------------------------------------------------------
# GraphSpec (design §4.2): all tuples, all frozen, deterministic order.
# Socket keys are RNA socket identifiers for regular nodes; group
# input/output and group-instance sockets are keyed by NAME (interface
# identifiers are auto-generated Socket_N — probed on 5.1.2 — so names,
# which we control and keep unique, are the deterministic handle).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeSpec:
    name: str
    bl_idname: str
    props: tuple = ()        # ((rna prop name, value), ...)
    inputs: tuple = ()       # ((socket key, default_value), ...) UNLINKED


@dataclass(frozen=True)
class LinkSpec:
    src: tuple               # (node name, output socket key)
    dst: tuple               # (node name, input socket key)


@dataclass(frozen=True)
class SocketSpec:
    name: str
    in_out: str              # 'INPUT' | 'OUTPUT'
    socket_type: str


@dataclass(frozen=True)
class TreeSpec:
    key: str                 # "root" | "material" | layer uid
    interface: tuple = ()
    nodes: tuple = ()
    links: tuple = ()


@dataclass(frozen=True)
class GraphSpec:
    trees: tuple = ()


# ---------------------------------------------------------------------------
# Deterministic naming — the role table (design §4.4). Node names are
# always derivable from (uid, role); zero StringProperty name slots.
# ---------------------------------------------------------------------------

def layer_tree_name(uid):
    return LAYER_TREE_PREFIX + uid


def uid_from_layer_tree_name(name):
    if name.startswith(LAYER_TREE_PREFIX):
        return name[len(LAYER_TREE_PREFIX):]
    return None


def new_uid(existing=()):
    uid = secrets.token_hex(4)
    while uid in existing:
        uid = secrets.token_hex(4)
    return uid


def n_src(uid):
    return "ps:%s:src" % uid


def n_uv(uid):
    return "ps:%s:uv" % uid


def n_out(uid):
    return "ps:%s:out" % uid


def n_scalar_src(uid):
    return "ps:%s:src.scalar" % uid


def n_binding_src(uid, key):
    return "ps:%s:ch.%s:src" % (uid, key)


def n_binding_uv(uid, key):
    return "ps:%s:ch.%s:uv" % (uid, key)


def n_binding_scalar(uid, key):
    return "ps:%s:ch.%s:scalar" % (uid, key)


def n_binding_gate(uid, key, i):
    return "ps:%s:ch.%s:gate.%d" % (uid, key, i)


def n_mask_src(uid, muid):
    return "ps:%s:mask.%s:src" % (uid, muid)


def n_mask_uv(uid, muid):
    return "ps:%s:mask.%s:uv" % (uid, muid)


def n_mask_op(uid, muid):
    return "ps:%s:mask.%s:op" % (uid, muid)


def n_mask_mul(uid, i):
    return "ps:%s:mask:mul.%d" % (uid, i)


def n_root_out():
    return "ps:root:out"


def n_root_layer(uid):
    return "ps:root:layer.%s" % uid


def n_blend(key, uid):
    return "ps:root:ch.%s:blend.%s" % (key, uid)


def n_fac(key, uid):
    return "ps:root:ch.%s:fac.%s" % (key, uid)


def n_bump():
    return "ps:root:ch.height:bump"


def n_normal_map():
    return "ps:root:ch.normal:decode"


def n_rnm(uid, role):
    return "ps:root:ch.normal:rnm.%s.%s" % (uid, role)


def n_scalar_out(key):
    return "ps:root:ch.%s:scalar" % key


def n_material_stack():
    return "ps:material:stack"


# ---------------------------------------------------------------------------
# Compiler helpers
# ---------------------------------------------------------------------------

def _uid_map(model):
    return {ly.uid: ly for ly in model.layers}


def _ancestors(model, layer):
    """Parent chain root-ward; cycle-safe."""
    by_uid = _uid_map(model)
    out, seen, cur = [], {layer.uid}, layer.parent_uid
    while cur and cur in by_uid and cur not in seen:
        g = by_uid[cur]
        out.append(g)
        seen.add(cur)
        cur = g.parent_uid
    return out


def layer_has_tree(layer):
    """PAINT layers and any layer with a visible image mask compile to a
    per-layer node group; a bare FILL layer compiles to no tree at all —
    its constants go straight into root-chain blend inputs."""
    if layer.layer_type == "GROUP":
        return False
    if layer.layer_type == "PAINT":
        return True
    return any(m.visible and m.image_name for m in layer.masks)


def _mask_signal(layer):
    """True when the layer tree's "mask" output carries a real signal
    (paint-image alpha and/or visible image masks)."""
    if layer.layer_type == "PAINT" and layer.image_name:
        return True
    return any(m.visible and m.image_name for m in layer.masks)


def effective_blend(layer, binding):
    b = binding.blend_mode
    if b and b != "LAYER":
        return b
    if layer.blend_mode:
        return layer.blend_mode
    return CHANNEL_MAP[binding.key].default_blend


def const_factor(model, layer, binding):
    """The constant fold of the effective blend factor (design §4.3):
    enabled? x visible? x layer.opacity x binding.opacity x
    product over ancestor groups of (visible? x opacity). Visibility
    contributes as 0/1 INSIDE the fold — that is what makes the eye icon
    a uniform update (§4.6)."""
    f = (1.0 if binding.enabled else 0.0)
    f *= (1.0 if layer.visible else 0.0)
    f *= layer.opacity * binding.opacity
    for g in _ancestors(model, layer):
        f *= (1.0 if g.visible else 0.0) * g.opacity
    return f


def _participates(model, layer, binding, grace):
    """Structure-inclusion rule. An active participant is always in.
    A disabled/hidden participant is included with factor 0 only while
    its layer (or an ancestor) is in the grace set — so toggles are
    instant uniform writes; the prune pass compiles with an empty grace
    set and slims the graph (§4.6)."""
    if layer.layer_type == "GROUP":
        return False
    active = (binding.enabled and layer.visible
              and all(g.visible for g in _ancestors(model, layer)))
    if active:
        return True
    if layer.uid in grace:
        return True
    return any(g.uid in grace for g in _ancestors(model, layer))


def _binding_for(layer, key):
    for b in layer.bindings:
        if b.key == key:
            return b
    return None


def binding_image(layer, binding):
    """Resolve a PAINT binding canvas with legacy layer compatibility."""
    return binding.image_name or layer.image_name


# ---------------------------------------------------------------------------
# compile_stack — the pure function (design §4.1/§4.3)
# ---------------------------------------------------------------------------

def _compile_layer_tree(layer):
    uid = layer.uid
    nodes, links, inputs_on_out = [], [], []

    shared_keys = sorted(
        (b.key for b in layer.bindings
         if b.mode == "SHARED" and b.key in CHANNEL_MAP),
        key=lambda k: CHANNEL_ORDER[k])
    gate_keys = sorted((b.key for b in layer.bindings if b.key in CHANNEL_MAP),
                       key=lambda k: CHANNEL_ORDER[k])
    interface = tuple(
        [SocketSpec("ch:%s" % k, "OUTPUT",
                    _SOCKET_TYPE_BY_KIND[CHANNEL_MAP[k].kind])
         for k in shared_keys]
        + [SocketSpec("mask:%s" % k, "OUTPUT", "NodeSocketFloat")
           for k in gate_keys])

    channel_alpha = {}
    emitted_sources = set()
    emitted_scalars = set()
    if layer.layer_type == "PAINT":
        for row, k in enumerate(gate_keys):
            b = _binding_for(layer, k)
            image_name = binding_image(layer, b)
            if not image_name:
                continue
            y = -220.0 * row
            src_name = (n_binding_src(uid, k) if b.image_name else n_src(uid))
            uv_name = (n_binding_uv(uid, k) if b.image_name else n_uv(uid))
            if src_name not in emitted_sources and layer.uv_map:
                nodes.append(NodeSpec(uv_name, "ShaderNodeUVMap",
                                      (("uv_map", layer.uv_map),
                                       ("location", (-820.0, y))), ()))
            if src_name not in emitted_sources:
                nodes.append(NodeSpec(src_name, "ShaderNodeTexImage",
                                      (("image", image_name),
                                       ("location", (-560.0, y))), ()))
                if layer.uv_map:
                    links.append(LinkSpec((uv_name, "UV"),
                                          (src_name, "Vector")))
                emitted_sources.add(src_name)
            source = (src_name, "Color")
            if b.mode == "SHARED" and CHANNEL_MAP[k].kind == "SCALAR":
                scalar_name = (n_binding_scalar(uid, k) if b.image_name
                               else n_scalar_src(uid))
                if scalar_name not in emitted_scalars:
                    nodes.append(NodeSpec(scalar_name, "ShaderNodeSeparateColor",
                                          (("mode", "RGB"),
                                           ("location", (-260.0, y))), ()))
                    links.append(LinkSpec(source, (scalar_name, "Color")))
                    emitted_scalars.add(scalar_name)
                source = (scalar_name, "Red")
            if b.mode == "SHARED":
                links.append(LinkSpec(source, (n_out(uid), "ch:%s" % k)))
            channel_alpha[k] = (src_name, "Alpha")

    # Mask chain: paint alpha (if any) x each visible image mask, where
    # invert + opacity fold into one MULTIPLY_ADD per mask (a*m + b):
    # normal: a=op, b=1-op; inverted: a=-op, b=1. Both knobs are
    # therefore uniform writes, never structure.
    mask_factors = []
    mask_y = -340.0
    for m in layer.masks:
        if not (m.visible and m.image_name):
            continue
        if m.uv_map:
            nodes.append(NodeSpec(n_mask_uv(uid, m.uid), "ShaderNodeUVMap",
                                  (("uv_map", m.uv_map),
                                   ("location", (-820.0, mask_y))), ()))
        nodes.append(NodeSpec(n_mask_src(uid, m.uid), "ShaderNodeTexImage",
                              (("image", m.image_name),
                               ("location", (-560.0, mask_y))), ()))
        if m.uv_map:
            links.append(LinkSpec((n_mask_uv(uid, m.uid), "UV"),
                                  (n_mask_src(uid, m.uid), "Vector")))
        op = float(m.opacity)
        a, b = (-op, 1.0) if m.invert else (op, 1.0 - op)
        nodes.append(NodeSpec(n_mask_op(uid, m.uid), "ShaderNodeMath",
                              (("operation", "MULTIPLY_ADD"),
                               ("location", (-260.0, mask_y))),
                              (("Value_001", a), ("Value_002", b))))
        links.append(LinkSpec((n_mask_src(uid, m.uid), "Color"),
                              (n_mask_op(uid, m.uid), "Value")))
        mask_factors.append(((n_mask_op(uid, m.uid), "Value"), m.channels))
        mask_y -= 340.0

    for k in gate_keys:
        factors = ([channel_alpha[k]] if k in channel_alpha else []) + [
            factor for factor, channels in mask_factors
            if not channels or k in channels]
        cur = factors[0] if factors else None
        for i, nxt in enumerate(factors[1:]):
            mul = n_binding_gate(uid, k, i)
            nodes.append(NodeSpec(mul, "ShaderNodeMath",
                                  (("operation", "MULTIPLY"),
                                   ("location", (0.0, -340.0 * (i + 1))),), ()))
            links.append(LinkSpec(cur, (mul, "Value")))
            links.append(LinkSpec(nxt, (mul, "Value_001")))
            cur = (mul, "Value")
        if cur is not None:
            links.append(LinkSpec(cur, (n_out(uid), "mask:%s" % k)))
        else:
            inputs_on_out.append(("mask:%s" % k, 1.0))

    nodes.append(NodeSpec(n_out(uid), "NodeGroupOutput",
                          (("location", (320.0, 0.0)),),
                          tuple(inputs_on_out)))
    return TreeSpec(uid, interface, tuple(nodes), tuple(links))


def _compile_root(model, grace):
    nodes, links, interface = [], [], []
    out_inputs = []

    treed = [ly for ly in model.layers if layer_has_tree(ly)]
    treed_uids = {ly.uid for ly in treed}
    used_uids = set()   # instances are emitted only if a chain taps them

    chan_keys = [k for k in model.channels if k in CHANNEL_MAP]
    chan_keys.sort(key=lambda k: CHANNEL_ORDER[k])
    max_chain = 0
    normal_interface_added = False
    normal_signal = None
    bump_signal = None
    for ci, key in enumerate(chan_keys):
        ch = CHANNEL_MAP[key]
        if key in {"normal", "height"}:
            if not normal_interface_added:
                interface.append(SocketSpec("Normal", "OUTPUT",
                                            "NodeSocketVector"))
                normal_interface_added = True
        else:
            interface.append(SocketSpec(ch.label, "OUTPUT",
                                        _SOCKET_TYPE_BY_KIND[ch.kind]))

        # bottom -> top: collection index 0 is the TOP of the stack
        prev = None
        chain_i = 0
        for layer in reversed(model.layers):
            binding = _binding_for(layer, key)
            if binding is None:
                continue
            if not _participates(model, layer, binding, grace):
                continue
            blend = n_blend(key, layer.uid)
            scalar_channel = ch.kind == "SCALAR"
            props = (("data_type", "FLOAT" if scalar_channel else "RGBA"),
                     # Normal-map pixels encode directions rather than
                     # ordinary colors.  This Mix only fades this layer
                     # toward the neutral tangent normal; the result is
                     # composed with the accumulated base by RNM below.
                     ("blend_type", ("MIX" if key == "normal"
                                     else effective_blend(layer, binding))),
                     ("clamp_factor", True),
                     ("clamp_result", False),
                     ("location", (-300.0 + 260.0 * chain_i,
                                   -320.0 * ci)))
            fac = const_factor(model, layer, binding)
            inputs = []
            use_fac_node = (binding.use_masks
                            and (bool(binding_image(layer, binding))
                                 or any(m.visible and m.image_name
                                        for m in layer.masks))
                            and layer.uid in treed_uids)
            if not use_fac_node:
                inputs.append(("Factor_Float", fac))
            if prev is None or key == "normal":
                inputs.append(("A_Float" if scalar_channel else "A_Color",
                               (float(ch.default_value[0]) if scalar_channel
                                else seed_rgba(ch))))
            shared_ok = (binding.mode == "SHARED"
                         and layer.uid in treed_uids
                         and layer.layer_type == "PAINT"
                         and bool(binding_image(layer, binding)))
            if shared_ok:
                used_uids.add(layer.uid)
                links.append(LinkSpec((n_root_layer(layer.uid),
                                       "ch:%s" % key),
                                      (blend, "B_Float" if scalar_channel
                                       else "B_Color")))
            elif binding.mode == "COLOR":
                inputs.append(("B_Float" if scalar_channel else "B_Color",
                               (float(binding.color[0]) if scalar_channel
                                else tuple(float(x)
                                           for x in binding.color))))
            elif binding.mode == "VALUE":
                v = float(binding.value)
                inputs.append(("B_Float" if scalar_channel else "B_Color",
                               v if scalar_channel else (v, v, v, 1.0)))
            else:
                # SHARED with a missing image: substitute the channel
                # default so the material stays valid (design §4.9).
                inputs.append(("B_Float" if scalar_channel else "B_Color",
                               (float(ch.default_value[0]) if scalar_channel
                                else seed_rgba(ch))))
            nodes.append(NodeSpec(blend, "ShaderNodeMix", props,
                                  tuple(inputs)))
            if use_fac_node:
                used_uids.add(layer.uid)
                nodes.append(NodeSpec(
                    n_fac(key, layer.uid), "ShaderNodeMath",
                    (("operation", "MULTIPLY"),
                     ("location", (-300.0 + 260.0 * chain_i - 70.0,
                                   -320.0 * ci + 170.0))),
                    (("Value_001", fac),)))
                links.append(LinkSpec((n_root_layer(layer.uid),
                                       "mask:%s" % key),
                                      (n_fac(key, layer.uid), "Value")))
                links.append(LinkSpec((n_fac(key, layer.uid), "Value"),
                                      (blend, "Factor_Float")))
            if prev is not None and key != "normal":
                links.append(LinkSpec(prev,
                                      (blend, "A_Float" if scalar_channel
                                       else "A_Color")))
            layer_result = (blend, "Result_Float" if scalar_channel
                            else "Result_Color")
            if key == "normal":
                # Optimized RNM formulation from "Blending in Detail":
                #   t = base * (2,2,2) + (-1,-1,0)
                #   u = detail * (-2,-2,2) + (1,1,-1)
                #   r = normalize(t * dot(t,u) / t.z - u)
                # The normalized result is re-encoded to RGB so another
                # detail layer can be composed before the one final Blender
                # tangent-space Normal Map decode.
                uid = layer.uid
                t_mul, t_add = n_rnm(uid, "t_mul"), n_rnm(uid, "t_add")
                u_mul, u_add = n_rnm(uid, "u_mul"), n_rnm(uid, "u_add")
                u_norm = n_rnm(uid, "u_normalize")
                dot, sep = n_rnm(uid, "dot"), n_rnm(uid, "sep")
                safe_z, div = n_rnm(uid, "safe_z"), n_rnm(uid, "div")
                scale = n_rnm(uid, "scale")
                sub, norm = n_rnm(uid, "sub"), n_rnm(uid, "normalize")
                enc_scale, enc_add = (n_rnm(uid, "encode_scale"),
                                      n_rnm(uid, "encode_add"))
                x = -300.0 + 260.0 * chain_i
                y = -320.0 * ci
                nodes.extend((
                    NodeSpec(t_mul, "ShaderNodeVectorMath",
                             (("operation", "MULTIPLY"),
                              ("location", (x + 180.0, y + 220.0))),
                             (("Vector_001", (2.0, 2.0, 2.0)),)),
                    NodeSpec(t_add, "ShaderNodeVectorMath",
                             (("operation", "ADD"),
                              ("location", (x + 360.0, y + 220.0))),
                             (("Vector_001", (-1.0, -1.0, 0.0)),)),
                    NodeSpec(u_mul, "ShaderNodeVectorMath",
                             (("operation", "MULTIPLY"),
                              ("location", (x + 180.0, y - 180.0))),
                             (("Vector_001", (-2.0, -2.0, 2.0)),)),
                    NodeSpec(u_add, "ShaderNodeVectorMath",
                             (("operation", "ADD"),
                              ("location", (x + 360.0, y - 180.0))),
                             (("Vector_001", (1.0, 1.0, -1.0)),)),
                    NodeSpec(u_norm, "ShaderNodeVectorMath",
                             (("operation", "NORMALIZE"),
                              ("location", (x + 540.0, y - 180.0))), ()),
                    NodeSpec(dot, "ShaderNodeVectorMath",
                             (("operation", "DOT_PRODUCT"),
                              ("location", (x + 720.0, y + 80.0))), ()),
                    NodeSpec(sep, "ShaderNodeSeparateXYZ",
                             (("location", (x + 540.0, y + 300.0)),), ()),
                    NodeSpec(safe_z, "ShaderNodeMath",
                             (("operation", "MAXIMUM"),
                              ("location", (x + 720.0, y + 300.0))),
                             (("Value_001", 1.0e-5),)),
                    NodeSpec(div, "ShaderNodeMath",
                             (("operation", "DIVIDE"),
                              ("location", (x + 900.0, y + 80.0))), ()),
                    NodeSpec(scale, "ShaderNodeVectorMath",
                             (("operation", "SCALE"),
                              ("location", (x + 1080.0, y + 160.0))), ()),
                    NodeSpec(sub, "ShaderNodeVectorMath",
                             (("operation", "SUBTRACT"),
                              ("location", (x + 1260.0, y))), ()),
                    NodeSpec(norm, "ShaderNodeVectorMath",
                             (("operation", "NORMALIZE"),
                              ("location", (x + 1440.0, y))), ()),
                    NodeSpec(enc_scale, "ShaderNodeVectorMath",
                             (("operation", "SCALE"),
                              ("location", (x + 1620.0, y))),
                             (("Scale", 0.5),)),
                    NodeSpec(enc_add, "ShaderNodeVectorMath",
                             (("operation", "ADD"),
                              ("location", (x + 1800.0, y))),
                             (("Vector_001", (0.5, 0.5, 0.5)),)),
                ))
                if prev is None:
                    nodes[-14] = NodeSpec(
                        t_mul, "ShaderNodeVectorMath",
                        (("operation", "MULTIPLY"),
                         ("location", (x + 180.0, y + 220.0))),
                        (("Vector", (0.5, 0.5, 1.0)),
                         ("Vector_001", (2.0, 2.0, 2.0))))
                else:
                    links.append(LinkSpec(prev, (t_mul, "Vector")))
                links.extend((
                    LinkSpec((t_mul, "Vector"), (t_add, "Vector")),
                    LinkSpec(layer_result, (u_mul, "Vector")),
                    LinkSpec((u_mul, "Vector"), (u_add, "Vector")),
                    LinkSpec((u_add, "Vector"), (u_norm, "Vector")),
                    LinkSpec((t_add, "Vector"), (dot, "Vector")),
                    LinkSpec((u_norm, "Vector"), (dot, "Vector_001")),
                    LinkSpec((t_add, "Vector"), (sep, "Vector")),
                    LinkSpec((sep, "Z"), (safe_z, "Value")),
                    LinkSpec((dot, "Value"), (div, "Value")),
                    LinkSpec((safe_z, "Value"), (div, "Value_001")),
                    LinkSpec((t_add, "Vector"), (scale, "Vector")),
                    LinkSpec((div, "Value"), (scale, "Scale")),
                    LinkSpec((scale, "Vector"), (sub, "Vector")),
                    LinkSpec((u_norm, "Vector"), (sub, "Vector_001")),
                    LinkSpec((sub, "Vector"), (norm, "Vector")),
                    LinkSpec((norm, "Vector"), (enc_scale, "Vector")),
                    LinkSpec((enc_scale, "Vector"), (enc_add, "Vector")),
                ))
                prev = (enc_add, "Vector")
            else:
                prev = layer_result
            chain_i += 1
        max_chain = max(max_chain, chain_i)

        if key == "normal":
            if prev is not None:
                nodes.append(NodeSpec(
                    n_normal_map(), "ShaderNodeNormalMap",
                    (("space", "TANGENT"),
                     ("location", (-300.0 + 260.0 * chain_i,
                                   -320.0 * ci))),
                    (("Strength", 1.0),)))
                links.append(LinkSpec(prev, (n_normal_map(), "Color")))
                normal_signal = (n_normal_map(), "Normal")
        elif key == "height":
            if prev is not None:
                nodes.append(NodeSpec(
                    n_bump(), "ShaderNodeBump",
                    (("location", (-300.0 + 260.0 * chain_i,
                                   -320.0 * ci)),), ()))
                links.append(LinkSpec(prev, (n_bump(), "Height")))
                bump_signal = (n_bump(), "Normal")
            # no participants: "Normal" output stays unlinked and the
            # material tree emits no Normal link (a linked zero-vector
            # would break shading).
        else:
            if prev is not None:
                links.append(LinkSpec(prev, (n_root_out(), ch.label)))
            else:
                out_inputs.append((ch.label, seed_native(ch)))

    # Decode tangent normals before applying height.  Blender's Bump node
    # accepts an existing tangent normal, so both channels compose into the
    # one vector that drives Principled rather than competing for its socket.
    if normal_signal is not None and bump_signal is not None:
        links.append(LinkSpec(normal_signal, (n_bump(), "Normal")))
        links.append(LinkSpec(bump_signal, (n_root_out(), "Normal")))
    elif normal_signal is not None:
        links.append(LinkSpec(normal_signal, (n_root_out(), "Normal")))
    elif bump_signal is not None:
        links.append(LinkSpec(bump_signal, (n_root_out(), "Normal")))

    instance_nodes = [
        NodeSpec(n_root_layer(ly.uid), "ShaderNodeGroup",
                 (("node_tree", layer_tree_name(ly.uid)),
                  ("location", (-620.0, -260.0 * i))), ())
        for i, ly in enumerate(treed) if ly.uid in used_uids]

    nodes.append(NodeSpec(n_root_out(), "NodeGroupOutput",
                          (("location",
                            (60.0 + 260.0 * (max_chain + 1), 0.0)),),
                          tuple(out_inputs)))
    return TreeSpec("root", tuple(interface),
                    tuple(instance_nodes) + tuple(nodes), tuple(links))


def _height_has_chain(model, grace):
    for layer in model.layers:
        b = _binding_for(layer, "height")
        if b is not None and _participates(model, layer, b, grace):
            return True
    return False


def _normal_has_chain(model, grace):
    for layer in model.layers:
        b = _binding_for(layer, "normal")
        if b is not None and _participates(model, layer, b, grace):
            return True
    return False


def _compile_material(model, grace):
    if model.material is None:
        return None
    pn = model.material.principled_node_name
    nodes = (NodeSpec(n_material_stack(), "ShaderNodeGroup",
                      (("node_tree", model.root_tree_name),
                       ("location", (-460.0, 220.0))), ()),)
    links = []
    chan_keys = [k for k in model.channels if k in CHANNEL_MAP]
    chan_keys.sort(key=lambda k: CHANNEL_ORDER[k])
    for key in chan_keys:
        ch = CHANNEL_MAP[key]
        if key in {"normal", "height"}:
            if ((_normal_has_chain(model, grace)
                 or _height_has_chain(model, grace))
                    and not any(link.dst == (pn, "Normal")
                                for link in links)):
                links.append(LinkSpec((n_material_stack(), "Normal"),
                                      (pn, "Normal")))
        else:
            links.append(LinkSpec((n_material_stack(), ch.label),
                                  (pn, ch.socket)))
    return TreeSpec("material", (), nodes, tuple(links))


def compile_stack(model, grace=frozenset()):
    """StackModel -> GraphSpec. Pure, deterministic, no bpy.

    ``grace`` is a frozenset of layer uids whose hidden/disabled
    participants are still emitted with factor 0 (the uniform-toggle
    grace period, §4.6). The prune pass compiles with an empty set.
    """
    trees = []
    for layer in sorted(model.layers, key=lambda l: l.uid):
        if layer_has_tree(layer):
            trees.append(_compile_layer_tree(layer))
    trees.append(_compile_root(model, grace))
    mat = _compile_material(model, grace)
    if mat is not None:
        trees.append(mat)
    return GraphSpec(tuple(trees))


# ---------------------------------------------------------------------------
# Serialization + hashing (the incrementality and golden-test mechanism)
# ---------------------------------------------------------------------------

def _jsonable(v):
    if isinstance(v, tuple):
        return [_jsonable(x) for x in v]
    return v


def node_to_jsonable(n):
    return {"name": n.name, "bl_idname": n.bl_idname,
            "props": [[k, _jsonable(v)] for k, v in n.props],
            "inputs": [[k, _jsonable(v)] for k, v in n.inputs]}


def tree_to_jsonable(t):
    return {
        "key": t.key,
        "interface": [[s.name, s.in_out, s.socket_type]
                      for s in t.interface],
        "nodes": [node_to_jsonable(n) for n in t.nodes],
        "links": [[list(l.src), list(l.dst)] for l in t.links],
    }


def spec_to_jsonable(spec):
    return {"trees": [tree_to_jsonable(t) for t in spec.trees]}


def tree_hash(tree_spec):
    blob = json.dumps(tree_to_jsonable(tree_spec), sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def structural_signature(tree_spec):
    """Everything about a TreeSpec EXCEPT unlinked-input default values
    (and node locations, which are cosmetic and only applied at node
    creation). Two specs with equal signatures differ only in
    UNIFORM-class values — the §4.6 invariant, made assertable."""
    return {
        "key": tree_spec.key,
        "interface": [[s.name, s.in_out, s.socket_type]
                      for s in tree_spec.interface],
        "nodes": [{"name": n.name, "bl_idname": n.bl_idname,
                   "props": [[k, _jsonable(v)] for k, v in n.props
                             if k != "location"],
                   "input_keys": [k for k, _ in n.inputs]}
                  for n in tree_spec.nodes],
        "links": [[list(l.src), list(l.dst)] for l in tree_spec.links],
    }
