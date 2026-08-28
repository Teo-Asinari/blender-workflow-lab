# SPDX-License-Identifier: GPL-2.0-or-later
"""GPU overlay: draws each face of the active mesh tinted by its UV
island color in the 3D viewport.

Design (mirrors seam_path_tool/preview.py):
- ALL gpu shader/batch work is deferred to draw time and exception-guarded,
  because it raises in `--background` mode. Enabling the overlay headlessly
  is a harmless no-op.
- Geometry extraction and island computation are pure (no gpu) and run
  either eagerly (refresh operator / enable) or lazily at the next draw
  after something marks the overlay dirty. Never per-frame: a clean
  overlay redraws from the cached batch.
- Drawing (v1.2.0) uses a custom create-info shader whose vertex stage
  applies a clip-space depth bias (CLIP_DEPTH_BIAS): soup positions are
  bit-identical to the mesh's, so adjacent faces share vertices exactly
  and the shell never cracks; z-fighting is resolved in the shader, not
  by displacing geometry.

Island sources (v1.1.0):
- 'UV'   — true UV-space connectivity (the actual current unwrap).
  Rebuilds happen at the next draw after a geometry update (bmesh path).
- 'SEAM' — seam-predicted islands (the partition the next Unwrap will
  produce). Rebuilds are LIVE while marking seams, via a debounced
  app-timer + cheap checksum, and use a vectorized numpy path over Mesh
  arrays (~5x faster than the bmesh path at 300k verts).

Display modes (v1.3.0):
- 'ISLANDS' — the classic per-island coloring above (unchanged: same
  shader, same attributes, same build paths).
- 'DENSITY' — texel-density checkerboard: the fragment stage computes
  checker parity from the mesh's actual per-loop UVs (passed as a vec2
  attribute), so islands with mismatched density show visibly different
  checker scales on the surface. Islands come from TRUE UV connectivity
  (density is a property of the actual unwrap — there are no UVs to
  measure on a seam prediction), so the SEAM live pipeline does not
  apply; geometry/UV updates ride the classic dirty -> rebuild-at-next-
  draw path instead (UV edits DO fire is_updated_geometry — probed on
  5.1.2 for foreach_set writes, edit-bmesh writes and uv.unwrap alike).
  An optional per-island tint (blue below / red above the mesh's median
  density) rides the same per-vertex color attribute ISLANDS mode uses.

Opacity + culling (v1.3.1):
- Per-mode opacity properties feed a FLOAT "overlay_opacity" push
  constant (the fragment stages ignore the baked vertex alpha), so the
  sliders are live with zero rebuild. Defaults: ISLANDS 0.4 (color
  wash), DENSITY 0.9 (near-opaque paint).
- Both modes draw with handedness-aware face culling: normally 'BACK',
  but 'FRONT' when either a closed shell or its object-to-world transform
  reverses winding. Open, non-manifold, disconnected, or mixed-winding
  geometry is drawn two-sided because it has no reliable global polarity.

Combined mode (v1.4.0, the new default):
- 'COMBINED' — island colors AND the texel-density checker at once:
  the DENSITY checker soup (positions + actual UVs) drawn through the
  SAME density shader, but with the ISLANDS per-island identity colors
  baked as the per-vertex tint instead of deviation tints. The density
  fragment stage just multiplies the attribute color by the checker
  parity, so NO new GLSL exists — hue reads island membership, checker
  scale reads texel density. Island COLORS follow the island-source
  enum exactly as ISLANDS mode does ('SEAM' predicted / 'UV' actual);
  the checker always samples the actual UV layer, and a mesh with no
  UV layer draws NOTHING (panel hint — no silent islands-only
  degrade). The deviation tint stays DENSITY-only (hue x checker x
  deviation would be unreadable); opacity comes from the Checker
  Opacity property (the combined overlay is checker-like paint).
  Invalidation: with the SEAM source ALL geometry updates ride the
  debounced live pipeline (its checksum additionally covers the UV
  layer, so seam edits AND UV edits/re-unwraps converge on ONE
  debounced rebuild — the draw path never rebuilds this mode/source,
  so no double build is possible); with the UV source the classic
  dirty -> rebuild-at-next-draw path is used, exactly like DENSITY.

The SEAM live path deliberately never calls Object.update_from_editmode():
probed on 5.1.2, that tags the depsgraph and would make the refresh loop
self-triggering. Instead, edit-mode state is snapshotted with
bm.to_mesh(scratch) into a private non-depsgraph Mesh datablock, which
fires no depsgraph events (probed) and costs ~27 ms at 300k verts.
"""

import time
import traceback
from contextlib import contextmanager

import bpy
import gpu

# Per-mode default overlay opacity (v1.3.1). Opacity is a FLOAT push
# constant read fresh from the WindowManager properties at draw time
# (same probed-on-5.1.2 mechanism as checker_res), so dragging the
# sliders is live — never a geometry or batch rebuild. Two separate
# properties (ISLANDS "Tint Opacity" / DENSITY "Checker Opacity")
# because the right defaults differ by an order of intent: the island
# tint is a translucent color WASH over the shaded surface (0.4), while
# the density checker should read like near-opaque PAINT (0.9) — one
# shared slider would drag the wrong value into the other mode on every
# mode switch.
#
# ALPHA is still baked into the per-vertex color alpha so the (pos,
# color) soup layout and the pure color helpers stay unchanged, but the
# fragment stages IGNORE that channel since v1.3.1 — the push constant
# wins.
ALPHA = 0.4                      # ISLANDS default: translucent wash
DEFAULT_DENSITY_OPACITY = 0.9    # DENSITY default: near-opaque paint

# Clip-space depth bias (v1.2.0). The soup's positions are BIT-IDENTICAL
# to the mesh's own vertex coordinates — no geometric offset. The old
# per-FACE-normal offset cracked the shell apart at every non-flat edge
# (two faces meeting at an angle pushed their copies of a shared vertex
# in different directions -> visible gaps between the colored faces).
# Z-fighting is instead beaten in the vertex shader by pulling
# gl_Position.z toward the viewer by CLIP_DEPTH_BIAS * gl_Position.w:
# scaling by w makes the bias a constant fraction of the NDC depth range
# regardless of distance/zoom, so it is robust across depth ranges.
# 1e-4 of the range is a few hundred steps of a 24-bit depth buffer —
# comfortably above z-fighting noise, far too small to bleed the overlay
# through foreground geometry around silhouette edges.
CLIP_DEPTH_BIAS = 1e-4

# GLSL for the custom depth-bias shader, as module-level constants so the
# headless suite can sanity-check them structurally (compiling is
# impossible in --background). Interface/attribute declarations live in
# _shader_create_info(); these are the bare stage bodies the create-info
# API expects. ModelViewProjectionMatrix is set explicitly at draw time
# from gpu.matrix state (it also matches the name Blender's own matrix
# binding uses, so either mechanism yields the same value).
VERT_SHADER_SRC = """
void main()
{
    gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);
    /* Depth bias: pull toward the viewer in clip space. Scaled by w so
     * the post-divide NDC offset is distance-independent. */
    gl_Position.z -= %r * gl_Position.w;
    finalColor = color;
}
""" % CLIP_DEPTH_BIAS

