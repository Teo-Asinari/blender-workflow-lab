# SPDX-License-Identifier: GPL-2.0-or-later
"""Impasto engine: trigger classification, debounced compile+reconcile,
uniform flushes, edit sessions, handlers, and the migration runner.

Property ``update=`` callbacks never mutate the graph directly (A1);
they land here and do exactly one of two cheap things (design §4.6):

- UNIFORM: immediately write socket default_values (via a whole-stack
  compile + values-only apply, so uniform writes are consistent with
  the compiler BY CONSTRUCTION), then mark dirty for the debounced pass;
- STRUCTURAL: mark dirty; the debounced compile+reconcile does the work.

Visibility / binding-enable toggles additionally enter the GRACE set:
the compiler keeps their participants in the graph with factor 0 (an
instant uniform write, zero recompile); the second debounce tier (3 s
idle) recompiles with the grace set cleared, pruning them out.

The bpy.app.timers driver is a thin shell over the pure DebounceState;
tests drive :func:`tick` with a fake clock (timers never fire in
--background, probed on 5.1.2).
"""

import time
from contextlib import contextmanager

import bpy
from bpy.app.handlers import persistent

from . import debounce
from . import model
from . import reconcile
from . import snapshot

DEBUG = False   # delta logging (the autopsy's 71 timing sites, done once)

_dirty = set()          # root tree names awaiting structural pass
_grace = {}             # root tree name -> set of layer uids
_debounce = debounce.DebounceState()
_edit_depth = 0
_edit_trees = set()

_last_deltas = None     # most recent Deltas (test/debug introspection)


def _log(msg):
    if DEBUG:
        print("[impasto] %s" % msg)


# ---------------------------------------------------------------------------
# stack discovery
# ---------------------------------------------------------------------------

def is_stack_tree(tree):
    return (tree is not None
            and getattr(tree, "impasto", None) is not None
            and tree.impasto.is_stack)


def iter_stack_trees():
    try:
        groups = bpy.data.node_groups
    except Exception:
        # register()/unregister() run under bpy's _RestrictData.
        return
    for ng in groups:
        if ng.bl_idname == "ShaderNodeTree" and is_stack_tree(ng):
            yield ng


def find_stack_for_material(mat):
    """The stack root tree driven by this material's group node."""
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    for node in mat.node_tree.nodes:
        if (node.bl_idname == "ShaderNodeGroup"
                and is_stack_tree(node.node_tree)):
            return node.node_tree
    return None


def material_for_stack(tree):
    for mat in bpy.data.materials:
        if not mat.use_nodes or mat.node_tree is None:
            continue
        for node in mat.node_tree.nodes:
            if (node.bl_idname == "ShaderNodeGroup"
                    and node.node_tree == tree):
                return mat
    return None


# ---------------------------------------------------------------------------
# compile + reconcile passes
# ---------------------------------------------------------------------------

def reconcile_stack(tree, grace=frozenset()):
    """Snapshot -> compile -> reconcile one stack. Returns Deltas."""
    global _last_deltas
    mat = material_for_stack(tree)
    spec = model.compile_stack(snapshot.snapshot(tree, mat), grace)
    deltas = reconcile.reconcile_graph(spec, tree, mat)
    _cleanup_stale_layer_trees()
    _dirty.discard(tree.name)
    _last_deltas = deltas
    _log("reconcile %s: %s" % (tree.name, deltas))
    return deltas


def uniform_flush(tree):
    """Immediate UNIFORM-class write: compile, then write ONLY unlinked
    input default_values. Mutates zero graph structure."""
    global _last_deltas
    mat = material_for_stack(tree)
    grace = frozenset(_grace.get(tree.name, ()))
    spec = model.compile_stack(snapshot.snapshot(tree, mat), grace)
    deltas = reconcile.apply_values(spec, tree, mat)
    _last_deltas = deltas
    _log("uniform %s: %s" % (tree.name, deltas))
    return deltas


