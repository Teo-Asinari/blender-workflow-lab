# SPDX-License-Identifier: GPL-2.0-or-later
"""Headless registration + operator lifecycle test (run inside
`blender --background --python`).

Loads the add-on from source, registers it, toggles the overlay on a real
mesh (gpu drawing must no-op gracefully in background), refreshes,
toggles off, unregisters cleanly and survives a re-register cycle.

Prints REGISTER_TESTS_PASSED on success.
"""

import ast
import inspect
import os
import sys
import traceback

import bpy

_ADDON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ADDONS_ROOT = os.path.dirname(_ADDON_DIR)
if _ADDONS_ROOT not in sys.path:
    sys.path.insert(0, _ADDONS_ROOT)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  ok  %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def gpu_state_guard_audit(module, guard_name="_gpu_state_restored"):
    """(guard_spans, offenders) for a drawing module.

    GPU state set/get raises SystemError in --background (probed on
    5.1.2), so restoration cannot be tested behaviorally headless.
    Instead this audits the module's AST: every ``gpu.state.*_set(...)``
    call must sit (a) inside a ``with <guard_name>(...):`` block, (b)
    inside the guard helper itself, or (c) inside a helper function
    whose every call site sits inside a guarded block.
    """
    tree = ast.parse(inspect.getsource(module))

    def span(node):
        return (node.lineno, node.end_lineno)

    guard_spans = []   # `with _gpu_state_restored(...)` blocks
    allowed = []       # spans where raw gpu.state sets are permitted
    funcs = {}         # function name -> span
    call_sites = {}    # callee name -> [line, ...]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            funcs[node.name] = span(node)
            if node.name == guard_name:
                allowed.append(span(node))
        elif isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                f = ctx.func if isinstance(ctx, ast.Call) else None
                name = getattr(f, "id", None) or getattr(f, "attr", None)
                if name == guard_name:
                    guard_spans.append(span(node))
                    allowed.append(span(node))
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            call_sites.setdefault(name, []).append(node.lineno)

    def is_allowed(line):
        return any(a <= line <= b for a, b in allowed)

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr.endswith("_set")
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "state"
                and getattr(node.func.value.value, "id", None) == "gpu"):
            continue
        line = node.lineno
        if is_allowed(line):
            continue
        # Innermost enclosing function; OK if all ITS call sites are
        # inside guarded blocks.
        owner, owner_size = None, None
        for fname, (a, b) in funcs.items():
            if a <= line <= b and fname != guard_name:
                if owner is None or (b - a) < owner_size:
                    owner, owner_size = fname, b - a
        sites = call_sites.get(owner, []) if owner else []
        if owner and sites and all(is_allowed(ln) for ln in sites):
            continue
        offenders.append("%s at line %d" % (node.func.attr, line))
    return guard_spans, offenders


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    import uv_island_overlay
    from uv_island_overlay import overlay, islands  # noqa: F401

    # Purity check: islands.py / density.py must not import bpy or gpu.
    src = open(os.path.join(_ADDON_DIR, "islands.py")).read()
    check("islands.py imports neither bpy nor gpu",
          "import bpy" not in src and "import gpu" not in src)
    src_d = open(os.path.join(_ADDON_DIR, "density.py")).read()
    check("density.py imports neither bpy nor gpu",
          "import bpy" not in src_d and "import gpu" not in src_d)

    # --- register -----------------------------------------------------------
    uv_island_overlay.register()
    check("bl_info present",
          isinstance(uv_island_overlay.bl_info, dict)
          and uv_island_overlay.bl_info.get("name") == "UV Island Overlay")
    check("operator uv.island_overlay_toggle registered",
          hasattr(bpy.ops.uv, "island_overlay_toggle")
          and bpy.ops.uv.island_overlay_toggle.idname_py()
          == "uv.island_overlay_toggle")
    check("operator uv.island_overlay_refresh registered",
          hasattr(bpy.ops.uv, "island_overlay_refresh"))
    check("WindowManager property registered",
          hasattr(bpy.context.window_manager, "uv_island_overlay"))
    src_prop = bpy.context.window_manager.bl_rna.properties.get(
        "uv_island_overlay_source")
    check("island-source enum registered with SEAM+UV items",
          src_prop is not None
          and {i.identifier for i in src_prop.enum_items} == {'SEAM', 'UV'})
    check("island-source default is SEAM (primary workflow: seam marking)",
          src_prop is not None and src_prop.default == 'SEAM')
    check("Overlays popover panel exists on this Blender (5.1 probe)",
          hasattr(bpy.types, "VIEW3D_PT_overlay"))
    check("depsgraph handler installed",
          any(h.__name__ == "_on_depsgraph_update"
              for h in bpy.app.handlers.depsgraph_update_post))

    # v1.4.0 flipped the default display mode to COMBINED; the legacy
    # sections below exercise the classic ISLANDS behavior, so pin it
    # here (test_combined.py covers the COMBINED default and behavior;
    # the sections below that need other modes set them explicitly).
    bpy.context.window_manager.uv_island_overlay_mode = 'ISLANDS'

    # --- build a mesh with 2 islands -----------------------------------------
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4,
                                    size=2.0)
    obj = bpy.context.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    import bmesh
    bm = bmesh.from_edit_mesh(obj.data)
    for e in bm.edges:
        if all(abs(v.co.x) < 1e-6 for v in e.verts):
            e.seam = True
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.unwrap(method='ANGLE_BASED', margin=0.02)

    # --- toggle ON in edit mode (primary use case) ----------------------------
    check("toggle poll passes", bpy.ops.uv.island_overlay_toggle.poll())
    result = bpy.ops.uv.island_overlay_toggle()
    check("toggle-on returned FINISHED", result == {'FINISHED'},
          "got %r" % (result,))
    check("overlay reports enabled", overlay.is_enabled())
    check("property flipped true",
          bpy.context.window_manager.uv_island_overlay is True)
    check("island count computed on toggle-on (2 islands)",
          overlay.island_count() == 2,
          "got %d" % overlay.island_count())
    check("overlay tracks the active object",
          overlay.tracked_object_name() == obj.name)

    # Geometry cache built headlessly (pure path) even though gpu batch
    # creation is impossible in --background.
    check("geometry extracted headlessly",
          overlay._state.coords is not None
          and len(overlay._state.coords) > 0
          and len(overlay._state.coords) == len(overlay._state.colors))

    # Draw callback must no-op gracefully with no GPU context.
    try:
        overlay._draw()
        drew = True
    except Exception:
        drew = False
    check("draw callback no-ops headlessly", drew)

    # --- refresh operator ------------------------------------------------------
    check("refresh poll passes while enabled",
          bpy.ops.uv.island_overlay_refresh.poll())
    result = bpy.ops.uv.island_overlay_refresh()
    check("refresh returned FINISHED", result == {'FINISHED'})
    check("island count stable after refresh", overlay.island_count() == 2)

    # --- object mode also works -------------------------------------------------
    bpy.ops.object.mode_set(mode='OBJECT')
    result = bpy.ops.uv.island_overlay_refresh()
    check("refresh works in object mode", result == {'FINISHED'}
          and overlay.island_count() == 2)

    # --- toggle OFF ---------------------------------------------------------------
    result = bpy.ops.uv.island_overlay_toggle()
    check("toggle-off returned FINISHED", result == {'FINISHED'})
    check("overlay disabled", not overlay.is_enabled())
    check("refresh poll fails while disabled",
          not bpy.ops.uv.island_overlay_refresh.poll())

    # --- direct property path (what the popover checkbox drives) ------------------
    bpy.context.window_manager.uv_island_overlay = True
    check("property=True enables overlay", overlay.is_enabled())
    bpy.context.window_manager.uv_island_overlay = False
    check("property=False disables overlay", not overlay.is_enabled())

    # --- v1.0.1 regressions ---------------------------------------------------

    # Discoverable sidebar N-panel, registered in its own tab.
    check("N-panel registered",
          hasattr(bpy.types, "VIEW3D_PT_uv_island_overlay"))
    pnl = bpy.types.VIEW3D_PT_uv_island_overlay
    check("N-panel lives in the View3D sidebar",
          pnl.bl_space_type == 'VIEW_3D' and pnl.bl_region_type == 'UI')
    check("N-panel category is 'UV Islands'",
          pnl.bl_category == "UV Islands")

    # Menu entries (F3 search only finds operators that live in menus).
    def menu_has_toggle(menu):
        try:
            return any(getattr(f, "__name__", "") == "_menu_draw"
                       for f in menu._dyn_ui_initialize())
        except Exception:
            return False
    check("View menu has the toggle entry",
          menu_has_toggle(bpy.types.VIEW3D_MT_view))
    check("Edit Mode UV menu has the toggle entry",
          menu_has_toggle(bpy.types.VIEW3D_MT_uv_map))
    try:
        popover_appended = any(
            getattr(f, "__name__", "") == "_overlay_popover_draw"
            for f in bpy.types.VIEW3D_PT_overlay._dyn_ui_initialize())
    except Exception:
        popover_appended = False
    check("Overlays popover draw appended", popover_appended)

    # Enable attempt with NO active mesh (popover-like sparse context):
    # must fail AND revert the checkbox. Blender 5.0 removed idprop
    # access to bpy.props storage, so the old self["..."] = False revert
    # silently stopped working — this locks in the real-assignment fix.
    bpy.context.view_layer.objects.active = None
    bpy.context.window_manager.uv_island_overlay = True
    check("enable without active mesh stays disabled",
          not overlay.is_enabled())
    check("checkbox property reverts to False (checkbox never lies)",
          bpy.context.window_manager.uv_island_overlay is False)
    bpy.context.view_layer.objects.active = obj

    # Dirty-flag lifecycle across a re-unwrap while enabled, and the
    # loud-once draw-error latch (in background the gpu section MUST
    # fail; it must be recorded once and cleared by refresh). This is
    # UV-mode behavior: SEAM mode routes updates through the debounced
    # live pipeline instead (tested further down), so pin the source.
    bpy.context.window_manager.uv_island_overlay = True
    check("re-enabled on the grid",
          overlay.is_enabled() and overlay.island_count() == 2)
    bpy.context.window_manager.uv_island_overlay_source = 'UV'
    check("UV source also sees the 2 unwrapped islands",
          overlay.island_count() == 2 and overlay.active_source() == 'UV')
    check("no draw error recorded before drawing",
          overlay.last_draw_error() is None)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.02)
    check("depsgraph hook marked overlay dirty after re-unwrap",
          overlay._state.dirty)
    overlay._draw()   # pure rebuild runs before the first gpu call
    check("draw-time rebuild consumed the dirty flag",
          not overlay._state.dirty)
    check("draw-time rebuild kept geometry",
          overlay._state.coords is not None
          and len(overlay._state.coords) > 0)
    check("draw error latched loudly (background gpu failure recorded)",
          overlay.last_draw_error() is not None
          and "background" in overlay.last_draw_error())
    err_before = overlay.last_draw_error()
    overlay._draw()
    check("second draw holds the latch (no per-frame spam)",
          overlay.last_draw_error() is err_before)
    result = bpy.ops.uv.island_overlay_refresh()
    check("refresh clears the draw-error latch",
          result == {'FINISHED'} and overlay.last_draw_error() is None)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.window_manager.uv_island_overlay = False

    # --- v1.1.0: seam-predicted islands + live refresh --------------------------

    wm = bpy.context.window_manager
    wm.uv_island_overlay_source = 'SEAM'

    # Cube with stale UVs: the primitive auto-generates a single-chart UV
    # layout; marking every edge as a seam does NOT touch the UVs. SEAM
    # mode must predict the 6 post-unwrap islands; UV mode must keep
    # reporting the stale single island.
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.active_object
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.mark_seam()

    wm.uv_island_overlay = True
    check("SEAM mode predicts 6 islands on all-seam cube (no unwrap!)",
          overlay.is_enabled() and overlay.island_count() == 6,
          "got %d" % overlay.island_count())
    check("overlay reports SEAM as the active source",
          overlay.active_source() == 'SEAM')
    check("refresh stored a SEAM-state checksum",
          overlay._state.seam_checksum is not None)
    coords_seam = overlay._state.coords

    wm.uv_island_overlay_source = 'UV'
    check("enum switch to UV invalidates + rebuilds: stale UVs, 1 island",
          overlay.island_count() == 1,
          "got %d" % overlay.island_count())
    check("enum switch replaced the cached geometry",
          overlay._state.coords is not coords_seam)
    wm.uv_island_overlay_source = 'SEAM'
    check("enum switch back to SEAM rebuilds: 6 predicted islands again",
          overlay.island_count() == 6)

    # Checksum: detects seam add/remove and vertex moves, ignores no-ops.
    ck0 = overlay.seam_state_checksum(cube)
    check("checksum is deterministic",
          overlay.seam_state_checksum(cube) == ck0)
    bpy.ops.mesh.select_all(action='DESELECT')
    check("selection-only change leaves the checksum alone",
          overlay.seam_state_checksum(cube) == ck0)
    bm = bmesh.from_edit_mesh(cube.data)
    bm.edges.ensure_lookup_table()
    bm.edges[0].seam = False
    bmesh.update_edit_mesh(cube.data)
    ck1 = overlay.seam_state_checksum(cube)
    check("removing a seam changes the checksum", ck1 != ck0)
    bm.edges[0].seam = True
    bmesh.update_edit_mesh(cube.data)
    check("restoring the seam restores the checksum",
          overlay.seam_state_checksum(cube) == ck0)
    bm.edges[0].seam = True     # "mark" an already-marked seam
    bmesh.update_edit_mesh(cube.data)
    check("re-marking an existing seam is a checksum no-op",
          overlay.seam_state_checksum(cube) == ck0)
    bm.verts.ensure_lookup_table()
    bm.verts[0].co.x += 0.25
    bmesh.update_edit_mesh(cube.data)
    check("moving a vertex changes the checksum",
          overlay.seam_state_checksum(cube) != ck0)
    bm.verts[0].co.x -= 0.25
    bmesh.update_edit_mesh(cube.data)

    # Depsgraph routing: in SEAM mode geometry updates feed the debounce,
    # never the dirty/draw path (rebuilds must not happen in a draw
    # callback because the SEAM path snapshots into a datablock).
    overlay.refresh(bpy.context)             # clean baseline
    overlay._state.debounce.reset()
    check("baseline is 6 islands", overlay.island_count() == 6)
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.mark_seam(clear=True)       # a real seam change
    check("geometry update routed to the debounce in SEAM mode",
          overlay._state.debounce.pending)
    check("SEAM mode never marks the draw path dirty",
          not overlay._state.dirty)
    check("live timer registered by activity",
          bpy.app.timers.is_registered(overlay._live_timer_cb))
    check("overlay NOT yet recomputed (debounced)",
          overlay.island_count() == 6)

    # Drive the debounce with a fake clock (app timers never fire in
    # --background — probed; the timer is only a thin driver of
    # _live_tick, which takes the clock as an argument).
    coords_before = overlay._state.coords
    t0 = 1000.0
    overlay._state.debounce.note_change(t0)          # burst...
    overlay._state.debounce.note_change(t0 + 0.10)
    overlay._state.debounce.note_change(t0 + 0.20)
    r = overlay._live_tick(t0 + 0.35)   # 0.15s of quiet: not yet
    check("tick during burst keeps polling without rebuilding",
          r == overlay.LIVE_POLL_S
          and overlay._state.coords is coords_before)
    r = overlay._live_tick(t0 + 0.55)   # quiet period elapsed
    check("one debounced rebuild after the burst goes quiet",
          r is None and overlay._state.coords is not coords_before)
    check("live rebuild saw the cleared seams (1 island)",
          overlay.island_count() == 1,
          "got %d" % overlay.island_count())

    # A no-op burst (nothing actually changed) is stopped by the checksum.
    coords_after = overlay._state.coords
    overlay.note_activity(now=2000.0)
    r = overlay._live_tick(2000.5)
    check("no-op activity fires the debounce but skips the rebuild",
          r is None and overlay._state.coords is coords_after
          and overlay.island_count() == 1)

    # Manual Refresh stays available as the escape hatch in SEAM mode.
    result = bpy.ops.uv.island_overlay_refresh()
    check("manual refresh works in SEAM mode",
          result == {'FINISHED'} and overlay.island_count() == 1)

    # Hidden faces are dropped from the soup by the fast path too.
    soup_before = len(overlay._state.coords)
    bm = bmesh.from_edit_mesh(cube.data)
    bm.faces.ensure_lookup_table()
    bm.faces[0].hide = True
    bmesh.update_edit_mesh(cube.data)
    overlay.refresh(bpy.context)
    check("hidden face dropped from the fast-path soup (2 tris = 6 verts)",
          len(overlay._state.coords) == soup_before - 6,
          "before %d after %d" % (soup_before,
                                  len(overlay._state.coords)))

    bpy.ops.object.mode_set(mode='OBJECT')
    wm.uv_island_overlay = False
    check("live timer stopped on disable",
          not bpy.app.timers.is_registered(overlay._live_timer_cb))
    check("snapshot scratch mesh removed on disable",
          bpy.data.meshes.get(overlay._SCRATCH_NAME) is None)

    # --- v1.1.1: GPU state hygiene ---------------------------------------------
    # gpu.state setters AND getters raise SystemError in --background
    # (probed on 5.1.2), so restoration is audited structurally: every
    # gpu.state.*_set in overlay.py must route through the
    # _gpu_state_restored guard, which restores in a finally-clause
    # (an exception mid-draw must not leak blend/depth/culling into
    # Blender's own drawing — the old depth_test LESS_EQUAL leak).
    guard_spans, offenders = gpu_state_guard_audit(overlay)
    check("every gpu.state set in overlay.py is guard-covered",
          not offenders, "unguarded: %s" % ", ".join(offenders))
    check("the draw callback uses the state guard",
          len(guard_spans) >= 1
          and "_gpu_state_restored" in inspect.getsource(overlay._draw))
    check("state guard restores in a finally-clause",
          "finally:" in inspect.getsource(overlay._gpu_state_restored))
    import gpu
    check("guard-relied getters exist on this build",
          all(hasattr(gpu.state, g)
              for g in ("blend_get", "depth_test_get")))
    check("probe: gpu.state.face_culling_get still absent on this build "
          "(guard restores the default 'NONE' instead)",
          not hasattr(gpu.state, "face_culling_get"))
    check("version bumped for the state-hygiene fix",
          uv_island_overlay.bl_info.get("version", (0,))[:3] >= (1, 1, 1))
    # Fail-closed: headless, the guard must raise while READING priors —
    # before its body (or any state mutation) can run.
    body_ran = False
    try:
        with overlay._gpu_state_restored():
            body_ran = True
        check("gpu state guard usable (GPU available)", True)
    except SystemError:
        check("gpu state guard fails closed in background "
              "(reads priors before mutating anything)", not body_ran)

    # --- v1.2.0: crack-free overlay (zero geometric offset + shader bias) -------
    # The old per-face-normal offset pushed adjacent faces apart at every
    # non-flat edge (visible gaps). Now the soup positions must be
    # BIT-IDENTICAL to the mesh's vertex coordinates, and z-fighting is
    # handled by a clip-space depth bias in a custom create-info shader.
    import numpy as np

    check("version bumped for the crack-free overlay",
          uv_island_overlay.bl_info.get("version", (0,))[:3] >= (1, 2, 0))
    check("normal-offset constants removed",
          not hasattr(overlay, "NORMAL_OFFSET_FACTOR")
          and not hasattr(overlay, "NORMAL_OFFSET_MIN"))

    # Shader sources: module-level constants, structurally sane (they
    # cannot be compiled headless — create_from_info raises SystemError
    # in --background, probed on 5.1.2 — so this is the strongest
    # headless check available; the GUI surfaces a real GLSL error via
    # the draw-error latch tested above).
    vs = getattr(overlay, "VERT_SHADER_SRC", None)
    fs = getattr(overlay, "FRAG_SHADER_SRC", None)
    check("shader sources are nonempty module-level strings",
          isinstance(vs, str) and vs.strip()
          and isinstance(fs, str) and fs.strip())
    check("vertex shader transforms by ModelViewProjectionMatrix",
          vs is not None and "ModelViewProjectionMatrix" in vs
          and "gl_Position" in vs)
    check("vertex shader has the w-scaled depth-bias term",
          vs is not None and "gl_Position.z -=" in vs
          and "* gl_Position.w" in vs
          and ("%r" % overlay.CLIP_DEPTH_BIAS) in vs)
    check("depth bias is small and pulls toward the viewer",
          0.0 < overlay.CLIP_DEPTH_BIAS <= 1e-3)
    check("vertex shader passes per-vertex color through",
          vs is not None and "finalColor = color" in vs)
    check("fragment shader writes the interpolated color at the "
          "opacity uniform (v1.3.1: vertex alpha ignored)",
          fs is not None
          and "vec4(finalColor.rgb, overlay_opacity)" in fs)

    # The create-info DESCRIPTOR must build headless (only compilation
    # needs a GPU), and must declare the attributes the batch supplies.
    try:
        info = overlay._shader_create_info()
        info_ok = info is not None
    except Exception:
        info_ok = False
    check("shader create-info descriptor builds headless", info_ok)
    ci_src = inspect.getsource(overlay._shader_create_info)
    check("create-info declares pos + color vertex attributes",
          "'VEC3', \"pos\"" in ci_src and "'VEC4', \"color\"" in ci_src)
    check("create-info declares the MVP push constant",
          "push_constant('MAT4', \"ModelViewProjectionMatrix\")" in ci_src)
    # Compilation itself must stay draw-time-only (headless it raises —
    # exactly what routes GUI GLSL errors into the loud latch).
    try:
        overlay._create_shader()
        compiled_headless = True
    except SystemError:
        compiled_headless = False
    check("shader compilation is impossible headless (stays lazy, "
          "draw-time, latch-guarded)", not compiled_headless)

    # Geometry: fast SEAM path — soup positions bit-equal mesh coords.
    bpy.context.view_layer.objects.active = cube
    cube.select_set(True)
    wm.uv_island_overlay_source = 'SEAM'
    wm.uv_island_overlay = True
    check("re-enabled on the cube for geometry checks",
          overlay.is_enabled()
          and overlay.tracked_object_name() == cube.name)
    me = cube.data
    vco = np.empty(len(me.vertices) * 3, dtype=np.float32)
    me.vertices.foreach_get("co", vco)
    vco = vco.reshape(-1, 3)
    tris = me.loop_triangles
    tv = np.empty(len(tris) * 3, dtype=np.int32)
    tris.foreach_get("vertices", tv)
    tp = np.empty(len(tris), dtype=np.int32)
    tris.foreach_get("polygon_index", tp)
    hide = np.empty(len(me.polygons), dtype=bool)
    me.polygons.foreach_get("hide", hide)
    tv = tv.reshape(-1, 3)[~hide[tp]]
    expected = vco[tv.ravel()]
    got = np.asarray(overlay._state.coords, dtype=np.float32)
    check("fast-path soup positions are BIT-identical to mesh coords "
          "(no offset)", got.shape == expected.shape
          and np.array_equal(got, expected),
          "shapes %r vs %r" % (got.shape, expected.shape))
    check("fast-path colors still one RGBA per soup vertex",
          len(overlay._state.colors) == len(overlay._state.coords))

    # Geometry: bmesh path (UV source) — every soup vertex must be an
    # exact mesh vertex coordinate (no displacement off the surface).
    wm.uv_island_overlay_source = 'UV'
    check("UV-source rebuild produced geometry",
          overlay._state.coords is not None
          and len(overlay._state.coords) > 0)
    vert_set = {tuple(v) for v in vco}
    soup = [tuple(np.float32(x) for x in c) for c in overlay._state.coords]
    check("bmesh-path soup positions are exact mesh vertex coords",
          all(c in vert_set for c in soup))
    wm.uv_island_overlay_source = 'SEAM'
    wm.uv_island_overlay = False

    # --- v1.3.0: texel-density checkerboard mode --------------------------------

    check("version bumped for the density mode",
          uv_island_overlay.bl_info.get("version", (0,))[:3] >= (1, 3, 0))
    mode_prop = wm.bl_rna.properties.get("uv_island_overlay_mode")
    check("display-mode enum registered with ISLANDS+DENSITY+COMBINED "
          "items",
          mode_prop is not None
          and {i.identifier for i in mode_prop.enum_items}
          == {'ISLANDS', 'DENSITY', 'COMBINED'})
    check("display-mode default is COMBINED (v1.4.0; WM properties are "
          "per-session, so the flip needs no migration)",
          mode_prop is not None and mode_prop.default == 'COMBINED')
    cs_prop = wm.bl_rna.properties.get("uv_island_overlay_checker_size")
    check("checker-size property registered, power-of-two default 32 "
          "(32 px checkers on a 1024 px texture)",
          cs_prop is not None and cs_prop.default == 32)
    ts_prop = wm.bl_rna.properties.get("uv_island_overlay_texture_size")
    check("texture-size property registered, default 1024",
          ts_prop is not None and ts_prop.default == 1024)
    tint_prop = wm.bl_rna.properties.get("uv_island_overlay_density_tint")
    check("deviation-tint bool registered, default on",
          tint_prop is not None and tint_prop.default is True)

    # ISLANDS-mode shader must be untouched by the new mode: no UV
    # attribute, no checker uniform anywhere near it (its own structural
    # checks above pin the rest).
    check("ISLANDS shader sources untouched (no uv/checker terms)",
          "uvInterp" not in vs and "checker" not in vs
          and "uvInterp" not in fs and "checker" not in fs)
    check("ISLANDS create-info declares no VEC2 attribute",
          "'VEC2'" not in ci_src)

    # Density shader: structural checks (compiling is impossible
    # headless, same as the ISLANDS shader).
    dvs = getattr(overlay, "DENSITY_VERT_SHADER_SRC", None)
    dfs = getattr(overlay, "DENSITY_FRAG_SHADER_SRC", None)
    check("density shader sources are nonempty module-level strings",
          isinstance(dvs, str) and dvs.strip()
          and isinstance(dfs, str) and dfs.strip())
    check("density vertex shader keeps the MVP transform",
          dvs is not None and "ModelViewProjectionMatrix" in dvs
          and "gl_Position" in dvs)
    check("density vertex shader keeps the w-scaled depth-bias term",
          dvs is not None and "gl_Position.z -=" in dvs
          and "* gl_Position.w" in dvs
          and ("%r" % overlay.CLIP_DEPTH_BIAS) in dvs)
    check("density vertex shader passes UV and tint color through",
          dvs is not None and "uvInterp = uv" in dvs
          and "finalColor = color" in dvs)
    check("density fragment shader computes checker parity from "
          "floor(uv * checker_res)",
          dfs is not None and "floor(uvInterp * checker_res)" in dfs
          and "mod(" in dfs)
    check("density fragment shader multiplies the per-island tint and "
          "takes alpha from the opacity uniform (v1.3.1)",
          dfs is not None and "finalColor.rgb * shade" in dfs
          and "overlay_opacity" in dfs)
    check("checker shades distinct and mid-toned (readable over light "
          "and dark viewport themes)",
          0.0 < overlay.CHECKER_DARK < overlay.CHECKER_LIGHT <= 1.0
          and overlay.CHECKER_LIGHT - overlay.CHECKER_DARK >= 0.3)

    try:
        dinfo = overlay._density_shader_create_info()
        dinfo_ok = dinfo is not None
    except Exception:
        dinfo_ok = False
    check("density create-info descriptor builds headless", dinfo_ok)
    dci_src = inspect.getsource(overlay._density_shader_create_info)
    check("density create-info declares pos + color + uv attributes",
          "'VEC3', \"pos\"" in dci_src and "'VEC4', \"color\"" in dci_src
          and "'VEC2', \"uv\"" in dci_src)
    check("probe: FLOAT push constant accepted on 5.1.2 -> checker_res "
          "is a uniform (changes never rebuild)",
          dinfo_ok and "push_constant('FLOAT', \"checker_res\")" in dci_src)
    try:
        overlay._create_density_shader()
        d_compiled = True
    except SystemError:
        d_compiled = False
    check("density shader compilation impossible headless (stays lazy, "
          "draw-time, latch-guarded)", not d_compiled)

    # Behavior: two disjoint quads, quad B's UVs exactly 2x quad A's.
    dme = bpy.data.meshes.new("DensityMesh")
    dme.from_pydata(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
         (2, 0, 0), (3, 0, 0), (3, 1, 0), (2, 1, 0)],
        [], [(0, 1, 2, 3), (4, 5, 6, 7)])
    dme.update()
    dlayer = dme.uv_layers.new(name="UVMap")
    dlayer.data.foreach_set(
        "uv", [c for uv in
               [(0.0, 0.0), (0.25, 0.0), (0.25, 0.25), (0.0, 0.25),
                (0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0)]
               for c in uv])
    dobj = bpy.data.objects.new("DensityMesh", dme)
    bpy.context.collection.objects.link(dobj)
    bpy.context.view_layer.objects.active = dobj
    dobj.select_set(True)

    wm.uv_island_overlay_mode = 'ISLANDS'
    wm.uv_island_overlay_source = 'UV'
    wm.uv_island_overlay = True
    check("enabled in ISLANDS mode on the density mesh (2 islands)",
          overlay.is_enabled() and overlay.active_mode() == 'ISLANDS'
          and overlay.island_count() == 2)
    check("ISLANDS mode carries no uv soup and no density stats",
          overlay._state.uvs is None and overlay.median_density() is None
          and not overlay.has_no_uvs())
    coords_islands = overlay._state.coords

    # Mode switch invalidates + rebuilds, exactly like the source switch.
    wm.uv_island_overlay_mode = 'DENSITY'
    check("mode switch to DENSITY invalidates and rebuilds",
          overlay.active_mode() == 'DENSITY'
          and overlay._state.coords is not coords_islands)
    check("DENSITY build produced a UV soup matching the position soup",
          overlay._state.uvs is not None
          and len(overlay._state.uvs) == len(overlay._state.coords))
    dens = overlay._state.densities
    check("2x-scaled-UV island reports exactly 2x density via the "
          "full pipeline",
          dens is not None and len(dens) == 2
          and dens[1] == 2.0 * dens[0], "got %r" % (dens,))
    check("median density exposed for the panel (0.375 -> 384 px/unit "
          "at 1024)", overlay.median_density() == 0.375,
          "got %r" % overlay.median_density())

    # Checker size is a push-constant uniform read at draw time: a
    # change must NOT invalidate or rebuild anything.
    coords_before_cs = overlay._state.coords
    wm.uv_island_overlay_checker_size = 64
    check("checker-size change is uniform-only: no dirty flag, no "
          "rebuild (probe result: FLOAT push constants supported)",
          overlay._state.coords is coords_before_cs
          and not overlay._state.dirty)
    wm.uv_island_overlay_checker_size = 32

    # Deviation-tint toggle rebuilds the baked color attribute.
    cols_before = overlay._state.colors
    wm.uv_island_overlay_density_tint = False
    ncols = np.asarray(overlay._state.colors)
    check("tint off rebuilds with all-neutral colors",
          overlay._state.colors is not cols_before
          and np.all(ncols[:, :3] == np.float32(1.0)))
    wm.uv_island_overlay_density_tint = True
    check("tint back on restores per-island deviation tints",
          not np.all(np.asarray(overlay._state.colors)[:, :3]
                     == np.float32(1.0)))

    # Depsgraph routing: in DENSITY mode geometry/UV updates take the
    # classic dirty -> rebuild-at-next-draw path, never the SEAM
    # debounce. A pure UV edit must be caught (probed on 5.1.2:
    # foreach_set UV writes report is_updated_geometry).
    overlay._state.debounce.reset()
    uvbuf = [0.0] * (len(dme.loops) * 2)
    dlayer.data.foreach_get("uv", uvbuf)
    dlayer.data.foreach_set("uv", [v * 2.0 for v in uvbuf])
    dme.update_tag()
    bpy.context.view_layer.update()
    check("UV edit routed to the dirty/draw path in DENSITY mode",
          overlay._state.dirty and not overlay._state.debounce.pending)
    dens_before = overlay._state.densities
    overlay._draw()
    check("draw-time DENSITY rebuild consumed the dirty flag and saw "
          "the new UVs (densities doubled)",
          not overlay._state.dirty
          and overlay._state.densities is not dens_before
          and overlay._state.densities[0] == 2.0 * dens_before[0])
    check("headless draw latched the gpu failure loudly (as in UV mode)",
          overlay.last_draw_error() is not None)
    result = bpy.ops.uv.island_overlay_refresh()
    check("refresh clears the latch in DENSITY mode",
          result == {'FINISHED'} and overlay.last_draw_error() is None)
    wm.uv_island_overlay = False

    # No-UV mesh: a hint state, not an error (no latch, nothing drawn).
    nme = bpy.data.meshes.new("NoUVMesh")
    nme.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
                    [], [(0, 1, 2, 3)])
    nme.update()
    nobj = bpy.data.objects.new("NoUVMesh", nme)
    bpy.context.collection.objects.link(nobj)
    bpy.context.view_layer.objects.active = nobj
    nobj.select_set(True)
    wm.uv_island_overlay = True
    check("DENSITY on a UV-less mesh: enabled but flagged no-UVs, "
          "0 islands", overlay.is_enabled() and overlay.has_no_uvs()
          and overlay.active_mode() == 'DENSITY'
          and overlay.island_count() == 0)
    check("no-UV mesh has no geometry and NO error latched",
          overlay._state.coords is None
          and overlay.last_draw_error() is None)
    overlay._draw()
    check("draw with no UVs no-ops without touching the error latch",
          overlay.last_draw_error() is None)
    wm.uv_island_overlay_mode = 'ISLANDS'
    check("switching back to ISLANDS on the UV-less mesh rebuilds "
          "(UV-source falls back to seams)",
          overlay.active_mode() == 'ISLANDS' and not overlay.has_no_uvs()
          and overlay.island_count() == 1
          and overlay._state.coords is not None
          and len(overlay._state.coords) > 0)
    wm.uv_island_overlay = False
    wm.uv_island_overlay_source = 'SEAM'

    # --- v1.3.1: per-mode opacity + back-face culling ---------------------------

    check("version bumped for the opacity/culling fix",
          uv_island_overlay.bl_info.get("version", (0,))[:3] >= (1, 3, 1))
    op_prop = wm.bl_rna.properties.get("uv_island_overlay_opacity")
    check("tint-opacity float registered: 0..1 factor, ISLANDS default "
          "0.4 (the classic translucent wash, unchanged by default)",
          op_prop is not None and op_prop.subtype == 'FACTOR'
          and op_prop.hard_min == 0.0 and op_prop.hard_max == 1.0
          and abs(op_prop.default - overlay.ALPHA) < 1e-6)
    dop_prop = wm.bl_rna.properties.get(
        "uv_island_overlay_density_opacity")
    check("checker-opacity float registered: 0..1 factor, DENSITY "
          "default 0.9 (near-opaque paint)",
          dop_prop is not None and dop_prop.subtype == 'FACTOR'
          and dop_prop.hard_min == 0.0 and dop_prop.hard_max == 1.0
          and abs(dop_prop.default - 0.9) < 1e-6
          and overlay.DEFAULT_DENSITY_OPACITY == 0.9)

    # Opacity rides a FLOAT push constant in BOTH shaders (probed
    # mechanism, same as checker_res): declared in both create-infos,
    # consumed by both fragment stages, fed per-mode at draw time.
    check("both create-infos declare the FLOAT overlay_opacity push "
          "constant",
          "push_constant('FLOAT', \"overlay_opacity\")" in ci_src
          and "push_constant('FLOAT', \"overlay_opacity\")" in dci_src)
    check("neither fragment stage reads the baked vertex alpha anymore",
          "finalColor.a" not in fs and "finalColor.a" not in dfs
          and "overlay_opacity" in fs and "overlay_opacity" in dfs)
    draw_src = inspect.getsource(overlay._draw)
    check("draw feeds the opacity uniform from the per-mode property",
          "uniform_float(\"overlay_opacity\"" in draw_src
          and "uv_island_overlay_opacity" in draw_src
          and "uv_island_overlay_density_opacity" in draw_src)

    # Culling is selected from cached mesh winding composed with the live
    # object transform determinant.  Open/ambiguous meshes draw two-sided.
    check("draw selects determinant- and mesh-aware culling",
          "_face_culling_mode(obj.matrix_world" in draw_src
          and "_state.mesh_orientation" in draw_src)
    mod_lines = inspect.getsource(overlay).splitlines()
    cull_lines = [i + 1 for i, ln in enumerate(mod_lines)
                  if "gpu.state.face_culling_set(" in ln
                  and "'NONE'" not in ln]
    check("the culling call sits inside a _gpu_state_restored block",
          len(cull_lines) == 1
          and any(a <= cull_lines[0] <= b for a, b in guard_spans))
    check("guard still restores the documented culling default 'NONE' "
          "(no face_culling_get exists on 5.1.2)",
          "face_culling_set('NONE')"
          in inspect.getsource(overlay._gpu_state_restored))

    # Behavior: an opacity change must be uniform-only — no dirty flag,
    # no geometry/batch rebuild — exactly like the checker-size test.
    bpy.context.view_layer.objects.active = dobj
    dobj.select_set(True)
    wm.uv_island_overlay_mode = 'DENSITY'
    wm.uv_island_overlay = True
    check("re-enabled in DENSITY mode for the opacity checks",
          overlay.is_enabled() and overlay.active_mode() == 'DENSITY'
          and not overlay.has_no_uvs())
    coords_before_op = overlay._state.coords
    colors_before_op = overlay._state.colors
    wm.uv_island_overlay_density_opacity = 0.5
    check("checker-opacity change is uniform-only: no dirty flag, no "
          "rebuild (FLOAT push constant, probed on 5.1.2)",
          overlay._state.coords is coords_before_op
          and overlay._state.colors is colors_before_op
          and not overlay._state.dirty)
    wm.uv_island_overlay_density_opacity = 0.9

    wm.uv_island_overlay_mode = 'ISLANDS'
    check("switched to ISLANDS for the tint-opacity check",
          overlay.active_mode() == 'ISLANDS')
    coords_before_op = overlay._state.coords
    wm.uv_island_overlay_opacity = 0.75
    check("tint-opacity change is uniform-only: no dirty flag, no "
          "rebuild",
          overlay._state.coords is coords_before_op
          and not overlay._state.dirty)
    wm.uv_island_overlay_opacity = overlay.ALPHA
    wm.uv_island_overlay = False

    # --- unregister -----------------------------------------------------------------
    uv_island_overlay.unregister()
    check("depsgraph handler removed",
          not any(h.__name__ == "_on_depsgraph_update"
                  for h in bpy.app.handlers.depsgraph_update_post))
    check("WindowManager property removed",
          not hasattr(bpy.types.WindowManager, "uv_island_overlay")
          or "uv_island_overlay"
          not in bpy.types.WindowManager.bl_rna.properties)
    check("island-source property removed",
          "uv_island_overlay_source"
          not in bpy.types.WindowManager.bl_rna.properties)
    check("opacity properties removed",
          "uv_island_overlay_opacity"
          not in bpy.types.WindowManager.bl_rna.properties
          and "uv_island_overlay_density_opacity"
          not in bpy.types.WindowManager.bl_rna.properties)

    # --- re-register cycle (idempotent lifecycle) -------------------------------------
    uv_island_overlay.register()
    result = bpy.ops.uv.island_overlay_toggle()
    check("toggle works after re-register", result == {'FINISHED'}
          and overlay.is_enabled())
    uv_island_overlay.unregister()
    check("unregister while overlay enabled is clean",
          not overlay.is_enabled())
    uv_island_overlay.register()
    uv_island_overlay.unregister()
    check("register/unregister cycle clean", True)


try:
    main()
except Exception:
    traceback.print_exc()
    FAILURES.append("unhandled exception")

sys.stdout.flush()
if FAILURES:
    print("REGISTER_TESTS_FAILED: %d failure(s): %s"
          % (len(FAILURES), ", ".join(FAILURES)))
else:
    print("REGISTER_TESTS_PASSED")
sys.stdout.flush()