FRAG_SHADER_SRC = """
void main()
{
    /* Alpha comes from the overlay_opacity push constant (v1.3.1), NOT
     * from the vertex color: opacity changes are a uniform update — no
     * geometry or batch rebuild, the slider is live. */
    fragColor = vec4(finalColor.rgb, overlay_opacity);
}
"""

# --- DENSITY mode (v1.3.0) --------------------------------------------------
# Checker shades, multiplied by the per-island tint in the fragment
# stage. Mid-tone grays 0.5 apart: over a dark viewport theme the light
# squares carry the contrast, over a light theme the dark ones do. At
# the near-opaque default opacity (0.9, v1.3.1) they still work: both
# sit off the extremes, so neither square crushes to black or blows to
# white, the 0.5 separation keeps the parity obvious, and the residual
# 0.1 surface show-through plus the deviation tint keep the underlying
# shading readable. We deliberately cannot blend the checker over the
# framebuffer's shaded result in the shader (no framebuffer access from
# a POST_VIEW draw callback), so plain alpha blending at 0.9 is the
# whole mechanism.
CHECKER_DARK = 0.35
CHECKER_LIGHT = 0.85

# Default checkers per UV unit (mirrors the WindowManager property so
# the draw path has a fallback if reading the property ever fails). At
# 32, each checker covers 32 px of a 1024 px texture.
DEFAULT_CHECKER_SIZE = 32

DENSITY_VERT_SHADER_SRC = """
void main()
{
    gl_Position = ModelViewProjectionMatrix * vec4(pos, 1.0);
    /* Same clip-space depth bias as the ISLANDS shader. */
    gl_Position.z -= %r * gl_Position.w;
    finalColor = color;
    uvInterp = uv;
}
""" % CLIP_DEPTH_BIAS

DENSITY_FRAG_SHADER_SRC = """
void main()
{
    /* Checker parity from the interpolated UV: checker_res cells per
     * UV unit. checker_res is a push constant (probed on 5.1.2: FLOAT
     * push constants are supported), so resolution changes are a
     * uniform update — no geometry or batch rebuild. */
    vec2 cell = floor(uvInterp * checker_res);
    float parity = mod(cell.x + cell.y, 2.0);
    float shade = mix(%r, %r, parity);
    /* Alpha from the overlay_opacity push constant (v1.3.1), same
     * live-uniform mechanism as checker_res — the baked vertex alpha
     * is ignored. */
    fragColor = vec4(finalColor.rgb * shade, overlay_opacity);
}
""" % (CHECKER_DARK, CHECKER_LIGHT)

# Live SEAM-mode refresh: recompute only after this much quiet since the
# last geometry update (a whole seam-marking burst costs ONE recompute),
# polling the debounce at LIVE_POLL_S. Per depsgraph tick the cost is
# O(1) (a timestamp); the checksum (~0.1 s at 300k verts) runs once per
# quiet period; the rebuild (~0.65 s at 300k verts) only when the
# checksum actually changed.
LIVE_QUIET_S = 0.30
LIVE_POLL_S = 0.10

# Private snapshot datablock for reading edit-mode state without tagging
# the depsgraph (leading dot keeps it out of UI datablock lists).
_SCRATCH_NAME = ".uv_island_overlay.snapshot"

from . import live


class _State:
    handle = None          # SpaceView3D draw handler
    enabled = False
    dirty = True           # geometry/batch needs rebuilding at next draw
    object_name = None     # active mesh object being overlaid
    source = 'SEAM'        # 'UV' or 'SEAM' (synced from the WM enum)
    mode = 'COMBINED'      # 'ISLANDS'/'DENSITY'/'COMBINED' (WM enum)
    island_count = 0
    coords = None          # triangle-soup positions (object space)
    colors = None          # per-vertex RGBA, flat per face
    uvs = None             # per-vertex UV soup (checker modes only)
    densities = None       # per-island densities (checker modes only)
    median_density = None  # unitless median (checker modes only)
    no_uvs = False         # checker mode on a mesh with no UV layer
    mesh_orientation = 0   # +1/-1 closed winding; 0 means cull neither
    batch = None           # gpu batch (lazily built, viewport only)
    shader = None
    density_shader = None  # checker shader (DENSITY + COMBINED, lazy)
    seam_checksum = None   # checksum of the state the SEAM build used
    debounce = live.Debounce(LIVE_QUIET_S)
    # First draw-time error since the last enable/refresh, as a formatted
    # traceback string. Latched so the console is not spammed every frame,
    # but the failure stays LOUD: it is printed once and shown in the UI.
    last_draw_error = None


_state = _State()


def is_enabled():
    return _state.enabled


def island_count():
    return _state.island_count


def tracked_object_name():
    return _state.object_name


def active_source():
    return _state.source


def active_mode():
    return _state.mode


def has_no_uvs():
    """True when the last checker-mode (DENSITY/COMBINED) build found no
    UV layer on the tracked mesh (a state the panel hints at — not an
    error). COMBINED deliberately draws nothing in that state instead
    of degrading to islands-only."""
    return _state.no_uvs


def median_density():
    """Unitless median texel density (sqrt(UV area / 3D area)) of the
    last checker-mode (DENSITY/COMBINED) build, or None (no build / no
    valid faces). Multiply by an assumed texture edge in px to get
    px/unit."""
    return _state.median_density


def last_draw_error():
    return _state.last_draw_error


def mark_dirty():
    _state.dirty = True


def set_source(source, context=None):
    """Switch the island source ('UV'/'SEAM'). Invalidates and rebuilds
    immediately when the overlay is enabled."""
    if source == _state.source:
        return
    _state.source = source
    _state.seam_checksum = None
    _state.debounce.reset()
    if _state.enabled:
        refresh(context)


def set_mode(mode, context=None):
    """Switch the display mode ('ISLANDS'/'DENSITY'/'COMBINED'). Same
    contract as set_source: no-op when unchanged, invalidates and
    rebuilds immediately when the overlay is enabled."""
    if mode == _state.mode:
        return
    _state.mode = mode
    _state.seam_checksum = None
    _state.debounce.reset()
    if _state.enabled:
        refresh(context)


def on_checker_size_changed():
    """Checker resolution changed: it is a push constant read at draw
    time (probed on 5.1.2 — FLOAT push constants are supported), so no
    rebuild of geometry or batch is needed; just repaint."""
    if _state.enabled:
        _tag_redraw_view3d()


def on_opacity_changed():
    """Overlay opacity changed (either mode's property): it is a FLOAT
    push constant read at draw time (same probed mechanism as
    checker_res), so no rebuild of geometry or batch is needed; just
    repaint."""
    if _state.enabled:
        _tag_redraw_view3d()


def on_density_tint_changed(context=None):
    """Deviation-tint toggle changed: the tint is baked into the
    per-vertex color attribute (same pipeline as ISLANDS colors), so
    this needs a rebuild — but only in DENSITY mode. COMBINED ignores
    the property entirely (its checker tint IS the island color; a
    third modulation would be unreadable), so no rebuild there."""
    if _state.enabled and _state.mode == 'DENSITY':
        refresh(context)


