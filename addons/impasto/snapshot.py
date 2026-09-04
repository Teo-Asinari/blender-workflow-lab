# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto snapshot: PropertyGroups -> StackModel (read-only walk).

The only module boundary between the stored stack state and the pure
compiler. Imports bpy-adjacent state but performs ZERO writes.
"""

from . import compat
from . import model


def _snapshot_binding(b):
    return model.BindingModel(
        key=b.name,
        image_name=b.image_name,
        enabled=b.enabled,
        mode=b.mode,
        value=b.value,
        color=tuple(b.color),
        blend_mode=b.blend_mode,
        opacity=b.opacity,
        use_masks=b.use_masks,
    )


def _snapshot_mask(m, state):
    asset = state.mask_assets.get(m.asset_uid) if m.asset_uid else None
    return model.MaskModel(
        uid=m.name,
        label=asset.label if asset else m.label,
        mask_type=m.mask_type,
        image_name=asset.image_name if asset else m.image_name,
        uv_map=asset.uv_map if asset else m.uv_map,
        blend=m.blend,
        invert=m.invert,
        opacity=m.opacity,
        visible=m.visible,
        channels=tuple(key for key, index in model.CHANNEL_ORDER.items()
                       if m.channels[index]),
    )


def _snapshot_layer(ly, state):
    return model.LayerModel(
        uid=ly.name,
        label=ly.label,
        layer_type=ly.layer_type,
        parent_uid=ly.parent_uid,
        visible=ly.visible,
        opacity=ly.opacity,
        blend_mode=ly.blend_mode,
        image_name=ly.image_name,
        uv_map=ly.uv_map,
        bindings=tuple(_snapshot_binding(b) for b in ly.bindings),
        masks=tuple(_snapshot_mask(m, state) for m in ly.masks),
    )


def snapshot(root_tree, material=None):
    """Walk the stack state stored on ``root_tree`` into a frozen
    StackModel. ``material``, when given, contributes the Principled
    node name so the compiler can emit the material TreeSpec."""
    state = root_tree.impasto
    mat_model = None
    if material is not None and material.node_tree is not None:
        principled = compat.find_principled(material.node_tree)
        if principled is not None:
            mat_model = model.MaterialModel(principled.name)
    return model.StackModel(
        root_tree_name=root_tree.name,
        channels=tuple(c.name for c in state.channels if c.enabled),
        layers=tuple(_snapshot_layer(ly, state) for ly in state.layers),
        material=mat_model,
    )
