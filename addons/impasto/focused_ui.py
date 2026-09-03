# SPDX-License-Identifier: GPL-2.0-or-later
"""Reversible, viewport-local UI reduction for Impasto GPU painting."""

REGION_VISIBILITY_PROPERTIES = (
    "show_region_toolbar",
    "show_region_tool_header",
    "show_region_asset_shelf",
)


def hide(space):
    """Hide paint chrome on *space* and return the exact prior state."""
    state = {}
    if space is None:
        return state
    for name in REGION_VISIBILITY_PROPERTIES:
        try:
            state[name] = bool(getattr(space, name))
            setattr(space, name, False)
        except (AttributeError, ReferenceError, TypeError):
            pass
    return state


def restore(space, state):
    """Restore a state returned by :func:`hide`; tolerate closed viewports."""
    if space is None:
        return
    for name, value in (state or {}).items():
        try:
            setattr(space, name, value)
        except (AttributeError, ReferenceError, TypeError):
            pass