def on_tracked_geometry_update():
    """Called by the depsgraph handler when the tracked object reports a
    geometry update. UV source (any mode) and DENSITY mode keep the
    classic behavior (dirty -> rebuild at next draw; UV edits and
    re-unwraps DO report is_updated_geometry — probed on 5.1.2). SEAM
    source in ISLANDS *and* COMBINED mode goes through the debounced
    live pipeline so a burst of seam edits costs one recompute, after
    the burst. Routing COMBINED+SEAM exclusively through the debounce
    is what keeps its two invalidation concerns (seam edits for the
    hues, UV edits for the checker) converged on ONE rebuild: the
    handler cannot tell the edit kinds apart, so feeding both paths
    would double-build — instead the debounce checksum also covers the
    UV layer in COMBINED, catching UV edits on the same single path."""
    if _state.source == 'SEAM' and _state.mode in ('ISLANDS', 'COMBINED'):
        note_activity()
    else:
        mark_dirty()


# ---------------------------------------------------------------------------
# Geometry extraction (pure — safe headless)
# ---------------------------------------------------------------------------

def build_geometry(obj, source='UV'):
    """Extract (coords, colors, island_count) for a mesh object using the
    given island source. Kept for compatibility; refresh() uses _build to
    also receive the SEAM-state checksum."""
    coords, colors, count, _checksum = _build(obj, source)
    return coords, colors, count


def _mesh_orientation(obj):
    """Return +1/-1 for a consistently wound closed mesh, else 0.

    This topology walk is intentionally a rebuild-time operation.  Clean
    draw frames consume only the cached result, while object transform
    handedness remains cheap to evaluate live in :func:`_draw`.
    """
    import bmesh

    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        owned = False
    else:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        owned = True
    try:
        if not bm.faces:
            return 0
        # A reliable inside/outside sign requires a closed 2-manifold, and
        # adjacent faces must traverse their shared edge in opposite
        # directions.  Reject boundaries, non-manifold edges, and local
        # winding disagreement instead of hiding an arbitrary side.
        for edge in bm.edges:
            loops = edge.link_loops
            if len(loops) != 2 or loops[0].vert is loops[1].vert:
                return 0
        # Treat multiple disconnected shells as ambiguous: their winding
        # signs can differ, so a single GPU culling mode cannot represent
        # them reliably without maintaining separate batches.
        first_face = next(iter(bm.faces))
        pending = [first_face]
        connected = {first_face}
        while pending:
            face = pending.pop()
            for edge in face.edges:
                for neighbor in edge.link_faces:
                    if neighbor not in connected:
                        connected.add(neighbor)
                        pending.append(neighbor)
        if len(connected) != len(bm.faces):
            return 0
        volume = bm.calc_volume(signed=True)
        # Degenerate closed shells have no stable orientation.  Scale the
        # threshold to the mesh bounds so tiny but valid meshes still work.
        xs = [vert.co.x for vert in bm.verts]
        ys = [vert.co.y for vert in bm.verts]
        zs = [vert.co.z for vert in bm.verts]
        extent = max(max(xs) - min(xs), max(ys) - min(ys),
                     max(zs) - min(zs))
        if abs(volume) <= max(extent ** 3, 1.0) * 1.0e-12:
            return 0
        return 1 if volume > 0.0 else -1
    finally:
        if owned:
            bm.free()


def _build(obj, source):
    """(coords, colors, island_count, seam_checksum-or-None). SEAM uses
    the vectorized Mesh-array path with a loud bmesh fallback; UV uses
    the bmesh path (loop-UV comparisons do not vectorize cheaply)."""
    if source == 'SEAM':
        try:
            return _build_seam_arrays(obj)
        except Exception:
            print("[uv_island_overlay] fast seam-island path failed for "
                  "%r; falling back to bmesh:" % obj.name)
            traceback.print_exc()
            coords, colors, count = _build_bmesh(obj, force_seam=True)
            return coords, colors, count, None
    coords, colors, count = _build_bmesh(obj, force_seam=False)
    return coords, colors, count, None


def _snapshot_mesh(obj):
    """A Mesh datablock reflecting the object's CURRENT state, readable
    with foreach_get. In Edit Mode this snapshots the edit bmesh into a
    private scratch mesh — unlike update_from_editmode() this fires no
    depsgraph update (probed on 5.1.2), so the live refresh can never
    trigger itself. In Object Mode the datablock is already current."""
    if obj.mode == 'EDIT':
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        scratch = bpy.data.meshes.get(_SCRATCH_NAME)
        if scratch is None:
            scratch = bpy.data.meshes.new(_SCRATCH_NAME)
        bm.to_mesh(scratch)   # ~27 ms at 300k verts; reusable in place
        return scratch
    return obj.data


def _remove_scratch_mesh():
    try:
        scratch = bpy.data.meshes.get(_SCRATCH_NAME)
        if scratch is not None:
            bpy.data.meshes.remove(scratch)
    except Exception:
        pass


def _checksum_from_arrays(n_verts, n_edges, n_faces, seam, co, uv=None):
    return hash((n_verts, n_edges, n_faces, seam.tobytes(), co.tobytes(),
                 None if uv is None else uv.tobytes()))


def _uv_array_from_mesh(me):
    """Flat float32 UV array of the mesh's active UV layer (empty array
    when there is no layer or no loops). Reads from a snapshot/Object-
    Mode mesh whose data arrays are valid (the edit-mode caveat of
    _density_soup_arrays does not apply to the bm.to_mesh snapshot)."""
    import numpy as np
    uv = np.empty(0, dtype=np.float32)
    layer = me.uv_layers.active
    if layer is not None and len(me.loops):
        uv = np.empty(len(me.loops) * 2, dtype=np.float32)
        layer.data.foreach_get("uv", uv)
    return uv


def seam_state_checksum(obj, include_uvs=False):
    """Cheap checksum of everything the SEAM overlay depends on: counts,
    seam flags and vertex positions (~0.1 s at 300k verts, dominated by
    the edit-mode snapshot + foreach_get). Selection/shading changes do
    not affect it, so no-op depsgraph events skip the rebuild. With
    include_uvs=True (COMBINED mode) the active UV layer's data is
    hashed too: the combined checker samples actual UVs, so UV edits
    and re-unwraps must count as changes there — in plain ISLANDS SEAM
    mode they deliberately do not (UV data is never read)."""
    import numpy as np
    me = _snapshot_mesh(obj)
    n_verts = len(me.vertices)
    n_edges = len(me.edges)
    n_faces = len(me.polygons)
    seam = np.empty(n_edges, dtype=bool)
    if n_edges:
        me.edges.foreach_get("use_seam", seam)
    co = np.empty(n_verts * 3, dtype=np.float32)
    if n_verts:
        me.vertices.foreach_get("co", co)
    uv = _uv_array_from_mesh(me) if include_uvs else None
    return _checksum_from_arrays(n_verts, n_edges, n_faces, seam, co, uv)