def rebuild(tree):
    """The one user-facing repair operator: drop hash caches, full
    reconcile (also clears any pending grace for the tree)."""
    reconcile.clear_caches()
    _grace.pop(tree.name, None)
    return reconcile_stack(tree)


def _cleanup_stale_layer_trees():
    """Remove generated layer trees whose uid no longer exists in any
    stack (layer deleted). Only unreferenced trees are touched."""
    live = set()
    for ng in iter_stack_trees():
        for ly in ng.impasto.layers:
            live.add(ly.name)
    for ng in list(bpy.data.node_groups):
        uid = model.uid_from_layer_tree_name(ng.name)
        if uid is not None and uid not in live and ng.users == 0:
            reconcile.invalidate(ng.name)
            bpy.data.node_groups.remove(ng)


# ---------------------------------------------------------------------------
# trigger classification entry points (called by props.py update=)
# ---------------------------------------------------------------------------

def on_structural(tree):
    if tree is None:
        return
    if _edit_depth:
        _edit_trees.add(tree.name)
        return
    _dirty.add(tree.name)
    _debounce.mark_structural(_now())
    _ensure_timer()


def on_uniform(tree):
    if tree is None:
        return
    if _edit_depth:
        _edit_trees.add(tree.name)
        return
    uniform_flush(tree)
    # the debounced compile re-emits the same values; hash-gated
    # reconcile then converges the cache with zero mutations.
    _dirty.add(tree.name)
    _debounce.mark_structural(_now())
    _ensure_timer()


def on_toggle(tree, uid):
    """Visibility / binding-enable flip: grace-listed so the compiler
    keeps the participant at factor 0 (instant uniform write); the
    prune tier slims the graph after 3 s of quiet."""
    if tree is None:
        return
    if uid:
        _grace.setdefault(tree.name, set()).add(uid)
    if _edit_depth:
        _edit_trees.add(tree.name)
        return
    uniform_flush(tree)
    _dirty.add(tree.name)
    _debounce.mark_structural(_now())
    _debounce.mark_prune(_now())
    _ensure_timer()


# ---------------------------------------------------------------------------
# batching (design §4.7): runtime-only flag, context-managed — an
# exception can never leave the .blend wedged.
# ---------------------------------------------------------------------------

@contextmanager
def stack_edit_session(tree):
    global _edit_depth
    _edit_depth += 1
    _edit_trees.add(tree.name)
    try:
        yield tree
    finally:
        _edit_depth -= 1
        if _edit_depth == 0:
            names, _edit_trees_snapshot = set(_edit_trees), None
            _edit_trees.clear()
            for name in names:
                t = bpy.data.node_groups.get(name)
                if t is not None and is_stack_tree(t):
                    grace = frozenset(_grace.get(name, ()))
                    try:
                        reconcile_stack(t, grace)
                    except Exception as exc:
                        print("[impasto] edit-session reconcile "
                              "failed for %r: %s" % (name, exc))


def in_edit_session():
    return _edit_depth > 0


# ---------------------------------------------------------------------------
# debounce driver (bpy.app.timers is a thin shell over the pure state)
# ---------------------------------------------------------------------------

def _now():
    return time.monotonic()


def tick(now):
    """Run due debounce actions; returns seconds until the next
    deadline or None when idle. Pure-drivable with a fake clock."""
    for action in _debounce.due(now):
        if action == "structural":
            flush_structural()
        elif action == "prune":
            flush_prune()
    return _debounce.next_delay(now)


def flush_structural():
    for name in list(_dirty):
        tree = bpy.data.node_groups.get(name)
        if tree is None or not is_stack_tree(tree):
            _dirty.discard(name)
            continue
        reconcile_stack(tree, frozenset(_grace.get(name, ())))