def _build_seam_arrays(obj):
    """Vectorized SEAM-mode build over Mesh arrays: islands via
    islands.compute_islands_by_seam_arrays, triangle soup via
    loop_triangles. Measured 0.64 s at 302k faces vs 3.08 s for the
    bmesh path (islands 0.06 s, the rest is foreach_get + numpy).
    Returns (coords, colors, count, checksum); coords/colors are
    contiguous float32 numpy arrays (batch_for_shader accepts them)."""
    import numpy as np
    from . import islands as islands_mod

    me = _snapshot_mesh(obj)
    n_verts = len(me.vertices)
    n_edges = len(me.edges)
    n_faces = len(me.polygons)

    seam = np.empty(n_edges, dtype=bool)
    if n_edges:
        me.edges.foreach_get("use_seam", seam)
    co = np.empty(n_verts * 3, dtype=np.float32)
    if n_verts:
        me.vertices.foreach_get("co", co)
    checksum = _checksum_from_arrays(n_verts, n_edges, n_faces, seam, co)
    if n_faces == 0:
        return [], [], 0, checksum

    loop_edge = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("edge_index", loop_edge)
    loop_total = np.empty(n_faces, dtype=np.int32)
    me.polygons.foreach_get("loop_total", loop_total)
    loop_face = np.repeat(np.arange(n_faces, dtype=np.int32), loop_total)

    face_to_island, count = islands_mod.compute_islands_by_seam_arrays(
        n_faces, loop_edge, loop_face, seam)
    palette = np.asarray(islands_mod.island_colors(count, alpha=ALPHA),
                         dtype=np.float32).reshape(count, 4)

    co = co.reshape(-1, 3)
    hide = np.empty(n_faces, dtype=bool)
    me.polygons.foreach_get("hide", hide)

    # loop_triangles is computed lazily on access in Blender 4.1+
    # (Mesh.calc_loop_triangles was removed).
    if hasattr(me, "calc_loop_triangles"):
        me.calc_loop_triangles()
    tris = me.loop_triangles
    n_tris = len(tris)
    tri_verts = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("vertices", tri_verts)
    tri_verts = tri_verts.reshape(-1, 3)
    tri_polys = np.empty(n_tris, dtype=np.int32)
    tris.foreach_get("polygon_index", tri_polys)

    visible = ~hide[tri_polys]
    tri_verts = tri_verts[visible]
    tri_polys = tri_polys[visible]

    # Positions bit-identical to the mesh's own vertex coordinates (no
    # geometric offset — the shader's clip-space depth bias handles
    # z-fighting), so adjacent faces stay perfectly connected.
    coords = co[tri_verts.ravel()]
    colors = np.repeat(palette[face_to_island[tri_polys]], 3, axis=0)
    return (np.ascontiguousarray(coords, dtype=np.float32),
            np.ascontiguousarray(colors, dtype=np.float32),
            count, checksum)


def _build_bmesh(obj, force_seam=False):
    """bmesh-path build: (coords, colors, island_count) for a mesh object.

    coords: object-space loop-triangle soup, positions bit-identical to
    the mesh's own vertex coordinates (z-fighting is handled by the
    shader's clip-space depth bias, not by displacing geometry). colors:
    matching per-vertex RGBA (flat per face). Uses the edit bmesh in
    Edit Mode so the overlay tracks live edits, a throwaway bmesh
    otherwise. force_seam=True computes seam-predicted islands even when
    a UV layer exists (SEAM-mode fallback path).
    """
    import bmesh
    from . import islands as islands_mod

    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        owned = False
    else:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        owned = True
    try:
        # Topology edits in Edit Mode can leave stale (duplicate/holey)
        # face indices on the shared edit bmesh; the island lookup below
        # is indexed by face.index, so renumber first. O(faces), trivial
        # next to the union-find pass.
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()

        uv_layer = None if force_seam else bm.loops.layers.uv.active
        if uv_layer is not None:
            isl = islands_mod.compute_islands(bm, uv_layer)
        else:
            isl = islands_mod.compute_islands_by_seam(bm)
        if not isl:
            return [], [], 0

        palette = islands_mod.island_colors(len(isl), alpha=ALPHA)
        face_to_island = islands_mod.face_index_to_island(isl)

        coords = []
        colors = []
        for tri in bm.calc_loop_triangles():
            face = tri[0].face
            if face.hide:
                continue
            color = palette[face_to_island[face.index]]
            for loop in tri:
                # Vertex position used verbatim (bit-identical to the
                # mesh) — see CLIP_DEPTH_BIAS for why no offset is added.
                co = loop.vert.co
                coords.append((co.x, co.y, co.z))
                colors.append(color)
        return coords, colors, len(isl)
    finally:
        if owned:
            bm.free()


# ---------------------------------------------------------------------------
# Checker-mode builds: DENSITY (v1.3.0) and COMBINED (v1.4.0)
# ---------------------------------------------------------------------------

def _density_soup_arrays(me):
    """Object-Mode fast path: (coords, uvs, tri_polygon) triangle soup
    with per-corner UVs via Mesh.foreach_get + numpy. Measured on a
    302,500-face grid (5.1.2): 0.37 s vs 2.9 s for the bmesh loop
    iteration below. Edit Mode CANNOT use this path: while the edit
    bmesh owns the mesh, the Mesh uv_layers data arrays are empty
    (probed on 5.1.2 — foreach_get reports a needed length of 0), hence
    the bmesh fallback."""
    import numpy as np

    uv_layer = me.uv_layers.active
    uv = np.empty(len(me.loops) * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    co = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)

    if hasattr(me, "calc_loop_triangles"):
        me.calc_loop_triangles()
    tris = me.loop_triangles
    n_tris = len(tris)
    tv = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("vertices", tv)
    tl = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("loops", tl)
    tp = np.empty(n_tris, dtype=np.int32)
    tris.foreach_get("polygon_index", tp)
    hide = np.empty(len(me.polygons), dtype=bool)
    me.polygons.foreach_get("hide", hide)

    visible = ~hide[tp]
    tv = tv.reshape(-1, 3)[visible]
    tl = tl.reshape(-1, 3)[visible]
    tp = tp[visible]

    # Positions bit-identical to the mesh's (crack-free contract, same
    # as the ISLANDS paths); UVs taken verbatim from the loops.
    coords = np.ascontiguousarray(co[tv.ravel()], dtype=np.float32)
    uvs = np.ascontiguousarray(uv[tl.ravel()], dtype=np.float32)
    return coords, uvs, tp.astype(np.int64)


def _density_soup_bmesh(bm, uv_layer):
    """Edit-Mode fallback: the same (coords, uvs, tri_face) soup from
    the edit bmesh via Python loop iteration (measured 2.9 s at 302k
    faces — required in Edit Mode, see _density_soup_arrays)."""
    import numpy as np

    coords = []
    uvs = []
    tri_face = []
    for tri in bm.calc_loop_triangles():
        face = tri[0].face
        if face.hide:
            continue
        tri_face.append(face.index)
        for loop in tri:
            co = loop.vert.co
            coords.append((co.x, co.y, co.z))
            u = loop[uv_layer].uv
            uvs.append((u.x, u.y))
    return (np.asarray(coords, dtype=np.float32).reshape(-1, 3),
            np.asarray(uvs, dtype=np.float32).reshape(-1, 2),
            np.asarray(tri_face, dtype=np.int64))


def _build_density(obj):
    """(coords, colors, uvs, island_count, densities, median, no_uvs)
    for DENSITY mode: the shared UV-checker build with deviation tints
    (or all-neutral when the tint property is off). Kept as the stable
    7-tuple entry point the density tests drive directly."""
    return _build_uv_checker(obj, tint='DEVIATION')


def _island_tint_colors(tri_isl, count):
    """Per-soup-vertex RGBA from the ISLANDS identity palette — the
    exact colors ISLANDS mode bakes (same islands.island_colors call,
    same min-face-index island ordering), one row per triangle corner.
    Used by COMBINED, where the density fragment shader multiplies this
    attribute by the checker parity. The baked alpha is vestigial since
    v1.3.1 (the overlay_opacity push constant wins); it keeps the soup
    layout unchanged."""
    import numpy as np
    from . import islands as islands_mod
    palette = np.asarray(islands_mod.island_colors(count, alpha=ALPHA),
                         dtype=np.float32).reshape(count, 4)
    return np.ascontiguousarray(np.repeat(palette[tri_isl], 3, axis=0),
                                dtype=np.float32)


def _build_uv_checker(obj, tint):
    """(coords, colors, uvs, island_count, densities, median, no_uvs)
    for the checker modes over TRUE UV connectivity: DENSITY
    (tint='DEVIATION') and COMBINED with the UV island source
    (tint='ISLAND' — per-island identity colors instead of deviation
    tints; same soup, same shader, only the baked tint differs).

    Islands come from TRUE UV connectivity (islands.compute_islands) —
    for DENSITY because density is a property of the actual unwrap, for
    COMBINED because this is exactly what the UV island source means.
    Per-island densities/median via density.py (see there for the
    sqrt(UV area / 3D area) convention and degenerate-face exclusions).
    The tint is baked into the same per-vertex color attribute ISLANDS
    mode uses; with the deviation tint off the DENSITY colors are all
    neutral. A mesh with no UV layer returns no_uvs=True and empty
    geometry — a state, not an error (no latch). Reads happen via bmesh
    in Edit Mode / Mesh arrays in Object Mode: no datablock snapshot,
    so this build is safe inside a draw callback (unlike the SEAM
    paths)."""
    import bmesh
    import numpy as np
    from . import density as density_mod
    from . import islands as islands_mod

    if obj.mode == 'EDIT':
        bm = bmesh.from_edit_mesh(obj.data)
        owned = False
    else:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        owned = True
    try:
        uv_layer = bm.loops.layers.uv.active
        if uv_layer is None:
            return None, None, None, 0, None, None, True

        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        isl = islands_mod.compute_islands(bm, uv_layer)
        if not isl:
            return [], [], [], 0, None, None, False
        count = len(isl)
        face_to_island = np.asarray(
            islands_mod.face_index_to_island(isl), dtype=np.int64)

        if obj.mode == 'EDIT':
            coords, uvs, tri_face = _density_soup_bmesh(bm, uv_layer)
        else:
            coords, uvs, tri_face = _density_soup_arrays(obj.data)

        tri_isl = face_to_island[tri_face]
        area_3d = density_mod.triangle_areas_3d(coords.reshape(-1, 3, 3))
        area_uv = density_mod.triangle_areas_uv(uvs.reshape(-1, 3, 2))
        densities = density_mod.island_densities(
            tri_isl, area_uv, area_3d, count)
        median = density_mod.median_density(densities)

        if tint == 'ISLAND':
            # COMBINED: bake the per-island identity colors as the
            # checker tint. The deviation tint stays DENSITY-only —
            # island hue x checker x deviation would be unreadable —
            # so the density-tint property is deliberately ignored.
            colors = _island_tint_colors(tri_isl, count)
        else:
            wm = getattr(bpy.context, "window_manager", None)
            tint_on = bool(getattr(wm, "uv_island_overlay_density_tint",
                                   True))
            if tint_on:
                tints = density_mod.deviation_tints(densities, median,
                                                    alpha=ALPHA)
            else:
                tints = np.empty((count, 4))
                tints[:, :3] = density_mod.TINT_NEUTRAL
                tints[:, 3] = ALPHA
            tints = np.ascontiguousarray(tints, dtype=np.float32)
            colors = np.ascontiguousarray(
                np.repeat(tints[tri_isl], 3, axis=0), dtype=np.float32)
        return coords, colors, uvs, count, densities, median, False
    finally:
        if owned:
            bm.free()


def _build_combined(obj, source):
    """(coords, colors, uvs, island_count, densities, median, no_uvs,
    checksum) for COMBINED mode (v1.4.0): the DENSITY checker soup
    (positions + actual UVs, drawn through the same density shader)
    with the ISLANDS-mode per-island identity colors baked as the
    per-vertex tint — hue reads island membership, checker scale reads
    texel density.

    Island COLORS follow the island-source enum exactly as ISLANDS mode
    does ('SEAM' predicted / 'UV' actual); the checker always samples
    the actual UV layer. A mesh with no UV layer returns no_uvs=True
    and empty geometry — the checker has nothing to sample, and
    silently degrading to islands-only would be surprising (the panel
    shows the "Mesh has no UVs" hint instead, exactly like DENSITY).
    With the SEAM source and stale UVs the hues are predictions while
    the checkers show the stale unwrap — expected and documented.

    'UV' source delegates to the shared UV-checker build (bmesh / Mesh
    array reads, no snapshot — safe inside a draw callback) and returns
    checksum None. 'SEAM' source reads everything from the snapshot
    mesh on the numpy paths (like the ISLANDS SEAM build it must NEVER
    run inside a draw callback) and returns a checksum that ALSO covers
    the UV layer, so the debounced live pipeline catches seam edits AND
    UV edits/re-unwraps — both invalidation concerns converge on ONE
    debounced rebuild (see on_tracked_geometry_update)."""
    import numpy as np
    from . import density as density_mod
    from . import islands as islands_mod

    if source != 'SEAM':
        coords, colors, uvs, count, densities, median, no_uvs = \
            _build_uv_checker(obj, tint='ISLAND')
        return coords, colors, uvs, count, densities, median, no_uvs, None

    me = _snapshot_mesh(obj)
    n_verts = len(me.vertices)
    n_edges = len(me.edges)
    n_faces = len(me.polygons)

    seam = np.empty(n_edges, dtype=bool)
    if n_edges:
        me.edges.foreach_get("use_seam", seam)
    co = np.empty(n_verts * 3, dtype=np.float32)
    if n_verts:
        me.vertices.foreach_get("co", co)
    # The UV bytes join the checksum (unlike plain ISLANDS SEAM mode):
    # this build samples them, so UV edits must count as changes. The
    # bm.to_mesh snapshot carries valid UV arrays even in Edit Mode
    # (the empty-arrays caveat of _density_soup_arrays applies only to
    # the mesh an edit bmesh currently owns).
    uv = _uv_array_from_mesh(me)
    checksum = _checksum_from_arrays(n_verts, n_edges, n_faces, seam, co,
                                     uv)
    if me.uv_layers.active is None:
        return None, None, None, 0, None, None, True, checksum
    if n_faces == 0:
        return [], [], [], 0, None, None, False, checksum

    # Seam-predicted islands: the exact mapping (and therefore the
    # exact colors) the ISLANDS SEAM path produces for this mesh.
    loop_edge = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("edge_index", loop_edge)
    loop_total = np.empty(n_faces, dtype=np.int32)
    me.polygons.foreach_get("loop_total", loop_total)
    loop_face = np.repeat(np.arange(n_faces, dtype=np.int32), loop_total)
    face_to_island, count = islands_mod.compute_islands_by_seam_arrays(
        n_faces, loop_edge, loop_face, seam)

    co = co.reshape(-1, 3)
    uv = uv.reshape(-1, 2)
    hide = np.empty(n_faces, dtype=bool)
    me.polygons.foreach_get("hide", hide)
    if hasattr(me, "calc_loop_triangles"):
        me.calc_loop_triangles()
    tris = me.loop_triangles
    n_tris = len(tris)
    tv = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("vertices", tv)
    tl = np.empty(n_tris * 3, dtype=np.int32)
    tris.foreach_get("loops", tl)
    tp = np.empty(n_tris, dtype=np.int32)
    tris.foreach_get("polygon_index", tp)

    visible = ~hide[tp]
    tv = tv.reshape(-1, 3)[visible]
    tl = tl.reshape(-1, 3)[visible]
    tp = tp[visible]

    # Positions bit-identical to the mesh's (crack-free contract) and
    # per-corner UVs verbatim, same as the DENSITY fast path.
    coords = np.ascontiguousarray(co[tv.ravel()], dtype=np.float32)
    uvs = np.ascontiguousarray(uv[tl.ravel()], dtype=np.float32)
    tri_isl = np.asarray(face_to_island, dtype=np.int64)[tp]

    # Density stats for the panel's median readout. Measured per
    # PREDICTED island here: with stale UVs that is a statistic over
    # the stale unwrap grouped by the predicted partition — coherent
    # with what is drawn (predicted hues over stale checkers).
    area_3d = density_mod.triangle_areas_3d(coords.reshape(-1, 3, 3))
    area_uv = density_mod.triangle_areas_uv(uvs.reshape(-1, 3, 2))
    densities = density_mod.island_densities(tri_isl, area_uv, area_3d,
                                             count)
    median = density_mod.median_density(densities)
    colors = _island_tint_colors(tri_isl, count)
    return coords, colors, uvs, count, densities, median, False, checksum