def flush_prune():
    names = set(_dirty) | set(_grace)
    _grace.clear()
    for name in names:
        tree = bpy.data.node_groups.get(name)
        if tree is None or not is_stack_tree(tree):
            _dirty.discard(name)
            continue
        reconcile_stack(tree)   # empty grace = pruned


def _timer_cb():
    try:
        return tick(_now())
    except Exception as exc:
        print("[impasto] timer error: %s" % exc)
        return None


def _ensure_timer():
    try:
        if not bpy.app.timers.is_registered(_timer_cb):
            bpy.app.timers.register(_timer_cb, first_interval=0.05)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# migrations (A5): machinery live from v0.
# Each entry: (from_schema, callable(stack_state)) applied in order
# while schema_version matches, then the stack is re-stamped.
# ---------------------------------------------------------------------------

def _migrate_1_per_binding_canvases(state):
    """Schema 1 -> 2: SHARED Paint bindings own their canvas explicitly.

    Legacy layers stored one canvas on the layer; the compiler still
    honors that fallback, so this migration only normalizes stored
    state — it copies the layer canvas into SHARED bindings that lack
    one and creates no images, so it is loss-free and idempotent."""
    for ly in state.layers:
        if ly.layer_type != 'PAINT' or not ly.image_name:
            continue
        for b in ly.bindings:
            if b.mode == 'SHARED' and not b.image_name:
                b.image_name = ly.image_name


def _migrate_2_shared_mask_assets(state):
    """Schema 2 -> 3: move mask image ownership to stack assets losslessly."""
    existing = {asset.name for asset in state.mask_assets}
    for ly in state.layers:
        for ref in ly.masks:
            if ref.asset_uid and state.mask_assets.get(ref.asset_uid):
                continue
            uid = ref.name
            if uid in existing:
                uid = model.new_uid(existing)
            asset = state.mask_assets.add()
            asset.name = uid
            asset.label = ref.label or "Mask"
            asset.image_name = ref.image_name
            asset.uv_map = ref.uv_map
            ref.asset_uid = uid
            existing.add(uid)


MIGRATIONS = ((1, _migrate_1_per_binding_canvases),
              (2, _migrate_2_shared_mask_assets))


def run_migrations(tree):
    state = tree.impasto
    version = state.schema_version
    for from_schema, fn in MIGRATIONS:
        if state.schema_version == from_schema:
            fn(state)
            state.schema_version = from_schema + 1
    if state.schema_version != version or tuple(
            state.blender_version) != tuple(bpy.app.version):
        state.blender_version = bpy.app.version
    return state.schema_version


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

@persistent
def _on_undo_post(*args):
    # node references are name-derived, so nothing dangles; runtime
    # caches are dropped and lazily rebuilt (design §2.1).
    reconcile.clear_caches()
    _grace.clear()


@persistent
def _on_redo_post(*args):
    reconcile.clear_caches()
    _grace.clear()


@persistent
def _on_load_post(*args):
    reconcile.clear_caches()
    _dirty.clear()
    _grace.clear()
    _debounce.reset()
    for tree in iter_stack_trees():
        try:
            run_migrations(tree)
            reconcile_stack(tree)   # self-heal older files/manual edits
        except Exception as exc:
            print("[impasto] load-post reconcile failed for %r: %s"
                  % (tree.name, exc))


def register():
    if _on_undo_post not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(_on_undo_post)
    if _on_redo_post not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(_on_redo_post)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister():
    for handler_list, fn in (
            (bpy.app.handlers.undo_post, _on_undo_post),
            (bpy.app.handlers.redo_post, _on_redo_post),
            (bpy.app.handlers.load_post, _on_load_post)):
        if fn in handler_list:
            handler_list.remove(fn)
    try:
        if bpy.app.timers.is_registered(_timer_cb):
            bpy.app.timers.unregister(_timer_cb)
    except Exception:
        pass
    _dirty.clear()
    _grace.clear()
    _edit_trees.clear()
    _debounce.reset()
    reconcile.clear_caches()