# ---------------------------------------------------------------------------
# Live SEAM-mode refresh (debounce is pure — live.py; this is the thin
# bpy.app.timers driver plus the checksum gate)
# ---------------------------------------------------------------------------

def note_activity(now=None):
    """Record that the tracked object's geometry MAY have changed and make
    sure the live timer is running. O(1); safe to call per depsgraph
    tick, even during a transform drag."""
    if now is None:
        now = time.monotonic()
    _state.debounce.note_change(now)
    _ensure_live_timer()


def _ensure_live_timer():
    try:
        if not bpy.app.timers.is_registered(_live_timer_cb):
            bpy.app.timers.register(_live_timer_cb,
                                    first_interval=LIVE_POLL_S)
    except Exception:
        # Headless test paths drive _live_tick directly instead.
        pass


def _stop_live_timer():
    try:
        if bpy.app.timers.is_registered(_live_timer_cb):
            bpy.app.timers.unregister(_live_timer_cb)
    except Exception:
        pass


def _live_timer_cb():
    try:
        return _live_tick(time.monotonic())
    except Exception:
        print("[uv_island_overlay] live refresh tick failed:")
        traceback.print_exc()
        return None


def _live_tick(now):
    """One debounce poll. Returns the next poll interval, or None to stop
    the timer. Takes the clock as an argument so tests can drive it with
    fake time (app timers never fire in --background)."""
    if not _state.enabled or _state.source != 'SEAM' \
            or _state.mode not in ('ISLANDS', 'COMBINED') \
            or not _state.debounce.pending:
        _state.debounce.reset()
        return None
    if not _state.debounce.try_fire(now):
        return LIVE_POLL_S
    # Quiet period over: one cheap checksum decides whether anything the
    # overlay depends on actually changed (seam flags, topology counts,
    # vertex positions; in COMBINED also the UV layer the checker
    # samples). No-op events (mode switches, re-marking an existing
    # seam, selection-only updates) stop here.
    obj = None
    if _state.object_name is not None:
        obj = bpy.data.objects.get(_state.object_name)
    if obj is None or obj.type != 'MESH':
        return None
    try:
        checksum = seam_state_checksum(
            obj, include_uvs=(_state.mode == 'COMBINED'))
    except Exception:
        print("[uv_island_overlay] seam checksum failed:")
        traceback.print_exc()
        return None
    if checksum != _state.seam_checksum:
        refresh(None)
    return LIVE_POLL_S if _state.debounce.pending else None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _resolve_active_mesh(context):
    """Active mesh object, robust to sparse contexts (e.g. the property
    update firing from a popover): falls back from the given context to
    its view layer, then to bpy.context."""
    for ctx in (context, bpy.context):
        if ctx is None:
            continue
        obj = getattr(ctx, "active_object", None)
        if obj is None:
            view_layer = getattr(ctx, "view_layer", None)
            if view_layer is not None:
                obj = view_layer.objects.active
        if obj is not None and obj.type == 'MESH':
            return obj
    return None


def enable(context):
    """Turn the overlay on for the active mesh object and compute its
    island data immediately. Safe in background mode."""
    obj = _resolve_active_mesh(context)
    if obj is None:
        return False
    _state.object_name = obj.name
    _state.enabled = True
    _state.last_draw_error = None
    wm = getattr(bpy.context, "window_manager", None)
    source = getattr(wm, "uv_island_overlay_source", None)
    if source in {'UV', 'SEAM'}:
        _state.source = source
    mode = getattr(wm, "uv_island_overlay_mode", None)
    if mode in {'ISLANDS', 'DENSITY', 'COMBINED'}:
        _state.mode = mode
    refresh(context)
    if _state.handle is None:
        try:
            _state.handle = bpy.types.SpaceView3D.draw_handler_add(
                _draw, (), 'WINDOW', 'POST_VIEW')
        except Exception:
            # No viewport (background mode): stay enabled logically so the
            # toggle round-trips; there is simply nothing to draw.
            _state.handle = None
    _tag_redraw_view3d()
    return True


def disable():
    if _state.handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_state.handle, 'WINDOW')
        except Exception:
            pass
        _state.handle = None
    _stop_live_timer()
    _state.debounce.reset()
    _state.seam_checksum = None
    _remove_scratch_mesh()
    _state.enabled = False
    _state.dirty = True
    _state.object_name = None
    _state.island_count = 0
    _state.coords = None
    _state.colors = None
    _state.uvs = None
    _state.densities = None
    _state.median_density = None
    _state.no_uvs = False
    _state.mesh_orientation = 0
    _state.batch = None
    _state.last_draw_error = None
    _tag_redraw_view3d()


def refresh(context):
    """Recompute islands + geometry for the tracked object right now
    (pure work only) and invalidate the gpu batch."""
    obj = None
    if _state.object_name is not None:
        obj = bpy.data.objects.get(_state.object_name)
    if obj is None or obj.type != 'MESH':
        obj = context.active_object if context is not None else None
        if obj is None or obj.type != 'MESH':
            _state.island_count = 0
            _state.coords = None
            _state.colors = None
            _state.uvs = None
            _state.densities = None
            _state.median_density = None
            _state.no_uvs = False
            _state.mesh_orientation = 0
            _state.batch = None
            return False
        _state.object_name = obj.name
    _state.last_draw_error = None    # give drawing a fresh chance to log
    try:
        if _state.mode == 'DENSITY':
            coords, colors, uvs, count, densities, median, no_uvs = \
                _build_density(obj)
            checksum = None
        elif _state.mode == 'COMBINED':
            # checksum is non-None only for the SEAM island source
            # (where the live debounce pipeline gates on it).
            (coords, colors, uvs, count, densities, median, no_uvs,
             checksum) = _build_combined(obj, _state.source)
        else:
            coords, colors, count, checksum = _build(obj, _state.source)
            uvs, densities, median, no_uvs = None, None, None, False
        mesh_orientation = _mesh_orientation(obj)
    except Exception:
        # refresh() is user/handler triggered (never per-frame), so a
        # failure here can afford to be loud every time.
        print("[uv_island_overlay] geometry rebuild failed for %r:"
              % _state.object_name)
        traceback.print_exc()
        coords, colors, count, checksum = None, None, 0, None
        uvs, densities, median, no_uvs = None, None, None, False
        mesh_orientation = 0
    _state.coords = coords
    _state.colors = colors
    _state.uvs = uvs
    _state.densities = densities
    _state.median_density = median
    _state.no_uvs = no_uvs
    _state.mesh_orientation = mesh_orientation
    _state.island_count = count
    _state.seam_checksum = checksum
    _state.batch = None      # rebuild from the new data at next draw
    _state.dirty = False
    _tag_redraw_view3d()
    return True


def _tag_redraw_view3d():
    wm = bpy.context.window_manager
    if wm is None:
        return
    try:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Drawing (viewport only; every gpu call guarded)
# ---------------------------------------------------------------------------

def _shader_create_info():
    """GPUShaderCreateInfo descriptor for the depth-bias shader.

    Descriptor construction/population is pure bookkeeping and works in
    --background (probed on 5.1.2) — only gpu.shader.create_from_info
    actually touches the GPU and raises SystemError headless — so the
    test suite can build (and structurally check) this without a GPU.
    Attributes match the batch content: "pos" (vec3) + per-vertex
    "color" (vec4, flat per face because all three corners of a triangle
    carry the same value). The FLOAT "overlay_opacity" push constant
    (v1.3.1) supplies the fragment alpha — same probed-on-5.1.2
    mechanism as the density shader's checker_res, so opacity changes
    are a uniform update, never a rebuild.
    """
    iface = gpu.types.GPUStageInterfaceInfo("uv_island_overlay_iface")
    iface.smooth('VEC4', "finalColor")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', "ModelViewProjectionMatrix")
    info.push_constant('FLOAT', "overlay_opacity")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC4', "color")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(VERT_SHADER_SRC)
    info.fragment_source(FRAG_SHADER_SRC)
    return info


def _create_shader():
    """Compile the depth-bias shader. GPU work — draw time only, behind
    the error latch: headless this raises SystemError, and a GLSL error
    in the GUI surfaces once via the loud "Draw failed" row instead of
    silently. The legacy raw-GLSL constructor is NOT an option here:
    probed on 5.1.2, gpu.types.GPUShader(vert, frag) raises TypeError
    ("cannot create 'GPUShader' instances"); create_from_info is the
    supported API."""
    return gpu.shader.create_from_info(_shader_create_info())


def _density_shader_create_info():
    """Create-info descriptor for the DENSITY checker shader (v1.3.0):
    the ISLANDS descriptor plus a per-loop vec2 "uv" attribute and a
    FLOAT "checker_res" push constant (probed on 5.1.2: FLOAT push
    constants are accepted by GPUShaderCreateInfo, so checker-size
    changes are a uniform update, never a rebuild). Builds headless,
    same as _shader_create_info."""
    iface = gpu.types.GPUStageInterfaceInfo("uv_island_density_iface")
    iface.smooth('VEC4', "finalColor")
    iface.smooth('VEC2', "uvInterp")
    info = gpu.types.GPUShaderCreateInfo()
    info.push_constant('MAT4', "ModelViewProjectionMatrix")
    info.push_constant('FLOAT', "checker_res")
    info.push_constant('FLOAT', "overlay_opacity")
    info.vertex_in(0, 'VEC3', "pos")
    info.vertex_in(1, 'VEC4', "color")
    info.vertex_in(2, 'VEC2', "uv")
    info.vertex_out(iface)
    info.fragment_out(0, 'VEC4', "fragColor")
    info.vertex_source(DENSITY_VERT_SHADER_SRC)
    info.fragment_source(DENSITY_FRAG_SHADER_SRC)
    return info


def _create_density_shader():
    """Compile the DENSITY checker shader — GPU work, draw time only,
    behind the error latch (same contract as _create_shader)."""
    return gpu.shader.create_from_info(_density_shader_create_info())


def _face_culling_mode(matrix_world, mesh_orientation=1):
    """Return the culling polarity for an object's effective winding.

    Raster face orientation is determined after the object-to-world
    transform.  A negative linear determinant reverses the triangle soup's
    winding, so culling its FRONT faces preserves the same visible,
    normal-facing surface that BACK culling selects for an ordinary
    transform.

    ``mesh_orientation`` composes a cached mesh-space orientation with the
    transform: +1 is the usual winding, -1 is globally reversed, and 0
    requests two-sided drawing for geometry where no reliable global
    orientation exists (for example an open or mixed-winding mesh).
    """
    if mesh_orientation == 0:
        return 'NONE'
    determinant = matrix_world.to_3x3().determinant()
    if abs(determinant) <= 1.0e-12:
        return 'NONE'
    return 'FRONT' if determinant * mesh_orientation < 0.0 else 'BACK'


@contextmanager
def _gpu_state_restored():
    """Save global gpu state, run the draw block, ALWAYS restore.

    Draw callbacks share global GPU state with all of Blender's own
    drawing; any state set here and not restored leaks into other
    editors' subsequent draws (a past leak of depth_test LESS_EQUAL from
    this very overlay corrupted other overlays). The finally-clause
    guarantees restoration even when the wrapped block raises mid-draw —
    the callback's try/except guard alone would silently skip restore
    calls placed after the draw.

    Getters probed on 5.1.2: blend_get and depth_test_get exist, so the
    actual prior values are restored. face_culling_get does NOT exist;
    Blender's default ('NONE') is restored instead.
    """
    prior_blend = gpu.state.blend_get()
    prior_depth_test = gpu.state.depth_test_get()
    try:
        yield
    finally:
        gpu.state.blend_set(prior_blend)
        gpu.state.depth_test_set(prior_depth_test)
        # No face_culling_get on 5.1.2: restore the documented default.
        gpu.state.face_culling_set('NONE')


def _draw():
    if not _state.enabled or _state.object_name is None:
        return
    if _state.last_draw_error is not None:
        # A previous frame already failed and logged; don't retry every
        # frame (refresh()/re-enable clears the latch and tries again).
        return
    try:
        obj = bpy.data.objects.get(_state.object_name)
        if obj is None or obj.type != 'MESH':
            return

        if _state.dirty:
            if _state.mode == 'DENSITY':
                # DENSITY reads via bmesh (Edit Mode) or Mesh arrays
                # (Object Mode) — no datablock snapshot, so rebuilding
                # here is as safe as the UV path below.
                (_state.coords, _state.colors, _state.uvs,
                 _state.island_count, _state.densities,
                 _state.median_density, _state.no_uvs) = \
                    _build_density(obj)
                _state.mesh_orientation = _mesh_orientation(obj)
                _state.batch = None
                _state.dirty = False
            elif _state.mode == 'COMBINED' and _state.source != 'SEAM':
                # COMBINED with the UV island source delegates to the
                # same snapshot-free reads as DENSITY — safe here. (The
                # returned checksum is None for this source.)
                (_state.coords, _state.colors, _state.uvs,
                 _state.island_count, _state.densities,
                 _state.median_density, _state.no_uvs,
                 _state.seam_checksum) = \
                    _build_combined(obj, _state.source)
                _state.mesh_orientation = _mesh_orientation(obj)
                _state.batch = None
                _state.dirty = False
            elif _state.source == 'SEAM':
                # SEAM rebuilds (ISLANDS or COMBINED) snapshot into a
                # datablock, which must not happen inside a draw
                # callback — hand off to the live timer (main loop) and
                # keep drawing the cached batch. For COMBINED this is
                # also what keeps the invalidation paths converged: the
                # debounced pipeline is the ONLY rebuild driver there,
                # so a draw-path rebuild can never race it.
                _state.dirty = False
                note_activity()
            else:
                # Something (depsgraph hook, mode change) invalidated the
                # cached geometry: rebuild once, not per-frame.
                _state.coords, _state.colors, _state.island_count = \
                    build_geometry(obj, _state.source)
                _state.mesh_orientation = _mesh_orientation(obj)
                _state.batch = None
                _state.dirty = False

        # NOTE: explicit None/len test — coords may be a numpy array,
        # whose truth value is ambiguous. A checker-mode (DENSITY or
        # COMBINED) mesh with no UV layer lands here too (coords is
        # None): draw nothing, no error latch — the panel shows the
        # "Mesh has no UVs" hint.
        if _state.coords is None or len(_state.coords) == 0:
            return

        if _state.mode in ('DENSITY', 'COMBINED'):
            if _state.density_shader is None:
                # Same lazy-compile-behind-the-latch contract as the
                # ISLANDS shader below. COMBINED reuses the DENSITY
                # shader UNCHANGED: its fragment stage already
                # multiplies the per-vertex attribute color by the
                # checker parity — COMBINED just bakes island identity
                # colors into that attribute instead of deviation
                # tints. No third shader variant exists.
                _state.density_shader = _create_density_shader()
            shader = _state.density_shader
        else:
            if _state.shader is None:
                # Custom depth-bias shader (v1.2.0): geometry sits
                # exactly ON the surface, the vertex shader pulls it
                # toward the viewer in clip space (see CLIP_DEPTH_BIAS).
                # Compiled lazily here so a failure (headless
                # SystemError, or a GLSL error in the GUI) lands in the
                # outer guard's loud latch.
                _state.shader = _create_shader()
            shader = _state.shader

        if _state.batch is None:
            from gpu_extras.batch import batch_for_shader
            if _state.mode in ('DENSITY', 'COMBINED'):
                _state.batch = batch_for_shader(
                    shader, 'TRIS',
                    {"pos": _state.coords, "color": _state.colors,
                     "uv": _state.uvs})
            else:
                _state.batch = batch_for_shader(
                    shader, 'TRIS',
                    {"pos": _state.coords, "color": _state.colors})

        # All state mutations live inside the guard: the priors are
        # captured first and restored in its finally-clause, so even an
        # exception mid-draw cannot leak blend/depth/culling state into
        # Blender's own subsequent drawing.
        with _gpu_state_restored():
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('LESS_EQUAL')
            # Face culling (v1.3.1, both modes): the soup's
            # triangle winding follows the mesh's loop order. The cached
            # mesh-space orientation and live world-transform determinant
            # select which polarity keeps the camera-facing shell side.
            # Without it, back faces of open/thin geometry depth-pass
            # their own front faces (the shader's viewer-ward bias) and
            # bleed through, making the overlay look translucent even
            # at high opacity. Geometry without a reliable global winding
            # falls back to 'NONE' so its preview remains complete. The guard's
            # finally-clause restores the documented default 'NONE'
            # (no face_culling_get exists on 5.1.2).
            gpu.state.face_culling_set(
                _face_culling_mode(obj.matrix_world,
                                   _state.mesh_orientation))
            with gpu.matrix.push_pop():
                gpu.matrix.multiply_matrix(obj.matrix_world)
                shader.bind()
                # Explicit MVP from gpu.matrix state (projection @
                # view @ object world, thanks to the multiply above).
                # The push-constant name also matches Blender's builtin
                # matrix binding, so batch.draw would feed the same
                # value; setting it explicitly removes the reliance on
                # that implicit behavior.
                mvp = (gpu.matrix.get_projection_matrix()
                       @ gpu.matrix.get_model_view_matrix())
                shader.uniform_float(
                    "ModelViewProjectionMatrix", mvp)
                # Opacity (and in the checker modes the checker
                # resolution): read fresh from the properties every
                # draw — both are push constants, so changing them
                # needs nothing but a redraw (dragging the sliders is
                # live, zero rebuild). COMBINED deliberately shares the
                # Checker Opacity property with DENSITY — the combined
                # overlay is checker-like paint, so the near-opaque
                # default is right; Tint Opacity stays ISLANDS-only.
                wm = bpy.context.window_manager
                if _state.mode in ('DENSITY', 'COMBINED'):
                    res = float(getattr(wm,
                                        "uv_island_overlay_checker_size",
                                        DEFAULT_CHECKER_SIZE))
                    shader.uniform_float("checker_res", res)
                    opacity = float(getattr(
                        wm, "uv_island_overlay_density_opacity",
                        DEFAULT_DENSITY_OPACITY))
                else:
                    opacity = float(getattr(
                        wm, "uv_island_overlay_opacity", ALPHA))
                shader.uniform_float("overlay_opacity", opacity)
                _state.batch.draw(shader)
    except Exception:
        # Never let a draw-time error take down the viewport callback —
        # but never hide it either: latch it, print the traceback ONCE,
        # and let the UI panels show an error row (background test runs
        # latch quietly; there is no viewport to fix there). GPU state
        # needs no cleanup here: _gpu_state_restored() already restored
        # it on the way out.
        _state.last_draw_error = traceback.format_exc()
        if not bpy.app.background:
            print("[uv_island_overlay] viewport draw failed; overlay "
                  "suspended for %r until Refresh. Traceback:"
                  % _state.object_name)
            print(_state.last_draw_error)
